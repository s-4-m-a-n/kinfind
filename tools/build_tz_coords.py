"""Build app/static/tz_coords.json: time zone name -> [lat, lng, city] of its reference city.

The home page uses it to open the map near the visitor without asking for their location:
browsers report their time zone (e.g. "Asia/Kathmandu") with no permission prompt, and the
public tz database gives the coordinates of each zone's main city. Run again after tzdata
updates:  python tools/build_tz_coords.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TZDIR = Path("/usr/share/zoneinfo")
OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "tz_coords.json"


def to_degrees(text: str, deg_digits: int) -> float:
    """'+2743' or '+404251' (lat, 2-digit degrees) / '+08519' (lng, 3-digit) -> decimal degrees."""
    sign = -1 if text[0] == "-" else 1
    digits = text[1:]
    d, rest = int(digits[:deg_digits]), digits[deg_digits:]
    m, s = int(rest[:2]), int(rest[2:4] or 0)
    return round(sign * (d + m / 60 + s / 3600), 2)


def main() -> None:
    coords: dict[str, list] = {}
    for table in ("zone.tab", "zone1970.tab"):
        for line in (TZDIR / table).read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            lat, lng = re.fullmatch(r"([+-]\d+)([+-]\d+)", parts[1]).groups()
            city = parts[2].split("/")[-1].replace("_", " ")
            coords.setdefault(parts[2], [to_degrees(lat, 2), to_degrees(lng, 3), city])
    # Old names browsers may still report, e.g. Asia/Katmandu -> Asia/Kathmandu (shown as "Kathmandu").
    for line in (TZDIR / "tzdata.zi").read_text().splitlines():
        if line.startswith("L "):
            _, target, alias = line.split()
            if target in coords:
                coords.setdefault(alias, coords[target])
    OUT.write_text(json.dumps(dict(sorted(coords.items())), separators=(",", ":")))
    print(f"wrote {len(coords)} time zones to {OUT}")


if __name__ == "__main__":
    main()
