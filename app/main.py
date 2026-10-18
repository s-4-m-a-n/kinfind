"""Kinfind — match missing-person reports with found people / recovered bodies.

Families file a report and see *where and when* a possible match was found.
They never see photos, the holding facility, or contact details: they ask for
verification and a responder contacts them.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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

from . import ai_match, db, gdacs, illustrate, sketch  # noqa: E402
from .matching import BODY_LOCATIONS, FEATURE_KINDS, HAIR_COLORS, rank_found, rank_missing  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "Kinfind"
RESPONDER_KEY = os.environ.get("RESPONDER_KEY", "responder-demo")
MAX_PHOTO_BYTES = 8 * 1024 * 1024
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
    task = asyncio.create_task(gdacs_loop()) if GDACS_SYNC_MINUTES > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.globals.update(app_name=APP_NAME, feature_kinds=FEATURE_KINDS, body_locations=BODY_LOCATIONS,
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


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx["is_responder"] = is_responder(request)
    ctx["photo_view"] = photo_view(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    message = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "Page not found."
    return render(request, "error.html", status_code=exc.status_code, message=message)


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


def _choice(value, options) -> str | None:
    return value if value in options else None


def _upload(value):
    """A file the user actually picked, or None for an empty file input."""
    return value if getattr(value, "filename", None) else None


def parse_features(form) -> list[dict]:
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


async def store_photo(conn, upload, owner_type: str, owner_id: int, feature_id: int | None = None) -> None:
    ext = PHOTO_TYPES.get(upload.content_type)
    if not ext:
        raise HTTPException(400, "Photos must be JPEG, PNG or WebP.")
    data = await upload.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(400, "Each photo must be under 8 MB.")
    try:  # reject files that only claim to be images: they would break sketches and AI checks
        from PIL import Image
        Image.open(io.BytesIO(data)).verify()
    except Exception:
        raise HTTPException(400, f"“{upload.filename}” could not be read as an image.")
    filename = secrets.token_hex(16) + ext
    (db.UPLOAD_DIR / filename).write_bytes(data)
    conn.execute("INSERT INTO photos (owner_type, owner_id, filename, feature_id) VALUES (?, ?, ?, ?)",
                 (owner_type, owner_id, filename, feature_id))


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

def _session_token() -> str:
    return hashlib.sha256(("disasterlens:" + RESPONDER_KEY).encode()).hexdigest()


def is_responder(request: Request) -> bool:
    return hmac.compare_digest(request.cookies.get("responder", ""), _session_token())


def require_responder(request: Request) -> None:
    if not is_responder(request):
        raise HTTPException(303, headers={"Location": "/responder/login"})


@app.get("/responder/login")
def login_form(request: Request):
    return render(request, "login.html", error=None)


@app.post("/responder/login")
async def login(request: Request):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("key", "")), RESPONDER_KEY):
        return render(request, "login.html", error="That access key is not valid.")
    resp = RedirectResponse("/responder", status_code=303)
    resp.set_cookie("responder", _session_token(), httponly=True, samesite="strict")
    return resp


@app.post("/responder/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("responder")
    return resp


# --- families ----------------------------------------------------------------

@app.get("/")
def home(request: Request):
    with db.session() as conn:
        zones = [dict(d) for d in disasters(conn)]
    return render(request, "index.html", zones=zones)


@app.get("/missing/new")
def missing_form(request: Request, disaster: int = 0, proposed: int = 0):
    with db.session() as conn:
        return render(request, "missing_form.html", disasters=disasters(conn, include=disaster),
                      selected=disaster, proposed=proposed)


@app.get("/zones/new")
def propose_zone_form(request: Request):
    with db.session() as conn:
        zones = [dict(d) for d in disasters(conn)]
    return render(request, "disaster_form.html", zones=zones, public=True)


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

    ref, token = new_ref("MP"), secrets.token_urlsafe(24)
    with db.session() as conn:
        cur = conn.execute(
            """INSERT INTO missing_reports (ref, access_token, disaster_id, full_name, sex, age,
                   height_cm, hair_color, last_seen_place, last_seen_date, clothing, description,
                   reporter_name, reporter_contact)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, token, disaster_id(conn, form), full_name,
             _choice(form.get("sex"), ("male", "female", "unknown")),
             _int(form.get("age"), 0, 120), _int(form.get("height_cm"), 40, 250),
             _choice(form.get("hair_color"), HAIR_COLORS),
             _text(form.get("last_seen_place"), 300), _text(form.get("last_seen_date"), 10),
             _text(form.get("clothing")), _text(form.get("description")),
             reporter_name, reporter_contact))
        await save_features(conn, parse_features(form), "missing", cur.lastrowid)
        await save_photos(conn, form, "missing", cur.lastrowid)
    background.add_task(ai_match.run_for, "missing", cur.lastrowid)  # no-op without an API key
    return RedirectResponse(f"/case/{ref}?k={token}&new=1", status_code=303)


def _load_case(conn, ref: str, k: str):
    row = conn.execute("SELECT * FROM missing_reports WHERE ref=?", (ref,)).fetchone()
    if not row or not hmac.compare_digest(row["access_token"], k or ""):
        raise HTTPException(404, "Case not found. Check the private link you were given.")
    return row


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
        return render(request, "case.html", case=profile, matches=matches, k=k, new=new,
                      disaster=disaster, photos=photo_count(conn, "missing", row["id"]))


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
        return render(request, "dashboard.html", queue=queue, found=found, missing=missing,
                      pending_zones=pending_zones, ai_flags=ai_flags, ai_enabled=ai_match.enabled())


@app.post("/responder/verifications/{vid}")
async def update_verification(request: Request, vid: int):
    require_responder(request)
    status = _choice((await request.form()).get("status"), ("open", "contacted", "confirmed", "rejected"))
    if not status:
        raise HTTPException(400)
    with db.session() as conn:
        conn.execute("UPDATE verification_requests SET status=? WHERE id=?", (status, vid))
    return RedirectResponse("/responder", status_code=303)


@app.get("/responder/found/new")
def found_form(request: Request, disaster: int = 0):
    require_responder(request)
    with db.session() as conn:
        return render(request, "found_form.html", disasters=disasters(conn, include=disaster), selected=disaster)


@app.get("/responder/disasters/new")
def disaster_form(request: Request):
    require_responder(request)
    with db.session() as conn:
        zones = [dict(d) for d in disasters(conn)]
    return render(request, "disaster_form.html", zones=zones)


@app.post("/responder/disasters")
async def create_disaster(request: Request):
    require_responder(request)
    zone = parse_zone(await request.form())
    with db.session() as conn:
        insert_zone(conn, zone, "active", "responder")
    return RedirectResponse("/#zones", status_code=303)


@app.get("/responder/zones")
def zone_review(request: Request, synced: str = ""):
    require_responder(request)
    with db.session() as conn:
        pending = [dict(r) for r in conn.execute("""
            SELECT d.*, (SELECT COUNT(*) FROM missing_reports m WHERE m.disaster_id = d.id) AS reports
            FROM disasters d WHERE status = 'pending'
            ORDER BY source = 'public' DESC, alert_level = 'Red' DESC, id DESC""")]
        active = [dict(d) for d in disasters(conn)]
    return render(request, "zones_review.html", pending=pending, active=active, synced=synced,
                  zones=pending + active)


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
    require_responder(request)
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
    return RedirectResponse("/responder/zones", status_code=303)


@app.post("/responder/found")
async def create_found(request: Request, background: BackgroundTasks):
    require_responder(request)
    form = await request.form()
    found_place = _text(form.get("found_place"), 300)
    status = _choice(form.get("status"), ("alive", "deceased"))
    if not (found_place and status):
        raise HTTPException(400, "Where the person was found and their status are required.")
    age_min, age_max = _int(form.get("age_min"), 0, 120), _int(form.get("age_max"), 0, 120)
    if age_min is not None and age_max is not None and age_min > age_max:
        age_min, age_max = age_max, age_min

    ref = new_ref("FD")
    with db.session() as conn:
        cur = conn.execute(
            """INSERT INTO found_records (ref, disaster_id, status, found_place, found_date, facility,
                   sex, age_min, age_max, height_cm, hair_color, clothing, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, disaster_id(conn, form), status, found_place, _text(form.get("found_date"), 10),
             _text(form.get("facility"), 300),
             _choice(form.get("sex"), ("male", "female", "unknown")), age_min, age_max,
             _int(form.get("height_cm"), 40, 250), _choice(form.get("hair_color"), HAIR_COLORS),
             _text(form.get("clothing")), _text(form.get("description"))))
        await save_features(conn, parse_features(form), "found", cur.lastrowid)
        await save_photos(conn, form, "found", cur.lastrowid)
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
    require_responder(request)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM found_records WHERE ref=?", (ref,)).fetchone()
        if not row:
            raise HTTPException(404)
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
    require_responder(request)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM missing_reports WHERE ref=?", (ref,)).fetchone()
        if not row:
            raise HTTPException(404)
        profile = db.missing_profile(conn, row)
        matches = rank_found(profile, db.found_profiles(conn, row["disaster_id"]))
        feature_photos, photo_ids = _photo_ids(conn, "missing", row["id"])
        ai = _ai_context(conn, "missing", row["id"])
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


@app.get("/responder/photos/{pid}")
def photo(request: Request, pid: int, view: str = "", confirm: int = 0):
    require_responder(request)
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
    require_responder(request)
    form = await request.form()
    view = _choice(form.get("view"), ("illustration", "sketch", "original")) or "sketch"
    back = str(form.get("next") or "/responder")
    if not back.startswith("/responder") or back.startswith("//"):
        back = "/responder"  # only ever redirect within the responder area
    if view == "original" and form.get("confirm") != "1":
        return render(request, "graphic_warning.html", pid=None, next=back)
    resp = RedirectResponse(back, status_code=303)
    resp.set_cookie("photo_view", view, httponly=True, samesite="strict", max_age=365 * 24 * 3600)
    return resp
