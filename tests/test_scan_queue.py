import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foodfinder.database import connect, initialize
from foodfinder.scanner import list_clubs, remove_club, run_scan, upsert_manual_club


class ScanQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "queue.sqlite3"
        initialize(self.path)
        for username in ("alpha", "bravo", "charlie"):
            self.add_account(username)

    def add_account(self, username):
        upsert_manual_club(name=username, username=username, db_path=self.path)

    def scan(self, collect):
        with patch("foodfinder.scanner.due_for_discovery", return_value=False), patch(
            "foodfinder.scanner._make_loader", return_value=(object(), "test")
        ), patch("foodfinder.scanner.iter_account_sources", side_effect=collect):
            return run_scan(self.path)

    def test_pauses_and_restarts_finish_pass_before_repeating_accounts(self):
        seen = []

        def restricted(username, **kwargs):
            seen.append(username)
            raise RuntimeError("401 Unauthorized: please wait")

        for _ in range(3):
            self.scan(restricted)
            initialize(self.path)
        self.assertEqual(seen, ["alpha", "bravo", "charlie"])
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT value FROM kv WHERE key='instagram_scan_pass'").fetchone()[0], "1")
        self.scan(restricted)
        self.assertEqual(seen[-1], "alpha")
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT value FROM kv WHERE key='instagram_scan_pass'").fetchone()[0], "2")

    def test_successful_accounts_are_not_revisited_in_unfinished_pass(self):
        seen = []

        def collect(username, **kwargs):
            seen.append(username)
            if username == "bravo":
                raise RuntimeError("401 Unauthorized: please wait")
            return iter(())

        self.scan(collect)
        self.assertEqual(seen, ["alpha", "bravo"])
        self.add_account("delta")
        initialize(self.path)
        self.scan(collect)
        self.assertEqual(seen, ["alpha", "bravo", "charlie", "delta"])

    def test_initial_pass_prioritizes_unchecked_accounts_over_saved_successes(self):
        with connect(self.path) as db:
            db.execute("UPDATE accounts SET ever_checked=1 WHERE username='alpha'")
            db.execute("ALTER TABLE accounts DROP COLUMN last_scan_pass")
        initialize(self.path)
        seen = []
        self.scan(lambda username, **kwargs: (seen.append(username), iter(()))[1])
        self.assertEqual(seen, ["bravo", "charlie"])
        seen.clear()
        self.scan(lambda username, **kwargs: (seen.append(username), iter(()))[1])
        self.assertCountEqual(seen, ["alpha", "bravo", "charlie"])

    def test_missing_login_does_not_consume_account_turn(self):
        with patch("foodfinder.scanner.due_for_discovery", return_value=False), patch(
            "foodfinder.scanner._make_loader", side_effect=RuntimeError("No saved session")
        ), patch("foodfinder.scanner.iter_account_sources") as collect:
            run_scan(self.path)
            collect.assert_not_called()
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT SUM(last_scan_pass) FROM accounts").fetchone()[0], 0)

    def test_removal_skips_an_account_already_queued(self):
        bravo = next(club for club in list_clubs(self.path) if club["instagram_username"] == "bravo")
        seen = []

        def collect(username, **kwargs):
            seen.append(username)
            if username == "alpha":
                remove_club(bravo["id"], self.path)
            return iter(())

        run = self.scan(collect)
        self.assertEqual(seen, ["alpha", "charlie"])
        with connect(self.path) as db:
            saved = db.execute("SELECT accounts_total,accounts_checked FROM scan_runs WHERE id=?", (run,)).fetchone()
            self.assertEqual(tuple(saved), (2, 2))
        seen.clear()
        self.scan(lambda username, **kwargs: (seen.append(username), iter(()))[1])
        self.assertEqual(seen, ["alpha", "charlie"])

    def test_paused_accounts_do_not_hold_up_pass(self):
        seen = []
        with connect(self.path) as db:
            db.execute("UPDATE clubs SET active=0 WHERE instagram_username='charlie'")
        self.scan(lambda username, **kwargs: (seen.append(username), iter(()))[1])
        self.assertEqual(seen, ["alpha", "bravo"])
        seen.clear()
        self.scan(lambda username, **kwargs: (seen.append(username), iter(()))[1])
        self.assertEqual(seen, ["alpha", "bravo"])
