"""Background scans, durable event detection, and human review."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from foodfinder.database import connect, initialize, link_club_account, upsert_club, utcnow
from foodfinder.discovery import Club, fetch_myutsu_clubs, fetch_sop_clubs, merge_directory_clubs
from foodfinder.events import EventCandidate, deduplicate_candidate, extract_events, _extract_dates, _extract_times
from foodfinder.instagram import MediaSource, _make_loader, collection_error_detail, collection_failure_kind, iter_account_sources
from foodfinder.retry import retry_plan
from foodfinder.settings import DISCOVERY_INTERVAL_DAYS, POST_LIMIT, POST_AGE_DAYS
from foodfinder.progress import ScanProgress, report

TORONTO = ZoneInfo("America/Toronto")
NAMESPACE = uuid.UUID("8fc28cea-5d40-4b2b-9509-8af079db70ed")
FIELD_MAP = {
    "title": "title",
    "event_date": "event_date",
    "event_time": "event_time",
    "location": "location",
    "food": "food",
    "entry_cost": "entry_cost",
    "restrictions": "restrictions",
    "eligibility": "eligibility",
    "rsvp": "rsvp",
    "food_confidence": "food_confidence",
    "review_reason": "review_reason",
    "status": "status",
}


def _stable_id(value: str) -> str:
    return str(uuid.uuid5(NAMESPACE, value))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def due_for_discovery(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    row = db.execute("SELECT value FROM kv WHERE key='last_discovery' ").fetchone()
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(row["value"])
    except ValueError:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return current - last >= timedelta(days=DISCOVERY_INTERVAL_DAYS)


def refresh_directory(
    db_path: Path | str | None = None,
    *,
    sop_fetch: Callable[[str], str] = None,  # type: ignore[assignment]
    utsu_fetch: Callable[[str], str] = None,  # type: ignore[assignment]
    enrich_profiles: bool = True,
) -> dict[str, int]:
    """Upsert discovered clubs only after each public directory fetch succeeds."""
    primary = fetch_sop_clubs(**({"fetch": sop_fetch} if sop_fetch else {}), enrich_profiles=enrich_profiles)
    if not primary:
        raise RuntimeError("The Student Organization Portal returned no St. George clubs; its listing markup may have changed.")
    try:
        supplementary = fetch_myutsu_clubs(**({"fetch": utsu_fetch} if utsu_fetch else {}))
    except Exception:
        supplementary = []
    clubs = merge_directory_clubs(primary, supplementary)
    now = utcnow()
    with connect(db_path) as db:
        for club in clubs:
            ident = upsert_club(
                db,
                source_key=club.source_key,
                name=club.name,
                campus=club.campus,
                portal_url=club.portal_url,
                website=club.website,
                username=club.instagram_username,
            )
        db.execute(
            "INSERT INTO kv(key, value) VALUES ('last_discovery', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now,),
        )
    return {"clubs": len(clubs), "accounts": len({club.instagram_username for club in clubs if club.instagram_username})}


def _update_source(db: sqlite3.Connection, account_id: str, source: MediaSource) -> str:
    ident = _stable_id(f"{account_id}:{source.source_key}")
    # Retain Story evidence bytes inside the local database only if the Story contains an event lead.
    blob = None
    if source.kind == "story" and source.evidence_path:
        try:
            path = Path(source.evidence_path)
            if path.stat().st_size <= 4_000_000:
                blob = path.read_bytes()
        except OSError:
            blob = None
    db.execute(
        """INSERT INTO sources(id, account_id, source_key, kind, url, caption, media_text, posted_at,
           expires_at, media_blob, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(account_id, source_key) DO UPDATE SET
           url=excluded.url, caption=excluded.caption, media_text=excluded.media_text,
           posted_at=excluded.posted_at, expires_at=excluded.expires_at,
           media_blob=COALESCE(excluded.media_blob, sources.media_blob), checked_at=excluded.checked_at""",
        (ident, account_id, source.source_key, source.kind, source.url, source.caption,
         source.media_text, source.posted_at, source.expires_at, blob, utcnow()),
    )
    row = db.execute("SELECT id FROM sources WHERE account_id=? AND source_key=?", (account_id, source.source_key)).fetchone()
    db.execute("UPDATE sources SET cache_version=?,ocr_complete=? WHERE id=?",
               (0 if source.collection_warning else 1, int(source.ocr_complete), row["id"]))
    return str(row["id"])


def _existing_for_clubs(db: sqlite3.Connection, account_id: str, event_day: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.execute(
            """SELECT DISTINCT e.* FROM events e JOIN sources s ON s.id=e.source_id
               WHERE e.event_date=? AND e.status NOT IN ('cancelled','dismissed','duplicate')
               AND EXISTS (SELECT 1 FROM club_accounts ca1 JOIN club_accounts ca2 ON ca1.club_id=ca2.club_id
                           WHERE ca1.account_id=? AND ca2.account_id=s.account_id)""",
            (event_day, account_id),
        )
    ]


def _apply_candidate(
    db: sqlite3.Connection,
    *,
    account_id: str,
    source_id: str,
    source: MediaSource,
    candidate: EventCandidate,
) -> tuple[str, bool]:
    event_id = _stable_id(f"{source_id}:{candidate.event_key}")

    if candidate.status == "cancelled":
        possible = db.execute(
            """SELECT e.* FROM events e JOIN sources s ON s.id=e.source_id
               WHERE s.account_id=? AND e.status NOT IN ('cancelled','dismissed','duplicate')
               ORDER BY e.updated_at DESC LIMIT 50""",
            (account_id,),
        ).fetchall()
        matches: list[sqlite3.Row] = []
        candidate_title = " ".join(candidate.title.lower().split())
        for row in possible:
            old_title = " ".join(str(row["title"]).lower().split())
            if candidate.event_date and row["event_date"] and row["event_date"] != candidate.event_date:
                continue
            if SequenceMatcher(None, candidate_title, old_title).ratio() >= 0.80:
                matches.append(row)
        if len(matches) == 1:
            match = matches[0]
            db.execute("UPDATE events SET status='cancelled', updated_at=? WHERE id=?", (utcnow(), match["id"]))
            db.execute(
                "INSERT OR IGNORE INTO evidence(event_id,source_id,excerpt,created_at) VALUES (?,?,?,?)",
                (match["id"], source_id, candidate.evidence_excerpt, utcnow()),
            )
            return str(match["id"]), False
        candidate = replace(
            candidate,
            status="upcoming",
            food_confidence="low",
            review_reason="Possible cancellation could not be matched to a single event.",
        )

    if candidate.event_date and candidate.status == "upcoming":
        possible = [row for row in _existing_for_clubs(db, account_id, candidate.event_date)
                    if row["source_id"] != source_id]
        duplicate_id = deduplicate_candidate(possible, candidate)
        if duplicate_id:
            # Use the earliest event id as the canonical entry and retain every announcement.
            db.execute(
                "INSERT OR IGNORE INTO evidence(event_id,source_id,excerpt,created_at) VALUES (?,?,?,?)",
                (duplicate_id, source_id, candidate.evidence_excerpt, utcnow()),
            )
            return duplicate_id, False

    existing = db.execute("SELECT manual_overrides_json FROM events WHERE id=?", (event_id,)).fetchone()
    overrides = json.loads(existing["manual_overrides_json"] or "{}") if existing else {}
    values = {
        "title": candidate.title,
        "event_date": candidate.event_date,
        "event_time": candidate.event_time,
        "location": candidate.location,
        "food": candidate.food,
        "entry_cost": candidate.entry_cost,
        "restrictions": candidate.restrictions,
        "eligibility": candidate.eligibility,
        "rsvp": candidate.rsvp,
        "food_confidence": candidate.food_confidence,
        "review_reason": candidate.review_reason,
        "status": candidate.status,
    }
    values.update({key: value for key, value in overrides.items() if key in FIELD_MAP})
    # An event date is final: do not let a later source rescan resurface it.
    if values["event_date"] and values["event_date"] < datetime.now(TORONTO).date().isoformat():
        values["status"] = "dismissed"
    db.execute(
        """INSERT INTO events(id,source_id,event_key,title,event_date,event_time,location,food,entry_cost,
           restrictions,eligibility,rsvp,food_confidence,review_reason,status,manual_overrides_json,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,event_key) DO UPDATE SET
           title=excluded.title,event_date=excluded.event_date,event_time=excluded.event_time,
           location=excluded.location,food=excluded.food,entry_cost=excluded.entry_cost,
           restrictions=excluded.restrictions,eligibility=excluded.eligibility,rsvp=excluded.rsvp,food_confidence=excluded.food_confidence,
           review_reason=excluded.review_reason,status=excluded.status,
           manual_overrides_json=excluded.manual_overrides_json,updated_at=excluded.updated_at""",
        (event_id, source_id, candidate.event_key, values["title"], values["event_date"],
         values["event_time"], values["location"], values["food"], values["entry_cost"],
         values["restrictions"], values["eligibility"], values["rsvp"], values["food_confidence"], values["review_reason"],
         values["status"], _json(overrides), utcnow()),
    )
    if source.kind == "story" and source.evidence_path and not existing:
        db.execute(
            "INSERT OR IGNORE INTO evidence(event_id,source_id,excerpt,created_at) VALUES (?,?,?,?)",
            (event_id, source_id, candidate.evidence_excerpt, utcnow()),
        )
    else:
        db.execute(
            "INSERT OR IGNORE INTO evidence(event_id,source_id,excerpt,created_at) VALUES (?,?,?,?)",
            (event_id, source_id, candidate.evidence_excerpt, utcnow()),
        )
    return event_id, candidate.status == "upcoming" and not candidate.review_reason


def process_source(db_path: Path | str | None, account_id: str, account_name: str, source: MediaSource) -> int:
    text = "\n".join(part for part in (source.caption, source.media_text) if part.strip())
    posted = datetime.fromisoformat(source.posted_at.replace("Z", "+00:00"))
    date_text = source.caption if _extract_dates(source.caption, posted, datetime.now(TORONTO)) else text
    candidates = extract_events(text, posted_at=source.posted_at, account_name=account_name, date_text=date_text)
    if source.kind == "story" and not candidates and source.evidence_path:
        source = replace(source, evidence_path="")
    with connect(db_path) as db:
        source_id = _update_source(db, account_id, source)
        saved = 0
        seen_keys = {candidate.event_key for candidate in candidates}
        for candidate in candidates:
            _, is_upcoming = _apply_candidate(
                db,
                account_id=account_id,
                source_id=source_id,
                source=source,
                candidate=candidate,
            )
            saved += int(is_upcoming)
        prior = db.execute(
            "SELECT id,event_key,manual_overrides_json FROM events WHERE source_id=? AND status='upcoming'",
            (source_id,),
        ).fetchall()
        for row in prior:
            if row["event_key"] in seen_keys:
                continue
            overrides = json.loads(row["manual_overrides_json"] or "{}")
            # Replaced automatic detections should not remain as extra cards.
            # Keep manually reviewed rows and events supported by other posts.
            other_evidence = db.execute("SELECT 1 FROM evidence WHERE event_id=? AND source_id<>?", (row["id"], source_id)).fetchone()
            if candidates and not overrides and not other_evidence:
                db.execute("UPDATE events SET status='duplicate',updated_at=? WHERE id=?", (utcnow(), row["id"]))
                continue
            if "review_reason" in overrides or "status" in overrides:
                continue
            db.execute(
                "UPDATE events SET review_reason=?,updated_at=? WHERE id=?",
                ("The source post changed; check whether the food offer is still valid.", utcnow(), row["id"]),
            )
    return saved


_scan_lock = threading.Lock()


def scan_active() -> bool:
    return _scan_lock.locked()


def _is_rate_limit_error(exc: Exception) -> bool:
    return collection_failure_kind(exc) == "rate_limit"


def run_scan(db_path: Path | str | None = None, *, force_discovery: bool = False) -> str:
    """Run one bounded account queue; persist progress after each account."""
    initialize(db_path)
    if not _scan_lock.acquire(blocking=False):
        with connect(db_path) as db:
            active = db.execute("SELECT id FROM scan_runs WHERE status='running' ORDER BY started_at DESC LIMIT 1").fetchone()
            return str(active["id"]) if active else ""
    run_id = str(uuid.uuid4())
    failures = 0
    events_found = 0
    posts_seen = 0
    accounts_checked = 0
    pause_message = ""
    pause_kind = ""
    error_detail = ""
    loader = None
    progress = ScanProgress()
    progress.start()
    try:
        report(f"Scan {run_id[:8]} started.")
        with connect(db_path) as db:
            should_discover = force_discovery or due_for_discovery(db)
            db.execute(
                "INSERT INTO scan_runs(id,kind,status,started_at,message) VALUES (?,?, 'running', ?, ?)",
                (run_id, "full" if should_discover else "instagram", utcnow(), "Discovering clubs" if should_discover else "Loading Instagram accounts"),
            )
        if should_discover:
            report("Refreshing club directories and looking for Instagram accounts.")
            try:
                stats = refresh_directory(db_path)
                report(f"Club discovery finished: {stats['clubs']} clubs, {stats['accounts']} linked accounts.")
            except Exception as exc:
                failures += 1
                report(f"Club discovery failed ({type(exc).__name__}); continuing with saved clubs.")
                with connect(db_path) as db:
                    db.execute(
                        "UPDATE scan_runs SET message=? WHERE id=?",
                        (f"Club discovery failed ({type(exc).__name__}); existing clubs were kept.", run_id),
                    )

        with connect(db_path) as db:
            accounts = db.execute(
                """SELECT a.* FROM accounts a WHERE a.enabled=1
                   AND EXISTS (SELECT 1 FROM club_accounts ca JOIN clubs c ON c.id=ca.club_id
                               WHERE ca.account_id=a.id AND c.active=1)
                   ORDER BY a.last_checked, a.username"""
            ).fetchall()
            db.execute("UPDATE scan_runs SET accounts_total=?, message=? WHERE id=?",
                       (len(accounts), "Preparing Instagram scan", run_id))

        loader = None
        loader_error: Exception | None = None
        report(f"Instagram queue: {len(accounts)} accounts. Checking captions first; images use OCR when needed.")
        for index, account in enumerate(accounts, 1):
            account_id, username = str(account["id"]), str(account["username"])
            report(f"Account {index}/{len(accounts)}: @{username} - starting.")
            with connect(db_path) as db:
                db.execute("UPDATE scan_runs SET message=? WHERE id=?", (f"Checking @{username}", run_id))
            try:
                if loader is None:
                    try:
                        import instaloader
                        loader, _ = _make_loader(instaloader)
                    except Exception as exc:
                        loader_error = exc
                        raise
                checked = 0
                truncated = False
                warnings: set[str] = set()
                with connect(db_path) as db:
                    saved_sources = {row["source_key"]: dict(row) for row in db.execute(
                        "SELECT source_key,caption,media_text,cache_version,ocr_complete FROM sources WHERE account_id=?",
                        (account_id,),
                    )}
                for source in iter_account_sources(username, loader=loader, saved_sources=saved_sources):
                    if source.collection_warning:
                        warnings.add(source.collection_warning)
                    if source.truncated:
                        truncated = True
                        continue
                    if not source.source_key:
                        continue
                    checked += 1
                    posts_seen += int(source.kind == "post")
                    detected = process_source(db_path, account_id, username, source)
                    events_found += detected
                    report(f"@{username}: saved {source.kind} {source.source_key}; {detected} upcoming detections. Run totals: {posts_seen} posts, {events_found} upcoming detections.")
                    with connect(db_path) as db:
                        db.execute("UPDATE scan_runs SET posts_seen=?,events_found=? WHERE id=?", (posts_seen, events_found, run_id))
                if truncated:
                    warnings.add(f"Reached the {POST_LIMIT}-post inspection limit; more posts may exist. Posts older than {POST_AGE_DAYS} days were skipped.")
                account_status = "partial" if warnings else "ok"
                message = " ".join(sorted(warnings)) if warnings else "Scanned posts and accessible Stories."
                failures += int(bool(warnings))
            except Exception as exc:
                failures += 1
                if loader_error is exc:
                    message = "No usable Instagram login session. Run login-instagram.ps1."
                    pause_message = "Scan paused because no Instagram session is configured. Run login-instagram.ps1, then scan again."
                elif _is_rate_limit_error(exc):
                    message = "Instagram returned HTTP 429 for this account; the queue was paused."
                    pause_message = "Scan paused after Instagram returned HTTP 429. Wait before starting another scan; remaining accounts were left queued."
                elif collection_failure_kind(exc) == "temporary_limit":
                    message = "Instagram temporarily restricted access with a please-wait or feedback response; the queue was paused."
                    pause_message = "Scan paused because Instagram temporarily restricted access. This does not establish that the saved login is expired."
                elif collection_failure_kind(exc) == "authentication":
                    message = "Instagram login needs attention; run login-instagram.ps1 again."
                    pause_message = "Scan paused because Instagram requires login verification. Resolve the challenge, then run login-instagram.ps1."
                elif collection_failure_kind(exc) in {"access", "response", "connection"}:
                    message = "Instagram access failed; the account queue was paused. Check your browser session and try a single account later."
                    pause_message = "Scan paused because Instagram did not return readable account data. A login restriction, network failure, or API change may be responsible."
                else:
                    message = f"Could not read this account ({type(exc).__name__})."
                error_detail = collection_error_detail(exc)
                category = collection_failure_kind(exc) or "unknown"
                message += f" [{category}: {error_detail}]"
                if pause_message:
                    pause_kind = "authentication" if loader_error is exc else category
                    pause_message += f" [{pause_kind}: {error_detail}]"
                account_status = "error"
                checked = 0
            with connect(db_path) as db:
                db.execute(
                    "UPDATE accounts SET last_checked=?,status=?,status_message=?,posts_found=?,updated_at=? WHERE id=?",
                    (utcnow(), account_status, message, checked, utcnow(), account_id),
                )
                db.execute(
                    "UPDATE clubs SET last_checked=?,check_status=?,check_message=? WHERE id IN "
                    "(SELECT club_id FROM club_accounts WHERE account_id=?)",
                    (utcnow(), account_status, message, account_id),
                )
                accounts_checked += 1
                db.execute(
                    "UPDATE scan_runs SET accounts_checked=?,posts_seen=?,events_found=?,message=? WHERE id=?",
                    (accounts_checked, posts_seen, events_found, f"Finished @{username}: {message}", run_id),
                )
            report(f"Account {index}/{len(accounts)}: @{username} - {account_status}. {message}")
            if pause_message:
                break
        with connect(db_path) as db:
            final_status = "partial" if failures else "complete"
            final_message = pause_message or (
                "Some discovery or account checks failed; see account status." if failures else "Scan finished."
            )
            db.execute(
                "UPDATE scan_runs SET status=?,finished_at=?,accounts_checked=?,posts_seen=?,events_found=?,message=?,failure_kind=?,error_detail=? WHERE id=?",
                (final_status, utcnow(), accounts_checked, posts_seen, events_found, final_message, pause_kind, error_detail, run_id),
            )
        report(f"Scan {final_status}: {accounts_checked}/{len(accounts)} accounts checked, {posts_seen} posts, {events_found} upcoming detections. {final_message}")
        return run_id
    except Exception as exc:
        report(f"Scan stopped ({type(exc).__name__}). Existing results were preserved.")
        with connect(db_path) as db:
            db.execute(
                "UPDATE scan_runs SET status='error',finished_at=?,message=? WHERE id=?",
                (utcnow(), f"Scan stopped ({type(exc).__name__}). Existing results were preserved.", run_id),
            )
        return run_id
    finally:
        if loader is not None and callable(getattr(loader, "close", None)):
            try:
                loader.close()
            except Exception:
                pass
        progress.close()
        _scan_lock.release()


def launch_scan(db_path: Path | str | None = None, *, force_discovery: bool = False) -> str:
    initialize(db_path)
    thread = threading.Thread(target=run_scan, kwargs={"db_path": db_path, "force_discovery": force_discovery}, daemon=True)
    thread.start()
    return "started"


def _event_rows(db, ids, *, summary=False):
    results = []
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset + 500]
        marks = ",".join("?" for _ in batch)
        fields = "e.id,e.title,e.event_date" if summary else "e.*,s.kind AS source_kind,s.url AS source_url,s.caption,s.media_text,s.posted_at,s.expires_at,a.username"
        rows = db.execute(f"""SELECT {fields}, GROUP_CONCAT(DISTINCT c.name) AS club_names
            FROM events e JOIN sources s ON s.id=e.source_id JOIN accounts a ON a.id=s.account_id
            LEFT JOIN club_accounts ca ON ca.account_id=a.id LEFT JOIN clubs c ON c.id=ca.club_id
            WHERE e.id IN ({marks}) GROUP BY e.id""", batch).fetchall()
        evidence = {}
        if not summary:
            for row in db.execute(f"""SELECT DISTINCT ev.event_id,s.url,s.kind,s.posted_at,s.expires_at,a.username,ev.excerpt
                FROM evidence ev JOIN sources s ON s.id=ev.source_id JOIN accounts a ON a.id=s.account_id
                WHERE ev.event_id IN ({marks}) ORDER BY s.posted_at DESC""", batch):
                item = dict(row)
                evidence.setdefault(item.pop("event_id"), []).append(item)
        by_id = {}
        for row in rows:
            result = dict(row)
            if not summary:
                result["manual_overrides"] = json.loads(result.pop("manual_overrides_json") or "{}")
                result["food_confidence_label"] = result["food_confidence"].title()
                result["entry_paid"] = bool(result["entry_cost"] and result["entry_cost"] != "Free entry")
                result["supporting_sources"] = evidence.get(result["id"], [])
            by_id[result["id"]] = result
        results.extend(by_id[ident] for ident in batch if ident in by_id)
    return results


def get_event(event_id: str, db_path: Path | str | None = None) -> dict[str, Any] | None:
    with connect(db_path) as db:
        rows = _event_rows(db, [event_id])
    return rows[0] if rows else None


def event_counts(db_path=None):
    return {view: len(list_events(view=view, db_path=db_path, _summary=True))
            for view in ("upcoming", "today", "review")}


def dismiss_expired_events(db_path: Path | str | None = None, *, now: datetime | None = None) -> int:
    """Dismiss past events, using an explicit end time when the announcement gives one."""
    current = now or datetime.now(TORONTO)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TORONTO)
    current = current.astimezone(TORONTO)
    cutoff = current.date().isoformat()
    due: list[str] = []
    with connect(db_path) as db:
        rows = db.execute(
            """SELECT e.id,e.event_date,e.event_time,e.title,s.caption,s.media_text
               FROM events e JOIN sources s ON s.id=e.source_id
               WHERE e.event_date!='' AND e.event_date<=?
                 AND e.status NOT IN ('dismissed','cancelled','duplicate')""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            try:
                event_day = date.fromisoformat(row["event_date"])
            except ValueError:
                continue
            if event_day < current.date():
                due.append(row["id"])
                continue
            start = row["event_time"]
            if not start:
                continue
            end = ""
            for text in (row["title"], row["caption"], row["media_text"]):
                times = _extract_times(text or "")
                if start in times:
                    index = times.index(start)
                    if index + 1 < len(times):
                        end = times[index + 1]
                        break
            # A start time alone doesn't tell us when the event has finished.
            if end and end <= current.strftime("%H:%M"):
                due.append(row["id"])
        if due:
            marks = ",".join("?" for _ in due)
            cursor = db.execute(
                f"UPDATE events SET status='dismissed', updated_at=? WHERE id IN ({marks})",
                (utcnow(), *due),
            )
            return cursor.rowcount
        return 0


def patch_event(event_id: str, patch: dict[str, Any], db_path: Path | str | None = None) -> dict[str, Any] | None:
    allowed = set(FIELD_MAP)
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError(f"Unsupported event fields: {', '.join(sorted(unknown))}")
    if "event_date" in patch and patch["event_date"] and not _valid_iso_date(str(patch["event_date"])):
        raise ValueError("event_date must be YYYY-MM-DD.")
    if "status" in patch and patch["status"] not in {"upcoming", "cancelled", "dismissed"}:
        raise ValueError("status must be upcoming, cancelled, or dismissed.")
    with connect(db_path) as db:
        row = db.execute("SELECT manual_overrides_json FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return None
        overrides = json.loads(row["manual_overrides_json"] or "{}")
        overrides.update({key: str(value).strip() for key, value in patch.items()})
        updates = ", ".join(f"{FIELD_MAP[key]}=?" for key in patch)
        if updates:
            db.execute(
                f"UPDATE events SET {updates}, manual_overrides_json=?, updated_at=? WHERE id=?",
                (*[str(value).strip() for value in patch.values()], _json(overrides), utcnow(), event_id),
            )
    dismiss_expired_events(db_path)
    return get_event(event_id, db_path)


def _valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return len(value) == 10
    except ValueError:
        return False


def list_events(
    *,
    view: str = "upcoming",
    club: str = "",
    from_date: str = "",
    to_date: str = "",
    free_entry: bool = False,
    unrestricted_entry: bool = False,
    db_path: Path | str | None = None,
    _summary: bool = False,
) -> list[dict[str, Any]]:
    today = datetime.now(TORONTO).date().isoformat()
    clauses = ["e.status NOT IN ('cancelled','dismissed','duplicate')"]
    params: list[str] = []
    if view == "review":
        clauses.append("(e.review_reason != '' OR e.food_confidence='low')")
    elif view == "today":
        clauses.append("e.event_date=?")
        clauses.append("e.event_time=''")
        clauses.append("e.status='upcoming'")
        params.append(today)
    else:
        clauses.append("e.status='upcoming'")
        clauses.append("(e.event_date='' OR e.event_date>=?)")
        params.append(today)
        if view != "review":
            clauses.append("e.review_reason=''")
    if club:
        clauses.append("EXISTS (SELECT 1 FROM club_accounts cx JOIN clubs cc ON cc.id=cx.club_id WHERE cx.account_id=s.account_id AND cc.id=? )")
        params.append(club)
    if from_date:
        clauses.append("e.event_date>=?")
        params.append(from_date)
    if to_date:
        clauses.append("e.event_date<=?")
        params.append(to_date)
    if free_entry:
        clauses.append("e.entry_cost='Free entry'")
    if unrestricted_entry:
        clauses.append("e.eligibility='Open to everyone'")
    query = (
        "SELECT e.id FROM events e JOIN sources s ON s.id=e.source_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY CASE WHEN e.event_date='' THEN 1 ELSE 0 END, e.event_date, e.event_time, e.updated_at DESC"
    )
    with connect(db_path) as db:
        ids = [row["id"] for row in db.execute(query, params)]
        event_rows = _event_rows(db, ids, summary=_summary)
    seen: set[tuple[str, str, str]] = set()
    results: list[dict[str, Any]] = []
    for result in event_rows:
        key = (result["title"].casefold(), result["event_date"], "|".join(sorted((result.get("club_names") or "").split(","))))
        if key in seen:
            continue
        seen.add(key)
        results.append(result)
    return results


def list_clubs(db_path: Path | str | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as db:
        rows = db.execute(
            """SELECT c.*, GROUP_CONCAT(DISTINCT a.username) AS accounts,
               GROUP_CONCAT(DISTINCT a.status) AS account_statuses
               FROM clubs c LEFT JOIN club_accounts ca ON ca.club_id=c.id
               LEFT JOIN accounts a ON a.id=ca.account_id GROUP BY c.id ORDER BY c.name COLLATE NOCASE"""
        ).fetchall()
    return [{**dict(row), "sources": json.loads(row["sources_json"] or "[]")} for row in rows]


def upsert_manual_club(
    *,
    name: str,
    username: str = "",
    website: str = "",
    active: bool = True,
    club_id: str = "",
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    name = name.strip()
    username = username.strip().lstrip("@")
    if len(name) < 2 or len(name) > 180:
        raise ValueError("Club name must be between 2 and 180 characters.")
    if username and not __import__("re").fullmatch(r"[A-Za-z0-9_.]{1,30}", username):
        raise ValueError("Enter an Instagram username without the @ sign.")
    with connect(db_path) as db:
        if club_id:
            current = db.execute("SELECT * FROM clubs WHERE id=?", (club_id,)).fetchone()
            if not current:
                raise KeyError(club_id)
            db.execute("DELETE FROM club_accounts WHERE club_id=?", (club_id,))
            db.execute(
                "UPDATE clubs SET name=?, website=?, instagram_username=?, active=?, last_discovered=? WHERE id=?",
                (name, website.strip(), username.lower(), int(active), utcnow(), club_id),
            )
            ident = club_id
            if username:
                link_club_account(db, ident, username)
        else:
            ident = upsert_club(
                db,
                source_key=f"manual:{uuid.uuid4()}",
                name=name,
                campus="St George",
                website=website.strip(),
                username=username,
                manual=True,
            )
            db.execute("UPDATE clubs SET active=? WHERE id=?", (int(active), ident))
    return next(club for club in list_clubs(db_path) if club["id"] == ident)


def scan_progress(db_path: Path | str | None = None) -> dict[str, Any]:
    with connect(db_path) as db:
        run = db.execute("SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        statuses = db.execute(
            "SELECT status, COUNT(*) AS count FROM accounts WHERE enabled=1 GROUP BY status"
        ).fetchall()
        missing = db.execute(
            "SELECT COUNT(*) AS n FROM clubs WHERE active=1 AND instagram_username=''"
        ).fetchone()["n"]
    return {
        "run": dict(run) if run else None,
        "retry": retry_plan(db_path),
        "accounts": {row["status"]: row["count"] for row in statuses},
        "missing_handles": missing,
        "running": bool(run and run["status"] == "running"),
    }


def source_image(event_id: str, db_path: Path | str | None = None) -> tuple[bytes, str] | None:
    with connect(db_path) as db:
        row = db.execute(
            "SELECT s.media_blob FROM events e JOIN sources s ON s.id=e.source_id WHERE e.id=?",
            (event_id,),
        ).fetchone()
    if row and row["media_blob"]:
        return bytes(row["media_blob"]), "image/jpeg"
    return None
