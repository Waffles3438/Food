import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foodfinder.database import connect, initialize, link_club_account, upsert_club, utcnow
from foodfinder.instagram import MediaSource
from foodfinder.progress import ScanProgress, configure_progress_logging, report
from foodfinder.scanner import run_scan


class ProgressTests(unittest.TestCase):
    def test_quiet_operation_reports_elapsed_without_claiming_success(self):
        progress = ScanProgress(interval=30)
        with patch("foodfinder.progress.time.monotonic", return_value=100):
            progress.note("Requesting Instagram timeline for @fixture")
        with self.assertLogs("foodfinder.progress", level="INFO") as output, patch(
            "foodfinder.progress.time.monotonic", return_value=165
        ):
            progress._heartbeat()
        self.assertIn("65s without a new update", output.output[0])
        self.assertIn("Requesting Instagram timeline for @fixture", output.output[0])

    def test_heartbeat_thread_stops_when_scan_ends(self):
        progress = ScanProgress()
        progress.start()
        try:
            report("Processing image 1/2")
            self.assertEqual(progress._message, "Processing image 1/2")
        finally:
            progress.close()
        self.assertFalse(progress._thread.is_alive())
        report("After scan")
        self.assertEqual(progress._message, "Processing image 1/2")

    def test_terminal_handler_is_timestamped_and_not_duplicated(self):
        logger = logging.getLogger("foodfinder")
        level, propagate = logger.level, logger.propagate
        stream = io.StringIO()
        try:
            with patch.object(logger, "handlers", []), patch("sys.stdout", stream):
                configure_progress_logging()
                configure_progress_logging()
                report("Account 1/2: @fixture - starting.")
                self.assertEqual(len(logger.handlers), 1)
        finally:
            logger.setLevel(level)
            logger.propagate = propagate
        self.assertRegex(stream.getvalue(), r"^\[\d{2}:\d{2}:\d{2}\] Account 1/2:")
        self.assertEqual(len(stream.getvalue().splitlines()), 1)

    def test_scan_logs_accounts_and_posts_and_saves_progress_mid_account(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.sqlite3"
            initialize(path)
            with connect(path) as db:
                club = upsert_club(db, source_key="manual:fixture", name="Fixture")
                link_club_account(db, club, "fixture")
                db.execute("INSERT INTO kv(key,value) VALUES ('last_discovery', ?)", (utcnow(),))

            def sources(*args, **kwargs):
                yield MediaSource("post:one", "post", "https://www.instagram.com/p/one/",
                                  "Caption text must not appear in logs", "", utcnow())
                with connect(path) as db:
                    row = db.execute("SELECT posts_seen,accounts_checked,message FROM scan_runs").fetchone()
                    self.assertEqual(row["posts_seen"], 1)
                    self.assertEqual(row["accounts_checked"], 0)
                    self.assertEqual(row["message"], "Checking @fixture")
                raise ValueError("An exception might contain sensitive data")

            with patch("foodfinder.scanner._make_loader", return_value=(object(), "fixture")), patch(
                "foodfinder.scanner.iter_account_sources", side_effect=sources
            ), self.assertLogs("foodfinder.progress", level="INFO") as output:
                run_id = run_scan(path)
            text = "\n".join(output.output)
            self.assertIn("Account 1/1: @fixture - starting", text)
            self.assertIn("saved post post:one", text)
            self.assertIn("Scan partial: 1/1 accounts checked, 1 posts", text)
            self.assertNotIn("Caption text must not appear", text)
            self.assertNotIn("An exception might contain", text)
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT status FROM scan_runs WHERE id=?", (run_id,)).fetchone()[0], "partial")
