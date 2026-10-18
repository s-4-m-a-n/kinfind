""""I'm safe" check-ins: finding needs name + phone, people with you, privacy and moderation."""
import os
import re
import tempfile

os.environ.setdefault("DISASTERLENS_DATA", tempfile.mkdtemp())
os.environ["GDACS_SYNC_MINUTES"] = "0"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.setdefault("ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")

from fastapi.testclient import TestClient  # noqa: E402

from app import limits, safe, seed  # noqa: E402
from app.main import app  # noqa: E402

LOGIN = {"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}


def test_name_and_phone_matching():
    assert safe.name_key("Sita  B. Thapa") == "b sita thapa"
    assert safe.names_match(safe.name_key("Thapa Sita"), safe.name_key("Sita Thapa"))      # order
    assert safe.names_match(safe.name_key("Sita Thapa"), safe.name_key("Sita Kumari Thapa"))  # middle name
    assert not safe.names_match(safe.name_key("Sita Thapa"), safe.name_key("Gita Thapa"))
    assert not safe.names_match(safe.name_key("Thapa"), safe.name_key("Sita Thapa"))         # surname alone
    assert safe.names_match(safe.name_key("सीता थापा"), safe.name_key("थापा सीता"))           # any script
    assert safe.phone_key("+977 980-1234567") == safe.phone_key("9801234567")
    assert safe.phone_key("12") is None


def _find(client, name, phone):
    return client.post("/safe/find", data={"name": name, "phone": phone}).text


def test_check_in_and_find():
    limits.reset()
    with TestClient(app) as client:
        seed.run()
        resp = client.post("/safe", data={
            "disaster_id": "1", "name": "Pemba Tamang", "phone": "+977 980-5550001",
            "other_name": ["Lhamu Tamang", "Dawa Tamang", ""], "other_phone": ["", "9805550002", ""],
            "message": "All fine, phone battery low.", "place": "Dhunche school shelter"}, follow_redirects=False)
        manage = resp.headers["location"]
        assert resp.status_code == 303 and manage.startswith("/safe/c/")

        # Found with name + their own number, in any format.
        page = _find(client, "Pemba Tamang", "9805550001")
        assert "Pemba Tamang has checked in as safe" in page and "All fine, phone battery low." in page
        assert "Dhunche school shelter" not in page          # place not shared unless ticked
        assert "Lhamu" not in page and "Dawa" not in page   # other people's names stay private
        # A child without a phone: their name + the number of whoever checked them in.
        assert "Lhamu Tamang has checked in as safe" in _find(client, "Lhamu Tamang", "980 555 0001")
        assert "Dawa Tamang has checked in as safe" in _find(client, "Dawa Tamang", "9805550002")
        # Wrong number, or a stranger guessing a surname: nothing.
        assert "No check-in found" in _find(client, "Pemba Tamang", "9800000000")
        assert "No check-in found" in _find(client, "Tamang", "9805550001")
        assert client.post("/safe/find", data={"name": "Pemba Tamang", "phone": ""}).status_code == 400
        assert client.get("/safe/find?name=Pemba+Tamang&phone=9805550001").status_code == 405  # never via URL

        # The person updates, shares the place, withdraws, restores.
        client.post(manage, data={"action": "update", "message": "Moved to Kathmandu.", "place": "Kalanki",
                                  "share_place": "1"})
        page = _find(client, "Pemba Tamang", "9805550001")
        assert "Moved to Kathmandu." in page and "Kalanki" in page
        client.post(manage, data={"action": "withdraw"})
        assert "No check-in found" in _find(client, "Pemba Tamang", "9805550001")
        client.post(manage, data={"action": "restore"})
        assert "has checked in as safe" in _find(client, "Pemba Tamang", "9805550001")
        assert client.get("/safe/c/not-a-real-token").status_code == 404

        # A family who gave the person's number on their report sees the check-in on their case page.
        resp = client.post("/missing", data={"disaster_id": "1", "full_name": "Dawa Tamang", "reporter_name": "Aunt",
                                             "reporter_contact": "x", "person_phone": "980-555-0002"},
                           follow_redirects=False)
        case = client.get(resp.headers["location"]).text
        assert "marked themselves safe" in case and "Moved to Kathmandu." in case
        resp = client.post("/missing", data={"disaster_id": "1", "full_name": "Pemba Tamang", "reporter_name": "B",
                                             "reporter_contact": "x"}, follow_redirects=False)
        assert "marked themselves safe" not in client.get(resp.headers["location"]).text  # no number given

        # Responders see similar names on missing records, and can remove fake check-ins.
        client.post("/responder/login", data=LOGIN)
        ref = re.search(r"/case/(MP-\w+)", resp.headers["location"]).group(1)
        record = client.get(f"/responder/missing/{ref}").text
        assert "Safe check-ins with a similar name" in record and "9805550001" in record.replace("-", "").replace(" ", "")
        listing = client.get("/responder/safe?q=Tamang").text
        cid = re.search(r'/responder/safe/(\d+)/remove', listing).group(1)
        client.post(f"/responder/safe/{cid}/remove")
        assert "No check-in found" in _find(client, "Pemba Tamang", "9805550001")
        assert client.get(manage).status_code == 404


def test_check_in_validation_and_limits():
    limits.reset()
    with TestClient(app) as client:
        seed.run()
        assert client.post("/safe", data={"disaster_id": "1", "name": "No Phone"}).status_code == 400
        assert client.post("/safe", data={"disaster_id": "1", "phone": "9801111111"}).status_code == 400
        assert client.post("/safe", data={"disaster_id": "999", "name": "A B", "phone": "9801111111"}).status_code == 400
        client.post("/safe", data={"disaster_id": "1", "name": "Bot Name", "phone": "9802222222", "website": "x"})
        assert "No check-in found" in _find(client, "Bot Name", "9802222222")  # honeypot: not stored

        codes = [client.post("/safe/find", data={"name": "Some One", "phone": f"98000000{i:02d}"}).status_code
                 for i in range(25)]
        assert 429 in codes  # guessing phone numbers is rate-limited
