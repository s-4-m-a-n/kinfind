"""End-to-end flow, including what families must NOT see."""
import io
import os
import re
import tempfile

os.environ["DISASTERLENS_DATA"] = tempfile.mkdtemp()
os.environ["GDACS_SYNC_MINUTES"] = "0"  # never call GDACS from tests
os.environ["OPENROUTER_API_KEY"] = ""  # ...or OpenRouter, even if .env has a key
os.environ["RESPONDER_KEY"] = "test-key"

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import seed  # noqa: E402
from app.main import app  # noqa: E402


def _img(fmt="JPEG", color="tan") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buf, fmt)
    return buf.getvalue()


def test_family_flow_hides_private_details():
    with TestClient(app) as client:
        seed.run()
        resp = client.post("/missing", data={
            "disaster_id": "1", "full_name": "Ram Thapa", "sex": "male", "age": "35",
            "height_cm": "171", "hair_color": "black", "last_seen_date": "2026-09-12",
            "clothing": "blue shirt", "reporter_name": "Sita Thapa", "reporter_contact": "98000000",
            "feature_kind": ["tattoo"], "feature_location": ["left forearm"],
            "feature_text": ["anchor with MAA written under it"],
        }, files={"photos": ("p.jpg", _img(), "image/jpeg")}, follow_redirects=False)
        assert resp.status_code == 303
        case_url = resp.headers["location"]

        page = client.get(case_url).text
        assert "Riverbank near Chatara bridge" in page          # where it was found
        assert "BP Koirala" not in page                          # facility hidden
        assert "deceased" not in page.lower()                    # status hidden
        assert "/responder/photos" not in page                   # no images

        # Wrong token cannot open the case.
        assert client.get(case_url.split("?")[0] + "?k=nope").status_code == 404

        # Request verification -> responder queue with family contact.
        ref = re.search(r"/case/(MP-\w+)", case_url).group(1)
        found_ref = re.search(r"/verify/(FD-\w+)", page).group(1)
        token = re.search(r"k=([^&]+)", case_url).group(1)
        client.post(f"/case/{ref}/verify/{found_ref}", data={"k": token})
        assert "Verification requested" in client.get(case_url).text

        assert client.get("/responder", follow_redirects=False).status_code == 303
        client.post("/responder/login", data={"key": "test-key"})
        dash = client.get("/responder").text
        assert "Ram Thapa" in dash and "98000000" in dash and "BP Koirala" in dash


def test_feature_photos_are_linked_and_responder_only():
    with TestClient(app) as client:
        seed.run()
        resp = client.post("/missing", data={
            "disaster_id": "1", "full_name": "Gita Rai", "reporter_name": "Hari Rai",
            "reporter_contact": "98111111",
            # Second row has a photo but no type: kept as "other" rather than dropped.
            "feature_kind": ["scar", ""], "feature_location": ["chin", "unknown"],
            "feature_text": ["small scar", ""],
        }, files=[("feature_photo", ("scar.jpg", _img(), "image/jpeg")),
                  ("feature_photo", ("mark.png", _img("PNG"), "image/png")),
                  ("photos", ("face.jpg", _img(), "image/jpeg"))],
            follow_redirects=False)
        assert resp.status_code == 303
        case_url = resp.headers["location"]
        page = client.get(case_url).text
        assert "2 distinctive features" in page and "3 photos on file" in page
        assert "/responder/photos" not in page

        ref = re.search(r"/case/(MP-\w+)", case_url).group(1)
        client.post("/responder/login", data={"key": "test-key"})
        record = client.get(f"/responder/missing/{ref}").text
        # A family's photos are of a living person: shown as they are, never filtered.
        assert 'alt="Photo of scar"' in record and 'alt="Photo of other"' in record
        assert "view=sketch" not in record and "Unfiltered" not in record

        # Photos of a deceased person are filtered (sketch, since AI is off in tests).
        resp = client.post("/responder/found", data={
            "disaster_id": "1", "status": "deceased", "found_place": "Riverbank", "sex": "female",
            "feature_kind": ["scar"], "feature_location": ["chin"], "feature_text": ["small scar"]},
            files=[("feature_photo", ("s.jpg", _img(color="darkred"), "image/jpeg"))], follow_redirects=False)
        found_url = resp.headers["location"]
        page = client.get(found_url).text
        assert 'alt="Photo of scar (sketch)"' in page and "Unfiltered ↗" in page
        pid = re.search(r"/responder/photos/(\d+)\?view=sketch", page).group(1)
        sketch = client.get(f"/responder/photos/{pid}")
        assert sketch.status_code == 200 and sketch.content[:2] == b"\xff\xd8"

        # Unfiltered needs a confirmation, for a single photo and for the page-wide setting.
        warning = client.get(f"/responder/photos/{pid}?view=original")
        assert "You are about to see unfiltered photos" in warning.text
        original = client.get(f"/responder/photos/{pid}?view=original&confirm=1")
        assert original.content[:2] == b"\xff\xd8" and original.content != sketch.content
        path = found_url.split("8765")[-1]
        assert "You are about to see" in client.post("/responder/photo-view", data={"view": "original", "next": path}).text
        client.post("/responder/photo-view", data={"view": "original", "confirm": "1", "next": path})
        page = client.get(path).text
        assert "view=original&amp;confirm=1" in page and "Showing unfiltered photos" in page
        client.post("/responder/photo-view", data={"view": "sketch", "next": path})

        resp = client.post("/responder/photo-view", data={"view": "sketch", "next": "https://evil.example"},
                           follow_redirects=False)
        assert resp.headers["location"] == "/responder"  # no open redirect

        bad = client.post("/missing", data={
            "disaster_id": "1", "full_name": "X", "reporter_name": "Y", "reporter_contact": "Z",
            "feature_kind": ["tattoo"], "feature_location": ["unknown"], "feature_text": ["rose"],
        }, files=[("feature_photo", ("a.gif", b"GIF89a", "image/gif"))])
        assert bad.status_code == 400
        fake = client.post("/missing", data={"disaster_id": "1", "full_name": "X", "reporter_name": "Y",
                                             "reporter_contact": "Z"},
                           files=[("photos", ("x.jpg", b"\xff\xd8not really", "image/jpeg"))])
        assert fake.status_code == 400 and "could not be read as an image" in fake.text


def test_disaster_zones_on_home_map():
    with TestClient(app) as client:
        seed.run()
        home = client.get("/").text
        assert "Koshi River Flood 2026" in home and '"lat": 26.62' in home
        assert "/responder/disasters/new" not in home  # families can't add zones

        zone = {"name": "Test Quake <b>", "region": "Somewhere", "lat": "10.5", "lng": "-70.25", "radius_km": "50"}
        assert client.post("/responder/disasters", data=zone, follow_redirects=False).status_code == 303
        assert "Test Quake" not in client.get("/").text  # not signed in: rejected

        client.post("/responder/login", data={"key": "test-key"})
        assert client.post("/responder/disasters", data={**zone, "lat": "95"}).status_code == 400
        assert client.post("/responder/disasters", data=zone, follow_redirects=False).status_code == 303
        home = client.get("/").text
        assert "Test Quake &lt;b&gt;" in home and "<b>" not in home.split("zone-data")[1].split("</script>")[0]

        did = re.search(r'/missing/new\?disaster=(\d+)"', home.split("Test Quake")[1]).group(1)
        form = client.get(f"/missing/new?disaster={did}").text
        assert f'value="{did}" selected' in form
