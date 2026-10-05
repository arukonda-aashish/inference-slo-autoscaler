"""Deterministic arrival schedules and prompts.

Arrivals are a Poisson process: exponential gaps at each phase's rate.
Restarting the gap at a phase boundary is valid because the exponential
distribution is memoryless.

Arrival times and request sizes come from separate seeded streams, so changing
the size mix never moves an arrival.
"""

import random
from dataclasses import dataclass

from isa.loadgen.profile import Profile

# Common English words that are each a single token in GPT-style BPE vocabularies
# (including Qwen2's), both at the start of a prompt and with a leading space.
# So a prompt's word count is its token count. Verified against vLLM's
# usage.prompt_tokens in Phase 5 (HANDOFF D9). Random word order also means no
# two prompts share a prefix, so prefix caching can't give any request a free prefill.
VOCAB = (
    "the", "of", "and", "to", "in", "is", "for", "on", "with", "as", "at", "by",
    "from", "this", "that", "it", "be", "are", "was", "an", "or", "not", "have",
    "all", "can", "more", "one", "time", "new", "some", "there", "so", "up", "out",
    "if", "about", "who", "get", "which", "go", "me", "when", "make", "like", "no",
    "just", "him", "know", "take", "people", "into", "year", "your", "good", "them",
    "see", "other", "than", "then", "now", "look", "only", "come", "its",
)


@dataclass(frozen=True)
class Arrival:
    idx: int
    req_id: str
    phase: int
    offset_s: float  # seconds after run start
    prompt_tokens: int
    max_tokens: int


def build_schedule(profile: Profile, max_model_len: int | None = None) -> list[Arrival]:
    if max_model_len is not None and profile.max_request_tokens > max_model_len:
        raise ValueError(
            f"profile {profile.name!r} can produce {profile.max_request_tokens} tokens per "
            f"request, over the server's max_model_len {max_model_len}"
        )
    arrivals_rng = random.Random(f"{profile.seed}:arrivals")
    sizes_rng = random.Random(f"{profile.seed}:sizes")
    mixture = profile.prompt_tokens.mixture
    weights = [c.weight for c in mixture]

    out: list[Arrival] = []
    phase_start = 0.0
    for phase_idx, phase in enumerate(profile.phases):
        phase_end = phase_start + phase.duration_s
        if phase.rate_rps > 0:
            t = phase_start
            while (t := t + arrivals_rng.expovariate(phase.rate_rps)) < phase_end:
                component = sizes_rng.choices(mixture, weights=weights)[0]
                idx = len(out)
                out.append(
                    Arrival(
                        idx=idx,
                        req_id=f"{profile.name}-{idx:06d}",
                        phase=phase_idx,
                        offset_s=t,
                        prompt_tokens=sizes_rng.randint(*component.range),
                        max_tokens=sizes_rng.randint(*profile.max_tokens.range),
                    )
                )
        phase_start = phase_end
    return out


def build_prompt(seed: int, idx: int, n_tokens: int) -> str:
    rng = random.Random(f"{seed}:prompt:{idx}")
    return " ".join(rng.choices(VOCAB, k=n_tokens))