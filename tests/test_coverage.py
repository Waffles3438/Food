import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foodfinder.database import connect, initialize
from foodfinder.scanner import run_scan, scan_progress, upsert_manual_club


class CoverageTests(unittest.TestCase):
    def test_successful_empty_account_stays_checked_after_restriction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.sqlite3"
            initialize(path)
            upsert_manual_club(name="Test club", username="testclub", db_path=path)
            with patch("foodfinder.scanner.due_for_discovery", return_value=False), patch(
                "foodfinder.scanner._make_loader", return_value=(object(), "test")
            ), patch("foodfinder.scanner.iter_account_sources", return_value=iter(())):
                run_scan(path)
            self.assertEqual(scan_progress(path)["accounts_checked"], 1)
            with patch("foodfinder.scanner.due_for_discovery", return_value=False), patch(
                "foodfinder.scanner._make_loader", return_value=(object(), "test")
            ), patch("foodfinder.scanner.iter_account_sources", side_effect=RuntimeError("401 Unauthorized: please wait")):
                run_scan(path)
            initialize(path)
            progress = scan_progress(path)
            self.assertEqual(progress["accounts_checked"], 1)
            self.assertEqual(progress["accounts"], {"error": 1})
            self.assertTrue(progress["retry"]["cooldown"])

    def test_legacy_successful_accounts_are_backfilled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.sqlite3"
            initialize(path)
            upsert_manual_club(name="Test club", username="testclub", db_path=path)
            with connect(path) as db:
                db.execute("ALTER TABLE accounts DROP COLUMN ever_checked")
                db.execute("UPDATE accounts SET status='ok'")
            initialize(path)
            initialize(path)
            self.assertEqual(scan_progress(path)["accounts_checked"], 1)
