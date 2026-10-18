"""GDACS suggestions, public zone proposals and the responder review queue."""
import os
import re
import tempfile

os.environ.setdefault("DISASTERLENS_DATA", tempfile.mkdtemp())
os.environ["GDACS_SYNC_MINUTES"] = "0"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.setdefault("RESPONDER_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, gdacs, seed  # noqa: E402
from app.main import RESPONDER_KEY, app  # noqa: E402

CYCLONE = {
    "type": "Feature", "geometry": {"type": "Point", "coordinates": [146.1, 12.6]},
    "properties": {"eventtype": "TC", "eventid": 1001338, "name": "Tropical Cyclone CHAMPI-26",
                   "alertlevel": "Red", "fromdate": "2026-10-14T12:00:00", "country": "Guam",
                   "affectedcountries": [{"countryname": "Guam"}],
                   "url": {"report": "https://www.gdacs.org/report.aspx?eventid=1001338"}},
}


def test_gdacs_event_becomes_pending_zone_once():
    zone = gdacs.to_zone(CYCLONE)
    assert zone["lat"] == 12.6 and zone["lng"] == 146.1 and zone["radius_km"] == 300
    assert zone["external_id"] == "gdacs:TC:1001338" and zone["started_on"] == "2026-10-14"
    assert gdacs.to_zone({**CYCLONE, "properties": {**CYCLONE["properties"], "alertlevel": "Green"}}) is None
    evil = {**CYCLONE, "properties": {**CYCLONE["properties"], "url": {"report": "javascript:alert(1)"}}}
    assert gdacs.to_zone(evil)["source_url"] is None

    db.init()
    assert gdacs.store([zone]) == 1
    assert gdacs.store([zone]) == 0  # already seen: not added again
    with db.session() as conn:
        row = conn.execute("SELECT status, source FROM disasters WHERE external_id=?", (zone["external_id"],)).fetchone()
    assert tuple(row) == ("pending", "gdacs")


def test_public_proposal_review_flow():
    with TestClient(app) as client:
        seed.run()
        resp = client.post("/zones", data={"name": "Melamchi Landslide", "region": "Sindhupalchok, Nepal",
                                           "lat": "27.83", "lng": "85.57", "radius_km": "20",
                                           "note": "village buried"}, follow_redirects=False)
        assert resp.status_code == 303
        zid = re.search(r"disaster=(\d+)&proposed=1", resp.headers["location"]).group(1)
        assert "Melamchi Landslide" not in client.get("/").text            # not public yet
        form = client.get(resp.headers["location"]).text
        assert "awaiting review" in form and f'value="{zid}" selected' in form

        # A family can report into the pending zone straight away.
        filed = client.post("/missing", data={"disaster_id": zid, "full_name": "Asha Tamang",
                                              "reporter_name": "B", "reporter_contact": "C"},
                            follow_redirects=False)
        assert filed.status_code == 303

        client.post("/responder/login", data={"key": RESPONDER_KEY})
        review = client.get("/responder/zones").text
        assert "Melamchi Landslide" in review and "village buried" in review and "1 missing report" in review

        # Can't reject a zone families reported into; merging moves the reports instead.
        assert client.post(f"/responder/zones/{zid}", data={"action": "reject"}).status_code == 400
        client.post(f"/responder/zones/{zid}", data={"action": "merge", "target": "1"})
        with db.session() as conn:
            assert conn.execute("SELECT status FROM disasters WHERE id=?", (zid,)).fetchone()[0] == "merged"
            assert conn.execute("SELECT disaster_id FROM missing_reports WHERE full_name='Asha Tamang'").fetchone()[0] == 1

        # Approve with edits: becomes public under the new name.
        resp = client.post("/zones", data={"name": "Quake", "region": "X", "lat": "1", "lng": "2", "radius_km": "5"},
                           follow_redirects=False)
        zid = re.search(r"disaster=(\d+)", resp.headers["location"]).group(1)
        client.post(f"/responder/zones/{zid}", data={"action": "approve", "name": "Quake near Y",
                                                     "region": "Y", "radius_km": "40"})
        assert "Quake near Y" in client.get("/").text
        assert client.post(f"/responder/zones/{zid}", data={"action": "reject"}).status_code == 404
