"""Replica selection. Pure: takes candidates, returns one."""

from collections.abc import Sequence
from typing import Protocol

from isa.router.registry import Replica


class Balancer(Protocol):
    name: str

    def pick(self, candidates: Sequence[Replica]) -> Replica | None: ...


class LeastOutstanding:
    """Fewest in-flight requests, ties broken round-robin.

    Without tie rotation, an idle pool would send every request to the first
    replica in the list, since all counts are equal at zero.

    Why not plain round-robin: with a mixed prompt-length workload, round-robin
    stacks long requests on whichever replica happens to be next, while another
    replica sits light. Least-outstanding reacts to actual load.
    """

    name = "least_outstanding"

    def __init__(self) -> None:
        self._turn = 0

    def pick(self, candidates: Sequence[Replica]) -> Replica | None:
        if not candidates:
            return None
        low = min(r.inflight for r in candidates)
        tied = [r for r in candidates if r.inflight == low]
        choice = tied[self._turn % len(tied)]
        self._turn += 1
        return choice


class RoundRobin:
    """Ignores load. Kept as a comparison point."""

    name = "round_robin"

    def __init__(self) -> None:
        self._turn = 0

    def pick(self, candidates: Sequence[Replica]) -> Replica | None:
        if not candidates:
            return None
        choice = candidates[self._turn % len(candidates)]
        self._turn += 1
        return choice


def make_balancer(name: str) -> Balancer:
    if name == "least_outstanding":
        return LeastOutstanding()
    if name == "round_robin":
        return RoundRobin()
    raise ValueError(f"unknown balancer: {name}")