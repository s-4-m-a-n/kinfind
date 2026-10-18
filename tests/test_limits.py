"""Rate limits and request-size limits (app/limits.py)."""
import io
import os
import tempfile

os.environ.setdefault("DISASTERLENS_DATA", tempfile.mkdtemp())
os.environ["GDACS_SYNC_MINUTES"] = "0"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.setdefault("RESPONDER_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import limits, seed  # noqa: E402
from app.main import app  # noqa: E402


def _img() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "tan").save(buf, "JPEG")
    return buf.getvalue()


def test_every_form_has_a_rule():
    assert limits.rule_for("POST", "/responder/login")[0] == "login"
    assert limits.rule_for("POST", "/missing") == ("report", 81 * limits.MB)
    assert limits.rule_for("POST", "/appeal/MP-ABC123/tips") == ("tip", 33 * limits.MB)
    assert limits.rule_for("POST", "/safe")[0] == "checkin"
    assert limits.rule_for("POST", "/safe/find")[0] == "safe_search"
    assert limits.rule_for("POST", "/safe/c/sometoken")[0] == "family_action"
    assert limits.rule_for("POST", "/case/MP-1/verify/FD-2")[0] == "family_action"
    assert limits.rule_for("POST", "/responder/found/FD-1/ai-check")[0] == "ai_check"
    assert limits.rule_for("POST", "/responder/found")[0] == "found"
    assert limits.rule_for("POST", "/responder/tips/3")[0] == "responder_action"
    assert limits.rule_for("POST", "/anything-new")[0] == "post"  # new forms are covered by default
    assert limits.rule_for("GET", "/") == (None, 64 * limits.KB)


def test_login_guessing_is_limited():
    with TestClient(app) as client:
        codes = [client.post("/responder/login", data={"key": f"guess-{i}"}).status_code for i in range(11)]
        assert codes[:10] == [200] * 10  # wrong key: the form again
        last = client.post("/responder/login", data={"key": "guess"})
        assert last.status_code == 429 and int(last.headers["retry-after"]) > 0
        assert "Too many requests" in last.text


def test_oversized_bodies_are_refused_before_reading():
    with TestClient(app) as client:
        seed.run()
        big = "x" * (40 * limits.KB)
        resp = client.post("/safe", data={"disaster_id": "1", "name": "A B", "phone": "9801234567", "message": big})
        assert resp.status_code == 413 and "The limit here is 32 KB" in resp.text

        # No Content-Length (chunked upload): counted as it arrives and stopped at the limit.
        def chunks():
            for _ in range(20):
                yield b"name=" + b"y" * 4096
        resp = client.post("/safe/find", content=chunks(),
                           headers={"content-type": "application/x-www-form-urlencoded"})
        assert resp.status_code == 413


def test_photo_and_feature_counts_per_record():
    with TestClient(app) as client:
        seed.run()
        base = {"disaster_id": "1", "full_name": "Count Test", "reporter_name": "R", "reporter_contact": "C"}
        photos = [("photos", (f"{i}.jpg", _img(), "image/jpeg")) for i in range(limits.MAX_RECORD_PHOTOS + 1)]
        resp = client.post("/missing", data=base, files=photos)
        assert resp.status_code == 400 and "at most 10 photos" in resp.text
        ok = client.post("/missing", data=base, files=photos[:limits.MAX_RECORD_PHOTOS], follow_redirects=False)
        assert ok.status_code == 303

        rows = limits.MAX_FEATURES + 1
        resp = client.post("/missing", data={**base, "feature_kind": ["tattoo"] * rows,
                                             "feature_location": ["unknown"] * rows, "feature_text": ["x"] * rows})
        assert resp.status_code == 400 and "at most 20 distinctive features" in resp.text


def test_overall_request_flood_is_limited(monkeypatch):
    monkeypatch.setitem(limits.BUCKETS, "all", (5, 60))
    with TestClient(app) as client:
        codes = [client.get("/").status_code for _ in range(6)]
        assert codes == [200] * 5 + [429]
        assert client.get("/static/style.css").status_code == 200  # static files aren't counted
