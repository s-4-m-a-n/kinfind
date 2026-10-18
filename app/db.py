"""SQLite storage. One file, no ORM."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DISASTERLENS_DATA", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "disasterlens.db"
UPLOAD_DIR = DATA_DIR / "uploads"

SCHEMA = """
CREATE TABLE IF NOT EXISTS disasters (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    lat REAL, lng REAL, radius_km REAL,  -- affected zone shown on the home map
    -- Only 'active' zones are public. GDACS alerts and public proposals start as 'pending'
    -- until a responder approves, rejects or merges them into another zone.
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending', 'rejected', 'merged')),
    source TEXT NOT NULL DEFAULT 'responder' CHECK (source IN ('responder', 'public', 'gdacs')),
    external_id TEXT,              -- e.g. 'gdacs:TC:1001338', so a sync never re-adds a zone
    alert_level TEXT, source_url TEXT, started_on TEXT,
    note TEXT,                     -- what the proposer says happened
    created_at TEXT DEFAULT (datetime('now'))
);

-- Filed by families. Reporter contact and photos are never shown publicly.
CREATE TABLE IF NOT EXISTS missing_reports (
    id INTEGER PRIMARY KEY,
    ref TEXT UNIQUE NOT NULL,
    access_token TEXT NOT NULL,
    disaster_id INTEGER NOT NULL REFERENCES disasters(id),
    full_name TEXT NOT NULL,
    sex TEXT, age INTEGER, height_cm INTEGER, hair_color TEXT,
    last_seen_place TEXT, last_seen_date TEXT,
    clothing TEXT, description TEXT,
    reporter_name TEXT NOT NULL, reporter_contact TEXT NOT NULL,
    -- Public appeal: the family chooses to publish a "have you seen this person?" page.
    appeal_public INTEGER NOT NULL DEFAULT 0,
    appeal_photo_id INTEGER REFERENCES photos(id),  -- the one photo shown publicly
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Logged by responders: a living unidentified person or a recovered body.
-- `status` and `facility` are responder-only.
CREATE TABLE IF NOT EXISTS found_records (
    id INTEGER PRIMARY KEY,
    ref TEXT UNIQUE NOT NULL,
    disaster_id INTEGER NOT NULL REFERENCES disasters(id),
    status TEXT NOT NULL CHECK (status IN ('alive', 'deceased')),
    found_place TEXT NOT NULL, found_date TEXT,
    facility TEXT,
    sex TEXT, age_min INTEGER, age_max INTEGER, height_cm INTEGER, hair_color TEXT,
    clothing TEXT, description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS features (
    id INTEGER PRIMARY KEY,
    owner_type TEXT NOT NULL CHECK (owner_type IN ('missing', 'found')),
    owner_id INTEGER NOT NULL,
    kind TEXT NOT NULL, location TEXT, text TEXT,
    ai_description TEXT  -- JSON description generated from the feature's photo (app/ai_match.py)
);
CREATE INDEX IF NOT EXISTS features_owner ON features(owner_type, owner_id);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    owner_type TEXT NOT NULL CHECK (owner_type IN ('missing', 'found')),
    owner_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    feature_id INTEGER REFERENCES features(id)  -- set when the photo shows one distinctive feature
);
CREATE INDEX IF NOT EXISTS photos_owner ON photos(owner_type, owner_id);

-- AI photo check results (app/ai_match.py). Responder-only suggestions.
CREATE TABLE IF NOT EXISTS ai_matches (
    id INTEGER PRIMARY KEY,
    missing_id INTEGER NOT NULL REFERENCES missing_reports(id),
    found_id INTEGER NOT NULL REFERENCES found_records(id),
    stage TEXT,                        -- screened_out | shortlisted | comparing | compared | error
    shortlist_score INTEGER, shortlist_reason TEXT,   -- text screening of feature descriptions
    score INTEGER, verdict TEXT, summary TEXT,        -- photo comparison (shortlist only)
    consistent TEXT, conflicts TEXT,   -- JSON lists of strings
    model TEXT, error TEXT,            -- error is set when the call failed
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (missing_id, found_id)
);

-- Sightings and tips from the community on a public appeal. Families see them straight
-- away, marked unverified; responders can verify or hide them. The tipster's contact
-- is shown to responders only.
CREATE TABLE IF NOT EXISTS tips (
    id INTEGER PRIMARY KEY,
    missing_id INTEGER NOT NULL REFERENCES missing_reports(id),
    seen_place TEXT, seen_lat REAL, seen_lng REAL, seen_at TEXT,
    condition TEXT CHECK (condition IN ('well', 'needs_help', 'unsure')),
    message TEXT NOT NULL,
    contact TEXT,
    status TEXT NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified', 'verified', 'hidden')),
    hidden_by TEXT,  -- 'family' or 'responder'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS tips_missing ON tips(missing_id);

CREATE TABLE IF NOT EXISTS tip_photos (
    id INTEGER PRIMARY KEY,
    tip_id INTEGER NOT NULL REFERENCES tips(id),
    filename TEXT NOT NULL
);

-- A family asking responders to check a suggested match. Responders contact
-- the family; families never get facility or contact details directly.
CREATE TABLE IF NOT EXISTS verification_requests (
    id INTEGER PRIMARY KEY,
    missing_id INTEGER NOT NULL REFERENCES missing_reports(id),
    found_id INTEGER NOT NULL REFERENCES found_records(id),
    score INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'contacted', 'confirmed', 'rejected')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (missing_id, found_id)
);
"""


MIGRATIONS = [
    ("photos", "feature_id", "INTEGER REFERENCES features(id)"),
    ("disasters", "lat", "REAL"),
    ("disasters", "lng", "REAL"),
    ("disasters", "radius_km", "REAL"),
    ("disasters", "status", "TEXT NOT NULL DEFAULT 'active'"),
    ("disasters", "source", "TEXT NOT NULL DEFAULT 'responder'"),
    ("disasters", "external_id", "TEXT"),
    ("disasters", "alert_level", "TEXT"),
    ("disasters", "source_url", "TEXT"),
    ("disasters", "started_on", "TEXT"),
    ("disasters", "note", "TEXT"),
    ("disasters", "created_at", "TEXT"),
    ("features", "ai_description", "TEXT"),
    ("missing_reports", "appeal_public", "INTEGER NOT NULL DEFAULT 0"),
    ("missing_reports", "appeal_photo_id", "INTEGER"),
    ("ai_matches", "stage", "TEXT"),
    ("ai_matches", "shortlist_score", "INTEGER"),
    ("ai_matches", "shortlist_reason", "TEXT"),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with session() as conn:
        conn.executescript(SCHEMA)
        # Add columns that older databases were created without.
        for table, column, decl in MIGRATIONS:
            if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS disasters_external ON disasters(external_id)")


# --- loading records in the shape app.matching expects -----------------------

def _features(conn, owner_type: str, owner_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, kind, location, text, ai_description FROM features WHERE owner_type=? AND owner_id=?",
                        (owner_type, owner_id))
    out = []
    for r in rows:
        f = dict(r)
        f["ai_description"] = json.loads(f["ai_description"]) if f["ai_description"] else None
        out.append(f)
    return out


def missing_profile(conn, row: sqlite3.Row) -> dict:
    p = dict(row)
    if p["age"] is not None:
        p["age_min"], p["age_max"] = p["age"] - 2, p["age"] + 2
    p["date"] = p["last_seen_date"]
    p["features"] = _features(conn, "missing", p["id"])
    return p


def found_profile(conn, row: sqlite3.Row) -> dict:
    p = dict(row)
    if p["age_min"] is not None and p["age_max"] is None:
        p["age_max"] = p["age_min"]
    p["date"] = p["found_date"]
    p["features"] = _features(conn, "found", p["id"])
    return p


def found_profiles(conn, disaster_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM found_records WHERE disaster_id=?", (disaster_id,))
    return [found_profile(conn, r) for r in rows.fetchall()]


def missing_profiles(conn, disaster_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM missing_reports WHERE disaster_id=?", (disaster_id,))
    return [missing_profile(conn, r) for r in rows.fetchall()]
