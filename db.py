"""SQLite storage for the contact finder.

Tables
- settings:   key/value (search criteria, baseline numbers for before/after comparison)
- contacted:  organizations B4P has already reached out to (the exclusion list)
- searches:   one row per AI research run (timing, counts, status)
- leads:      organizations the AI found, plus the human verification + outreach state
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "contacts.db"))

LEAD_STATUSES = ("new", "verified", "needs_call", "outdated", "rejected")
OUTREACH_STATUSES = ("not_sent", "sent", "bounced", "responded")

DEFAULT_SETTINGS = {
    "criteria": (
        "Free or sliding-scale clinics and shelters in Washtenaw County and metro Detroit "
        "that serve uninsured or underserved populations."
    ),
    "target_count": "10",
    # Numbers from before using AI, so the Results tab can show a before/after comparison.
    "baseline_minutes_per_org": "25",
    "baseline_bounce_rate": "",
    "baseline_response_rate": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacted (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL,
    website    TEXT NOT NULL DEFAULT '',
    domain     TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    added_at   TEXT NOT NULL,
    origin     TEXT NOT NULL DEFAULT 'manual'   -- manual | import | outreach
);
CREATE INDEX IF NOT EXISTS contacted_name_key ON contacted(name_key);

CREATE TABLE IF NOT EXISTS searches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    criteria        TEXT NOT NULL,
    target_count    INTEGER NOT NULL,
    status          TEXT NOT NULL,              -- running | done | error
    method          TEXT NOT NULL DEFAULT 'claude_ai',  -- claude_ai (pasted) | api
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    ai_seconds      REAL,
    found_count     INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    summary         TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    web_searches    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS leads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id         INTEGER REFERENCES searches(id),
    name              TEXT NOT NULL,
    name_key          TEXT NOT NULL,
    serves            TEXT NOT NULL DEFAULT '',
    org_type          TEXT NOT NULL DEFAULT '',
    address           TEXT NOT NULL DEFAULT '',
    phone             TEXT NOT NULL DEFAULT '',
    email             TEXT NOT NULL DEFAULT '',
    website           TEXT NOT NULL DEFAULT '',
    domain            TEXT NOT NULL DEFAULT '',
    contact_form_url  TEXT NOT NULL DEFAULT '',
    source_url        TEXT NOT NULL DEFAULT '',
    source_type       TEXT NOT NULL DEFAULT '',  -- org_website | directory | government | news | other
    source_date       TEXT NOT NULL DEFAULT '',
    confidence        TEXT NOT NULL DEFAULT '',  -- high | medium | low (AI's own estimate)
    ai_notes          TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'new',
    verified_by       TEXT NOT NULL DEFAULT '',
    verified_at       TEXT,
    verify_minutes    REAL,
    notes             TEXT NOT NULL DEFAULT '',
    outreach          TEXT NOT NULL DEFAULT 'not_sent',
    outreach_at       TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_name_key ON leads(name_key);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(searches)")}
        if "method" not in columns:  # databases created before the Claude.ai option
            conn.execute("ALTER TABLE searches ADD COLUMN method TEXT NOT NULL DEFAULT 'api'")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))


# ---------------------------------------------------------------------------
# Name / domain normalization, used to catch duplicates
# ---------------------------------------------------------------------------

_STOPWORDS = {"the", "inc", "llc", "of", "and", "a", "an", "co", "corp", "org", "nfp"}


def name_key(name):
    """'The Corner Health Center, Inc.' -> 'corner health center'"""
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w not in _STOPWORDS)


def domain_of(url):
    if not url:
        return ""
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def find_duplicate(conn, name, website):
    """Return a short description of what this org duplicates, or None."""
    key = name_key(name)
    domain = domain_of(website)
    if key:
        row = conn.execute("SELECT name FROM contacted WHERE name_key = ?", (key,)).fetchone()
        if row:
            return f"already contacted ({row['name']})"
    if domain:
        row = conn.execute("SELECT name FROM contacted WHERE domain = ?", (domain,)).fetchone()
        if row:
            return f"already contacted ({row['name']}, same website)"
    if key:
        row = conn.execute("SELECT name FROM leads WHERE name_key = ?", (key,)).fetchone()
        if row:
            return f"already in leads ({row['name']})"
    if domain:
        row = conn.execute("SELECT name FROM leads WHERE domain = ?", (domain,)).fetchone()
        if row:
            return f"already in leads ({row['name']}, same website)"
    return None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def update_settings(conn, values):
    for key, value in values.items():
        if key in DEFAULT_SETTINGS:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value).strip()),
            )


# ---------------------------------------------------------------------------
# Contacted list
# ---------------------------------------------------------------------------

def add_contacted(conn, name, website="", notes="", origin="manual"):
    name = (name or "").strip()
    if not name:
        return None
    key = name_key(name)
    domain = domain_of(website)
    existing = conn.execute(
        "SELECT id FROM contacted WHERE name_key = ? OR (domain != '' AND domain = ?)", (key, domain)
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO contacted(name, name_key, website, domain, notes, added_at, origin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, key, (website or "").strip(), domain, (notes or "").strip(), now_iso(), origin),
    )
    return cur.lastrowid


def rows(cursor):
    return [dict(r) for r in cursor.fetchall()]
