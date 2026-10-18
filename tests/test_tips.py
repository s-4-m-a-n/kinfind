"""Public appeals and community sightings: who sees what, moderation and abuse limits."""
import io
import os
import re
import tempfile

os.environ.setdefault("DISASTERLENS_DATA", tempfile.mkdtemp())
os.environ["GDACS_SYNC_MINUTES"] = "0"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.setdefault("RESPONDER_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import main, seed  # noqa: E402
from app.main import RESPONDER_KEY, app  # noqa: E402


def _img(color="tan") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(buf, "JPEG")
    return buf.getvalue()


def _report(client, name, contact):
    resp = client.post("/missing", data={"disaster_id": "1", "full_name": name, "age": "29",
                                         "last_seen_place": "Dhunche bazaar", "reporter_name": "Family of " + name,
                                         "reporter_contact": contact},
                       files=[("photos", ("face.jpg", _img(), "image/jpeg"))], follow_redirects=False)
    url = resp.headers["location"]
    ref, k = re.search(r"/case/(MP-\w+)\?k=([^&]+)", url).groups()
    return ref, k


def test_appeal_and_tips_flow():
    main._recent_tips.clear()
    with TestClient(app) as client:
        seed.run()
        ref, k = _report(client, "Nima Lama", "family-phone-555")
        other_ref, other_k = _report(client, "Other Person", "other-555")

        # Private until the family publishes it.
        assert client.get(f"/appeal/{ref}").status_code == 404
        assert "Nima Lama" not in client.get("/appeals").text

        case = client.get(f"/case/{ref}?k={k}").text
        own_pid = re.search(rf"/case/{ref}/photos/(\d+)\?k=", case).group(1)
        other_case = client.get(f"/case/{other_ref}?k={other_k}").text
        other_pid = re.search(rf"/case/{other_ref}/photos/(\d+)\?k=", other_case).group(1)

        # A family can only publish one of their own photos.
        client.post(f"/case/{ref}/appeal", data={"k": k, "publish": "1", "photo_id": other_pid})
        assert client.get(f"/appeal/{ref}/photo").status_code == 404
        client.post(f"/case/{ref}/appeal", data={"k": k, "publish": "1", "photo_id": own_pid})
        public = client.get(f"/appeal/{ref}").text
        assert "Have you seen Nima Lama?" in public and "Dhunche bazaar" in public
        assert client.get(f"/appeal/{ref}/photo").status_code == 200
        assert "family-phone-555" not in public and "Family of" not in public and k not in public
        assert "Nima Lama" in client.get("/appeals").text and "Nima Lama" in client.get("/").text
        assert client.post(f"/case/{ref}/appeal", data={"k": "wrong", "publish": "0"}).status_code == 404

        # A member of the public sends a sighting.
        resp = client.post(f"/appeal/{ref}/tips", data={
            "seen_place": "Syabrubesi shelter", "seen_at": "2026-10-12T09:30", "lat": "28.16", "lng": "85.34",
            "condition": "needs_help", "message": "Saw her helping at the shelter kitchen.", "contact": "tipster-777"},
            files=[("photos", ("tip.jpg", _img("grey"), "image/jpeg"))], follow_redirects=False)
        assert resp.status_code == 303 and "sent=1" in resp.headers["location"]

        # Family sees it at once, marked unverified, photo blurred, no tipster contact.
        case = client.get(f"/case/{ref}?k={k}").text
        assert "Saw her helping at the shelter kitchen." in case and "Unverified" in case
        assert "Syabrubesi shelter" in case and "Injured or needed help" in case
        assert "tipster-777" not in case and 'class="tip-photo blurred"' in case
        tip_photo = re.search(rf"/case/{ref}/tips/photos/(\d+)\?k=", case).group(1)
        assert client.get(f"/case/{ref}/tips/photos/{tip_photo}?k={k}").status_code == 200
        assert client.get(f"/case/{ref}/tips/photos/{tip_photo}?k=nope").status_code == 404
        assert client.get(f"/case/{other_ref}/tips/photos/{tip_photo}?k={other_k}").status_code == 404
        assert "Saw her helping" not in client.get(f"/appeal/{ref}").text  # tips are never public
        assert "deceased" not in case.lower()

        # Responders see the contact and can check or hide tips.
        client.post("/responder/login", data={"key": RESPONDER_KEY})
        record = client.get(f"/responder/missing/{ref}").text
        assert "tipster-777" in record and "Saw her helping" in record
        assert "Nima Lama" in client.get("/responder").text.split("New community sightings")[1]
        tid = re.search(r'action="/responder/tips/(\d+)"', record).group(1)
        client.post(f"/responder/tips/{tid}", data={"action": "verify"})
        client.post("/responder/logout")
        assert "Checked by a responder" in client.get(f"/case/{ref}?k={k}").text

        client.post("/responder/login", data={"key": RESPONDER_KEY})
        client.post(f"/responder/tips/{tid}", data={"action": "hide"})
        assert "Hidden" in client.get(f"/responder/missing/{ref}").text  # responders still see it
        client.post("/responder/logout")
        assert "Saw her helping" not in client.get(f"/case/{ref}?k={k}").text
        assert client.get(f"/case/{ref}/tips/photos/{tip_photo}?k={k}").status_code == 404

        # The family can hide a tip themselves.
        client.post(f"/appeal/{ref}/tips", data={"message": "Rude message"})
        case = client.get(f"/case/{ref}?k={k}").text
        family_tid = re.search(rf'action="/case/{ref}/tips/(\d+)/hide"', case).group(1)
        client.post(f"/case/{ref}/tips/{family_tid}/hide", data={"k": k})
        assert "Rude message" not in client.get(f"/case/{ref}?k={k}").text

        # Taking the appeal down stops new tips.
        client.post(f"/case/{ref}/appeal", data={"k": k, "publish": "0"})
        assert client.get(f"/appeal/{ref}").status_code == 404
        assert client.post(f"/appeal/{ref}/tips", data={"message": "late"}).status_code == 404


def test_tip_abuse_limits():
    main._recent_tips.clear()
    with TestClient(app) as client:
        seed.run()
        ref, k = _report(client, "Rate Limit", "x")
        client.post(f"/case/{ref}/appeal", data={"k": k, "publish": "1"})

        # Bots filling the hidden honeypot field are thanked but nothing is stored.
        client.post(f"/appeal/{ref}/tips", data={"message": "spam", "website": "http://spam"})
        assert "spam" not in client.get(f"/case/{ref}?k={k}").text

        assert client.post(f"/appeal/{ref}/tips", data={"message": ""}).status_code == 400
        too_many = [("photos", (f"{i}.jpg", _img(), "image/jpeg")) for i in range(5)]
        assert client.post(f"/appeal/{ref}/tips", data={"message": "m"}, files=too_many).status_code == 400

        codes = [client.post(f"/appeal/{ref}/tips", data={"message": f"tip {i}"}, follow_redirects=False).status_code
                 for i in range(6)]
        assert 429 in codes  # several tips from one address in a few minutes are refused
