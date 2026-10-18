"""Responder accounts: the admin team page, one-time passwords, sessions and the activity log."""
import os
import re
import tempfile

os.environ.setdefault("DISASTERLENS_DATA", tempfile.mkdtemp())
os.environ["GDACS_SYNC_MINUTES"] = "0"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.setdefault("ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")

from fastapi.testclient import TestClient  # noqa: E402

from app import accounts, db, seed  # noqa: E402
from app.main import app  # noqa: E402

LOGIN = {"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]}


def _issued_password(page: str) -> str:
    return re.search(r'<code class="secret">([^<]+)</code>', page).group(1)


def test_admin_adds_responder_who_must_choose_a_password():
    with TestClient(app) as admin, TestClient(app) as new:
        admin.post("/responder/login", data=LOGIN)
        page = admin.post("/responder/team", data={"name": "Maya Gurung", "email": "Maya@Example.org",
                                                    "organisation": "Red Cross", "role": "responder"}).text
        assert "Account created for Maya Gurung" in page and "maya@example.org" in page
        temp = _issued_password(page)
        assert temp not in admin.get("/responder/team").text  # shown once only

        assert admin.post("/responder/team", data={"name": "Again", "email": "maya@example.org"}).status_code == 400

        resp = new.post("/responder/login", data={"email": "maya@example.org", "password": temp},
                        follow_redirects=False)
        assert resp.headers["location"] == "/responder/password"
        # Nothing else opens until the one-time password is replaced.
        assert new.get("/responder", follow_redirects=False).headers["location"] == "/responder/password"
        short = new.post("/responder/password", data={"current_password": temp, "new_password": "short",
                                                      "confirm_password": "short"})
        assert short.status_code == 400
        new.post("/responder/password", data={"current_password": temp, "new_password": "maya-own-pass",
                                              "confirm_password": "maya-own-pass"})
        assert new.get("/responder").status_code == 200

        # Responders can't reach the admin pages.
        assert new.get("/responder/team").status_code == 403
        assert new.get("/responder/activity").status_code == 403
        assert new.post("/responder/team", data={"name": "x", "email": "x@x.org"}).status_code == 403


def test_deactivate_and_reset_sign_the_person_out():
    with TestClient(app) as admin, TestClient(app) as member:
        admin.post("/responder/login", data=LOGIN)
        with db.session() as conn:
            rid, _ = accounts.create(conn, "Bikash", "bikash@example.org", password="bikash-pass-1",
                                     must_change=False)
        member.post("/responder/login", data={"email": "bikash@example.org", "password": "bikash-pass-1"})
        assert member.get("/responder").status_code == 200

        admin.post(f"/responder/team/{rid}", data={"action": "deactivate"})
        assert member.get("/responder", follow_redirects=False).status_code == 303
        failed = member.post("/responder/login", data={"email": "bikash@example.org", "password": "bikash-pass-1"})
        assert failed.status_code == 401

        admin.post(f"/responder/team/{rid}", data={"action": "activate"})
        temp = _issued_password(admin.post(f"/responder/team/{rid}", data={"action": "reset"}).text)
        assert member.post("/responder/login", data={"email": "bikash@example.org",
                                                     "password": "bikash-pass-1"}).status_code == 401
        resp = member.post("/responder/login", data={"email": "bikash@example.org", "password": temp},
                           follow_redirects=False)
        assert resp.headers["location"] == "/responder/password"


def test_admin_cannot_lock_themselves_out():
    with TestClient(app) as admin:
        admin.post("/responder/login", data=LOGIN)
        with db.session() as conn:
            me = conn.execute("SELECT id FROM responders WHERE email=?", (LOGIN["email"],)).fetchone()["id"]
        for action in ("deactivate", "make-responder"):
            assert admin.post(f"/responder/team/{me}", data={"action": action}).status_code == 400
        assert admin.get("/responder/team").status_code == 200


def test_sign_out_ends_the_session():
    with TestClient(app) as client:
        client.post("/responder/login", data=LOGIN)
        cookie = client.cookies.get("responder")
        client.post("/responder/logout")
        client.cookies.set("responder", cookie)  # replaying the old cookie no longer works
        assert client.get("/responder", follow_redirects=False).status_code == 303


def test_activity_log_records_who_did_what():
    with TestClient(app) as client:
        seed.run()
        client.post("/responder/login", data={"email": "nobody@example.org", "password": "wrong-password"})
        client.post("/responder/login", data=LOGIN)
        client.post("/responder/found", data={"disaster_id": "1", "status": "alive", "found_place": "Camp 2"})
        log = client.get("/responder/activity").text
        assert "Failed sign-in" in log and "nobody@example.org" in log
        assert "Signed in" in log and "Logged found person (alive)" in log


def test_repeated_wrong_passwords_lock_the_email_out():
    with TestClient(app) as client:
        with db.session() as conn:
            accounts.create(conn, "Guessed", "guessed@example.org", password="real-password-1", must_change=False)
        for _ in range(accounts.MAX_FAILED_SIGN_INS):
            assert client.post("/responder/login", data={"email": "Guessed@example.org",
                                                         "password": "wrong-guess"}).status_code == 401
        # Even the right password is refused while locked out.
        locked = client.post("/responder/login", data={"email": "guessed@example.org", "password": "real-password-1"})
        assert locked.status_code == 429 and "Too many failed attempts" in locked.text
        with db.session() as conn:  # 15 minutes later
            conn.execute("UPDATE sign_in_failures SET created_at = datetime('now', '-16 minutes') WHERE email=?",
                         ("guessed@example.org",))
        resp = client.post("/responder/login", data={"email": "guessed@example.org", "password": "real-password-1"},
                           follow_redirects=False)
        assert resp.status_code == 303


def test_sessions_expire():
    with TestClient(app) as client:
        client.post("/responder/login", data=LOGIN)
        assert client.get("/responder").status_code == 200
        with db.session() as conn:
            conn.execute("UPDATE sessions SET expires_at = datetime('now', '-1 minute')")
        assert client.get("/responder", follow_redirects=False).status_code == 303


def test_activity_log_links_each_case_ref():
    from app.main import ref_links
    html = str(ref_links("MP-AB23CD ↔ FD-XY45ZW <b>"))
    assert '<a href="/responder/missing/MP-AB23CD">MP-AB23CD</a>' in html
    assert '<a href="/responder/found/FD-XY45ZW">FD-XY45ZW</a>' in html
    assert "<b>" not in html and "&lt;b&gt;" in html


def test_create_admin_command():
    accounts.main(["create-admin", "--email", "second@example.org", "--password", "second-admin-pass"])
    with db.session() as conn:
        row = accounts.authenticate(conn, "second@example.org", "second-admin-pass")
    assert row and row["role"] == "admin" and not row["must_change_password"]
