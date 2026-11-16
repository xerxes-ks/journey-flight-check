import datetime as dt

from flightcheck.model import UTC
from flightcheck.turn import default_min_turn


def test_narrow_body_is_35():
    assert default_min_turn("B738") == 35
    assert default_min_turn("A320") == 35


def test_wide_body_is_60():
    assert default_min_turn("B77W") == 60
    assert default_min_turn("B788") == 60
    assert default_min_turn("A359") == 60
    assert default_min_turn("A333") == 60
    assert default_min_turn("A388") == 60


def test_long_haul_block_is_60():
    out = dt.datetime(2027, 3, 15, 18, 0, tzinfo=UTC)
    inn = out + dt.timedelta(hours=6)
    assert default_min_turn("B738", out, inn) == 60


def test_short_haul_stays_35():
    out = dt.datetime(2027, 3, 15, 18, 0, tzinfo=UTC)
    inn = out + dt.timedelta(hours=3)
    assert default_min_turn("B738", out, inn) == 35
