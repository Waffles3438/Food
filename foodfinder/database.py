"""SQLite persistence; every operation owns its short-lived connection."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from foodfinder.settings import database_path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path) if db_path else database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize(db_path: Path | str | None = None) -> None:
    with connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS clubs (
                id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                campus TEXT NOT NULL DEFAULT 'St George',
                portal_url TEXT NOT NULL DEFAULT '',
                website TEXT NOT NULL DEFAULT '',
                instagram_username TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                sources_json TEXT NOT NULL DEFAULT '[]',
                first_seen TEXT NOT NULL,
                last_discovered TEXT NOT NULL,
                last_checked TEXT NOT NULL DEFAULT '',
                check_status TEXT NOT NULL DEFAULT 'never',
                check_message TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_clubs_active ON clubs(active, name);
            CREATE INDEX IF NOT EXISTS idx_clubs_username ON clubs(instagram_username);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_checked TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'never',
                status_message TEXT NOT NULL DEFAULT '',
                posts_found INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS club_accounts (
                club_id TEXT NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                PRIMARY KEY(club_id, account_id)
            );
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                source_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                media_text TEXT NOT NULL DEFAULT '',
                posted_at TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                media_path TEXT NOT NULL DEFAULT '',
                media_blob BLOB,
                checked_at TEXT NOT NULL,
                UNIQUE(account_id, source_key)
            );
            CREATE INDEX IF NOT EXISTS idx_sources_account_date ON sources(account_id, posted_at);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                event_key TEXT NOT NULL,
                title TEXT NOT NULL,
                event_date TEXT NOT NULL DEFAULT '',
                event_time TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                food TEXT NOT NULL DEFAULT '',
                entry_cost TEXT NOT NULL DEFAULT '',
                restrictions TEXT NOT NULL DEFAULT '',
                eligibility TEXT NOT NULL DEFAULT '',
                rsvp TEXT NOT NULL DEFAULT '',
                food_confidence TEXT NOT NULL DEFAULT 'low',
                review_reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'upcoming',
                manual_overrides_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(source_id, event_key)
            );
            CREATE INDEX IF NOT EXISTS idx_events_date_status ON events(event_date, status);
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                excerpt TEXT NOT NULL DEFAULT '',
                image_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(event_id, source_id)
            );
            CREATE TABLE IF NOT EXISTS scan_runs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                accounts_total INTEGER NOT NULL DEFAULT 0,
                accounts_checked INTEGER NOT NULL DEFAULT 0,
                posts_seen INTEGER NOT NULL DEFAULT 0,
                events_found INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        existing_event_columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
        if "eligibility" not in existing_event_columns:
            db.execute("ALTER TABLE events ADD COLUMN eligibility TEXT NOT NULL DEFAULT ''")


def account_for(db: sqlite3.Connection, username: str) -> str:
    username = username.strip().lstrip("@").lower()
    row = db.execute("SELECT id FROM accounts WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    if row:
        db.execute("UPDATE accounts SET enabled = 1, updated_at = ? WHERE id = ?", (utcnow(), row["id"]))
        return str(row["id"])
    ident = str(uuid.uuid4())
    db.execute(
        "INSERT INTO accounts(id, username, updated_at) VALUES (?, ?, ?)",
        (ident, username, utcnow()),
    )
    return ident


def link_club_account(db: sqlite3.Connection, club_id: str, username: str) -> str:
    aid = account_for(db, username)
    db.execute("INSERT OR IGNORE INTO club_accounts(club_id, account_id) VALUES (?, ?)", (club_id, aid))
    return aid


def upsert_club(
    db: sqlite3.Connection,
    *,
    source_key: str,
    name: str,
    campus: str = "St George",
    portal_url: str = "",
    website: str = "",
    username: str = "",
    manual: bool = False,
) -> str:
    now = utcnow()
    existing = db.execute("SELECT * FROM clubs WHERE source_key = ?", (source_key,)).fetchone()
    if existing:
        sources = set(json.loads(existing["sources_json"] or "[]"))
        if source_key not in sources:
            sources.add(source_key)
        db.execute(
            """UPDATE clubs SET name=?, campus=?, portal_url=?, website=COALESCE(NULLIF(?, ''), website),
               instagram_username=COALESCE(NULLIF(?, ''), instagram_username), active=1, sources_json=?,
               last_discovered=? WHERE id=?""",
            (name, campus, portal_url, website, username.lower().lstrip("@"), json.dumps(sorted(sources)), now, existing["id"]),
        )
        ident = str(existing["id"])
    else:
        ident = str(uuid.uuid4())
        db.execute(
            """INSERT INTO clubs(id, source_key, name, campus, portal_url, website, instagram_username,
               sources_json, first_seen, last_discovered) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ident, source_key, name, campus, portal_url, website, username.lower().lstrip("@"),
             json.dumps([source_key]), now, now),
        )
    actual_user = username or (existing["instagram_username"] if existing else "")
    if actual_user:
        link_club_account(db, ident, actual_user)
    return ident


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
