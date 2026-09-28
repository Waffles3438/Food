import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from foodfinder.database import connect, initialize
from foodfinder.instagram import collection_error_detail
from foodfinder.retry import retry_plan
from foodfinder.web import _scheduler


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite3"
        initialize(self.path)
        self.count = 0

    def add(self, kind, *, status="partial", message=""):
        self.count += 1
        ended = NOW + timedelta(minutes=self.count)
        with connect(self.path) as db:
            db.execute("INSERT INTO scan_runs(id,kind,status,started_at,finished_at,failure_kind,message) VALUES (?,'instagram',?,?,?,?,?)",
                       (str(self.count), status, ended.isoformat(), ended.isoformat(), kind, message))
        return ended

    def test_transient_backoff_survives_rereads_and_success_resets_it(self):
        for delay in (5, 15, 30, 360, 360):
            ended = self.add("connection")
            plan = retry_plan(self.path, now=ended)
            self.assertEqual(datetime.fromisoformat(plan["next_scan_at"]), ended + timedelta(minutes=delay))
            self.assertTrue(plan["cooldown"])
            self.assertEqual(retry_plan(self.path, now=ended), plan)
            self.assertFalse(retry_plan(self.path, now=ended + timedelta(minutes=delay))["cooldown"])
        self.add("", status="complete")
        ended = self.add("response")
        self.assertEqual(datetime.fromisoformat(retry_plan(self.path)["next_scan_at"]), ended + timedelta(minutes=5))

    def test_access_restrictions_do_not_use_fast_retry(self):
        for kind in ("rate_limit", "temporary_limit", "access", "interrupted"):
            ended = self.add(kind)
            self.assertEqual(datetime.fromisoformat(retry_plan(self.path)["next_scan_at"]), ended + timedelta(hours=6))

    def test_authentication_requires_action_and_running_scan_has_no_retry(self):
        self.add("authentication")
        self.assertTrue(retry_plan(self.path)["requires_login"])
        self.assertEqual(retry_plan(self.path)["next_scan_at"], "")
        self.add("", status="running")
        self.assertEqual(retry_plan(self.path)["next_scan_at"], "")

    def test_safe_error_details_do_not_include_raw_response_or_session(self):
        error = RuntimeError("403 Forbidden response contains sessionid=private-cookie")
        self.assertEqual(collection_error_detail(error), "RuntimeError, HTTP 403")

    def test_old_database_gets_error_columns_without_losing_history(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE scan_runs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,started_at TEXT,finished_at TEXT,accounts_total INTEGER,accounts_checked INTEGER,posts_seen INTEGER,events_found INTEGER,message TEXT)")
            db.execute("INSERT INTO scan_runs(id,message) VALUES ('old','kept')")
        initialize(path)
        initialize(path)
        with connect(path) as db:
            row = db.execute("SELECT message,failure_kind,error_detail FROM scan_runs WHERE id='old'").fetchone()
        self.assertEqual(tuple(row), ("kept", "", ""))


class AutomaticRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_retries_due_response_error_without_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.sqlite3"
            initialize(path)
            finished = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
            with connect(path) as db:
                db.execute("INSERT INTO scan_runs(id,kind,status,started_at,finished_at,failure_kind) VALUES ('failed','instagram','partial',?,?,'response')", (finished, finished))
            app = SimpleNamespace(state=SimpleNamespace(db_path=str(path)))
            with patch("foodfinder.web.launch_scan") as launch, patch("foodfinder.web.asyncio.sleep", side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await _scheduler(app)
            launch.assert_called_once_with(str(path))

    async def test_scheduler_does_not_retry_early_or_while_authentication_is_blocked(self):
        for kind, age in (("response", 1), ("authentication", 1000), ("rate_limit", 6)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "test.sqlite3"
                initialize(path)
                finished = (datetime.now(timezone.utc) - timedelta(minutes=age)).isoformat()
                with connect(path) as db:
                    db.execute("INSERT INTO scan_runs(id,kind,status,started_at,finished_at,failure_kind) VALUES ('failed','instagram','partial',?,?,?)", (finished, finished, kind))
                app = SimpleNamespace(state=SimpleNamespace(db_path=str(path)))
                with patch("foodfinder.web.launch_scan") as launch, patch("foodfinder.web.asyncio.sleep", side_effect=asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await _scheduler(app)
                launch.assert_not_called()
