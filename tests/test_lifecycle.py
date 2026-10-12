import json

import pytest

from isa.controller.lifecycle import Phase, Pool, TransitionError
from isa.controller.targets import write_targets
from isa.router.registry import ReplicaState


def test_create_allocates_ids_and_lowest_free_port():
    pool = Pool(port_base=8001, port_count=4)
    a, b = pool.create(now=0), pool.create(now=0)
    assert (a.replica_id, a.port, a.phase) == ("r1", 8001, Phase.STARTING)
    assert (b.replica_id, b.port) == ("r2", 8002)
    assert a.url == "http://127.0.0.1:8001"


def test_ids_never_reused_but_ports_are():
    pool = Pool(port_base=8001, port_count=4)
    a = pool.create(now=0)
    pool.transition("r1", Phase.FAILED, now=1)
    c = pool.create(now=2)
    assert c.replica_id == "r2"  # new process, new id
    assert c.port == a.port  # port freed by the failed replica


def test_full_legal_lifecycle_and_history():
    pool = Pool()
    pool.create(now=0)
    pool.transition("r1", Phase.READY, now=30)
    pool.transition("r1", Phase.DRAINING, now=100)
    pool.transition("r1", Phase.TERMINATED, now=110)
    assert pool.active() == []
    assert [(f, t) for _, _, f, t in pool.history] == [
        (Phase.STARTING, Phase.READY),
        (Phase.READY, Phase.DRAINING),
        (Phase.DRAINING, Phase.TERMINATED),
    ]


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ([], Phase.DRAINING),  # STARTING -> DRAINING: never served, nothing to drain
        ([Phase.READY], Phase.STARTING),
        ([Phase.READY, Phase.DRAINING], Phase.READY),  # no un-draining
    ],
)
def test_illegal_transitions_rejected(path, illegal):
    pool = Pool()
    pool.create(now=0)
    for phase in path:
        pool.transition("r1", phase, now=1)
    with pytest.raises(TransitionError):
        pool.transition("r1", illegal, now=2)


def test_counts_and_router_view():
    pool = Pool()
    for _ in range(3):
        pool.create(now=0)
    pool.transition("r1", Phase.READY, now=1)
    pool.transition("r2", Phase.READY, now=1)
    pool.transition("r2", Phase.DRAINING, now=2)
    assert (pool.count(Phase.STARTING), pool.count(Phase.READY), pool.count(Phase.DRAINING)) == (
        1, 1, 1,
    )
    states = {s.replica_id: s.state for s in pool.router_specs()}
    assert states == {
        "r1": ReplicaState.READY,
        "r2": ReplicaState.DRAINING,
        "r3": ReplicaState.STARTING,
    }
    assert {r.replica_id for r in pool.scrape_targets()} == {"r1", "r2"}  # not r3: not listening


def test_no_free_port_raises():
    pool = Pool(port_count=1)
    pool.create(now=0)
    with pytest.raises(RuntimeError, match="no free port"):
        pool.create(now=0)


def test_write_targets_format_and_empty(tmp_path):
    pool = Pool()
    pool.create(now=0)
    pool.transition("r1", Phase.READY, now=1)
    path = tmp_path / "targets" / "replicas.json"
    write_targets(path, pool.scrape_targets())
    assert json.loads(path.read_text()) == [
        {"targets": ["127.0.0.1:8001"], "labels": {"replica": "r1"}}
    ]
    write_targets(path, [])
    assert json.loads(path.read_text()) == []
    assert not (tmp_path / "targets" / "replicas.json.tmp").exists()