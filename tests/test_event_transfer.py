import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from foodfinder.database import connect, initialize
from foodfinder.event_transfer import FORMAT, export_events, import_events
from foodfinder.scanner import list_events, patch_event
from foodfinder.web import create_app


def fixture():
    return {"format": FORMAT, "version": 1, "events": [{
        "id": "test-event", "title": "Pizza & games 🍕", "event_date": "2099-09-30",
        "event_time": "17:30", "location": "Student centre", "food": "Pizza",
        "entry_cost": "Free entry", "restrictions": "RSVP required", "eligibility": "Open to everyone",
        "rsvp": "Register online", "food_confidence": "high", "review_reason": "", "status": "upcoming",
        "username": "campusclub", "club_names": "Campus Club", "source_kind": "post",
        "source_url": "https://www.instagram.com/p/example/", "caption": "Free pizza!\nBring a friend.",
        "media_text": "5:30 PM", "posted_at": "2099-09-29T12:00:00+00:00", "expires_at": "",
        "supporting_sources": [{"url": "https://www.instagram.com/p/example/", "kind": "post",
                                "username": "campusclub", "posted_at": "2099-09-29T12:00:00+00:00",
                                "expires_at": "", "excerpt": "Free pizza!"},
                               {"url": "https://www.instagram.com/stories/otherclub/123/", "kind": "story",
                                "username": "otherclub", "posted_at": "2099-09-29T13:00:00+00:00",
                                "expires_at": "2099-09-30T13:00:00+00:00", "excerpt": "Join us"}]
    }]}


class EventTransferTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "events.sqlite3"
        initialize(self.path)

    def test_round_trip_preserves_details_and_sources_without_private_data(self):
        payload = fixture()
        self.assertEqual(import_events(payload, self.path), {"imported": 1, "duplicates": 0, "past": 0})
        exported = export_events(self.path)
        # Evidence is ordered newest first by the dashboard.
        expected = payload["events"][0]
        actual = exported["events"][0]
        self.assertEqual({k: v for k, v in actual.items() if k != "supporting_sources"},
                         {k: v for k, v in expected.items() if k != "supporting_sources"})
        self.assertCountEqual(actual["supporting_sources"], expected["supporting_sources"])
        other = self.path.with_name("other.sqlite3")
        initialize(other)
        import_events(exported, other)
        self.assertEqual(export_events(other)["events"], exported["events"])
        with connect(other) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts WHERE enabled=1").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM clubs WHERE active=1").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT instagram_username FROM clubs").fetchone()[0], "campusclub")

    def test_repeat_import_does_not_overwrite_edits_or_restore_dismissed_events(self):
        payload = fixture()
        import_events(payload, self.path)
        patch_event("test-event", {"title": "My correction", "status": "dismissed"}, self.path)
        self.assertEqual(import_events(payload, self.path)["duplicates"], 1)
        with connect(self.path) as db:
            row = db.execute("SELECT title,status FROM events").fetchone()
            self.assertEqual(tuple(row), ("My correction", "dismissed"))

    def test_export_omits_local_media_and_review_events(self):
        import_events(fixture(), self.path)
        with connect(self.path) as db:
            db.execute("UPDATE sources SET media_path='private-local-path',media_blob=?", (b"private-image",))
            db.execute("UPDATE evidence SET image_path='private-local-path'")
        self.assertNotIn("private-", str(export_events(self.path)))
        patch_event("test-event", {"review_reason": "Confirm date"}, self.path)
        self.assertEqual(export_events(self.path)["events"], [])

    def test_duplicate_ids_in_one_file_are_skipped(self):
        payload = fixture()
        payload["events"].append(copy.deepcopy(payload["events"][0]))
        self.assertEqual(import_events(payload, self.path), {"imported": 1, "duplicates": 1, "past": 0})

    def test_invalid_later_record_rejects_entire_file(self):
        payload = fixture()
        bad = copy.deepcopy(payload["events"][0])
        bad.update(id="bad-event", event_date="2099-02-30")
        payload["events"].append(bad)
        with self.assertRaisesRegex(ValueError, "Event 2"):
            import_events(payload, self.path)
        with connect(self.path) as db:
            for table in ("events", "accounts", "sources", "clubs"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_past_events_skipped_and_empty_file_allowed(self):
        payload = fixture()
        payload["events"][0]["event_date"] = "2000-01-01"
        self.assertEqual(import_events(payload, self.path)["past"], 1)
        payload["events"] = []
        self.assertEqual(import_events(payload, self.path)["imported"], 0)
        self.assertEqual(export_events(self.path)["events"], [])

    def test_validation_rejects_unsafe_links_and_wrong_types(self):
        for key, value in (("source_url", "javascript:alert(1)"), ("title", {}),
                           ("event_time", "25:00"), ("supporting_sources", None),
                           ("username", "bad handle"), ("food_confidence", "certain")):
            with self.subTest(key=key):
                payload = fixture()
                payload["events"][0][key] = value
                with self.assertRaises(ValueError):
                    import_events(payload, self.path)
        payload = fixture()
        payload["events"][0]["supporting_sources"][0]["url"] = "data:text/html,test"
        with self.assertRaises(ValueError):
            import_events(payload, self.path)

    def test_import_preserves_existing_account_scan_settings(self):
        with connect(self.path) as db:
            db.execute("INSERT INTO accounts(id,username,enabled,updated_at) VALUES ('existing','campusclub',0,'')")
        import_events(fixture(), self.path)
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT enabled FROM accounts WHERE id='existing'").fetchone()[0], 0)

    def test_http_download_upload_and_invalid_file(self):
        with TestClient(create_app(db_path=self.path, start_scheduler=False), base_url="http://127.0.0.1") as client:
            response = client.post("/api/events/import", json=fixture())
            self.assertEqual(response.status_code, 200, response.text)
            response = client.get("/api/events/export")
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment; filename=", response.headers["content-disposition"])
            self.assertEqual(client.post("/api/events/import", content=response.content).json()["duplicates"], 1)
            self.assertEqual(client.post("/api/events/import", content=b"bad json").status_code, 422)
            self.assertEqual(client.post("/api/events/import", json={"version": 9}).status_code, 422)
            self.assertEqual(client.post("/api/events/import", content=b" " * (10 * 1024 * 1024 + 1)).status_code, 413)
            html = client.get("/").text
            self.assertIn('id="import-events"', html)
            self.assertIn('href="/api/events/export"', html)
        self.assertEqual(len(list_events(db_path=self.path)), 1)
