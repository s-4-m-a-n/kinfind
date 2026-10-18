"""Kinfind — match missing-person reports with found people / recovered bodies.

Families file a report and see *where and when* a possible match was found.
They never see photos, the holding facility, or contact details: they ask for
verification and a responder contacts them.
"""
from __future__ import annotations

import asyncio
import hmac
import io
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException


def load_env_file(path: Path) -> None:
    """Read KEY=value lines from a git-ignored .env file. Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if sep and key and not key.startswith("#") and value:
            os.environ.setdefault(key, value)


load_env_file(Path(__file__).resolve().parent.parent / ".env")  # before anything reads os.environ

from . import accounts, ai_match, db, gdacs, illustrate, limits, photos, safe, sketch  # noqa: E402
from .matching import BODY_LOCATIONS, FEATURE_KINDS, HAIR_COLORS, rank_found, rank_missing  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "Kinfind"
MAX_PHOTO_BYTES = limits.MAX_PHOTO_BYTES
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
GDACS_SYNC_MINUTES = float(os.environ.get("GDACS_SYNC_MINUTES", "60"))  # 0 turns the sync off
log = logging.getLogger("uvicorn.error")  # shows up in the server output next to uvicorn's own lines


async def gdacs_loop():
    """Pull new GDACS alerts into the responders' review queue, forever."""
    while True:
        try:
            added = await asyncio.to_thread(gdacs.sync)
            if added:
                log.warning("GDACS: %d new suggested zone(s) awaiting review", added)
        except Exception as exc:  # GDACS down or offline: try again next round
            log.warning("GDACS sync failed: %s", exc)
        await asyncio.sleep(GDACS_SYNC_MINUTES * 60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    accounts.bootstrap_admin()
    task = asyncio.create_task(gdacs_loop()) if GDACS_SYNC_MINUTES > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
# Changes whenever a static file changes, so browsers fetch the new CSS/JS instead of a cached copy.
ASSET_VERSION = str(int(max(f.stat().st_mtime for f in (APP_DIR / "static").iterdir())))
templates.env.globals.update(asset_v=ASSET_VERSION, max_record_photos=limits.MAX_RECORD_PHOTOS, app_name=APP_NAME, feature_kinds=FEATURE_KINDS, body_locations=BODY_LOCATIONS,
                             hair_colors=HAIR_COLORS)


def photo_view(request: Request) -> str:
    """How a responder sees photos of deceased people: 'illustration', 'sketch' or 'original'.

    Filtered (illustration, or sketch without an AI key) unless they chose otherwise.
    Other photos (families' photos, people found alive) are always shown as they are.
    """
    view = request.cookies.get("photo_view")
    if view == "original" or (view == "sketch"):
        return view
    return "illustration" if illustrate.enabled() else "sketch"


def illustration_ready(pid: int) -> bool:
    with db.session() as conn:
        row = conn.execute("SELECT filename FROM photos WHERE id=?", (pid,)).fetchone()
    return bool(row) and illustrate.ready(row["filename"])


templates.env.tests["illustration_ready"] = illustration_ready  # {{ ids|reject('illustration_ready') }}
templates.env.filters["illustration_failure"] = illustrate.failure
_REF = re.compile(r"\b(MP|FD)-[A-Z0-9]{6}\b")


def ref_links(text) -> Markup:
    """Escape text and link every case ref in it, e.g. 'MP-AB12CD ↔ FD-XY34ZW'."""
    def link(m):
        return f'<a href="/responder/{"missing" if m.group(1) == "MP" else "found"}/{m.group(0)}">{m.group(0)}</a>'
    return Markup(_REF.sub(link, str(escape(text or ""))))


templates.env.filters["ref_links"] = ref_links
templates.env.globals["google_client_id"] = accounts.google_client_id()  # None hides "Continue with Google"


async def _reject(scope, receive, send, status: int, message: str, headers: dict) -> None:
    """Error page for requests stopped by app/limits.py (rate or size limits)."""
    response = render(Request(scope, receive), "error.html", status_code=status, message=message)
    response.headers.update(headers)
    await response(scope, receive, send)


app.add_middleware(limits.RequestLimits, reject=_reject)


def is_https(scope) -> bool:
    """True when the visitor uses HTTPS, including behind a proxy that ends HTTPS (Cloud Run)."""
    proto = dict(scope.get("headers") or []).get(b"x-forwarded-proto", b"").decode("latin-1")
    return scope.get("scheme") == "https" or proto.split(",")[0].strip() == "https"


# Map tiles, Leaflet and Google's sign-in button are the only things loaded from elsewhere.
# Inline scripts and styles are still used by the templates, hence 'unsafe-inline'.
GSI = "https://accounts.google.com/gsi/"
CSP = "; ".join([
    "default-src 'self'", f"script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com {GSI}client",
    f"style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com {GSI}style",
    "img-src 'self' data: blob: https://tile.openstreetmap.org https://cdnjs.cloudflare.com",
    f"connect-src 'self' {GSI}", f"frame-src {GSI}", "object-src 'none'", "base-uri 'self'",
    "form-action 'self'", "frame-ancestors 'none'"])
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Private links carry their key in the URL: other sites only ever get our origin, never the path
    # or query. (No Referer at all would break the map: OpenStreetMap's tile servers require one.)
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(self)",
}
PRIVATE_PATHS = ("/responder", "/case/", "/safe/c/", "/account")  # pages with contacts or private keys: never cache


class SecurityHeaders:
    """ASGI middleware adding the headers above to every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if is_https(scope):
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000")
                if scope["path"].startswith(PRIVATE_PATHS):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)  # added last, so it also covers rate-limit and size-limit errors


class HideSecretsInLogs(logging.Filter):
    """Keep private-link keys out of the access log: anyone reading the log could open the case."""
    PATTERNS = [(re.compile(r"([?&]k=)[^&\s]+"), r"\1[hidden]"), (re.compile(r"(/safe/c/)[^/?\s]+"), r"\1[hidden]")]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self.clean(a) if isinstance(a, str) else a for a in record.args)
        return True

    @classmethod
    def clean(cls, text: str) -> str:
        for pattern, replacement in cls.PATTERNS:
            text = pattern.sub(replacement, text)
        return text


logging.getLogger("uvicorn.access").addFilter(HideSecretsInLogs())


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx["user"] = current_user(request)
    ctx["is_responder"] = ctx["user"] is not None
    ctx["family"] = current_family(request)
    ctx["photo_view"] = photo_view(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    message = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "Page not found."
    response = render(request, "error.html", status_code=exc.status_code, message=message)
    response.headers.update(exc.headers or {})
    return response


# --- helpers -----------------------------------------------------------------

def new_ref(prefix: str) -> str:
    return prefix + "-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(6))


def _int(value, lo: int, hi: int) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _float(value, lo: float, hi: float) -> float | None:
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _text(value, limit: int = 2000) -> str | None:
    value = (value or "").strip()
    return value[:limit] or None


def _date(value) -> str | None:
    """A real YYYY-MM-DD date that isn't in the future (a day's slack for time zones), else None.

    Matching compares these as text, so a malformed date would silently hide every match.
    """
    try:
        d = date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    return d.isoformat() if d <= date.today() + timedelta(days=1) else None


def _choice(value, options) -> str | None:
    return value if value in options else None


def _upload(value):
    """A file the user actually picked, or None for an empty file input."""
    return value if getattr(value, "filename", None) else None


def check_photo_count(form) -> None:
    """Size is capped by app/limits.py; this caps how many photos one record can carry."""
    count = sum(1 for u in form.getlist("photos") + form.getlist("feature_photo") if _upload(u))
    if count > limits.MAX_RECORD_PHOTOS:
        raise HTTPException(400, f"Please add at most {limits.MAX_RECORD_PHOTOS} photos ({count} were added).")


def parse_features(form) -> list[dict]:
    if len(form.getlist("feature_kind")) > limits.MAX_FEATURES:
        raise HTTPException(400, f"Please list at most {limits.MAX_FEATURES} distinctive features.")
    rows = zip(form.getlist("feature_kind"), form.getlist("feature_location"),
               form.getlist("feature_text"))
    photos = form.getlist("feature_photo")
    out = []
    for i, (kind, location, text) in enumerate(rows):
        photo = _upload(photos[i]) if i < len(photos) else None
        kind, text = _choice(kind, FEATURE_KINDS), _text(text, 500)
        location = _choice(location, BODY_LOCATIONS) or "unknown"
        if photo and not kind:
            kind = "other"  # keep the photo even if the type was left blank
        if kind and (text or location != "unknown" or photo):
            out.append({"kind": kind, "location": location, "text": text, "photo": photo})
    return out


async def save_upload(upload) -> str:
    """Check an uploaded photo and write it to the uploads folder. Returns its filename."""
    ext = PHOTO_TYPES.get(upload.content_type)
    if not ext:
        raise HTTPException(400, "Photos must be JPEG, PNG or WebP.")
    data = await upload.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(400, "Each photo must be under 8 MB.")
    try:  # strips GPS and other metadata; rejects non-images and decompression bombs
        data = photos.clean(data, ext)
    except photos.BadImage as exc:
        raise HTTPException(400, f"“{upload.filename}” {exc}.")
    filename = secrets.token_hex(16) + ext
    (db.UPLOAD_DIR / filename).write_bytes(data)
    return filename


async def store_photo(conn, upload, owner_type: str, owner_id: int, feature_id: int | None = None) -> None:
    conn.execute("INSERT INTO photos (owner_type, owner_id, filename, feature_id) VALUES (?, ?, ?, ?)",
                 (owner_type, owner_id, await save_upload(upload), feature_id))


async def save_photos(conn, form, owner_type: str, owner_id: int) -> None:
    for upload in filter(None, map(_upload, form.getlist("photos"))):
        await store_photo(conn, upload, owner_type, owner_id)


async def save_features(conn, features: list[dict], owner_type: str, owner_id: int) -> None:
    for f in features:
        cur = conn.execute(
            "INSERT INTO features (owner_type, owner_id, kind, location, text) VALUES (?, ?, ?, ?, ?)",
            (owner_type, owner_id, f["kind"], f["location"], f["text"]))
        if f["photo"]:
            await store_photo(conn, f["photo"], owner_type, owner_id, cur.lastrowid)


def disasters(conn, include: int = 0):
    """Active zones, plus one pending zone when asked for (e.g. one a family just proposed)."""
    return conn.execute("""SELECT * FROM disasters
                           WHERE status = 'active' OR (id = ? AND status = 'pending')
                           ORDER BY status = 'pending' DESC, id DESC""", (include,)).fetchall()


def disaster_id(conn, form) -> int:
    # Pending zones are allowed so a family who proposed one isn't blocked from reporting.
    did = _int(form.get("disaster_id"), 1, 10**9)
    if not did or not conn.execute("SELECT 1 FROM disasters WHERE id=? AND status IN ('active', 'pending')",
                                   (did,)).fetchone():
        raise HTTPException(400, "Choose the disaster this report belongs to.")
    return did


def parse_zone(form) -> dict:
    zone = {"name": _text(form.get("name"), 200), "region": _text(form.get("region"), 200),
            "lat": _float(form.get("lat"), -90, 90), "lng": _float(form.get("lng"), -180, 180),
            "radius_km": _float(form.get("radius_km"), 1, 1000)}
    if not (zone["name"] and zone["region"]):
        raise HTTPException(400, "Give the disaster a name and the affected region.")
    if zone["lat"] is None or zone["lng"] is None or zone["radius_km"] is None:
        raise HTTPException(400, "Click the map to place the affected zone, and set a radius between 1 and 1000 km.")
    return zone


def insert_zone(conn, zone: dict, status: str, source: str, note: str | None = None) -> int:
    return conn.execute(
        """INSERT INTO disasters (name, region, lat, lng, radius_km, status, source, note)
           VALUES (:name, :region, :lat, :lng, :radius_km, :status, :source, :note)""",
        {**zone, "status": status, "source": source, "note": note}).lastrowid


def photo_count(conn, owner_type: str, owner_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM photos WHERE owner_type=? AND owner_id=?",
                        (owner_type, owner_id)).fetchone()[0]


# --- responder auth ----------------------------------------------------------

def current_user(request: Request):
    """The signed-in responder's account row, or None. Looked up once per request."""
    if not hasattr(request.state, "user"):
        with db.session() as conn:
            request.state.user = accounts.from_session(conn, request.cookies.get("responder"))
    return request.state.user


def is_responder(request: Request) -> bool:
    return current_user(request) is not None


def require_responder(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(303, headers={"Location": "/responder/login"})
    if user["must_change_password"] and request.url.path != "/responder/password":
        raise HTTPException(303, headers={"Location": "/responder/password"})
    return user


def require_admin(request: Request):
    user = require_responder(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Only admins can manage the team.")
    return user


def check_origin(request: Request) -> None:
    """Refuse sign-in and sign-up forms posted from another site (login CSRF).

    Browsers always send Origin on cross-site POSTs, so a missing header means same-site or a non-browser.
    """
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "This form must be sent from Kinfind itself.")


def signed_in(request: Request, kind: str, account_id: int, location: str) -> RedirectResponse:
    """Start a session and send the browser on, with the session cookie."""
    with db.session() as conn:
        token = accounts.start_session(conn, account_id, kind)
    resp = RedirectResponse(location, status_code=303)
    resp.set_cookie(kind, token, httponly=True, samesite="lax" if kind == "family" else "strict",
                    secure=is_https(request.scope), max_age=accounts.session_seconds(kind))
    return resp


def password_problem(form) -> str | None:
    password = str(form.get("password", ""))
    if len(password) < accounts.MIN_PASSWORD:
        return f"Choose a password of at least {accounts.MIN_PASSWORD} characters."
    if password != form.get("confirm_password"):
        return "The two passwords don't match."
    return None


TOO_MANY_TRIES = f"Too many failed attempts for this email. Wait {accounts.LOCKOUT_MINUTES} minutes and try again."
WAITING = "Your request for responder access is waiting for an admin to approve it."


def _login_page(request: Request, status_code: int = 200, **ctx):
    ctx.setdefault("error", None)
    ctx.setdefault("notice", None)
    ctx.setdefault("email", "")
    return render(request, "login.html", status_code=status_code, **ctx)


@app.get("/responder/login")
def login_form(request: Request):
    return _login_page(request)


@app.post("/responder/login")
async def login(request: Request):
    check_origin(request)
    form = await request.form()
    email, password = str(form.get("email", "")).strip().lower()[:200], str(form.get("password", ""))
    ip = limits.client_ip(request.scope)
    with db.session() as conn:
        if accounts.locked_out(conn, email, ip):
            accounts.log(conn, None, "blocked sign-in", email, ip)  # not counted, so the lock-out still ends
            return _login_page(request, 429, email=email, error=TOO_MANY_TRIES)
        account = accounts.check_credentials(conn, "responder", email, password)
        user = account if account and account["active"] else None
        accounts.log(conn, user, "signed in" if user else "failed sign-in", None if user else email, ip)
        if not account:
            accounts.record_failure(conn, "responder", email, ip)
    if account and account["approval"] == "pending":  # right password: safe to say why
        return _login_page(request, 403, email=email, notice=WAITING)
    if not user:
        return _login_page(request, 401, email=email, error="That email and password don't match an active account.")
    return signed_in(request, "responder", user["id"],
                     "/responder/password" if user["must_change_password"] else "/responder")


@app.post("/responder/google")
async def responder_google(request: Request):
    """'Continue with Google' for responders: signs in an approved responder, or files an access request."""
    check_origin(request)
    claims = accounts.verify_google(str((await request.form()).get("credential", "")))
    if not claims:
        return _login_page(request, 401, error="Google sign-in didn't work. Try again, or use your email and password.")
    with db.session() as conn:
        account = accounts.google_account(conn, "responder", claims)
        if not account:
            rid, _ = accounts.create(conn, (claims.get("name") or claims["email"].split("@")[0])[:200],
                                     claims["email"], active=False, approval="pending", google_sub=claims["sub"])
            accounts.log(conn, {"id": rid}, "requested access (Google)", claims["email"])
        elif account["active"]:
            accounts.log(conn, account, "signed in with Google")
    if not account:
        return _login_page(request, notice="Thanks. Your request for responder access has been sent. "
                                           "You can sign in with Google once an admin approves it.")
    if account["approval"] == "pending":
        return _login_page(request, 403, notice=WAITING)
    if not account["active"]:
        return _login_page(request, 401, error="This account isn't active. Ask an admin in your team.")
    return signed_in(request, "responder", account["id"],
                     "/responder/password" if account["must_change_password"] else "/responder")


@app.get("/responder/signup")
def responder_signup_form(request: Request):
    return render(request, "responder_signup.html", error=None, form={}, sent=False)


@app.post("/responder/signup")
async def responder_signup(request: Request):
    """A rescue worker asks for access. The account stays switched off until an admin approves it."""
    check_origin(request)
    form = await request.form()
    name, email = _text(form.get("name"), 200), (_text(form.get("email"), 200) or "").lower()
    organisation = _text(form.get("organisation"), 200)
    keep = {"name": name or "", "email": email, "organisation": organisation or ""}
    error = password_problem(form)
    if not (name and email and "@" in email and organisation):
        error = "Give your name, work email and the organisation you respond with."
    if error:
        return render(request, "responder_signup.html", status_code=400, error=error, form=keep, sent=False)
    with db.session() as conn:
        if not accounts.find(conn, "responder", email):  # existing emails get the same reply, so they can't be probed
            rid, _ = accounts.create(conn, name, email, organisation=organisation, password=str(form.get("password")),
                                     must_change=False, active=False, approval="pending")
            accounts.log(conn, {"id": rid}, "requested access", email)
    return render(request, "responder_signup.html", error=None, form=keep, sent=True)


@app.post("/responder/logout")
def logout(request: Request):
    user = current_user(request)
    with db.session() as conn:
        if user:
            accounts.log(conn, user, "signed out")
        accounts.end_session(conn, request.cookies.get("responder"))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("responder")
    return resp


@app.get("/responder/password")
def password_form(request: Request):
    user = require_responder(request)
    return render(request, "password.html", first=bool(user["must_change_password"]), error=None)


@app.post("/responder/password")
async def change_password(request: Request):
    user = require_responder(request)
    form = await request.form()
    new = str(form.get("new_password", ""))
    error = None
    if not accounts.check_password(str(form.get("current_password", "")), user["password_hash"]):
        error = "Your current password is not right."
    elif len(new) < accounts.MIN_PASSWORD:
        error = f"Choose a password of at least {accounts.MIN_PASSWORD} characters."
    elif new != form.get("confirm_password"):
        error = "The two new passwords don't match."
    elif hmac.compare_digest(new, str(form.get("current_password", ""))):
        error = "Choose a password different from the current one."
    if error:
        return render(request, "password.html", status_code=400, first=bool(user["must_change_password"]),
                      error=error)
    with db.session() as conn:
        accounts.set_password(conn, user["id"], new)
        accounts.end_all_sessions(conn, user["id"], keep=request.cookies.get("responder"))
        accounts.log(conn, user, "changed password")
    return RedirectResponse("/responder", status_code=303)


# --- team (admins) -----------------------------------------------------------

def _team_page(request: Request, status_code: int = 200, **ctx):
    with db.session() as conn:
        team = conn.execute("""SELECT * FROM responders WHERE approval IS NULL
                               ORDER BY active DESC, role = 'admin' DESC, name COLLATE NOCASE""").fetchall()
        requests_ = conn.execute("SELECT * FROM responders WHERE approval = 'pending' ORDER BY id").fetchall()
    ctx.setdefault("error", None)
    ctx.setdefault("issued", None)
    return render(request, "team.html", status_code=status_code, team=team, access_requests=requests_,
                  roles=accounts.ROLES, **ctx)


@app.get("/responder/team")
def team(request: Request):
    require_admin(request)
    return _team_page(request)


@app.post("/responder/team")
async def add_member(request: Request):
    admin = require_admin(request)
    form = await request.form()
    name, email = _text(form.get("name"), 200), _text(form.get("email"), 200)
    role = _choice(form.get("role"), accounts.ROLES) or "responder"
    if not (name and email and "@" in email):
        return _team_page(request, 400, error="Give a name and a valid email address.")
    try:
        with db.session() as conn:
            _, password = accounts.create(conn, name, email, role, _text(form.get("organisation"), 200))
            accounts.log(conn, admin, f"added {role}", email.lower())
    except sqlite3.IntegrityError:
        return _team_page(request, 400, error=f"{email} already has an account.")
    # Shown once, on this page only: it's never put in a URL or stored in clear.
    return _team_page(request, issued={"name": name, "email": email.lower(), "password": password, "new": True})


@app.post("/responder/team/{rid}")
async def update_member(request: Request, rid: int):
    admin = require_admin(request)
    action = _choice((await request.form()).get("action"),
                     ("approve", "reject", "deactivate", "activate", "reset", "make-admin", "make-responder"))
    if not action:
        raise HTTPException(400)
    if rid == admin["id"]:  # so there is always at least one active admin
        return _team_page(request, 400, error="You can't change your own account here. "
                                              "Use Change password, or ask another admin.")
    issued = None
    with db.session() as conn:
        member = conn.execute("SELECT * FROM responders WHERE id=?", (rid,)).fetchone()
        if not member:
            raise HTTPException(404)
        if (action in ("approve", "reject")) != (member["approval"] == "pending"):
            raise HTTPException(400, "Only access requests can be approved or rejected.")
        if action == "approve":
            conn.execute("UPDATE responders SET active=1, approval=NULL WHERE id=?", (rid,))
        elif action == "reject":
            conn.execute("UPDATE responders SET active=0, approval='rejected' WHERE id=?", (rid,))
        elif action in ("deactivate", "activate"):
            conn.execute("UPDATE responders SET active=? WHERE id=?", (int(action == "activate"), rid))
            if action == "deactivate":
                accounts.end_all_sessions(conn, rid)
        elif action == "reset":
            password = accounts.temp_password()
            accounts.set_password(conn, rid, password, must_change=True)
            accounts.end_all_sessions(conn, rid)
            issued = {"name": member["name"], "email": member["email"], "password": password, "new": False}
        else:
            conn.execute("UPDATE responders SET role=? WHERE id=?", (action.removeprefix("make-"), rid))
        accounts.log(conn, admin, {"approve": "approved access request", "reject": "rejected access request",
                                   "deactivate": "deactivated account", "activate": "reactivated account",
                                   "reset": "reset password", "make-admin": "made admin",
                                   "make-responder": "made responder"}[action], member["email"])
    if issued:
        return _team_page(request, issued=issued)
    return RedirectResponse("/responder/team", status_code=303)


@app.get("/responder/activity")
def activity(request: Request):
    require_admin(request)
    with db.session() as conn:
        entries = accounts.recent_activity(conn)
    return render(request, "activity.html", entries=entries)


# --- family accounts (optional) -----------------------------------------------
#
# Private links keep working without an account. An account just keeps a family's cases and
# check-ins in one place: reports filed while signed in are added automatically, and older ones
# can be added from their private page.

def current_family(request: Request):
    if not hasattr(request.state, "family"):
        with db.session() as conn:
            request.state.family = accounts.from_session(conn, request.cookies.get("family"), "family")
    return request.state.family


def _family_auth_page(request: Request, page: str, status_code: int = 200, **ctx):
    ctx.setdefault("error", None)
    ctx.setdefault("form", {})
    ctx["next"] = _local_path(request.query_params.get("next") or ctx.get("next"))
    return render(request, page, status_code=status_code, **ctx)


def _local_path(value) -> str:
    """Where to go after signing in: a path on this site only."""
    value = str(value or "")
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else "/account"


@app.get("/account/signup")
def family_signup_form(request: Request):
    return _family_auth_page(request, "account_signup.html")


@app.post("/account/signup")
async def family_signup(request: Request):
    check_origin(request)
    form = await request.form()
    name, email = _text(form.get("name"), 200), (_text(form.get("email"), 200) or "").lower()
    keep, nxt = {"name": name or "", "email": email}, form.get("next")
    error = password_problem(form)
    if not (name and email and "@" in email):
        error = "Give your name and a valid email address."
    if error:
        return _family_auth_page(request, "account_signup.html", 400, error=error, form=keep, next=nxt)
    try:
        with db.session() as conn:
            fid = accounts.create_family(conn, name, email, str(form.get("password")))
    except sqlite3.IntegrityError:
        return _family_auth_page(request, "account_signup.html", 400, form=keep, next=nxt,
                                 error="There's already an account with this email. Sign in instead.")
    return signed_in(request, "family", fid, _local_path(nxt))


@app.get("/account/login")
def family_login_form(request: Request):
    return _family_auth_page(request, "account_login.html")


@app.post("/account/login")
async def family_login(request: Request):
    check_origin(request)
    form = await request.form()
    email, password, nxt = str(form.get("email", "")).strip().lower()[:200], str(form.get("password", "")), form.get("next")
    ip = limits.client_ip(request.scope)
    with db.session() as conn:
        if accounts.locked_out(conn, email, ip, "family"):
            return _family_auth_page(request, "account_login.html", 429, error=TOO_MANY_TRIES,
                                     form={"email": email}, next=nxt)
        family = accounts.authenticate(conn, email, password, "family")
        if not family:
            accounts.record_failure(conn, "family", email, ip)
    if not family:
        return _family_auth_page(request, "account_login.html", 401, form={"email": email}, next=nxt,
                                 error="That email and password don't match an account.")
    return signed_in(request, "family", family["id"], _local_path(nxt))


@app.post("/account/google")
async def family_google(request: Request):
    """'Continue with Google' for families: signs in, or creates the account on first use."""
    check_origin(request)
    form = await request.form()
    claims = accounts.verify_google(str(form.get("credential", "")))
    if not claims:
        return _family_auth_page(request, "account_login.html", 401, next=form.get("next"),
                                 error="Google sign-in didn't work. Try again, or use your email and password.")
    with db.session() as conn:
        family = accounts.google_account(conn, "family", claims)
        fid = family["id"] if family else accounts.create_family(
            conn, (claims.get("name") or claims["email"].split("@")[0])[:200], claims["email"], google_sub=claims["sub"])
    if family and not family["active"]:
        return _family_auth_page(request, "account_login.html", 401, error="This account has been closed.")
    return signed_in(request, "family", fid, _local_path(form.get("next")))


@app.post("/account/logout")
def family_logout(request: Request):
    with db.session() as conn:
        accounts.end_session(conn, request.cookies.get("family"), "family")
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("family")
    return resp


@app.get("/account")
def family_account(request: Request):
    family = current_family(request)
    if not family:
        return RedirectResponse("/account/login", status_code=303)
    with db.session() as conn:
        cases = conn.execute("""
            SELECT m.ref, m.access_token, m.full_name, m.created_at, m.appeal_public, m.appeal_review, d.name AS disaster,
                   (SELECT COUNT(*) FROM tips t WHERE t.missing_id = m.id AND t.status != 'hidden') AS tips,
                   (SELECT COUNT(*) FROM verification_requests v WHERE v.missing_id = m.id) AS requests
            FROM missing_reports m JOIN disasters d ON d.id = m.disaster_id
            WHERE m.family_id = ? ORDER BY m.id DESC""", (family["id"],)).fetchall()
        checkins = conn.execute("""
            SELECT c.token, c.status, c.updated_at, d.name AS disaster,
                   (SELECT group_concat(full_name, ', ') FROM checkin_people p WHERE p.checkin_id = c.id) AS people
            FROM checkins c JOIN disasters d ON d.id = c.disaster_id
            WHERE c.family_id = ? AND c.status != 'removed' ORDER BY c.id DESC""", (family["id"],)).fetchall()
    return render(request, "account.html", cases=cases, checkins=checkins)


def _save_to_account(request: Request, table: str, key_column: str, key: str, back: str):
    """Add a case or check-in, opened with its private link, to the signed-in family's account."""
    family = current_family(request)
    if not family:
        return RedirectResponse(f"/account/login?next={quote(back, safe='/')}", status_code=303)
    with db.session() as conn:
        row = conn.execute(f"SELECT id, family_id FROM {table} WHERE {key_column}=?", (key,)).fetchone()
        if row["family_id"] not in (None, family["id"]):
            raise HTTPException(403, "This is already saved to another family account.")
        conn.execute(f"UPDATE {table} SET family_id=? WHERE id=?", (family["id"], row["id"]))
    return RedirectResponse(back, status_code=303)


@app.post("/case/{ref}/save")
async def save_case(request: Request, ref: str):
    k = str((await request.form()).get("k", ""))
    with db.session() as conn:
        _load_case(conn, ref, k)  # needs the private link
    return _save_to_account(request, "missing_reports", "ref", ref, f"/case/{ref}?k={k}")


@app.post("/safe/c/{token}/save")
def save_checkin(request: Request, token: str):
    with db.session() as conn:
        _load_checkin(conn, token)
    return _save_to_account(request, "checkins", "token", token, f"/safe/c/{token}")


# --- families ----------------------------------------------------------------

ZONE_PAGE = 20


def zone_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM disasters WHERE status = 'active'").fetchone()[0]


@app.get("/")
def home(request: Request):
    # The map and the full zone list load /zones.json in the browser; the page itself only
    # lists the newest few, so it stays small and works without JavaScript.
    with db.session() as conn:
        recent = conn.execute("SELECT * FROM disasters WHERE status = 'active' ORDER BY id DESC LIMIT 8").fetchall()
        return render(request, "index.html", recent_zones=recent, zone_total=zone_count(conn),
                      appeals=public_appeals(conn, limit=6))


@app.get("/zones.json")
def zones_json():
    """Every active zone with a location, as compact rows: [id, name, region, lat, lng, radius_km, since]."""
    with db.session() as conn:
        rows = conn.execute("""SELECT id, name, region, lat, lng, radius_km, COALESCE(started_on, substr(created_at, 1, 10))
                               FROM disasters WHERE status = 'active' AND lat IS NOT NULL ORDER BY id DESC""").fetchall()
    return JSONResponse({"zones": [list(r) for r in rows]}, headers={"Cache-Control": "public, max-age=60"})


@app.get("/zones")
def zones_page(request: Request, q: str = "", page: int = 1):
    """All active zones, searchable and paged, for browsing without the map."""
    q, page = q.strip()[:100], max(1, page)
    where, args = "status = 'active'", []
    for word in q.split()[:6]:
        where += " AND (name LIKE ? ESCAPE '\\' OR region LIKE ? ESCAPE '\\')"
        like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [like, like]
    with db.session() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM disasters WHERE {where}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM disasters WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                            args + [ZONE_PAGE, (page - 1) * ZONE_PAGE]).fetchall()
    return render(request, "zones_page.html", zones=rows, q=q, page=page, total=total,
                  pages=max(1, -(-total // ZONE_PAGE)))


@app.get("/missing/new")
def missing_form(request: Request, disaster: int = 0, proposed: int = 0):
    with db.session() as conn:
        return render(request, "missing_form.html", disasters=disasters(conn, include=disaster),
                      selected=disaster, proposed=proposed)


@app.get("/zones/new")
def propose_zone_form(request: Request):
    return render(request, "disaster_form.html", public=True)


@app.post("/zones")
async def propose_zone(request: Request):
    form = await request.form()
    zone = parse_zone(form)
    with db.session() as conn:
        zid = insert_zone(conn, zone, "pending", "public", _text(form.get("note"), 1000))
    return RedirectResponse(f"/missing/new?disaster={zid}&proposed=1", status_code=303)


@app.post("/missing")
async def create_missing(request: Request, background: BackgroundTasks):
    form = await request.form()
    full_name = _text(form.get("full_name"), 200)
    reporter_name = _text(form.get("reporter_name"), 200)
    reporter_contact = _text(form.get("reporter_contact"), 200)
    if not (full_name and reporter_name and reporter_contact):
        raise HTTPException(400, "Name of the missing person and your contact details are required.")
    check_photo_count(form)

    ref, token = new_ref("MP"), secrets.token_urlsafe(24)
    with db.session() as conn:
        cur = conn.execute(
            """INSERT INTO missing_reports (ref, access_token, disaster_id, full_name, sex, age,
                   height_cm, hair_color, last_seen_place, last_seen_date, clothing, description,
                   reporter_name, reporter_contact, person_phone)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, token, disaster_id(conn, form), full_name,
             _choice(form.get("sex"), ("male", "female", "unknown")),
             _int(form.get("age"), 0, 120), _int(form.get("height_cm"), 40, 250),
             _choice(form.get("hair_color"), HAIR_COLORS),
             _text(form.get("last_seen_place"), 300), _date(form.get("last_seen_date")),
             _text(form.get("clothing")), _text(form.get("description")),
             reporter_name, reporter_contact, _text(form.get("person_phone"), 40)))
        await save_features(conn, parse_features(form), "missing", cur.lastrowid)
        await save_photos(conn, form, "missing", cur.lastrowid)
        if family := current_family(request):
            conn.execute("UPDATE missing_reports SET family_id=? WHERE id=?", (family["id"], cur.lastrowid))
    background.add_task(ai_match.run_for, "missing", cur.lastrowid)  # no-op without an API key
    return RedirectResponse(f"/case/{ref}?k={token}&new=1", status_code=303)


def _load_case(conn, ref: str, k: str):
    row = conn.execute("SELECT * FROM missing_reports WHERE ref=?", (ref,)).fetchone()
    if not row or not hmac.compare_digest(row["access_token"], k or ""):
        raise HTTPException(404, "Case not found. Check the private link you were given.")
    return row


@app.get("/case/{ref}/photos/{pid}")
def family_own_photo(ref: str, pid: int, k: str = ""):
    """A family's own uploaded photo, so they can pick one for their public appeal."""
    with db.session() as conn:
        row = _load_case(conn, ref, k)
        photo = conn.execute("SELECT filename FROM photos WHERE id=? AND owner_type='missing' AND owner_id=?",
                             (pid, row["id"])).fetchone()
    if not photo:
        raise HTTPException(404)
    return FileResponse(db.UPLOAD_DIR / photo["filename"], headers={"Cache-Control": "private, no-store"})


@app.get("/case/{ref}")
def case(request: Request, ref: str, k: str = "", new: int = 0):
    with db.session() as conn:
        row = _load_case(conn, ref, k)
        profile = db.missing_profile(conn, row)
        requests_ = {r["found_id"]: r["status"] for r in conn.execute(
            "SELECT found_id, status FROM verification_requests WHERE missing_id=?", (row["id"],))}
        matches = []
        for m in rank_found(profile, db.found_profiles(conn, row["disaster_id"])):
            status = requests_.get(m["record"]["id"])
            if status == "rejected":
                continue  # responders ruled it out — don't keep showing it
            rec = m["record"]
            # Only what a family may see: no status, facility, photos or contacts.
            matches.append({"ref": rec["ref"], "found_place": rec["found_place"],
                            "found_date": rec["found_date"], "score": m["score"],
                            "reasons": m["reasons"], "request_status": status})
        disaster = conn.execute("SELECT * FROM disasters WHERE id=?", (row["disaster_id"],)).fetchone()
        own_photos = [r[0] for r in conn.execute(
            "SELECT id FROM photos WHERE owner_type='missing' AND owner_id=? ORDER BY feature_id IS NOT NULL, id",
            (row["id"],))]
        return render(request, "case.html", case=profile, matches=matches, k=k, new=new,
                      disaster=disaster, photos=len(own_photos), own_photos=own_photos,
                      appeal_public=bool(row["appeal_public"]), appeal_photo_id=row["appeal_photo_id"],
                      appeal_review=row["appeal_review"],
                      safe_checkins=safe.find(conn, row["full_name"], row["person_phone"]) if row["person_phone"] else [],
                      tips=tips_for(conn, row["id"], include_hidden=False))


@app.post("/case/{ref}/verify/{found_ref}")
async def request_verification(request: Request, ref: str, found_ref: str):
    k = str((await request.form()).get("k", ""))
    with db.session() as conn:
        row = _load_case(conn, ref, k)
        found = conn.execute("SELECT * FROM found_records WHERE ref=? AND disaster_id=?",
                             (found_ref, row["disaster_id"])).fetchone()
        if not found:
            raise HTTPException(404)
        # Re-score server side so the stored score can't be tampered with.
        match = next((m for m in rank_found(db.missing_profile(conn, row),
                                            [db.found_profile(conn, found)])), None)
        if not match:
            raise HTTPException(400, "This record is no longer a suggested match.")
        conn.execute("""INSERT OR IGNORE INTO verification_requests (missing_id, found_id, score)
                        VALUES (?, ?, ?)""", (row["id"], found["id"], match["score"]))
    return RedirectResponse(f"/case/{ref}?k={k}", status_code=303)


# --- public appeals and community tips ---------------------------------------
#
# A family can publish a public appeal for their missing person: name, description and one
# photo they choose. Anyone can send a sighting or tip from it. The family sees tips straight
# away, marked unverified; responders can verify or hide them. Tipsters never see the
# family's details, and their own contact is shown to responders only.

TIP_CONDITIONS = {"well": "Seemed well", "needs_help": "Injured or needed help", "unsure": "Not sure"}
MAX_TIP_PHOTOS = limits.MAX_TIP_PHOTOS
templates.env.globals["tip_conditions"] = TIP_CONDITIONS


def _load_appeal(conn, ref: str):
    row = conn.execute("SELECT * FROM missing_reports WHERE ref=? AND appeal_public=1 AND appeal_review='approved'",
                       (ref,)).fetchone()
    if not row:
        raise HTTPException(404, "This appeal isn't available. The family may have taken it down.")
    return row


def tips_for(conn, missing_id: int, include_hidden: bool) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM tips WHERE missing_id=? ORDER BY id DESC", (missing_id,)):
        if r["status"] == "hidden" and not include_hidden:
            continue
        tip = dict(r)
        tip["photo_ids"] = [p[0] for p in conn.execute("SELECT id FROM tip_photos WHERE tip_id=? ORDER BY id", (r["id"],))]
        out.append(tip)
    return out


def public_appeals(conn, disaster: int = 0, limit: int = 60) -> list[dict]:
    rows = conn.execute("""SELECT m.ref, m.full_name, m.sex, m.age, m.last_seen_place, m.last_seen_date,
                                  m.appeal_photo_id, d.name AS disaster
                           FROM missing_reports m JOIN disasters d ON d.id = m.disaster_id
                           WHERE m.appeal_public = 1 AND m.appeal_review = 'approved' AND (? = 0 OR m.disaster_id = ?)
                           ORDER BY m.id DESC LIMIT ?""", (disaster, disaster, limit))
    return [dict(r) for r in rows]


@app.post("/case/{ref}/appeal")
async def update_appeal(request: Request, ref: str):
    form = await request.form()
    k = str(form.get("k", ""))
    with db.session() as conn:
        row = _load_case(conn, ref, k)
        publish = form.get("publish") == "1"
        photo_id = _int(form.get("photo_id"), 1, 10**9)
        if photo_id and not conn.execute("SELECT 1 FROM photos WHERE id=? AND owner_type='missing' AND owner_id=?",
                                          (photo_id, row["id"])).fetchone():
            photo_id = None  # only one of this report's own photos can be made public
        review = row["appeal_review"]
        if publish and (review != "approved" or photo_id != row["appeal_photo_id"]):
            review = "pending"  # a responder checks every new appeal and every new public photo
        conn.execute("UPDATE missing_reports SET appeal_public=?, appeal_photo_id=?, appeal_review=? WHERE id=?",
                     (1 if publish else 0, photo_id if publish else None, review, row["id"]))
    return RedirectResponse(f"/case/{ref}?k={k}#appeal", status_code=303)


@app.post("/case/{ref}/tips/{tid}/hide")
async def family_hide_tip(request: Request, ref: str, tid: int):
    k = str((await request.form()).get("k", ""))
    with db.session() as conn:
        row = _load_case(conn, ref, k)
        conn.execute("UPDATE tips SET status='hidden', hidden_by='family' WHERE id=? AND missing_id=?", (tid, row["id"]))
    return RedirectResponse(f"/case/{ref}?k={k}#tips", status_code=303)


@app.get("/case/{ref}/tips/photos/{tpid}")
def family_tip_photo(ref: str, tpid: int, k: str = ""):
    with db.session() as conn:
        row = _load_case(conn, ref, k)
        photo = conn.execute("""SELECT p.filename FROM tip_photos p JOIN tips t ON t.id = p.tip_id
                                WHERE p.id=? AND t.missing_id=? AND t.status != 'hidden'""", (tpid, row["id"])).fetchone()
    if not photo:
        raise HTTPException(404)
    return FileResponse(db.UPLOAD_DIR / photo["filename"], headers={"Cache-Control": "private, no-store"})


@app.get("/appeals")
def appeals(request: Request, disaster: int = 0):
    with db.session() as conn:
        return render(request, "appeals.html", appeals=public_appeals(conn, disaster), zones=disasters(conn),
                      selected=disaster)


@app.get("/appeal/{ref}")
def appeal(request: Request, ref: str, sent: int = 0):
    with db.session() as conn:
        row = _load_appeal(conn, ref)
        profile = db.missing_profile(conn, row)
        disaster = conn.execute("SELECT * FROM disasters WHERE id=?", (row["disaster_id"],)).fetchone()
        zone = [dict(disaster)] if disaster["lat"] is not None else []
    return render(request, "appeal.html", person=profile, disaster=disaster, zones=zone, sent=sent)


@app.get("/appeal/{ref}/photo")
def appeal_photo(ref: str):
    with db.session() as conn:
        row = _load_appeal(conn, ref)
        photo = conn.execute("SELECT filename FROM photos WHERE id=? AND owner_type='missing' AND owner_id=?",
                             (row["appeal_photo_id"], row["id"])).fetchone()
    if not photo:
        raise HTTPException(404)
    return FileResponse(db.UPLOAD_DIR / photo["filename"], headers={"Cache-Control": "public, max-age=300"})


@app.post("/appeal/{ref}/tips")
async def create_tip(request: Request, ref: str):
    form = await request.form()
    if form.get("website"):  # honeypot: hidden from people, filled in by bots
        return RedirectResponse(f"/appeal/{ref}?sent=1", status_code=303)
    message = _text(form.get("message"), 2000)
    if not message:
        raise HTTPException(400, "Please describe what you saw.")
    photos = [u for u in map(_upload, form.getlist("photos")) if u]
    if len(photos) > MAX_TIP_PHOTOS:
        raise HTTPException(400, f"Please send at most {MAX_TIP_PHOTOS} photos.")
    seen_at = _text(form.get("seen_at"), 16)
    if seen_at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?", seen_at):
        seen_at = None
    with db.session() as conn:
        row = _load_appeal(conn, ref)
        filenames = [await save_upload(u) for u in photos]  # validate every photo before saving the tip
        cur = conn.execute(
            """INSERT INTO tips (missing_id, seen_place, seen_lat, seen_lng, seen_at, condition, message, contact)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], _text(form.get("seen_place"), 300), _float(form.get("lat"), -90, 90),
             _float(form.get("lng"), -180, 180), seen_at, _choice(form.get("condition"), TIP_CONDITIONS),
             message, _text(form.get("contact"), 200)))
        conn.executemany("INSERT INTO tip_photos (tip_id, filename) VALUES (?, ?)",
                         [(cur.lastrowid, f) for f in filenames])
    return RedirectResponse(f"/appeal/{ref}?sent=1", status_code=303)


# --- "I'm safe" check-ins ------------------------------------------------------
#
# People in a disaster zone mark themselves (and people with them) safe. Relatives find them
# with full name + phone number only; see app/safe.py for why. Searches are POSTed so phone
# numbers stay out of URLs, browser history and server logs.

MAX_COMPANIONS = 12


def _parse_people(form) -> list[dict]:
    """The person checking in, then the people with them. Names without a phone are fine (children)."""
    people = []
    rows = [(form.get("name"), form.get("phone"), True)] + [
        (n, p, False) for n, p in zip(form.getlist("other_name"), form.getlist("other_phone"))]
    for name, phone, me in rows:
        name, phone = _text(name, 200), _text(phone, 40)
        if name and safe.name_key(name):
            people.append({"full_name": name, "name_key": safe.name_key(name), "phone": phone,
                           "phone_key": safe.phone_key(phone), "is_reporter": int(me)})
    return people


def _load_checkin(conn, token: str):
    row = conn.execute("SELECT * FROM checkins WHERE token=? AND status != 'removed'", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "Check-in not found. Check the private link you were given.")
    return row


@app.get("/safe")
def safe_home(request: Request, disaster: int = 0):
    with db.session() as conn:
        return render(request, "safe.html", zones=disasters(conn), selected=disaster, results=None, query={})


@app.get("/safe/new")
def safe_form(request: Request, disaster: int = 0):
    with db.session() as conn:
        return render(request, "safe_form.html", disasters=disasters(conn), selected=disaster)


@app.post("/safe")
async def create_checkin(request: Request):
    form = await request.form()
    if form.get("website"):  # honeypot for bots
        return RedirectResponse("/safe", status_code=303)
    people = _parse_people(form)
    if not people or not people[0]["is_reporter"]:
        raise HTTPException(400, "Please enter your full name.")
    if not people[0]["phone_key"]:
        raise HTTPException(400, "Please enter a phone number your family knows, so they can find you.")
    if len(people) > MAX_COMPANIONS + 1:
        raise HTTPException(400, f"You can list up to {MAX_COMPANIONS} people with you.")
    token = secrets.token_urlsafe(24)
    with db.session() as conn:
        cur = conn.execute("INSERT INTO checkins (token, disaster_id, message, place, share_place) VALUES (?, ?, ?, ?, ?)",
                           (token, disaster_id(conn, form), _text(form.get("message"), 500),
                            _text(form.get("place"), 200), 1 if form.get("share_place") else 0))
        conn.executemany("""INSERT INTO checkin_people (checkin_id, full_name, name_key, phone, phone_key, is_reporter)
                            VALUES (:checkin_id, :full_name, :name_key, :phone, :phone_key, :is_reporter)""",
                         [{**person, "checkin_id": cur.lastrowid} for person in people])
        if family := current_family(request):
            conn.execute("UPDATE checkins SET family_id=? WHERE id=?", (family["id"], cur.lastrowid))
    return RedirectResponse(f"/safe/c/{token}?new=1", status_code=303)


@app.get("/safe/c/{token}")
def manage_checkin(request: Request, token: str, new: int = 0):
    with db.session() as conn:
        row = _load_checkin(conn, token)
        people = conn.execute("SELECT * FROM checkin_people WHERE checkin_id=? ORDER BY is_reporter DESC, id",
                              (row["id"],)).fetchall()
        disaster = conn.execute("SELECT * FROM disasters WHERE id=?", (row["disaster_id"],)).fetchone()
    return render(request, "safe_manage.html", checkin=row, people=people, disaster=disaster, new=new)


@app.post("/safe/c/{token}")
async def update_checkin(request: Request, token: str):
    form = await request.form()
    action = _choice(form.get("action"), ("update", "withdraw", "restore"))
    with db.session() as conn:
        row = _load_checkin(conn, token)
        if action == "update":
            conn.execute("""UPDATE checkins SET message=?, place=?, share_place=?, updated_at=datetime('now')
                            WHERE id=?""", (_text(form.get("message"), 500), _text(form.get("place"), 200),
                                           1 if form.get("share_place") else 0, row["id"]))
        elif action in ("withdraw", "restore"):
            conn.execute("UPDATE checkins SET status=?, updated_at=datetime('now') WHERE id=?",
                         ("withdrawn" if action == "withdraw" else "active", row["id"]))
    return RedirectResponse(f"/safe/c/{token}", status_code=303)


@app.post("/safe/find")
async def find_safe(request: Request):
    form = await request.form()
    query = {"name": _text(form.get("name"), 200) or "", "phone": _text(form.get("phone"), 40) or ""}
    if not query["name"] or not safe.phone_key(query["phone"]):
        raise HTTPException(400, "Enter the person's full name and their phone number.")
    with db.session() as conn:
        results = safe.find(conn, query["name"], query["phone"])
        return render(request, "safe.html", zones=disasters(conn), selected=0, results=results, query=query)


# --- responders --------------------------------------------------------------

@app.get("/responder")
def dashboard(request: Request):
    require_responder(request)
    with db.session() as conn:
        queue = conn.execute("""
            SELECT v.*, m.ref AS missing_ref, m.full_name, m.reporter_name, m.reporter_contact,
                   f.ref AS found_ref, f.found_place, f.facility, f.status AS found_status
            FROM verification_requests v
            JOIN missing_reports m ON m.id = v.missing_id
            JOIN found_records f ON f.id = v.found_id
            ORDER BY v.status = 'open' DESC, v.score DESC""").fetchall()
        found = conn.execute("""SELECT f.*, d.name AS disaster FROM found_records f
                                JOIN disasters d ON d.id = f.disaster_id
                                ORDER BY f.id DESC LIMIT 50""").fetchall()
        missing = conn.execute("""SELECT m.*, d.name AS disaster FROM missing_reports m
                                  JOIN disasters d ON d.id = m.disaster_id
                                  ORDER BY m.id DESC LIMIT 50""").fetchall()
        pending_zones = conn.execute("SELECT COUNT(*) FROM disasters WHERE status='pending'").fetchone()[0]
        ai_flags = conn.execute("""
            SELECT a.score, a.verdict, a.summary, a.stage, a.shortlist_score, a.shortlist_reason, m.ref AS missing_ref, m.full_name, f.ref AS found_ref, f.status
            FROM ai_matches a JOIN missing_reports m ON m.id = a.missing_id JOIN found_records f ON f.id = a.found_id
            WHERE a.verdict IN ('strong', 'possible') OR (a.stage = 'shortlisted' AND a.shortlist_score >= 60)
            ORDER BY a.verdict = 'strong' DESC, COALESCE(a.score, a.shortlist_score) DESC LIMIT 10""").fetchall()
        safe_count = conn.execute("""SELECT COUNT(*) FROM checkin_people p JOIN checkins c ON c.id = p.checkin_id
                                      WHERE c.status = 'active'""").fetchone()[0]
        new_tips = conn.execute("""
            SELECT t.id, t.seen_place, t.seen_at, t.message, t.condition, t.created_at,
                   m.ref AS missing_ref, m.full_name,
                   (SELECT COUNT(*) FROM tip_photos p WHERE p.tip_id = t.id) AS photos
            FROM tips t JOIN missing_reports m ON m.id = t.missing_id
            WHERE t.status = 'unverified' ORDER BY t.id DESC LIMIT 10""").fetchall()
        pending_appeals = conn.execute("""
            SELECT m.ref, m.full_name, m.appeal_photo_id, m.reporter_name, m.reporter_contact, d.name AS disaster
            FROM missing_reports m JOIN disasters d ON d.id = m.disaster_id
            WHERE m.appeal_public = 1 AND m.appeal_review = 'pending' ORDER BY m.id""").fetchall()
        return render(request, "dashboard.html", queue=queue, found=found, missing=missing,
                      pending_zones=pending_zones, ai_flags=ai_flags, ai_enabled=ai_match.enabled(),
                      new_tips=new_tips, safe_count=safe_count, pending_appeals=pending_appeals)


@app.post("/responder/appeals/{ref}")
async def review_appeal(request: Request, ref: str):
    user = require_responder(request)
    action = _choice((await request.form()).get("action"), ("approve", "reject"))
    if not action:
        raise HTTPException(400)
    with db.session() as conn:
        updated = conn.execute("""UPDATE missing_reports SET appeal_review=?
                                  WHERE ref=? AND appeal_public=1 AND appeal_review='pending'""",
                               ("approved" if action == "approve" else "rejected", ref)).rowcount
        if not updated:
            raise HTTPException(404, "That appeal is no longer awaiting approval.")
        accounts.log(conn, user, "approved appeal" if action == "approve" else "rejected appeal", ref)
    return RedirectResponse("/responder#appeals", status_code=303)


@app.post("/responder/verifications/{vid}")
async def update_verification(request: Request, vid: int):
    user = require_responder(request)
    status = _choice((await request.form()).get("status"), ("open", "contacted", "confirmed", "rejected"))
    if not status:
        raise HTTPException(400)
    with db.session() as conn:
        pair = conn.execute("""SELECT m.ref AS missing_ref, f.ref AS found_ref FROM verification_requests v
                               JOIN missing_reports m ON m.id = v.missing_id JOIN found_records f ON f.id = v.found_id
                               WHERE v.id=?""", (vid,)).fetchone()
        if not pair:
            raise HTTPException(404)
        conn.execute("UPDATE verification_requests SET status=? WHERE id=?", (status, vid))
        accounts.log(conn, user, f"marked match {status}", f"{pair['missing_ref']} ↔ {pair['found_ref']}")
    return RedirectResponse("/responder", status_code=303)


@app.get("/responder/found/new")
def found_form(request: Request, disaster: int = 0):
    require_responder(request)
    with db.session() as conn:
        return render(request, "found_form.html", disasters=disasters(conn, include=disaster), selected=disaster)


@app.get("/responder/disasters/new")
def disaster_form(request: Request):
    require_responder(request)
    return render(request, "disaster_form.html")


@app.post("/responder/disasters")
async def create_disaster(request: Request):
    user = require_responder(request)
    zone = parse_zone(await request.form())
    with db.session() as conn:
        insert_zone(conn, zone, "active", "responder")
        accounts.log(conn, user, "added zone", zone["name"])
    return RedirectResponse("/#zones", status_code=303)


@app.get("/responder/zones")
def zone_review(request: Request, synced: str = ""):
    require_responder(request)
    with db.session() as conn:
        pending = [dict(r) for r in conn.execute("""
            SELECT d.*, (SELECT COUNT(*) FROM missing_reports m WHERE m.disaster_id = d.id) AS reports
            FROM disasters d WHERE status = 'pending'
            ORDER BY source = 'public' DESC, alert_level = 'Red' DESC, id DESC""")]
        active = [dict(d) for d in disasters(conn)]  # for the merge dropdown
    # The map shows pending zones from the page and active ones (grey) from /zones.json.
    return render(request, "zones_review.html", pending=pending, active=active, synced=synced, zones=pending)


@app.post("/responder/zones/sync")
async def zone_sync(request: Request):
    require_responder(request)
    try:
        added = await asyncio.to_thread(gdacs.sync)
    except Exception as exc:
        log.warning("GDACS sync failed: %s", exc)
        return RedirectResponse("/responder/zones?synced=error", status_code=303)
    return RedirectResponse(f"/responder/zones?synced={added}", status_code=303)


@app.post("/responder/zones/{zid}")
async def zone_decide(request: Request, zid: int):
    user = require_responder(request)
    form = await request.form()
    action = _choice(form.get("action"), ("approve", "reject", "merge"))
    with db.session() as conn:
        zone = conn.execute("SELECT * FROM disasters WHERE id=? AND status='pending'", (zid,)).fetchone()
        if not zone or not action:
            raise HTTPException(404, "That zone is no longer awaiting review.")
        if action == "approve":
            conn.execute("""UPDATE disasters SET status='active', name=?, region=?, radius_km=? WHERE id=?""",
                         (_text(form.get("name"), 200) or zone["name"], _text(form.get("region"), 200) or zone["region"],
                          _float(form.get("radius_km"), 1, 1000) or zone["radius_km"], zid))
        elif action == "merge":
            target = _int(form.get("target"), 1, 10**9)
            if not target or not conn.execute("SELECT 1 FROM disasters WHERE id=? AND status='active'",
                                              (target,)).fetchone():
                raise HTTPException(400, "Choose an active zone to merge into.")
            for table in ("missing_reports", "found_records"):
                conn.execute(f"UPDATE {table} SET disaster_id=? WHERE disaster_id=?", (target, zid))
            conn.execute("UPDATE disasters SET status='merged' WHERE id=?", (zid,))
        else:
            # Families already reported into this zone: their reports must land somewhere.
            if conn.execute("SELECT 1 FROM missing_reports WHERE disaster_id=?", (zid,)).fetchone():
                raise HTTPException(400, "Families have already reported into this zone. Merge it into an active zone instead.")
            conn.execute("UPDATE disasters SET status='rejected' WHERE id=?", (zid,))
        accounts.log(conn, user, {"approve": "approved zone", "reject": "rejected zone",
                                  "merge": "merged zone"}[action], zone["name"])
    return RedirectResponse("/responder/zones", status_code=303)


@app.post("/responder/found")
async def create_found(request: Request, background: BackgroundTasks):
    user = require_responder(request)
    form = await request.form()
    found_place = _text(form.get("found_place"), 300)
    status = _choice(form.get("status"), ("alive", "deceased"))
    if not (found_place and status):
        raise HTTPException(400, "Where the person was found and their status are required.")
    check_photo_count(form)
    age_min, age_max = _int(form.get("age_min"), 0, 120), _int(form.get("age_max"), 0, 120)
    if age_min is not None and age_max is not None and age_min > age_max:
        age_min, age_max = age_max, age_min

    ref = new_ref("FD")
    with db.session() as conn:
        cur = conn.execute(
            """INSERT INTO found_records (ref, disaster_id, status, found_place, found_date, facility,
                   sex, age_min, age_max, height_cm, hair_color, clothing, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, disaster_id(conn, form), status, found_place, _date(form.get("found_date")),
             _text(form.get("facility"), 300),
             _choice(form.get("sex"), ("male", "female", "unknown")), age_min, age_max,
             _int(form.get("height_cm"), 40, 250), _choice(form.get("hair_color"), HAIR_COLORS),
             _text(form.get("clothing")), _text(form.get("description"))))
        await save_features(conn, parse_features(form), "found", cur.lastrowid)
        await save_photos(conn, form, "found", cur.lastrowid)
        accounts.log(conn, user, f"logged found person ({status})", ref)
    background.add_task(ai_match.run_for, "found", cur.lastrowid)  # no-op without an API key
    if status == "deceased":
        background.add_task(_illustrate_record, cur.lastrowid)
    return RedirectResponse(f"/responder/found/{ref}", status_code=303)


def _illustrate_record(found_id: int) -> None:
    with db.session() as conn:
        pids = [r[0] for r in conn.execute("SELECT id FROM photos WHERE owner_type='found' AND owner_id=?", (found_id,))]
    for pid in pids:
        illustrate.ensure(pid)


def _photo_ids(conn, owner_type: str, owner_id: int) -> tuple[dict[int, list[int]], list[int]]:
    """({feature_id: [photo ids]}, [general photo ids]) for one record."""
    by_feature, general = {}, []
    for r in conn.execute("SELECT id, feature_id FROM photos WHERE owner_type=? AND owner_id=? ORDER BY id",
                          (owner_type, owner_id)):
        if r["feature_id"]:
            by_feature.setdefault(r["feature_id"], []).append(r["id"])
        else:
            general.append(r["id"])
    return by_feature, general


@app.get("/responder/found/{ref}")
def found_detail(request: Request, ref: str):
    user = require_responder(request)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM found_records WHERE ref=?", (ref,)).fetchone()
        if not row:
            raise HTTPException(404)
        accounts.log(conn, user, "opened found record", ref)  # lists matching families' contact details
        profile = db.found_profile(conn, row)
        matches = rank_missing(profile, db.missing_profiles(conn, row["disaster_id"]))
        feature_photos, photo_ids = _photo_ids(conn, "found", row["id"])
        ai = _ai_context(conn, "found", row["id"])
        _start_illustrations(request, conn, photo_ids + sum(feature_photos.values(), [])
                             + sum((a["other_photo_ids"] for a in ai["ai_results"]), []))
        return render(request, "record.html", kind="found", record=profile, matches=matches,
                      feature_photos=feature_photos, photo_ids=photo_ids, **ai)


@app.get("/responder/missing/{ref}")
def missing_detail(request: Request, ref: str):
    user = require_responder(request)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM missing_reports WHERE ref=?", (ref,)).fetchone()
        if not row:
            raise HTTPException(404)
        accounts.log(conn, user, "opened missing report", ref)  # the page shows the family's contact details
        profile = db.missing_profile(conn, row)
        matches = rank_found(profile, db.found_profiles(conn, row["disaster_id"]))
        feature_photos, photo_ids = _photo_ids(conn, "missing", row["id"])
        ai = _ai_context(conn, "missing", row["id"])
        ai["tips"] = tips_for(conn, row["id"], include_hidden=True)
        ai["safe_similar"] = safe.similar_names(conn, row["full_name"], row["disaster_id"])
        _start_illustrations(request, conn, photo_ids + sum(feature_photos.values(), [])
                             + sum((a["other_photo_ids"] for a in ai["ai_results"]), []))
        return render(request, "record.html", kind="missing", record=profile, matches=matches,
                      feature_photos=feature_photos, photo_ids=photo_ids, **ai)


def _start_illustrations(request: Request, conn, photo_ids) -> None:
    """Start illustrations for the deceased people's photos this page will show.

    Done here rather than when each image is requested: lazy-loaded thumbnails further down
    the page aren't requested until scrolled to.
    """
    if photo_view(request) != "illustration" or not illustrate.enabled():
        return
    for pid in set(photo_ids):
        row = illustrate.is_sensitive(conn, pid)
        if row and not illustrate.ready(row["filename"]):
            illustrate.ensure_in_background(pid)


def _ai_context(conn, kind: str, record_id: int) -> dict:
    results, screened_out = ai_match.results_for(conn, kind, record_id)
    return {"ai_enabled": ai_match.enabled(), "ai_model": ai_match.MODEL,
            "ai_running": (kind, record_id) in ai_match.RUNNING,
            "ai_results": results, "ai_screened_out": screened_out}


@app.post("/responder/{kind}/{ref}/ai-check")
def rerun_ai_check(request: Request, kind: str, ref: str, background: BackgroundTasks):
    require_responder(request)
    table = {"missing": "missing_reports", "found": "found_records"}.get(kind)
    if not table or not ai_match.enabled():
        raise HTTPException(404)
    with db.session() as conn:
        row = conn.execute(f"SELECT id FROM {table} WHERE ref=?", (ref,)).fetchone()
    if not row:
        raise HTTPException(404)
    ai_match.RUNNING.add((kind, row["id"]))  # show "checking…" straight away
    background.add_task(_rerun, kind, row["id"])
    return RedirectResponse(f"/responder/{kind}/{ref}#ai", status_code=303)


def _rerun(kind: str, record_id: int) -> None:
    ai_match.RUNNING.discard((kind, record_id))  # run_for skips records already marked running
    ai_match.run_for(kind, record_id, force=True)


@app.get("/responder/safe")
def responder_checkins(request: Request, disaster: int = 0, q: str = ""):
    user = require_responder(request)
    with db.session() as conn:
        accounts.log(conn, user, "opened safe check-ins", f"search: {q[:100]}" if q else None)  # lists phone numbers
        rows = conn.execute("""
            SELECT c.*, d.name AS disaster,
                   (SELECT group_concat(full_name || COALESCE(' (' || phone || ')', ''), ', ')
                    FROM checkin_people p WHERE p.checkin_id = c.id) AS people
            FROM checkins c JOIN disasters d ON d.id = c.disaster_id
            WHERE c.status != 'removed' AND (? = 0 OR c.disaster_id = ?)
            ORDER BY c.updated_at DESC LIMIT 200""", (disaster, disaster)).fetchall()
        if q:
            key = safe.name_key(q)
            keep = {r["checkin_id"] for r in conn.execute("SELECT checkin_id, name_key FROM checkin_people")
                    if safe.names_match(key, r["name_key"]) or set(key.split()) & set(r["name_key"].split())}
            rows = [r for r in rows if r["id"] in keep]
        return render(request, "responder_safe.html", checkins=rows, zones=disasters(conn), selected=disaster, q=q)


@app.post("/responder/safe/{cid}/remove")
def remove_checkin(request: Request, cid: int):
    user = require_responder(request)
    with db.session() as conn:
        conn.execute("UPDATE checkins SET status='removed', updated_at=datetime('now') WHERE id=?", (cid,))
        accounts.log(conn, user, "removed safe check-in", f"check-in #{cid}")
    return RedirectResponse("/responder/safe", status_code=303)


@app.post("/responder/tips/{tid}")
async def moderate_tip(request: Request, tid: int):
    user = require_responder(request)
    action = _choice((await request.form()).get("action"), ("verify", "hide", "unhide"))
    status = {"verify": "verified", "hide": "hidden", "unhide": "unverified"}.get(action)
    if not status:
        raise HTTPException(400)
    with db.session() as conn:
        tip = conn.execute("""SELECT m.ref FROM tips t JOIN missing_reports m ON m.id = t.missing_id
                              WHERE t.id=?""", (tid,)).fetchone()
        if not tip:
            raise HTTPException(404)
        conn.execute("UPDATE tips SET status=?, hidden_by=? WHERE id=?",
                     (status, "responder" if status == "hidden" else None, tid))
        accounts.log(conn, user, {"verify": "verified tip", "hide": "hid tip", "unhide": "unhid tip"}[action],
                     f"tip #{tid} on {tip['ref']}")
    return RedirectResponse(f"/responder/missing/{tip['ref']}#tips", status_code=303)


@app.get("/responder/tips/photos/{tpid}")
def responder_tip_photo(request: Request, tpid: int):
    require_responder(request)
    with db.session() as conn:
        photo = conn.execute("SELECT filename FROM tip_photos WHERE id=?", (tpid,)).fetchone()
    if not photo:
        raise HTTPException(404)
    return FileResponse(db.UPLOAD_DIR / photo["filename"], headers={"Cache-Control": "private, no-store"})


@app.get("/responder/photos/{pid}")
def photo(request: Request, pid: int, view: str = "", confirm: int = 0):
    user = require_responder(request)
    with db.session() as conn:
        row = conn.execute("SELECT filename FROM photos WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404)
        sensitive = illustrate.is_sensitive(conn, pid) is not None
    headers = {"Cache-Control": "private, no-store"}
    original = db.UPLOAD_DIR / row["filename"]
    if not sensitive:
        return FileResponse(original, headers=headers)

    view = view or photo_view(request)
    if view == "original":
        if not confirm:  # e.g. a copied link: warn before showing it
            return render(request, "graphic_warning.html", pid=pid, next=None)
        with db.session() as conn:
            accounts.log(conn, user, "viewed unfiltered photo", f"photo #{pid}")
        return FileResponse(original, headers=headers)
    if view == "illustration" and illustrate.enabled():
        if illustrate.ready(row["filename"]):
            return FileResponse(illustrate.path_for(row["filename"]), headers=headers)
        illustrate.ensure_in_background(pid)  # the exact sketch is shown until it's ready
    try:
        return FileResponse(sketch.sketch_for(row["filename"]), headers=headers)
    except Exception:  # never fall back to the original: it may be graphic
        raise HTTPException(422, "A filtered version of this photo could not be made.")


@app.post("/responder/photo-view")
async def set_photo_view(request: Request):
    user = require_responder(request)
    form = await request.form()
    view = _choice(form.get("view"), ("illustration", "sketch", "original")) or "sketch"
    back = str(form.get("next") or "/responder")
    if not back.startswith("/responder") or back.startswith("//"):
        back = "/responder"  # only ever redirect within the responder area
    if view == "original" and form.get("confirm") != "1":
        return render(request, "graphic_warning.html", pid=None, next=back)
    if view == "original":
        with db.session() as conn:
            accounts.log(conn, user, "turned on unfiltered photos")
    resp = RedirectResponse(back, status_code=303)
    resp.set_cookie("photo_view", view, httponly=True, samesite="strict", secure=is_https(request.scope),
                    max_age=365 * 24 * 3600)
    return resp
