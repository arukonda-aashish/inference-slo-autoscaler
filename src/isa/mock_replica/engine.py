"""Continuous-batching engine simulator for the mock replica.

Pure and synchronous: no I/O, no real clock. The caller passes `now` and gets back
how long the step takes; an async driver (step 5) sleeps that long and streams the
tokens. Tests drive it with simulated time, so every behavior is deterministic.

Per step, in vLLM's scheduling order:
  1. Decode: every sequence past prefill emits one token. Crossing a block
     boundary needs a new KV block; if none is free, the most recently admitted
     running request is preempted (recompute: freed, requeued at the front, and
     must re-prefill its prompt plus everything it has generated so far).
  2. Admit: FCFS while a sequence slot and KV blocks for the prompt are free,
     keeping a watermark in reserve. Stops at the first request that doesn't fit
     (head-of-line blocking, as in vLLM's FCFS scheduler). Skipped entirely in a
     step that preempted, so a just-evicted request can't thrash straight back in.
  3. Prefill: leftover token budget goes to prefill chunks, FCFS. A request whose
     prefill completes emits a token at the end of this step.
  4. step_time = base + per_seq * decode_seqs + per_prefill_token * prefill_tokens
"""

from collections import deque
from dataclasses import dataclass
from enum import Enum
from math import ceil

from pydantic import Field, model_validator

from isa.common.config import StrictModel


class EngineConfig(StrictModel):
    block_size: int = Field(gt=0)
    num_kv_blocks: int = Field(gt=0)
    watermark_frac: float = Field(ge=0, lt=0.5)
    max_num_seqs: int = Field(gt=0)
    max_batched_tokens: int = Field(gt=0)
    max_model_len: int = Field(gt=0)
    step_base_ms: float = Field(gt=0)
    step_per_seq_ms: float = Field(ge=0)
    prefill_per_token_ms: float = Field(ge=0)

    @property
    def watermark_blocks(self) -> int:
        return ceil(self.num_kv_blocks * self.watermark_frac)

    @model_validator(mode="after")
    def _check_feasible(self) -> "EngineConfig":
        if self.max_num_seqs > self.max_batched_tokens:
            raise ValueError(
                f"max_num_seqs ({self.max_num_seqs}) > max_batched_tokens "
                f"({self.max_batched_tokens}): a full decode batch wouldn't fit one step"
            )
        # A single max-length request must fit in an otherwise empty cache, or the
        # engine could deadlock with a request that can never be admitted.
        max_req_blocks = ceil(self.max_model_len / self.block_size)
        usable = self.num_kv_blocks - self.watermark_blocks
        if max_req_blocks > usable:
            raise ValueError(
                f"max_model_len needs {max_req_blocks} blocks but only {usable} are "
                f"usable after the watermark"
            )
        return self


class ReqState(Enum):
    WAITING = "waiting"
    PREFILL = "prefill"
    DECODE = "decode"


@dataclass(eq=False)  # identity equality: deque.remove / list.remove match by object
class _Req:
    req_id: str
    prompt_tokens: int
    max_tokens: int
    arrival_t: float
    prefill_target: int  # tokens to prefill: prompt, or prompt + generated after preemption
    state: ReqState = ReqState.WAITING
    prefilled: int = 0
    context_len: int = 0  # tokens whose KV is resident
    generated: int = 0
    blocks: int = 0
    first_token_t: float | None = None
    preemptions: int = 0


@dataclass(frozen=True)
class TokenEvent:
    req_id: str
    index: int  # 1-based position in the request's output


@dataclass(frozen=True)
class FinishedRecord:
    req_id: str
    prompt_tokens: int
    output_tokens: int
    arrival_t: float
    first_token_t: float
    finish_t: float
    preemptions: int

    @property
    def ttft_s(self) -> float:
        return self.first_token_t - self.arrival_t

    @property
    def tpot_s(self) -> float | None:
        if self.output_tokens < 2:
            return None
        return (self.finish_t - self.first_token_t) / (self.output_tokens - 1)


@dataclass(frozen=True)
class StepResult:
    duration_s: float
    t_end: float
    tokens: list[TokenEvent]
    finished: list[FinishedRecord]
    admitted: list[str]
    preempted: list[str]
    decode_seqs: int
    prefill_tokens: int
    first_token_ttfts: list[tuple[str, float]]  # (req_id, ttft_s) for first tokens this step


class Engine:
    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self._waiting: deque[_Req] = deque()
        self._running: list[_Req] = []  # admission order: oldest first
        self._by_id: dict[str, _Req] = {}
        self._free_blocks = cfg.num_kv_blocks

    # ---- observability (read by the metrics layer) -------------------------

    @property
    def num_waiting(self) -> int:
        return len(self._waiting)

    @property
    def num_running(self) -> int:
        return len(self._running)

    @property
    def kv_usage(self) -> float:
        return 1.0 - self._free_blocks / self.cfg.num_kv_blocks

    @property
    def has_work(self) -> bool:
        return bool(self._waiting or self._running)

    # ---- request API --------------------------------------------------------

    def submit(self, req_id: str, prompt_tokens: int, max_tokens: int, now: float) -> None:
        if req_id in self._by_id:
            raise ValueError(f"duplicate req_id: {req_id}")
        if prompt_tokens < 1 or max_tokens < 1:
            raise ValueError("prompt_tokens and max_tokens must be >= 1")
        if prompt_tokens + max_tokens > self.cfg.max_model_len:
            raise ValueError(
                f"prompt_tokens + max_tokens = {prompt_tokens + max_tokens} exceeds "
                f"max_model_len {self.cfg.max_model_len}"
            )
        req = _Req(req_id, prompt_tokens, max_tokens, now, prefill_target=prompt_tokens)
        self._waiting.append(req)
        self._by_id[req_id] = req

    def abort(self, req_id: str) -> bool:
        """Drop a request (client disconnected). Returns False if unknown or finished."""
        req = self._by_id.pop(req_id, None)
        if req is None:
            return False
        if req.state is ReqState.WAITING:
            self._waiting.remove(req)
        else:
            self._running.remove(req)
            self._release(req)
        return True

    # ---- the scheduler step -------------------------------------------------

    def step(self, now: float) -> StepResult:
        cfg = self.cfg
        tokens: list[TokenEvent] = []
        admitted: list[str] = []
        preempted: list[str] = []
        first_token: list[_Req] = []

        # 1. Decode
        decoded: list[_Req] = []
        for req in [r for r in self._running if r.state is ReqState.DECODE]:
            if req.state is not ReqState.DECODE:
                continue  # preempted earlier in this step by an older request's growth
            if not self._grow(req, preempted):
                continue  # had to preempt itself
            req.context_len += 1
            req.generated += 1
            decoded.append(req)
            tokens.append(TokenEvent(req.req_id, req.generated))

        budget = cfg.max_batched_tokens - len(decoded)

        # 2. Admit
        if not preempted:
            while self._waiting and len(self._running) < cfg.max_num_seqs:
                head = self._waiting[0]
                need = self._blocks_for(head.prefill_target)
                if self._free_blocks - need < cfg.watermark_blocks:
                    break  # head-of-line blocking
                self._waiting.popleft()
                self._free_blocks -= need
                head.blocks = need
                head.state = ReqState.PREFILL
                self._running.append(head)
                admitted.append(head.req_id)

        # 3. Prefill
        prefill_tokens = 0
        prefill_done: list[_Req] = []
        for req in self._running:
            if budget <= 0:
                break
            if req.state is not ReqState.PREFILL:
                continue
            chunk = min(budget, req.prefill_target - req.prefilled)
            req.prefilled += chunk
            budget -= chunk
            prefill_tokens += chunk
            if req.prefilled == req.prefill_target:
                req.state = ReqState.DECODE
                req.context_len = req.prefill_target
                req.generated += 1
                tokens.append(TokenEvent(req.req_id, req.generated))
                prefill_done.append(req)
                if req.first_token_t is None:
                    first_token.append(req)

        # 4. Cost
        if not decoded and prefill_tokens == 0:

            return StepResult(
                duration_s=0.0,
                t_end=now,
                tokens=[],
                finished=[],
                admitted=admitted,
                preempted=preempted,
                decode_seqs=0,
                prefill_tokens=0,
                first_token_ttfts=[],
            )

        duration = (
            cfg.step_base_ms
            + cfg.step_per_seq_ms * len(decoded)
            + cfg.prefill_per_token_ms * prefill_tokens
        ) / 1000.0
        t_end = now + duration

        for req in first_token:
            req.first_token_t = t_end

        finished: list[FinishedRecord] = []
        for req in decoded + prefill_done:  # disjoint: DECODE vs PREFILL at step start
            if req.generated >= req.max_tokens:
                finished.append(self._finish(req, t_end))

        return StepResult(
            duration_s=duration,
            t_end=t_end,
            tokens=tokens,
            finished=finished,
            admitted=admitted,
            preempted=preempted,
            decode_seqs=len(decoded),
            prefill_tokens=prefill_tokens,
            first_token_ttfts=[(r.req_id, t_end - r.arrival_t) for r in first_token],
        )

    # ---- internals ----------------------------------------------------------

    def _blocks_for(self, n_tokens: int) -> int:
        return ceil(n_tokens / self.cfg.block_size)

    def _release(self, req: _Req) -> None:
        self._free_blocks += req.blocks
        req.blocks = 0

    def _grow(self, req: _Req, preempted: list[str]) -> bool:
        """Ensure req has KV for one more token, preempting newest-first if needed.

        Returns False if req itself was the victim. The watermark isn't applied
        here: like vLLM, it only guards admission.
        """
        need = self._blocks_for(req.context_len + 1) - req.blocks
        while need > 0:
            if self._free_blocks >= need:
                self._free_blocks -= need
                req.blocks += need
                return True
            victim = self._running[-1]
            self._preempt(victim, preempted)
            if victim is req:
                return False
        return True

    def _preempt(self, req: _Req, preempted: list[str]) -> None:
        self._running.remove(req)
        self._release(req)
        req.state = ReqState.WAITING
        req.prefill_target = req.prompt_tokens + req.generated
        req.prefilled = 0
        req.context_len = 0
        req.preemptions += 1
        self._waiting.appendleft(req)
        preempted.append(req.req_id)

    def _finish(self, req: _Req, t_end: float) -> FinishedRecord:
        self._running.remove(req)
        self._release(req)
        del self._by_id[req.req_id]
        assert req.first_token_t is not None
        return FinishedRecord(
            req_id=req.req_id,
            prompt_tokens=req.prompt_tokens,
            output_tokens=req.generated,
            arrival_t=req.arrival_t,
            first_token_t=req.first_token_t,
            finish_t=t_end,
            preemptions=req.preemptions,
        )