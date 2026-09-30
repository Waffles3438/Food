"""Portable event lists, independent of Instagram sessions and local media."""

import json
import re
import uuid
from datetime import date, datetime
from urllib.parse import urlsplit

from foodfinder.database import connect, upsert_club, utcnow
from foodfinder.scanner import FIELD_MAP, TORONTO, list_events

FORMAT = "foodfinder.upcoming-events"
MAX_BYTES = 10 * 1024 * 1024
EVENT_FIELDS = tuple(FIELD_MAP) + ("id", "username", "club_names", "source_kind",
                                     "source_url", "caption", "media_text", "posted_at", "expires_at")
SOURCE_FIELDS = ("url", "kind", "username", "posted_at", "expires_at", "excerpt")


def export_events(db_path):
    events = []
    for event in list_events(db_path=db_path):
        item = {key: event.get(key) or "" for key in EVENT_FIELDS}
        item["supporting_sources"] = [
            {key: source.get(key) or "" for key in SOURCE_FIELDS}
            for source in event["supporting_sources"]
        ]
        events.append(item)
    return {"format": FORMAT, "version": 1, "exported_at": utcnow(), "events": events}


def _strings(value, fields):
    if not isinstance(value, dict):
        raise ValueError("Each event and source must be an object.")
    result = {}
    for key in fields:
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) > 20000:
            raise ValueError(f"{key} must be text of at most 20,000 characters.")
        result[key] = text
    return result


def _source(value):
    result = _strings(value, SOURCE_FIELDS)
    if not re.fullmatch(r"[a-zA-Z0-9_.]{1,30}", result["username"]):
        raise ValueError("Each source needs a valid Instagram username.")
    result["username"] = result["username"].lower()
    if result["kind"] not in {"post", "story"}:
        raise ValueError("Source kind must be post or story.")
    url = result["url"]
    parsed = urlsplit(url)
    if url and (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or any(ord(c) < 33 for c in url)):
        raise ValueError("Source links must be HTTP or HTTPS URLs without credentials.")
    for key in ("posted_at", "expires_at"):
        if result[key]:
            datetime.fromisoformat(result[key])
    return result


def _validate(payload):
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Choose a version 1 Free Food Finder event export.")
    events = payload.get("events")
    if not isinstance(events, list) or len(events) > 5000:
        raise ValueError("The file must contain a list of at most 5,000 events.")
    cleaned = []
    for index, value in enumerate(events, 1):
        try:
            event = _strings(value, EVENT_FIELDS)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event["id"]) or not event["title"].strip():
                raise ValueError("An event ID and title are required.")
            if event["status"] != "upcoming" or event["review_reason"]:
                raise ValueError("Only upcoming events without review flags can be imported.")
            if event["food_confidence"] not in {"low", "medium", "high"}:
                raise ValueError("Food confidence must be low, medium, or high.")
            if event["event_date"]:
                if date.fromisoformat(event["event_date"]).isoformat() != event["event_date"]:
                    raise ValueError("Event dates must use YYYY-MM-DD.")
            if event["event_time"] and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", event["event_time"]):
                raise ValueError("Event times must use HH:MM (24-hour time).")
            primary = _source({"url": event["source_url"], "kind": event["source_kind"],
                               "username": event["username"], "posted_at": event["posted_at"],
                               "expires_at": event["expires_at"], "excerpt": ""})
            event["username"] = primary["username"]
            sources = value.get("supporting_sources", [])
            if not isinstance(sources, list) or len(sources) > 100:
                raise ValueError("Supporting sources must be a list of at most 100 sources.")
            event["supporting_sources"] = [_source(source) for source in sources]
            event["primary"] = primary
            cleaned.append(event)
        except ValueError as exc:
            raise ValueError(f"Event {index}: {exc}") from exc
    return cleaned


def _account(db, username, club_name):
    row = db.execute("SELECT id FROM accounts WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    if row:
        ident = row["id"]
    else:
        ident = str(uuid.uuid4())
        db.execute("INSERT INTO accounts(id,username,enabled,updated_at) VALUES (?,?,0,?)", (ident, username, utcnow()))
    if club_name and not db.execute("SELECT 1 FROM club_accounts WHERE account_id=?", (ident,)).fetchone():
        club = upsert_club(db, source_key=f"import:{username}", name=club_name)
        db.execute("UPDATE clubs SET active=0,instagram_username=? WHERE id=?", (username, club))
        db.execute("INSERT INTO club_accounts(club_id,account_id) VALUES (?,?)", (club, ident))
    return ident


def import_events(payload, db_path):
    # Validate the entire file before opening the write transaction.
    events = _validate(payload)
    counts = {"imported": 0, "duplicates": 0, "past": 0}
    today = datetime.now(TORONTO).date().isoformat()
    with connect(db_path) as db:
        for event in events:
            if db.execute("SELECT 1 FROM events WHERE id=?", (event["id"],)).fetchone():
                counts["duplicates"] += 1
                continue
            if event["event_date"] and event["event_date"] < today:
                counts["past"] += 1
                continue
            sources = [event["primary"], *event["supporting_sources"]]
            source_ids = []
            for index, source in enumerate(sources):
                aid = _account(db, source["username"], event["club_names"] if index == 0 else "")
                sid = str(uuid.uuid4())
                db.execute("""INSERT INTO sources(id,account_id,source_key,kind,url,caption,media_text,
                              posted_at,expires_at,checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                           (sid, aid, f"import:{event['id']}:{index}", source["kind"], source["url"],
                            event["caption"] if index == 0 else "", event["media_text"] if index == 0 else "",
                            source["posted_at"], source["expires_at"], utcnow()))
                source_ids.append(sid)
            fields = tuple(FIELD_MAP)
            db.execute(f"""INSERT INTO events(id,source_id,event_key,{','.join(fields)},manual_overrides_json,updated_at)
                           VALUES ({','.join('?' for _ in range(len(fields) + 5))})""",
                       (event["id"], source_ids[0], f"import:{event['id']}",
                        *(event[key] for key in fields), json.dumps({key: event[key] for key in fields}), utcnow()))
            # Preserve the exported evidence list exactly; primary metadata lives on the event source.
            for sid, source in zip(source_ids[1:], sources[1:]):
                db.execute("INSERT INTO evidence(event_id,source_id,excerpt,created_at) VALUES (?,?,?,?)",
                           (event["id"], sid, source["excerpt"], utcnow()))
            counts["imported"] += 1
    return counts
