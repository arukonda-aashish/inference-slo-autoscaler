from isa.controller.signals import Signals
from isa.controller.stabilizer import Stabilizer, StabilizerConfig


def sig(**kw) -> Signals:
    base = dict(
        ts=0.0, ready=1, starting=0, draining=0, waiting_total=0.0, running_total=0.0,
        kv_usage_max=0.1, queue_slope_per_s=0.0, gpu_util_mean=0.0,
    )
    return Signals(**{**base, **kw})


def make(**kw) -> Stabilizer:
    base = dict(min_replicas=1, max_replicas=3, max_step_up=2, up_cooldown_s=30, down_window_s=90)
    return Stabilizer(StabilizerConfig(**{**base, **kw}))


def test_desired_is_bounded():
    st = make()
    a = st.apply(10, sig(ready=1), now=0)
    assert (a.desired_bounded, a.target, a.reason) == (3, 3, "scale_up")
    assert make().apply(0, sig(ready=1), now=0).reason == "steady"  # bounded up to min


def test_stale_signals_hold():
    a = make().apply(3, sig(ready=1, stale=True), now=0)
    assert (a.target, a.reason) == (1, "hold_stale_signals")


def test_step_up_is_capped():
    a = make(max_replicas=10).apply(9, sig(ready=1), now=0)
    assert a.target == 3  # 1 + max_step_up


def test_pending_capacity_prevents_overshoot():
    st = make(max_replicas=10, up_cooldown_s=0)  # isolate pending accounting
    assert st.apply(3, sig(ready=1), now=0).target == 3
    # Five seconds later the two new replicas are still booting and the queue is
    # still high. Counting STARTING as capacity means nothing more is added.
    a = st.apply(3, sig(ready=1, starting=2), now=5)
    assert (a.current, a.target, a.reason) == (3, 3, "steady")


def test_up_cooldown():
    st = make(max_replicas=10)
    assert st.apply(3, sig(ready=1), now=0).target == 3
    held = st.apply(5, sig(ready=3), now=10)
    assert (held.target, held.reason) == (3, "hold_up_cooldown")
    assert st.apply(5, sig(ready=3), now=31).target == 5


def test_scale_down_waits_for_the_window():
    st = make()
    st.apply(3, sig(ready=3), now=0)
    for t in range(5, 91, 5):  # demand drops, but t=0's "3" is still inside the window
        assert st.apply(1, sig(ready=3), now=t).reason == "hold_down_stabilization"
    a = st.apply(1, sig(ready=3), now=95)  # t=0 has left the window
    assert (a.target, a.reason) == (2, "scale_down")  # one replica per action


def test_flapping_demand_never_drains():
    st = make()
    for i, t in enumerate(range(0, 300, 5)):
        a = st.apply(3 if i % 2 == 0 else 1, sig(ready=3), now=t)
        assert a.reason != "scale_down"


def test_no_scale_down_during_a_transition():
    st = make(down_window_s=0)
    a = st.apply(1, sig(ready=2, draining=1), now=0)
    assert a.reason == "hold_transition_in_progress"
    a = st.apply(1, sig(ready=1, starting=1), now=5)
    assert a.reason == "hold_transition_in_progress"


def test_restore_min_ignores_cooldown():
    st = make()
    st.apply(3, sig(ready=1), now=0)  # starts the up-cooldown
    a = st.apply(1, sig(ready=0), now=5)  # every replica crashed
    assert (a.target, a.reason) == (1, "restore_min")