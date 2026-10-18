"""Google Maps tiles for the zone maps (Map Tiles API, drawn by Leaflet).

Billed to the Google Cloud project: 100,000 tile requests a month are free, then the credit.
The browser key is restricted to this API and this site's address (HTTP referrer), so it is
safe in the page. Without MAPS_API_KEY the maps fall back to OpenStreetMap (local development).

A session token (valid about two weeks) is shared by all visitors and renewed a day before it ends.
"""
from __future__ import annotations

import os
import threading
import time

import httpx

API = "https://tile.googleapis.com"
_session: dict = {}  # {"token": ..., "expiry": epoch seconds}
_lock = threading.Lock()


def api_key() -> str | None:
    return os.environ.get("MAPS_API_KEY") or None


def _new_session(key: str, referer: str) -> dict:
    # The key is referrer-restricted, so this server call names our own site as the referrer.
    resp = httpx.post(f"{API}/v1/createSession?key={key}", headers={"Referer": referer}, timeout=15,
                      json={"mapType": "roadmap", "language": "en", "region": "US"})
    resp.raise_for_status()
    data = resp.json()
    return {"token": data["session"], "expiry": int(data["expiry"])}


def tiles(origin: str) -> dict:
    """What zones.js needs to draw Google tiles, or {} to fall back to OpenStreetMap."""
    key = api_key()
    if not key:
        return {}
    with _lock:
        if not _session or _session["expiry"] - time.time() < 86400:
            _session.update(_new_session(key, origin.rstrip("/") + "/"))
        token = _session["token"]
    q = f"session={token}&key={key}"
    return {"tiles": f"{API}/v1/2dtiles/{{z}}/{{x}}/{{y}}?{q}", "viewport": f"{API}/tile/v1/viewport?{q}"}
