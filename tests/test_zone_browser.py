"""Zone browsing at scale: compact JSON feed, searchable paged list, light home page."""
import os
import re
import tempfile
import time

os.environ.setdefault("DISASTERLENS_DATA", tempfile.mkdtemp())
os.environ["GDACS_SYNC_MINUTES"] = "0"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.setdefault("RESPONDER_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, seed  # noqa: E402
from app.main import app  # noqa: E402

N = 3000


def _many_zones():
    with db.session() as conn:
        if conn.execute("SELECT COUNT(*) FROM disasters WHERE external_id LIKE 'scale:%'").fetchone()[0]:
            return
        conn.executemany(
            """INSERT INTO disasters (name, region, lat, lng, radius_km, status, source, external_id)
               VALUES (?, ?, ?, ?, 30, 'active', 'responder', ?)""",
            [(f"Scale flood {i}", f"Region {i % 50}, Testland", (i % 140) - 70, (i * 7 % 360) - 180, f"scale:{i}")
             for i in range(N)])
        conn.execute("""INSERT INTO disasters (name, region, lat, lng, radius_km, status, source)
                        VALUES ('Hidden pending zone', 'X', 1, 1, 10, 'pending', 'public')""")


def test_thousands_of_zones_stay_fast_and_small():
    with TestClient(app) as client:
        seed.run()
        _many_zones()

        home = client.get("/").text
        assert home.count('class="zone-dot"') <= 8           # only the newest few are in the page
        assert len(home) < 60_000                            # no per-zone data embedded
        assert re.search(r"data-zone-total>\d{4,}<", home)   # but the total is shown

        start = time.time()
        feed = client.get("/zones.json")
        assert time.time() - start < 2
        zones = feed.json()["zones"]
        assert len(zones) >= N and len(feed.content) < 120 * N   # compact rows, ~70 bytes per zone
        assert "public" in feed.headers["cache-control"]
        assert not any(z[1] == "Hidden pending zone" for z in zones)  # only active zones

        # Report form: every zone is still choosable, and the dropdown is marked for filtering.
        assert "data-filterable" in client.get("/missing/new").text


def test_zones_page_search_and_paging():
    with TestClient(app) as client:
        seed.run()
        _many_zones()
        page = client.get("/zones").text
        assert page.count('class="zone-dot"') == 20 and "page 1 of" in page
        assert "Older →" in page and "← Newer" not in page

        found = client.get("/zones?q=Koshi").text
        assert "Koshi River Flood 2026" in found and "1 zone match" in found
        multi = client.get("/zones?q=scale+flood+1234").text  # every word must match
        assert "Scale flood 1234" in multi
        assert "No zones match" in client.get("/zones?q=%25%25%25").text  # % is searched literally
        assert "Hidden pending zone" not in client.get("/zones?q=Hidden").text
        last = client.get("/zones?page=9999").text
        assert "No zones match" in last or 'class="zone-dot"' not in last


def test_time_zone_table_for_opening_near_the_visitor():
    import json
    from pathlib import Path

    from tools.build_tz_coords import to_degrees

    assert to_degrees("+2743", 2) == 27.72 and to_degrees("+08519", 3) == 85.32
    assert to_degrees("-404251", 2) == -40.71  # degrees, minutes, seconds
    table = json.loads((Path(__file__).resolve().parent.parent / "app/static/tz_coords.json").read_text())
    assert table["Asia/Kathmandu"] == [27.72, 85.32, "Kathmandu"]
    assert table["Asia/Katmandu"][2] == "Kathmandu"  # old name some browsers report
    assert len(table) > 400
    with TestClient(app) as client:
        assert 'data-tz-src="/static/tz_coords.json?v=' in client.get("/").text
