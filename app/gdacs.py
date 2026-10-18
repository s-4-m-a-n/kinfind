"""Suggest disaster zones from GDACS (Global Disaster Alert and Coordination System).

GDACS is run by the UN and the European Commission and needs no API key. New
Orange/Red alerts are stored as *pending* zones; a responder approves them before
they appear on the public map, because only responders know where they are
actually recording people.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date, timedelta

from . import db

API = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
EVENT_TYPES = {"EQ": "earthquake", "TC": "tropical cyclone", "FL": "flood", "VO": "volcano", "WF": "wildfire"}
LOOKBACK_DAYS = 30

# Starting radius (km) by event type and alert level. A responder adjusts it on approval.
RADIUS_KM = {
    "EQ": {"Orange": 75, "Red": 150},
    "TC": {"Orange": 200, "Red": 300},
    "FL": {"Orange": 100, "Red": 150},
    "VO": {"Orange": 30, "Red": 50},
    "WF": {"Orange": 50, "Red": 100},
}


def fetch(today: date | None = None, timeout: float = 20) -> list[dict]:
    today = today or date.today()
    query = urllib.parse.urlencode({
        "eventlist": ";".join(EVENT_TYPES), "alertlevel": "Orange;Red",
        "fromdate": (today - timedelta(days=LOOKBACK_DAYS)).isoformat(), "todate": today.isoformat(),
    })
    req = urllib.request.Request(f"{API}?{query}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body).get("features", []) if body.strip() else []  # GDACS sends an empty body for no events


def to_zone(feature: dict) -> dict | None:
    """One GDACS event -> a row for the disasters table, or None if unusable."""
    p, geom = feature.get("properties") or {}, feature.get("geometry") or {}
    kind, level = p.get("eventtype"), p.get("alertlevel")
    if kind not in RADIUS_KM or level not in ("Orange", "Red") or geom.get("type") != "Point":
        return None
    try:
        lng, lat = (float(c) for c in geom["coordinates"][:2])
    except (KeyError, TypeError, ValueError):
        return None
    report = (p.get("url") or {}).get("report") or ""
    countries = ", ".join(c.get("countryname", "") for c in p.get("affectedcountries") or []) or p.get("country")
    return {
        "external_id": f"gdacs:{kind}:{p.get('eventid')}",
        "name": " ".join((p.get("name") or EVENT_TYPES[kind].capitalize()).split())[:200],
        "region": (countries or "Unknown region")[:200],
        "lat": lat, "lng": lng, "radius_km": RADIUS_KM[kind][level],
        "alert_level": level,
        "started_on": (p.get("fromdate") or "")[:10] or None,
        "source_url": report if report.startswith("https://www.gdacs.org/") else None,
    }


def store(zones: list[dict]) -> int:
    """Insert new suggestions; events already seen (even if rejected) are skipped."""
    with db.session() as conn:
        before = conn.total_changes
        conn.executemany(
            """INSERT OR IGNORE INTO disasters (name, region, lat, lng, radius_km, status, source,
                   external_id, alert_level, started_on, source_url)
               VALUES (:name, :region, :lat, :lng, :radius_km, 'pending', 'gdacs',
                   :external_id, :alert_level, :started_on, :source_url)""", zones)
        return conn.total_changes - before


def sync() -> int:
    """Fetch GDACS and store new suggestions. Returns how many were added."""
    return store([z for z in map(to_zone, fetch()) if z])
