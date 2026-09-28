"""Persisted scan history determines retry timing across app restarts."""

from datetime import datetime, timedelta, timezone

from foodfinder.database import connect
from foodfinder.settings import SCAN_INTERVAL_HOURS


def failure_kind(run) -> str:
    kind = run["failure_kind"]
    if kind:
        return kind
    message = run["message"]
    # Older versions did not save structured error categories.
    if "returned HTTP 429" in message:
        return "rate_limit"
    if "Instagram temporarily restricted access" in message:
        return "temporary_limit"
    if "requires login verification" in message or "no Instagram session" in message:
        return "authentication"
    if message.startswith("Previous app session ended during this scan;"):
        return "interrupted"
    if "did not return readable account data" in message:
        return "access"
    return ""


def retry_plan(db_path, now=None) -> dict:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    with connect(db_path) as db:
        runs = db.execute("SELECT * FROM scan_runs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchall()
    empty = {"next_scan_at": "", "requires_login": False, "cooldown": False, "reason": ""}
    if not runs or runs[0]["status"] == "running" or not runs[0]["finished_at"]:
        return empty
    latest = runs[0]
    kind = failure_kind(latest)
    failed = bool(kind) or latest["status"] in {"partial", "error"}
    minutes = SCAN_INTERVAL_HOURS * 60
    reason = "Next scheduled scan"
    if kind == "rate_limit":
        minutes = 6 * 60
        reason = "Automatic retry after the six-hour HTTP 429 cooldown"
    elif failed:
        minutes = 5
        reason = "Automatic retry after the five-minute failure cooldown"
        if kind == "authentication":
            reason = "Login needs attention; reimport the browser session if needed. Automatic retry"
    try:
        finished = datetime.fromisoformat(latest["finished_at"])
    except ValueError:
        # A damaged timestamp must not trigger an immediate retry storm.
        return {**empty, "cooldown": True, "reason": "Invalid scan timestamp; check the saved scan status."}
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    due = finished + timedelta(minutes=minutes)
    return {"next_scan_at": due.isoformat(), "requires_login": kind == "authentication",
            "cooldown": failed and current < due, "reason": reason}
