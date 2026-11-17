"""Pick the origin-local ticket date, not the UTC date, near midnight."""

from flightcheck import check_flight
from flightcheck.model import parse_time
from tests.conftest import scenario_fetch

# 02:55 IST on 12 Dec 2027 is 21:25 UTC on 11 Dec.
# 02:55 IST on 13 Dec 2027 is 21:25 UTC on 12 Dec.
NOW = "2027-12-11T12:00:00Z"


def test_picks_origin_local_date_not_utc_date():
    fetch = scenario_fetch(now=NOW)
    r = check_flight(
        {"flight": "XX754", "date": "2027-12-12", "origin": "DEL"},
        fetch=fetch,
        now=parse_time(NOW),
    )
    assert r.flight.status == "landed" or r.flight.fa_flight_id == "XXX754-1700001200-airline-0012"
    assert r.flight.fa_flight_id == "XXX754-1700001200-airline-0012"
    so = r.flight.to_dict()["scheduled_out"]
    assert so["tz"] == "Asia/Kolkata"
    assert so["local"].startswith("2027-12-12T02:55:00")
    assert "DEL time" in so["human"]
    # Must not have selected the next local day's instance (UTC date 12 Dec).
    assert r.flight.registration == "VT0XY"


def test_next_local_day_is_a_different_instance():
    now = "2027-12-12T12:00:00Z"
    fetch = scenario_fetch(now=now)
    r = check_flight(
        {"flight": "XX754", "date": "2027-12-13", "origin": "DEL"},
        fetch=fetch,
        now=parse_time(now),
    )
    assert r.flight.fa_flight_id == "XXX754-1700001300-airline-0013"
    so = r.flight.to_dict()["scheduled_out"]
    assert so["local"].startswith("2027-12-13T02:55:00")
    assert r.flight.registration == "VT0XX"
