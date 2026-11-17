"""Pick the origin-local ticket date, not the UTC date, near the dateline."""

from flightcheck import check_flight
from flightcheck.model import parse_time
from tests.conftest import scenario_fetch

# 11:20 NZDT on 8 Mar 2027 is 22:20 UTC on 7 Mar.
# 11:20 NZDT on 9 Mar 2027 is 22:20 UTC on 8 Mar.
NOW = "2027-03-08T12:00:00Z"


def test_picks_origin_local_date_not_utc_date():
    fetch = scenario_fetch(now=NOW)
    r = check_flight(
        {"flight": "XX321", "date": "2027-03-08", "origin": "XXC"},
        fetch=fetch,
        now=parse_time(NOW),
    )
    assert r.flight.status == "landed" or r.flight.fa_flight_id == "XXX321-1700000800-airline-0008"
    assert r.flight.fa_flight_id == "XXX321-1700000800-airline-0008"
    so = r.flight.to_dict()["scheduled_out"]
    assert so["tz"] == "Pacific/Auckland"
    assert so["local"].startswith("2027-03-08T11:20:00")
    assert "XXC time" in so["human"]
    assert r.flight.registration == "N321XY"


def test_next_local_day_is_a_different_instance():
    now = "2027-03-08T12:00:00Z"
    fetch = scenario_fetch(now=now)
    r = check_flight(
        {"flight": "XX321", "date": "2027-03-09", "origin": "XXC"},
        fetch=fetch,
        now=parse_time(now),
    )
    assert r.flight.fa_flight_id == "XXX321-1700000900-airline-0009"
    so = r.flight.to_dict()["scheduled_out"]
    assert so["local"].startswith("2027-03-09T11:20:00")
    assert r.flight.registration == "N321XX"
