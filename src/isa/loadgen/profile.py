"""Load profiles: phases of constant Poisson arrival rate, plus request-size distributions."""

from pydantic import Field, model_validator

from isa.common.config import StrictModel


class Phase(StrictModel):
    duration_s: float = Field(gt=0)
    rate_rps: float = Field(ge=0)  # 0 is allowed: an idle phase, e.g. to watch scale-down


class IntRange(StrictModel):
    range: tuple[int, int]

    @model_validator(mode="after")
    def _check_range(self) -> "IntRange":
        lo, hi = self.range
        if lo < 1 or hi < lo:
            raise ValueError(f"range must satisfy 1 <= lo <= hi, got {self.range}")
        return self


class MixtureComponent(IntRange):
    weight: float = Field(gt=0)


class PromptTokens(StrictModel):
    mixture: list[MixtureComponent] = Field(min_length=1)


class Profile(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]+$")  # used in request ids and file names
    seed: int
    phases: list[Phase] = Field(min_length=1)
    prompt_tokens: PromptTokens
    max_tokens: IntRange

    @property
    def duration_s(self) -> float:
        return sum(p.duration_s for p in self.phases)

    @property
    def max_request_tokens(self) -> int:
        """Largest possible prompt + output, for checking against the server's max_model_len."""
        return max(c.range[1] for c in self.prompt_tokens.mixture) + self.max_tokens.range[1]