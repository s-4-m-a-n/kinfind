"""Sign-up and sign-in: family accounts, responder access requests, and 'Continue with Google'."""
import os
import re
import tempfile

os.environ.setdefault("DISASTERLENS_DATA", tempfile.mkdtemp())
os.environ["GDACS_SYNC_MINUTES"] = "0"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.setdefault("ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import accounts, limits, seed  # noqa: E402
from app.main import app  # noqa: E402

LOGIN = {"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}


@pytest.fixture
def google(monkeypatch):
    """Stand in for Google: the credential 'token:<sub>:<email>' is accepted as a verified account."""
    def verify(credential):
        if not credential.startswith("token:"):
            return None
        _, sub, email = credential.split(":")
        return {"sub": sub, "email": email, "email_verified": True, "name": email.split("@")[0].title()}
    monkeypatch.setattr(accounts, "verify_google", verify)


def _signup(client, email, name="Sita Thapa", password="family-pass-1", **extra):
    return client.post("/account/signup", data={"name": name, "email": email, "password": password,
                                                "confirm_password": password, **extra}, follow_redirects=False)


def _report(client, name="Hari Thapa"):
    resp = client.post("/missing", data={"disaster_id": "1", "full_name": name, "reporter_name": "Sita",
                                         "reporter_contact": "98000"}, follow_redirects=False)
    return re.search(r"/case/(MP-\w+)\?k=([^&]+)", resp.headers["location"]).groups()


def test_family_account_keeps_cases_in_one_place():
    limits.reset()
    with TestClient(app) as family, TestClient(app) as stranger:
        seed.run()
        old_ref, old_k = _report(family, "Filed Before")  # before signing up: private link only

        assert _signup(family, "short@example.org", password="short").status_code == 400
        resp = _signup(family, "Sita@Example.org")
        assert resp.status_code == 303 and resp.headers["location"] == "/account"
        assert _signup(stranger, "sita@example.org").status_code == 400  # email already used

        new_ref, new_k = _report(family, "Filed Signed In")  # saved to the account automatically
        page = family.get("/account").text
        assert "Filed Signed In" in page and f"/case/{new_ref}?k={new_k}" in page
        assert "Filed Before" not in page

        # An older case is added from its private page.
        assert "Save to my account" in family.get(f"/case/{old_ref}?k={old_k}").text
        family.post(f"/case/{old_ref}/save", data={"k": old_k})
        assert "Filed Before" in family.get("/account").text
        assert family.post(f"/case/{old_ref}/save", data={"k": "wrong"}).status_code == 404  # needs the link

        # Another family can't take it over, even with the link.
        _signup(stranger, "other@example.org")
        assert stranger.post(f"/case/{old_ref}/save", data={"k": old_k}).status_code == 403
        assert "Filed Before" not in stranger.get("/account").text

        # Signing out and back in.
        family.post("/account/logout")
        assert family.get("/account", follow_redirects=False).headers["location"] == "/account/login"
        assert family.post("/account/login", data={"email": "sita@example.org", "password": "nope"}).status_code == 401
        family.post("/account/login", data={"email": "sita@example.org", "password": "family-pass-1"})
        assert "Filed Before" in family.get("/account").text


def test_family_accounts_never_open_responder_pages():
    limits.reset()
    with TestClient(app) as client:
        _signup(client, "family-only@example.org")
        assert client.get("/responder", follow_redirects=False).headers["location"] == "/responder/login"
        assert client.post("/responder/login", data={"email": "family-only@example.org",
                                                      "password": "family-pass-1"}).status_code == 401


def test_sign_in_returns_to_the_case():
    limits.reset()
    with TestClient(app) as client:
        seed.run()
        ref, k = _report(client)
        back = f"/case/{ref}?k={k}"
        resp = _signup(client, "returning@example.org", next=back)
        assert resp.headers["location"] == back
        assert _signup(client, "evil@example.org", next="//evil.example/x").headers["location"] == "/account"


def test_responder_requests_access_and_an_admin_approves():
    limits.reset()
    with TestClient(app) as applicant, TestClient(app) as admin:
        form = {"name": "Maya Rai", "email": "maya@redcross.example", "organisation": "Red Cross",
                "password": "maya-pass-123", "confirm_password": "maya-pass-123"}
        assert "Request sent" in applicant.post("/responder/signup", data=form).text
        assert "Request sent" in applicant.post("/responder/signup", data=form).text  # same reply: no probing

        pending = applicant.post("/responder/login", data={"email": "maya@redcross.example", "password": "maya-pass-123"})
        assert pending.status_code == 403 and "waiting for an admin" in pending.text
        assert applicant.get("/responder", follow_redirects=False).status_code == 303

        admin.post("/responder/login", data=LOGIN)
        team = admin.get("/responder/team").text
        assert "Access requests" in team and "Maya Rai" in team
        rid = re.search(r'action="/responder/team/(\d+)"[^>]*>\s*<button name="action" value="approve"', team).group(1)
        admin.post(f"/responder/team/{rid}", data={"action": "approve"})
        assert "approved access request" in admin.get("/responder/activity").text.lower()

        ok = applicant.post("/responder/login", data={"email": "maya@redcross.example", "password": "maya-pass-123"},
                            follow_redirects=False)
        assert ok.status_code == 303 and ok.headers["location"] == "/responder"
        assert applicant.get("/responder/team").status_code == 403  # a responder, not an admin


def test_rejected_requests_cannot_sign_in():
    limits.reset()
    with TestClient(app) as applicant, TestClient(app) as admin:
        applicant.post("/responder/signup", data={"name": "Fake", "email": "fake@example.org", "organisation": "X",
                                                  "password": "fake-pass-123", "confirm_password": "fake-pass-123"})
        admin.post("/responder/login", data=LOGIN)
        rid = re.search(r'action="/responder/team/(\d+)"[^>]*>\s*<button name="action" value="approve"',
                        admin.get("/responder/team").text).group(1)
        admin.post(f"/responder/team/{rid}", data={"action": "reject"})
        assert admin.post(f"/responder/team/{rid}", data={"action": "approve"}).status_code == 400
        assert applicant.post("/responder/login", data={"email": "fake@example.org",
                                                        "password": "fake-pass-123"}).status_code == 401


def test_continue_with_google_for_families(google):
    limits.reset()
    with TestClient(app) as client:
        assert client.post("/account/google", data={"credential": "forged"}).status_code == 401
        resp = client.post("/account/google", data={"credential": "token:g-100:nima@gmail.com"}, follow_redirects=False)
        assert resp.headers["location"] == "/account" and "nima@gmail.com" in client.get("/account").text

        # A family who signed up with a password links Google on first use (same verified email).
        with TestClient(app) as other:
            _signup(other, "pema@gmail.com")
            other.post("/account/logout")
            other.post("/account/google", data={"credential": "token:g-200:pema@gmail.com"})
            assert "pema@gmail.com" in other.get("/account").text


def test_continue_with_google_for_responders(google):
    limits.reset()
    with TestClient(app) as applicant, TestClient(app) as admin:
        first = applicant.post("/responder/google", data={"credential": "token:g-300:tashi@gmail.com"})
        assert "request for responder access has been sent" in first.text
        assert applicant.get("/responder", follow_redirects=False).status_code == 303
        again = applicant.post("/responder/google", data={"credential": "token:g-300:tashi@gmail.com"})
        assert again.status_code == 403 and "waiting for an admin" in again.text

        admin.post("/responder/login", data=LOGIN)
        team = admin.get("/responder/team").text
        assert "tashi@gmail.com · via Google" in team
        rid = re.search(r'action="/responder/team/(\d+)"[^>]*>\s*<button name="action" value="approve"', team).group(1)
        admin.post(f"/responder/team/{rid}", data={"action": "approve"})

        ok = applicant.post("/responder/google", data={"credential": "token:g-300:tashi@gmail.com"},
                            follow_redirects=False)
        assert ok.headers["location"] == "/responder"
        # Google-only accounts have no password that works.
        assert applicant.post("/responder/login", data={"email": "tashi@gmail.com", "password": "!"}).status_code == 401


def test_sign_in_forms_refuse_other_sites(google):
    limits.reset()
    with TestClient(app) as client:
        evil = {"Origin": "https://evil.example"}
        for path, data in [("/account/google", {"credential": "token:g-1:a@b.org"}),
                           ("/account/login", {"email": "a@b.org", "password": "x"}),
                           ("/responder/login", LOGIN), ("/account/signup", {"name": "x"})]:
            assert client.post(path, data=data, headers=evil).status_code == 403, path
        ok = client.post("/responder/login", data=LOGIN, headers={"Origin": "http://testserver"},
                         follow_redirects=False)
        assert ok.status_code == 303
