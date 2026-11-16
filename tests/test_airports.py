from flightcheck.airports import IATA_TZ, tz_for_iata


def test_major_hubs_are_mapped():
    assert tz_for_iata("LHR") == "Europe/London"
    assert tz_for_iata("SIN") == "Asia/Singapore"
    assert tz_for_iata("SYD") == "Australia/Sydney"
    assert tz_for_iata("JFK") == "America/New_York"
    assert tz_for_iata("CDG") == "Europe/Paris"
    assert tz_for_iata("DXB") == "Asia/Dubai"


def test_map_is_broad_and_alphabetical():
    keys = list(IATA_TZ)
    assert keys == sorted(keys)
    assert len(IATA_TZ) >= 100


def test_unknown_iata_falls_back_to_utc():
    assert tz_for_iata("ZZZ") == "UTC"
    assert tz_for_iata(None) == "UTC"
    assert tz_for_iata("lhr") == "Europe/London"
