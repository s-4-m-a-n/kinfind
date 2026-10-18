"""Security fixes: photo metadata, decompression bombs, appeal approval, proxies, lock-outs, headers, logs."""
import io
import logging
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

from app import accounts, db, limits, photos, seed  # noqa: E402
from app.main import HideSecretsInLogs, app  # noqa: E402

LOGIN = {"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}
GPS_IFD = 0x8825


def _jpeg_with_gps(size=(40, 40)) -> bytes:
    exif = Image.Exif()
    exif[GPS_IFD] = {1: "N", 2: (27.0, 42.0, 30.0), 3: "E", 4: (85.0, 19.0, 12.0)}
    buf = io.BytesIO()
    Image.new("RGB", size, "tan").save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _gps(data: bytes) -> dict:
    return dict(Image.open(io.BytesIO(data)).getexif().get_ifd(GPS_IFD))


def _published_appeal(client):
    resp = client.post("/missing", data={"disaster_id": "1", "full_name": "Pema Sherpa", "reporter_name": "R",
                                         "reporter_contact": "C"},
                       files=[("photos", ("p.jpg", _jpeg_with_gps(), "image/jpeg"))], follow_redirects=False)
    ref, k = re.search(r"/case/(MP-\w+)\?k=([^&]+)", resp.headers["location"]).groups()
    pid = re.search(rf"/case/{ref}/photos/(\d+)", client.get(f"/case/{ref}?k={k}").text).group(1)
    client.post(f"/case/{ref}/appeal", data={"k": k, "publish": "1", "photo_id": pid})
    return ref, k, pid


def test_photos_lose_their_gps_location():
    assert _gps(_jpeg_with_gps())  # the test photo really has a location
    for ext in photos.FORMATS:
        assert not _gps(photos.clean(_jpeg_with_gps(), ext)), ext
    with TestClient(app) as client:
        seed.run()
        ref, k, _ = _published_appeal(client)
        client.post("/responder/login", data=LOGIN)
        client.post(f"/responder/appeals/{ref}", data={"action": "approve"})
        client.post("/responder/logout")
        assert not _gps(client.get(f"/appeal/{ref}/photo").content)  # public photo
        client.post(f"/appeal/{ref}/tips", data={"message": "seen"},
                    files=[("photos", ("t.jpg", _jpeg_with_gps(), "image/jpeg"))])
        tpid = re.search(rf"/case/{ref}/tips/photos/(\d+)", client.get(f"/case/{ref}?k={k}").text).group(1)
        assert not _gps(client.get(f"/case/{ref}/tips/photos/{tpid}?k={k}").content)  # tipster's photo


def test_decompression_bombs_and_huge_photos_are_refused():
    buf = io.BytesIO()
    Image.new("L", (12000, 12000), 0).save(buf, "PNG")  # 144 megapixels in a small file
    with TestClient(app) as client:
        seed.run()
        resp = client.post("/missing", data={"disaster_id": "1", "full_name": "x", "reporter_name": "y",
                                             "reporter_contact": "z"},
                           files=[("photos", ("b.png", buf.getvalue(), "image/png"))], follow_redirects=False)
        assert resp.status_code == 400 and "megapixels" in resp.text
    big = photos.clean(_jpeg_with_gps((5000, 3000)), ".jpg")
    assert max(Image.open(io.BytesIO(big)).size) == photos.MAX_EDGE


def test_appeals_go_public_only_after_a_responder_approves():
    limits.reset()
    with TestClient(app) as client:
        seed.run()
        ref, k, _ = _published_appeal(client)
        assert client.get(f"/appeal/{ref}").status_code == 404
        assert client.post(f"/appeal/{ref}/tips", data={"message": "where she is"}).status_code == 404
        assert client.post(f"/responder/appeals/{ref}", data={"action": "approve"},
                           follow_redirects=False).status_code == 303  # not signed in: sent to the login page
        assert client.get(f"/appeal/{ref}").status_code == 404

        client.post("/responder/login", data=LOGIN)
        assert "Public appeals awaiting approval" in client.get("/responder").text
        client.post(f"/responder/appeals/{ref}", data={"action": "reject"})
        assert "rejected appeal" in client.get("/responder/activity").text.lower()
        client.post("/responder/logout")
        assert client.get(f"/appeal/{ref}").status_code == 404
        assert "Not approved" in client.get(f"/case/{ref}?k={k}").text


def test_changing_the_public_photo_needs_approval_again():
    limits.reset()
    with TestClient(app) as client:
        seed.run()
        ref, k, pid = _published_appeal(client)
        client.post("/responder/login", data=LOGIN)
        client.post(f"/responder/appeals/{ref}", data={"action": "approve"})
        client.post("/responder/logout")
        assert client.get(f"/appeal/{ref}").status_code == 200
        client.post(f"/case/{ref}/appeal", data={"k": k, "publish": "1", "photo_id": pid})  # same photo: still public
        assert client.get(f"/appeal/{ref}").status_code == 200
        client.post(f"/case/{ref}/appeal", data={"k": k, "publish": "1", "photo_id": ""})  # changed: checked again
        assert client.get(f"/appeal/{ref}").status_code == 404


def test_client_address_behind_a_proxy(monkeypatch):
    scope = {"client": ("10.0.0.1", 1), "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")]}
    assert limits.client_ip(scope) == "10.0.0.1"  # no proxy configured: the header is ignored
    monkeypatch.setattr(limits, "TRUSTED_PROXY_HOPS", 1)
    assert limits.client_ip(scope) == "203.0.113.9"  # the address Cloud Run saw, not the faked one

    limits.reset()
    with TestClient(app) as client:  # visitors behind the proxy no longer share one limit
        codes = [client.post("/responder/login", data={"email": f"p{i}@x.org", "password": "x"},
                             headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code for i in range(12)]
        assert 429 not in codes
        faked = [client.post("/responder/login", data={"email": f"q{i}@x.org", "password": "x"},
                             headers={"X-Forwarded-For": f"{i}.1.1.1, 198.51.100.200"}).status_code for i in range(11)]
        assert faked[-1] == 429  # faking the left-hand entries doesn't dodge the limit


def test_a_stranger_cannot_lock_a_responder_out(monkeypatch):
    monkeypatch.setattr(limits, "TRUSTED_PROXY_HOPS", 1)
    limits.reset()
    with TestClient(app) as client:
        with db.session() as conn:
            accounts.create(conn, "Target", "target@example.org", password="target-password", must_change=False)
        for _ in range(accounts.MAX_FAILED_SIGN_INS):
            client.post("/responder/login", data={"email": "target@example.org", "password": "x"},
                        headers={"X-Forwarded-For": "198.51.100.66"})
        blocked = client.post("/responder/login", data={"email": "target@example.org", "password": "target-password"},
                              headers={"X-Forwarded-For": "198.51.100.66"})
        assert blocked.status_code == 429  # the guesser is locked out...
        ok = client.post("/responder/login", data={"email": "target@example.org", "password": "target-password"},
                         headers={"X-Forwarded-For": "203.0.113.5"}, follow_redirects=False)
        assert ok.status_code == 303  # ...the real responder elsewhere is not


def test_security_headers_and_https_cookies():
    limits.reset()
    with TestClient(app) as client:
        headers = client.get("/").headers
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-frame-options"] == "DENY" and headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert headers["x-content-type-options"] == "nosniff"
        assert "strict-transport-security" not in headers  # plain HTTP locally
        assert client.get("/responder/login").headers["cache-control"] == "no-store"

        resp = client.post("/responder/login", data=LOGIN, headers={"X-Forwarded-Proto": "https"},
                           follow_redirects=False)
        assert "secure" in resp.headers["set-cookie"].lower()
        assert "strict-transport-security" in resp.headers


def test_bad_or_future_dates_are_dropped_instead_of_hiding_matches():
    from app.main import _date
    assert _date("2026-09-10") == "2026-09-10"
    assert _date("not-a-date") is None and _date("2026-13-45") is None and _date("2099-01-01") is None
    limits.reset()
    with TestClient(app) as client:
        seed.run()
        resp = client.post("/missing", data={
            "disaster_id": "1", "full_name": "Ram Thapa", "reporter_name": "S", "reporter_contact": "C",
            "sex": "male", "age": "35", "last_seen_date": "zzz", "feature_kind": ["tattoo"],
            "feature_location": ["left forearm"], "feature_text": ["anchor with MAA"]}, follow_redirects=False)
        assert "Chatara" in client.get(resp.headers["location"]).text  # the seeded anchor-tattoo match still shows


def test_private_keys_are_hidden_in_the_access_log():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4", "GET", "/case/MP-ABC123?k=SECRET-KEY&new=1", "1.1", 200), None)
    HideSecretsInLogs().filter(record)
    assert "SECRET-KEY" not in record.getMessage() and "k=[hidden]&new=1" in record.getMessage()
    record.args = ("1.2.3.4", "GET", "/safe/c/CHECKIN-TOKEN", "1.1", 200)
    HideSecretsInLogs().filter(record)
    assert "CHECKIN-TOKEN" not in record.getMessage()
