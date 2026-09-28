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
        return "access"  # Unknown legacy failure: keep the longer delay.
    return ""


def retry_plan(db_path, now=None) -> dict:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    with connect(db_path) as db:
        runs = db.execute("SELECT * FROM scan_runs ORDER BY started_at DESC, rowid DESC LIMIT 4").fetchall()
    empty = {"next_scan_at": "", "requires_login": False, "cooldown": False, "reason": ""}
    if not runs or runs[0]["status"] == "running" or not runs[0]["finished_at"]:
        return empty
    latest = runs[0]
    kind = failure_kind(latest)
    if kind == "authentication":
        return {**empty, "requires_login": True, "reason": "Login needs attention. Reimport the browser session, then select Scan now."}
    minutes = SCAN_INTERVAL_HOURS * 60
    reason = "Next scheduled scan"
    if kind in {"connection", "response"}:
        streak = 0
        for run in runs:
            if failure_kind(run) not in {"connection", "response"}:
                break
            streak += 1
        minutes = (5, 15, 30, max(30, SCAN_INTERVAL_HOURS * 60))[min(streak, 4) - 1]
        reason = "Automatic retry after a temporary Instagram failure"
    elif kind in {"access", "rate_limit", "temporary_limit", "interrupted"}:
        reason = "Automatic retry after the longer scan cooldown"
    try:
        finished = datetime.fromisoformat(latest["finished_at"])
    except ValueError:
        # A damaged timestamp must not trigger an immediate retry storm.
        return {**empty, "cooldown": True, "reason": "Invalid scan timestamp; check the saved scan status."}
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    due = finished + timedelta(minutes=minutes)
    return {"next_scan_at": due.isoformat(), "requires_login": False,
            "cooldown": bool(kind) and current < due, "reason": reason}
