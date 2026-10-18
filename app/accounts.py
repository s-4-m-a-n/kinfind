"""Accounts: responders (and admins) and families. One sign-in each, sessions that expire,
and a log of what responders do.

The two kinds are kept completely apart: separate tables, sessions and cookies, so a family
account can never open a responder page.

- Responders: an admin adds them on the Team page and hands over a one-time password, or they
  request access themselves (email or Google) and an admin approves the request.
- Families: optional. Anyone can sign up (email or Google) to keep all their cases and
  check-ins in one place. Private links keep working without an account.

    python -m app.accounts create-admin --email you@example.org      # first admin
    python -m app.accounts reset-password --email you@example.org    # locked out
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from . import db

ROLES = ("responder", "admin")
MIN_PASSWORD = 10
MAX_FAILED_SIGN_INS, MAX_FAILED_SIGN_INS_ANY_IP, LOCKOUT_MINUTES = 5, 50, 15
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
NO_PASSWORD = "!"  # stored for Google-only accounts: matches no password

# kind -> (account table, session table, session column pointing at the account, session length)
KINDS = {
    "responder": ("responders", "sessions", "responder_id", timedelta(hours=12)),
    "family": ("families", "family_sessions", "family_id", timedelta(days=30)),
}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        _, salt, digest = (stored or "").split("$")
        test = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(test.hex(), digest)


def temp_password() -> str:
    """A one-time password an admin can read out or send, e.g. 'k7pq-3mxa-9wbe'."""
    return "-".join("".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(4))
                    for _ in range(3))


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")  # same format as SQLite's datetime('now'), so they compare


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()  # a stolen database can't be used to sign in


# --- accounts ----------------------------------------------------------------

def create(conn, name: str, email: str, role: str = "responder", organisation: str | None = None,
           password: str | None = None, must_change: bool = True, active: bool = True,
           approval: str | None = None, google_sub: str | None = None) -> tuple[int, str | None]:
    """Add a responder account. Returns (id, password).

    Without a password a one-time one is made, unless google_sub is given (a Google-only account).
    """
    if password is None and not google_sub:
        password = temp_password()
    cur = conn.execute(
        """INSERT INTO responders (name, email, organisation, role, password_hash, must_change_password,
                                   active, approval, google_sub)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, email.strip().lower(), organisation, role, hash_password(password) if password else NO_PASSWORD,
         int(must_change and password is not None), int(active), approval, google_sub))
    return cur.lastrowid, password


def create_family(conn, name: str, email: str, password: str | None = None, google_sub: str | None = None) -> int:
    return conn.execute("INSERT INTO families (name, email, password_hash, google_sub) VALUES (?, ?, ?, ?)",
                        (name, email.strip().lower(), hash_password(password) if password else NO_PASSWORD,
                         google_sub)).lastrowid


def find(conn, kind: str, email: str) -> sqlite3.Row | None:
    table = KINDS[kind][0]
    return conn.execute(f"SELECT * FROM {table} WHERE email=?", (email.strip().lower(),)).fetchone()


def check_credentials(conn, kind: str, email: str, password: str) -> sqlite3.Row | None:
    """The account if the password is right, whether or not it is active. Same timing either way."""
    row = find(conn, kind, email)
    if not row:
        hash_password(password)  # take as long as a real check, so emails can't be probed by timing
        return None
    return row if check_password(password, row["password_hash"]) else None


def authenticate(conn, email: str, password: str, kind: str = "responder") -> sqlite3.Row | None:
    row = check_credentials(conn, kind, email, password)
    return row if row and row["active"] else None


def set_password(conn, responder_id: int, password: str, must_change: bool = False) -> None:
    conn.execute("UPDATE responders SET password_hash=?, must_change_password=? WHERE id=?",
                 (hash_password(password), int(must_change), responder_id))


def bootstrap_admin() -> None:
    """Create the first admin from ADMIN_EMAIL / ADMIN_PASSWORD when there are no accounts yet."""
    email, password = os.environ.get("ADMIN_EMAIL"), os.environ.get("ADMIN_PASSWORD")
    if not (email and password):
        return
    if len(password) < MIN_PASSWORD:
        raise SystemExit(f"ADMIN_PASSWORD must be at least {MIN_PASSWORD} characters")
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM responders").fetchone():
            create(conn, "Administrator", email, "admin", password=password, must_change=False)


# --- Google sign-in ----------------------------------------------------------

def google_client_id() -> str | None:
    return os.environ.get("GOOGLE_CLIENT_ID") or None


def verify_google(credential: str) -> dict | None:
    """Claims of a Google ID token issued to our client for a verified email, or None."""
    client_id = google_client_id()
    if not client_id or not credential:
        return None
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
    try:
        claims = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    except (ValueError, GoogleAuthError):
        return None
    return claims if claims.get("email_verified") and claims.get("email") else None


def google_account(conn, kind: str, claims: dict) -> sqlite3.Row | None:
    """The account for these Google claims: by Google ID, else by the (verified) email, which is then linked."""
    table = KINDS[kind][0]
    row = conn.execute(f"SELECT * FROM {table} WHERE google_sub=?", (claims["sub"],)).fetchone()
    if row:
        return row
    row = find(conn, kind, claims["email"])
    if row and not row["google_sub"]:
        conn.execute(f"UPDATE {table} SET google_sub=? WHERE id=?", (claims["sub"], row["id"]))
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row["id"],)).fetchone()
    return row


# --- sessions ----------------------------------------------------------------

def start_session(conn, account_id: int, kind: str = "responder") -> str:
    table, sessions, column, length = KINDS[kind]
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute(f"DELETE FROM {sessions} WHERE expires_at <= ?", (_stamp(now),))
    conn.execute(f"INSERT INTO {sessions} (token_hash, {column}, expires_at) VALUES (?, ?, ?)",
                 (_token_hash(token), account_id, _stamp(now + length)))
    conn.execute(f"UPDATE {table} SET last_login_at=datetime('now') WHERE id=?", (account_id,))
    return token


def session_seconds(kind: str) -> int:
    return int(KINDS[kind][3].total_seconds())


def from_session(conn, token: str | None, kind: str = "responder") -> sqlite3.Row | None:
    if not token:
        return None
    table, sessions, column, _ = KINDS[kind]
    return conn.execute(f"""SELECT a.* FROM {sessions} s JOIN {table} a ON a.id = s.{column}
                            WHERE s.token_hash=? AND s.expires_at > ? AND a.active = 1""",
                        (_token_hash(token), _stamp(_now()))).fetchone()


def end_session(conn, token: str | None, kind: str = "responder") -> None:
    if token:
        conn.execute(f"DELETE FROM {KINDS[kind][1]} WHERE token_hash=?", (_token_hash(token),))


def end_all_sessions(conn, account_id: int, keep: str | None = None, kind: str = "responder") -> None:
    """Sign someone out everywhere, optionally keeping the session they're using now."""
    _, sessions, column, _ = KINDS[kind]
    conn.execute(f"DELETE FROM {sessions} WHERE {column}=? AND token_hash != ?",
                 (account_id, _token_hash(keep) if keep else ""))


# --- password guessing -------------------------------------------------------

def record_failure(conn, kind: str, email: str, ip: str) -> None:
    conn.execute("INSERT INTO sign_in_failures (kind, email, ip) VALUES (?, ?, ?)", (kind, email.strip().lower(), ip))


def locked_out(conn, email: str, ip: str, kind: str = "responder") -> bool:
    """Too many failed sign-ins for this email recently: slows down password guessing.

    Counted per email *and* address, so a stranger guessing doesn't lock the real person out.
    A much higher limit per email alone stops guessing spread over many addresses.
    """
    since = _stamp(_now() - timedelta(minutes=LOCKOUT_MINUTES))
    mine, everyone = conn.execute("""SELECT COALESCE(SUM(ip = ?), 0), COUNT(*) FROM sign_in_failures
                                     WHERE kind=? AND email=? AND created_at > ?""",
                                  (ip, kind, email.strip().lower(), since)).fetchone()
    return mine >= MAX_FAILED_SIGN_INS or everyone >= MAX_FAILED_SIGN_INS_ANY_IP


# --- activity log (responders) -----------------------------------------------

def log(conn, user, action: str, detail: str | None = None, ip: str | None = None) -> None:
    conn.execute("INSERT INTO audit_log (responder_id, action, detail, ip) VALUES (?, ?, ?, ?)",
                 (user["id"] if user else None, action, detail, ip))


def recent_activity(conn, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute("""SELECT a.*, r.name, r.email FROM audit_log a
                           LEFT JOIN responders r ON r.id = a.responder_id
                           ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()


# --- command line ------------------------------------------------------------

def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.accounts")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="Add an admin account")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", default="Administrator")
    admin.add_argument("--password", help="Prompted for if omitted")
    reset = sub.add_parser("reset-password", help="Set a new password for a responder account")
    reset.add_argument("--email", required=True)
    reset.add_argument("--password", help="Prompted for if omitted")
    args = parser.parse_args(argv)

    password = args.password or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD:
        sys.exit(f"Password must be at least {MIN_PASSWORD} characters")
    db.init()
    with db.session() as conn:
        if args.command == "create-admin":
            try:
                create(conn, args.name, args.email, "admin", password=password, must_change=False)
            except sqlite3.IntegrityError:
                sys.exit(f"{args.email} already has an account; use reset-password instead")
            print(f"Created admin {args.email}")
        else:
            row = find(conn, "responder", args.email)
            if not row:
                sys.exit(f"No account for {args.email}")
            set_password(conn, row["id"], password)
            conn.execute("UPDATE responders SET active=1, approval=NULL WHERE id=?", (row["id"],))
            end_all_sessions(conn, row["id"])
            print(f"Password updated for {args.email}")


if __name__ == "__main__":
    main()
