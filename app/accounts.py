"""Responder accounts: one sign-in each, sessions that expire, and a log of who did what.

Admins add responders on the Team page and hand them a one-time password; responders
choose their own password at first sign-in.

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
SESSION_HOURS = 12
MIN_PASSWORD = 10
MAX_FAILED_SIGN_INS, MAX_FAILED_SIGN_INS_ANY_IP, LOCKOUT_MINUTES = 5, 50, 15
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
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
           password: str | None = None, must_change: bool = True) -> tuple[int, str]:
    """Add an account. Returns (id, password); the password is a one-time one unless given."""
    password = password or temp_password()
    cur = conn.execute(
        """INSERT INTO responders (name, email, organisation, role, password_hash, must_change_password)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, email.strip().lower(), organisation, role, hash_password(password), int(must_change)))
    return cur.lastrowid, password


def authenticate(conn, email: str, password: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM responders WHERE email=?", (email.strip().lower(),)).fetchone()
    if not row:
        hash_password(password)  # take as long as a real check, so emails can't be probed by timing
        return None
    ok = check_password(password, row["password_hash"])  # checked even for deactivated accounts, for the same reason
    return row if ok and row["active"] else None


def locked_out(conn, email: str, ip: str) -> bool:
    """Too many failed sign-ins for this email recently: slows down password guessing.

    Counted per email *and* address, so a stranger guessing doesn't lock the real person out.
    A much higher limit per email alone stops guessing spread over many addresses.
    """
    since = _stamp(_now() - timedelta(minutes=LOCKOUT_MINUTES))
    mine, everyone = conn.execute("""SELECT COALESCE(SUM(ip = ?), 0), COUNT(*) FROM audit_log
                                     WHERE action='failed sign-in' AND detail=? AND created_at > ?""",
                                  (ip, email.strip().lower(), since)).fetchone()
    return mine >= MAX_FAILED_SIGN_INS or everyone >= MAX_FAILED_SIGN_INS_ANY_IP


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


# --- sessions ----------------------------------------------------------------

def start_session(conn, responder_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_stamp(now),))
    conn.execute("INSERT INTO sessions (token_hash, responder_id, expires_at) VALUES (?, ?, ?)",
                 (_token_hash(token), responder_id, _stamp(now + timedelta(hours=SESSION_HOURS))))
    conn.execute("UPDATE responders SET last_login_at=datetime('now') WHERE id=?", (responder_id,))
    return token


def from_session(conn, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute("""SELECT r.* FROM sessions s JOIN responders r ON r.id = s.responder_id
                           WHERE s.token_hash=? AND s.expires_at > ? AND r.active = 1""",
                        (_token_hash(token), _stamp(_now()))).fetchone()


def end_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def end_all_sessions(conn, responder_id: int, keep: str | None = None) -> None:
    """Sign someone out everywhere, optionally keeping the session they're using now."""
    conn.execute("DELETE FROM sessions WHERE responder_id=? AND token_hash != ?",
                 (responder_id, _token_hash(keep) if keep else ""))


# --- activity log ------------------------------------------------------------

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
    reset = sub.add_parser("reset-password", help="Set a new password for an account")
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
            row = conn.execute("SELECT id FROM responders WHERE email=?", (args.email.strip().lower(),)).fetchone()
            if not row:
                sys.exit(f"No account for {args.email}")
            set_password(conn, row["id"], password)
            conn.execute("UPDATE responders SET active=1 WHERE id=?", (row["id"],))
            end_all_sessions(conn, row["id"])
            print(f"Password updated for {args.email}")


if __name__ == "__main__":
    main()
