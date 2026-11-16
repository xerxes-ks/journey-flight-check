from flightcheck.airports import tz_for_iata


def test_required_airports_are_mapped():
    assert tz_for_iata("OKA") == "Asia/Tokyo"
    assert tz_for_iata("CTS") == "Asia/Tokyo"
    assert tz_for_iata("SJD") == "America/Mazatlan"
    assert tz_for_iata("OGN") == "Asia/Tokyo"
    assert tz_for_iata("MEX") == "America/Mexico_City"
    assert tz_for_iata("KIX") == "Asia/Tokyo"
    assert tz_for_iata("ITM") == "Asia/Tokyo"
    assert tz_for_iata("FUK") == "Asia/Tokyo"


def test_unknown_iata_falls_back_to_utc():
    assert tz_for_iata("ZZZ") == "UTC"
    assert tz_for_iata(None) == "UTC"
    assert tz_for_iata("del") == "Asia/Kolkata"
