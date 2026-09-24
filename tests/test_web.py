from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - enabled after setup.ps1 installs runtime dependencies
    TestClient = None

from foodfinder.database import connect, initialize, link_club_account, upsert_club, utcnow
from foodfinder.instagram import MediaSource
from foodfinder.scanner import process_source
from foodfinder.web import _recover_orphaned_run, _scheduler


@unittest.skipIf(TestClient is None, "Install the pinned FastAPI dependencies to run the web tests.")
class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "web.sqlite3"
        initialize(self.db_path)
        with connect(self.db_path) as db:
            self.club_id = upsert_club(db, source_key="manual:fixture", name="Test Food Club")
            self.account_id = link_club_account(db, self.club_id, "testfoodclub")
        process_source(
            self.db_path,
            self.account_id,
            "testfoodclub",
            MediaSource(
                "post:fixture", "post", "https://www.instagram.com/p/fixture/",
                "Open social! Complimentary pizza. September 27 2026. Open to everyone.",
                "", datetime(2026, 9, 23, 12).isoformat(),
            ),
        )
        from foodfinder.web import create_app

        self.client_context = TestClient(create_app(db_path=self.db_path, start_scheduler=False), base_url="http://127.0.0.1")
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_dashboard_and_filtered_event_json(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Food on the horizon", page.text)
        response = self.client.get("/api/events?unrestricted_entry=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["eligibility"], "Open to everyone")

    def test_club_management_and_event_review(self):
        added = self.client.post("/api/clubs", json={"name": "New Club", "instagram_username": "newclub"})
        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.json()["instagram_username"], "newclub")
        paused = self.client.patch(f"/api/clubs/{added.json()['id']}", json={"active": False})
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["active"], 0)

        event = self.client.get("/api/events").json()[0]
        corrected = self.client.patch(f"/api/events/{event['id']}", json={"location": "Sidney Smith Hall"})
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(corrected.json()["location"], "Sidney Smith Hall")

    def test_scan_action_queues_a_single_background_scan(self):
        with patch("foodfinder.web.launch_scan") as launch:
            response = self.client.post("/api/scan", json={"discover": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["started"])
        launch.assert_called_once_with(str(self.db_path), force_discovery=True)

    def test_scan_now_is_blocked_during_recent_instagram_rate_limit(self):
        now = utcnow()
        with connect(self.db_path) as db:
            db.execute(
                """INSERT INTO scan_runs(id,kind,status,started_at,finished_at,message)
                   VALUES ('rate-limited','instagram','partial',?,?,?)""",
                (now, now, "Scan paused after Instagram returned HTTP 429."),
            )
        with patch("foodfinder.web.launch_scan") as launch:
            response = self.client.post("/api/scan", json={})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["started"])
        self.assertIn("until the next scheduled scan window", response.json()["message"])
        launch.assert_not_called()

    def test_scan_now_is_blocked_after_interrupted_run_recovery(self):
        now = utcnow()
        with connect(self.db_path) as db:
            db.execute(
                """INSERT INTO scan_runs(id,kind,status,started_at,finished_at,message)
                   VALUES ('interrupted','instagram','partial',?,?,?)""",
                (now, now, "Previous app session ended during this scan; remaining accounts are deferred to the next scheduled scan."),
            )
        with patch("foodfinder.web.launch_scan") as launch:
            response = self.client.post("/api/scan", json={})
        self.assertFalse(response.json()["started"])
        self.assertIn("until the next scheduled scan window", response.json()["message"])
        launch.assert_not_called()

    def test_app_rejects_non_loopback_hosts(self):
        other = TestClient(self.client.app, base_url="http://attacker.example")
        response = other.get("/api/clubs")
        self.assertEqual(response.status_code, 403)


class SchedulerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_scan_waits_for_next_scheduled_window(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "recovery.sqlite3"
            initialize(db_path)
            with connect(db_path) as db:
                db.execute(
                    "INSERT INTO scan_runs(id,kind,status,started_at,message) VALUES ('interrupted','full','running',?,'Scanning')",
                    (utcnow(),),
                )
            _recover_orphaned_run(db_path)
            app = SimpleNamespace(state=SimpleNamespace(db_path=str(db_path)))
            with patch("foodfinder.web.launch_scan") as launch, patch(
                "foodfinder.web.asyncio.sleep", side_effect=asyncio.CancelledError
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await _scheduler(app)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
