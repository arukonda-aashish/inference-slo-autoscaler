from isa.router.balancer import LeastOutstanding, RoundRobin
from isa.router.registry import Registry, Replica, ReplicaSpec, ReplicaState


def rep(rid: str, inflight: int = 0) -> Replica:
    return Replica(rid, f"http://{rid}", ReplicaState.READY, inflight)


def test_least_outstanding_picks_minimum():
    b = LeastOutstanding()
    a, bb, c = rep("a", 5), rep("b", 1), rep("c", 3)
    assert b.pick([a, bb, c]) is bb


def test_least_outstanding_rotates_ties():
    b = LeastOutstanding()
    reps = [rep("a"), rep("b"), rep("c", 4)]
    picks = [b.pick(reps).replica_id for _ in range(4)]
    assert picks == ["a", "b", "a", "b"]  # c never: it's busier


def test_round_robin_ignores_load():
    b = RoundRobin()
    reps = [rep("a", 100), rep("b"), rep("c")]
    assert [b.pick(reps).replica_id for _ in range(4)] == ["a", "b", "c", "a"]


def test_empty_candidates():
    assert LeastOutstanding().pick([]) is None
    assert RoundRobin().pick([]) is None


def test_registry_update_adds_changes_and_removes():
    reg = Registry()
    reg.update([ReplicaSpec(replica_id=r, url=f"http://{r}/") for r in ("a", "b", "c")])
    assert [r.replica_id for r in reg.ready()] == ["a", "b", "c"]
    assert reg.get("a").url == "http://a"  # trailing slash stripped

    reg.get("a").inflight = 2
    reg.update([
        ReplicaSpec(replica_id="a", url="http://a"),
        ReplicaSpec(replica_id="b", url="http://b", state=ReplicaState.DRAINING),
    ])
    assert [r.replica_id for r in reg.all()] == ["a", "b"]  # c removed
    assert [r.replica_id for r in reg.ready()] == ["a"]  # b draining
    assert reg.get("a").inflight == 2  # update never resets live counts