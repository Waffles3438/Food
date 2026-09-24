"""FastAPI routes and the loopback-only dashboard."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request

from foodfinder.database import connect, initialize, utcnow
from foodfinder.scanner import (
    get_event,
    launch_scan,
    list_clubs,
    list_events,
    patch_event,
    scan_progress,
    upsert_manual_club,
)
from foodfinder.settings import SCAN_INTERVAL_HOURS, database_path

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))


def _days_since(iso_timestamp: str) -> int | None:
    if not iso_timestamp:
        return None
    try:
        timestamp = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - timestamp).days)


def _recover_orphaned_run(db_path: Path | str) -> None:
    with connect(db_path) as db:
        db.execute(
            """UPDATE scan_runs SET status='partial', finished_at=?,
               message='Previous app session ended during this scan; remaining accounts are deferred to the next scheduled scan.'
               WHERE status='running'""",
            (utcnow(),),
        )


def _scan_cooldown_active(db_path: Path | str, now: datetime | None = None) -> bool:
    with connect(db_path) as db:
        latest = db.execute(
            "SELECT finished_at,message FROM scan_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    if not latest or not latest["finished_at"]:
        return False
    message = latest["message"]
    rate_limited = "returned HTTP 429" in message
    interrupted = message.startswith("Previous app session ended during this scan; remaining accounts are deferred")
    if not (rate_limited or interrupted):
        return False
    try:
        finished = datetime.fromisoformat(latest["finished_at"])
    except ValueError:
        return False
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - finished.astimezone(timezone.utc) < timedelta(hours=SCAN_INTERVAL_HOURS)


async def _scheduler(app: FastAPI) -> None:
    """Launch one catch-up scan on start and then honour the saved six-hour cadence."""
    while True:
        try:
            with connect(app.state.db_path) as db:
                latest = db.execute("SELECT status,finished_at,message FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            if latest is None:
                launch_scan(app.state.db_path)
            elif latest["status"] != "running" and latest["finished_at"]:
                try:
                    finished = datetime.fromisoformat(latest["finished_at"])
                    if finished.tzinfo is None:
                        finished = finished.replace(tzinfo=timezone.utc)
                    due = datetime.now(timezone.utc) - finished.astimezone(timezone.utc) >= timedelta(hours=SCAN_INTERVAL_HOURS)
                except ValueError:
                    due = True
                if due:
                    launch_scan(app.state.db_path)
        except Exception:
            # A scan failure must not take down the user's dashboard.
            pass
        await asyncio.sleep(60)


def create_app(*, db_path: Path | str | None = None, start_scheduler: bool = True) -> FastAPI:
    database = str(db_path or database_path())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize(database)
        _recover_orphaned_run(database)
        scheduler_task = None
        if start_scheduler:
            scheduler_task = asyncio.create_task(_scheduler(app))
        try:
            yield
        finally:
            if scheduler_task:
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="U of T Free Food Finder",
        description="A local dashboard for complimentary food events at St. George clubs.",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.db_path = database
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.middleware("http")
    async def loopback_only(request: Request, call_next: Any):
        host = (request.headers.get("host", "").split(":", 1)[0]).strip("[]").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"detail": "This personal dashboard is available only on this computer."}, status_code=403)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin:
                try:
                    origin_host = (Request(scope=request.scope).url_for("index").hostname or "").lower()
                    requested = (origin.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]).lower()
                except Exception:
                    origin_host, requested = "", ""
                if requested and requested not in {"127.0.0.1", "localhost", origin_host}:
                    return JSONResponse({"detail": "Cross-origin requests are not accepted."}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse, name="index")
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={},
        )

    @app.get("/api/events")
    async def events(
        view: str = Query("upcoming", pattern="^(upcoming|review|today)$"),
        club: str = "",
        from_date: str = "",
        to_date: str = "",
        free_entry: bool = False,
        unrestricted_entry: bool = False,
    ):
        for value in (from_date, to_date):
            if value:
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError as exc:
                    raise HTTPException(422, "Dates must use YYYY-MM-DD.") from exc
        return list_events(
            view=view,
            club=club,
            from_date=from_date,
            to_date=to_date,
            free_entry=free_entry,
            unrestricted_entry=unrestricted_entry,
            db_path=database,
        )

    @app.get("/api/events/{event_id}")
    async def event_detail(event_id: str):
        result = get_event(event_id, database)
        if not result:
            raise HTTPException(404, "Event not found.")
        return result

    @app.patch("/api/events/{event_id}")
    async def event_review(event_id: str, patch: dict[str, Any]):
        try:
            result = patch_event(event_id, patch, database)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not result:
            raise HTTPException(404, "Event not found.")
        return result

    @app.get("/api/events/{event_id}/evidence")
    async def event_evidence(event_id: str):
        with connect(database) as db:
            row = db.execute(
                """SELECT s.media_blob FROM evidence ev JOIN sources s ON s.id=ev.source_id
                   WHERE ev.event_id=? AND s.media_blob IS NOT NULL LIMIT 1""",
                (event_id,),
            ).fetchone()
        if not row or not row["media_blob"]:
            raise HTTPException(404, "No saved Story image is available for this event.")
        return Response(content=bytes(row["media_blob"]), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})

    @app.get("/api/clubs")
    async def clubs():
        return list_clubs(database)

    @app.post("/api/clubs")
    async def create_club(payload: dict[str, Any]):
        try:
            return upsert_manual_club(
                name=str(payload.get("name", "")),
                username=str(payload.get("instagram_username", "")),
                website=str(payload.get("website", "")),
                db_path=database,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/clubs/{club_id}")
    async def edit_club(club_id: str, payload: dict[str, Any]):
        current = next((item for item in list_clubs(database) if item["id"] == club_id), None)
        if not current:
            raise HTTPException(404, "Club not found.")
        try:
            return upsert_manual_club(
                club_id=club_id,
                name=str(payload.get("name", current["name"])),
                username=str(payload.get("instagram_username", current["instagram_username"])),
                website=str(payload.get("website", current["website"])),
                active=bool(payload.get("active", current["active"])),
                db_path=database,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/scan")
    async def scan_status():
        return scan_progress(database)

    @app.post("/api/scan")
    async def request_scan(payload: dict[str, Any] | None = None):
        progress = scan_progress(database)
        if progress["running"]:
            return {"started": False, "message": "A scan is already running.", **progress}
        if _scan_cooldown_active(database):
            return {
                "started": False,
                "message": "The last scan was rate-limited or interrupted. Scan now is paused until the next scheduled scan window.",
                **progress,
            }
        launch_scan(database, force_discovery=bool((payload or {}).get("discover", False)))
        return {"started": True, "message": "Scan queued. The dashboard will update as accounts are checked."}

    return app


app = create_app()
