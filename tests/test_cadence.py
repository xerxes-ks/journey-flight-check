import datetime as dt

from flightcheck import poll_interval
from flightcheck.model import UTC


def t(hours_before_dep=0, block_hours=3):
    dep = dt.datetime(2027, 3, 15, 18, 0, tzinfo=UTC)
    arr = dep + dt.timedelta(hours=block_hours)
    now = dep - dt.timedelta(hours=hours_before_dep)
    return dep, now, arr


def test_far_out_is_silent():
    dep, now, arr = t(60)
    assert poll_interval(dep, now, landed=False, sched_arr=arr) is None


def test_three_hourly_until_t6():
    dep, now, arr = t(24)
    assert poll_interval(dep, now, False, arr) == 180
    dep, now, arr = t(6)
    assert poll_interval(dep, now, False, arr) == 180


def test_hourly_until_t3():
    dep, now, arr = t(5)
    assert poll_interval(dep, now, False, arr) == 60
    dep, now, arr = t(3)
    assert poll_interval(dep, now, False, arr) == 60


def test_fifteen_until_departure():
    dep, now, arr = t(2)
    assert poll_interval(dep, now, False, arr) == 15
    dep, now, arr = t(0)
    assert poll_interval(dep, now, False, arr) == 15


def test_airborne_short_haul_is_30():
    dep, now, arr = t(-1, block_hours=3)
    assert poll_interval(dep, now, False, arr, actual_dep=dep) == 30


def test_airborne_long_haul_is_60():
    dep, now, arr = t(-1, block_hours=8)
    assert poll_interval(dep, now, False, arr, actual_dep=dep) == 60


def test_at_gate_after_std_stays_15_until_actual_off():
    dep, now, arr = t(-0.5, block_hours=3)
    assert poll_interval(dep, now, False, arr) == 15
    assert poll_interval(dep, now, False, arr, actual_dep=dep) == 30


def test_stop_at_landing():
    dep, now, arr = t(-2, block_hours=3)
    assert poll_interval(dep, now, landed=True, sched_arr=arr) is None


def test_positional_signature():
    dep = dt.datetime(2027, 3, 15, 18, 0, tzinfo=UTC)
    now = dep - dt.timedelta(hours=1)
    assert poll_interval(dep, now, False) == 15


def test_airborne_keys_off_actual_departure():
    dep = dt.datetime(2027, 3, 15, 18, 0, tzinfo=UTC)
    now = dep - dt.timedelta(minutes=30)  # still before STD
    actual = now - dt.timedelta(minutes=10)  # already pushed back
    arr = dep + dt.timedelta(hours=3)
    assert poll_interval(dep, now, False, arr) == 15
    assert poll_interval(dep, now, False, arr, actual_dep=actual) == 30
