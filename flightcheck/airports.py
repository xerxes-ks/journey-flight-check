"""Small static IATA → IANA timezone map.

Used only when we make no AeroAPI call (the too_early path). After a live
lookup, timezones always come from AeroAPI `origin.timezone` /
`destination.timezone`. Unknown codes fall back to UTC.

This map is intentionally small and public (major airports). It is not a
substitute for AeroAPI and is not loaded from a third-party package so the
runtime stays stdlib-only.
"""

IATA_TZ = {
    "ATL": "America/New_York",
    "AMS": "Europe/Amsterdam",
    "BKK": "Asia/Bangkok",
    "BLR": "Asia/Kolkata",
    "BOM": "Asia/Kolkata",
    "BOS": "America/New_York",
    "CDG": "Europe/Paris",
    "CLT": "America/New_York",
    "DEL": "Asia/Kolkata",
    "DEN": "America/Denver",
    "DFW": "America/Chicago",
    "DOH": "Asia/Qatar",
    "DTW": "America/Detroit",
    "DXB": "Asia/Dubai",
    "EWR": "America/New_York",
    "FCO": "Europe/Rome",
    "FRA": "Europe/Berlin",
    "GIG": "America/Sao_Paulo",
    "GRU": "America/Sao_Paulo",
    "HEL": "Europe/Helsinki",
    "HKG": "Asia/Hong_Kong",
    "HND": "Asia/Tokyo",
    "IAD": "America/New_York",
    "IAH": "America/Chicago",
    "ICN": "Asia/Seoul",
    "IST": "Europe/Istanbul",
    "JFK": "America/New_York",
    "KUL": "Asia/Kuala_Lumpur",
    "LAS": "America/Los_Angeles",
    "LAX": "America/Los_Angeles",
    "LGA": "America/New_York",
    "LHR": "Europe/London",
    "MAD": "Europe/Madrid",
    "MEL": "Australia/Melbourne",
    "MEX": "America/Mexico_City",
    "MIA": "America/New_York",
    "MUC": "Europe/Berlin",
    "NRT": "Asia/Tokyo",
    "ORD": "America/Chicago",
    "ORY": "Europe/Paris",
    "PEK": "Asia/Shanghai",
    "PHL": "America/New_York",
    "PHX": "America/Phoenix",
    "PVG": "Asia/Shanghai",
    "SEA": "America/Los_Angeles",
    "SFO": "America/Los_Angeles",
    "SIN": "Asia/Singapore",
    "SYD": "Australia/Sydney",
    "TPE": "Asia/Taipei",
    "YUL": "America/Toronto",
    "YVR": "America/Vancouver",
    "YYZ": "America/Toronto",
    "ZRH": "Europe/Zurich",
}


def tz_for_iata(code: str | None) -> str:
    if not code:
        return "UTC"
    return IATA_TZ.get(code.strip().upper(), "UTC")
