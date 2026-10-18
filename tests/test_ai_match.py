"""AI photo check pipeline (describe -> screen -> compare), with the model mocked: no network, no key."""
import io
import os
import re
import tempfile

os.environ.setdefault("DISASTERLENS_DATA", tempfile.mkdtemp())
os.environ["GDACS_SYNC_MINUTES"] = "0"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.setdefault("ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import ai_match, illustrate, seed  # noqa: E402
from app.main import app  # noqa: E402

LOGIN = {"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def fake_model(calls):
    def call(messages, name, schema):
        user = messages[1]["content"]
        images = sum(1 for c in user if isinstance(c, dict) and c.get("type") == "image_url") if isinstance(user, list) else 0
        calls.append((name, images))
        if name == "feature_description":
            note = user[0]["text"]
            anchor = "anchor" in note
            return {"kind": "tattoo", "placement": "left upper arm", "motifs": ["anchor"] if anchor else ["swallow"],
                    "lettering": ["MAA"] if "MAA" in note else [], "colours": ["blue-black"], "style": "fine line",
                    "distinctive_details": [], "visibility": "clear"}
        if name == "screen":
            refs = re.findall(r"CANDIDATE (\S+):\n(.*?)(?=\n\nCANDIDATE|\n\nScore every)", user, re.S)
            return {"candidates": [{"ref": ref, "score": 85 if "MAA" in text else 10, "reason": "test"}
                                   for ref, text in refs]}
        return {"score": 91, "verdict": "strong", "summary": "Same anchor and MAA lettering.",
                "consistent": ["MAA lettering"], "conflicts": []}
    return call


def test_pipeline_describes_screens_then_compares_shortlist(monkeypatch):
    calls = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(ai_match, "call_model", fake_model(calls))
    monkeypatch.setattr(illustrate, "redraw", lambda src: _png("white"))  # no image-model calls either
    with TestClient(app) as client:
        seed.run()
        client.post("/responder/login", data=LOGIN)
        client.post("/responder/disasters", data={"name": "AI test zone", "region": "Nowhere",
                                                  "lat": "0", "lng": "0", "radius_km": "10"})
        zone = re.search(r'disaster=(\d+)"[^>]*>Report missing', client.get("/").text.split("AI test zone")[1]).group(1)

        def found(text, color):
            return client.post("/responder/found", data={
                "disaster_id": zone, "status": "deceased", "found_place": "Riverbank", "sex": "male",
                "feature_kind": ["tattoo"], "feature_location": ["left upper arm"], "feature_text": [text]},
                files=[("feature_photo", ("f.png", _png(color), "image/png"))], follow_redirects=False)

        decoy = found("old school swallow", "green")
        match = found("anchor, letters MAA", "brown")
        assert calls == [("feature_description", 1)] * 2  # described on save; nothing to screen against yet
        client.post("/responder/logout")

        resp = client.post("/missing", data={
            "disaster_id": zone, "full_name": "Test Person A", "sex": "male", "reporter_name": "R",
            "reporter_contact": "C", "feature_kind": ["tattoo"], "feature_location": ["left upper arm"],
            "feature_text": ["anchor with MAA under it"]},
            files=[("feature_photo", ("a.png", _png("tan"), "image/png")),
                   ("photos", ("face.png", _png("grey"), "image/png"))], follow_redirects=False)
        case_url = resp.headers["location"]

        names = [n for n, _ in calls]
        assert names.count("feature_description") == 3            # each feature photo, one image each
        assert all(i == 1 for n, i in calls if n == "feature_description")
        assert names.count("screen") == 1                          # one text-only call for all candidates
        assert [i for n, i in calls if n == "screen"] == [0]
        assert [i for n, i in calls if n == "photo_comparison"] == [2]  # only the shortlisted pair; no face photo

        client.post("/responder/login", data=LOGIN)
        page = client.get(match.headers["location"]).text
        assert "Same anchor and MAA lettering." in page and "91%" in page and "AI description" in page
        assert "Test Person A" not in client.get(decoy.headers["location"]).text.split('id="ai"')[1].split("</section>")[0]
        assert "Test Person A" in client.get("/responder").text.split("Flagged by the AI photo check")[1]

        # Photos of the deceased are illustrated when the record is saved (redraw mocked above).
        page = client.get(match.headers["location"]).text
        assert "?view=illustration" in page and "Illustration" in page
        pid = re.search(r"/responder/photos/(\d+)\?view=illustration", page).group(1)
        illustration = client.get(f"/responder/photos/{pid}")
        assert illustration.status_code == 200 and illustration.content[:2] == b"\xff\xd8"

                # Re-running re-screens and re-compares, but photos are never described twice.
        calls.clear()
        ref = re.search(r"/responder/found/(FD-\w+)", match.headers["location"]).group(1)
        client.post(f"/responder/found/{ref}/ai-check")
        assert [n for n, _ in calls] == ["screen", "photo_comparison"]

        client.post("/responder/logout")
        family = client.get(case_url).text
        assert "AI photo check" not in family and "MAA lettering" not in family
