from __future__ import annotations

import tempfile
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from foodfinder.database import connect, initialize, link_club_account, upsert_club
from foodfinder.discovery import Club, fetch_sop_clubs, merge_directory_clubs, parse_sop_page
from foodfinder.events import extract_events
from foodfinder.instagram import MediaSource, _checkpoint_url, _fail_fast_on_429, _import_browser_session
from foodfinder.scanner import get_event, list_events, patch_event, process_source, refresh_directory, run_scan, upsert_manual_club


TORONTO = ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=TORONTO)
POSTED = datetime(2026, 9, 23, 12, 0, tzinfo=TORONTO)


class InstagramLoginTests(unittest.TestCase):
    def test_loader_treats_429_as_fatal_without_changing_other_retries(self):
        loader = SimpleNamespace(context=SimpleNamespace(fatal_status_codes=[401]))
        _fail_fast_on_429(loader)
        _fail_fast_on_429(loader)
        self.assertEqual(loader.context.fatal_status_codes, [401, 429])

    def test_checkpoint_path_is_completed_on_instagram_domain(self):
        url = _checkpoint_url(
            "Login: Checkpoint required. Point your browser to /auth_platform/?apc=temporary "
            "- follow the instructions, then retry."
        )
        self.assertEqual(url, "https://www.instagram.com/auth_platform/?apc=temporary")

    def test_checkpoint_url_cannot_redirect_to_another_site(self):
        url = _checkpoint_url(
            "Checkpoint required. Point your browser to https://example.com/steal - follow the instructions."
        )
        self.assertEqual(url, "https://www.instagram.com/")

    def test_browser_session_import_keeps_only_instagram_cookies(self):
        cookies = [
            SimpleNamespace(domain=".instagram.com", name="sessionid", value="local-test-cookie"),
            SimpleNamespace(domain="facebook.com", name="c_user", value="not-imported"),
        ]
        browser_cookie3 = SimpleNamespace(edge=lambda: cookies)

        class Context:
            def update_cookies(self, value):
                self.cookies = value

        context = Context()
        loader = SimpleNamespace(context=context, test_login=lambda: "uoftfoodscraper")
        with patch.dict(sys.modules, {"browser_cookie3": browser_cookie3}):
            username = _import_browser_session(loader, "edge")
        self.assertEqual(username, "uoftfoodscraper")
        self.assertEqual(context.cookies, {"sessionid": "local-test-cookie"})
        self.assertEqual(context.username, "uoftfoodscraper")

    def test_chrome_cookie_decryption_error_suggests_firefox(self):
        browser_cookie3 = SimpleNamespace(
            chrome=lambda: (_ for _ in ()).throw(RuntimeError("Unable to get key for cookie decryption"))
        )
        loader = SimpleNamespace(context=SimpleNamespace())
        with patch.dict(sys.modules, {"browser_cookie3": browser_cookie3}):
            with self.assertRaisesRegex(RuntimeError, "sign in to Instagram in Firefox"):
                _import_browser_session(loader, "chrome")


class DiscoveryTests(unittest.TestCase):
    def test_sop_campus_filter_and_stable_club_ids(self):
        html = """
        <main>
          <div class='card'><a href='/group/baking-club/'>University of Toronto Baking Club</a><span>St George</span></div>
          <div class='card'><a href='/group/utm-baking/'>Baking Club</a><span>UTM</span></div>
          <div class='card'><a href='/group/utsc-baking/'>Baking Club</a><span>UTSC</span></div>
          <div class='card'><a href='/group/baking-club/'>duplicate</a><span>St George</span></div>
        </main>
        """
        clubs = parse_sop_page(html)
        self.assertEqual([club.source_key for club in clubs], ["sop:baking-club"])
        self.assertEqual(clubs[0].campus, "St George")

    def test_directory_pagination_walks_all_linked_pages(self):
        pages = {
            "https://sop.utoronto.ca/groups/": "<a href='?pg=1'>1</a><a href='?pg=2'>2</a><div><a href='/group/first/'>First Club</a><span>St George</span></div>",
            "https://sop.utoronto.ca/groups/?pg=2": "<div><a href='/group/second/'>Second Club</a><span>St George</span></div><div><a href='/group/utm/'>UTM</a><span>UTM</span></div>",
        }
        fetched: list[str] = []

        def fake_fetch(url: str) -> str:
            fetched.append(url)
            return pages[url]

        clubs = fetch_sop_clubs(fake_fetch, enrich_profiles=False)
        self.assertEqual({club.name for club in clubs}, {"First Club", "Second Club"})
        self.assertEqual(len(fetched), 2)

    def test_myutsu_supplement_matches_normalized_names(self):
        primary = [Club("sop:baking", "University of Toronto Baking Club")]
        supplements = [Club("myutsu:12", "Baking Club at U of T", "St George", "https://myutsu.ca/groups/id/12/")]
        merged = merge_directory_clubs(primary, supplements)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source_key, "sop:baking")
        self.assertIn("myutsu.ca", merged[0].website)

    def test_changed_directory_markup_does_not_erase_or_hide_existing_clubs(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "directory.sqlite3"
            initialize(db_path)
            with connect(db_path) as db:
                upsert_club(db, source_key="sop:existing", name="Existing Club")
            with self.assertRaisesRegex(RuntimeError, "markup may have changed"):
                refresh_directory(
                    db_path,
                    sop_fetch=lambda _url: "<html><main>New unrecognized directory layout</main></html>",
                    utsu_fetch=lambda _url: "",
                    enrich_profiles=False,
                )
            with connect(db_path) as db:
                clubs = db.execute("SELECT name FROM clubs").fetchall()
            self.assertEqual([row["name"] for row in clubs], ["Existing Club"])


class EventExtractionTests(unittest.TestCase):
    def test_poster_dates_food_time_and_location(self):
        candidates = extract_events(
            "FALL CLUB SOCIAL\nFree pizza and lunch\nDate: September 27, 2026\nTime: 6 pm\nLocation: UC 161\nRSVP",
            posted_at=POSTED,
            account_name="bakingclub",
            now=NOW,
        )
        self.assertEqual(len(candidates), 1)
        event = candidates[0]
        self.assertEqual(event.title, "FALL CLUB SOCIAL")
        self.assertEqual(event.event_date, "2026-09-27")
        self.assertEqual(event.event_time, "18:00")
        self.assertEqual(event.location, "UC 161")
        self.assertEqual(event.food_confidence, "high")
        self.assertTrue(event.rsvp)
        self.assertEqual(event.review_reason, "")

    def test_free_food_with_paid_entry_shows_both(self):
        candidates = extract_events(
            "Networking mixer — complimentary snacks! September 30 2026. Tickets $8 at the door. RSVP required.",
            posted_at=POSTED,
            account_name="club",
            now=NOW,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].food_confidence, "high")
        self.assertIn("$8", candidates[0].entry_cost)
        self.assertNotEqual(candidates[0].entry_cost, "Free entry")

    def test_entry_and_access_filters_need_explicit_evidence(self):
        candidate = extract_events(
            "Cultural mixer! Free refreshments. September 30 2026. Everyone welcome, RSVP required.",
            posted_at=POSTED,
            account_name="club",
            now=NOW,
        )[0]
        self.assertEqual(candidate.entry_cost, "")
        self.assertEqual(candidate.eligibility, "Open to everyone")
        self.assertTrue(candidate.rsvp)

    def test_gluten_free_is_not_a_free_food_offer(self):
        candidates = extract_events(
            "GLUTEN-FREE PIZZA SOCIAL\nSaturday October 3, 2026\nJoin us for dietary options.",
            posted_at=POSTED,
            account_name="club",
            now=NOW,
        )
        self.assertEqual(candidates, [])

    def test_negated_offer_is_not_listed_as_free(self):
        candidates = extract_events(
            "Club pizza social — no free pizza. September 27 2026. RSVP for the event.",
            posted_at=POSTED,
            account_name="club",
            now=NOW,
        )
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].food, "")
        self.assertTrue(candidates[0].review_reason)

    def test_relative_day_is_based_on_the_source_post(self):
        candidates = extract_events(
            "Community event tomorrow: free food provided!",
            posted_at=POSTED,
            account_name="club",
            now=NOW,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].event_date, "2026-09-24")
        self.assertTrue(candidates[0].review_reason)

    def test_date_without_a_known_time_does_not_guess_one(self):
        candidates = extract_events(
            "Join us for the free breakfast event September 27, 2026.",
            posted_at=POSTED,
            account_name="club",
            now=NOW,
        )
        self.assertEqual(candidates[0].event_time, "")
        self.assertEqual(candidates[0].review_reason, "")

    def test_past_announcement_is_reviewed_without_rollover(self):
        candidates = extract_events(
            "Free pizza workshop October 2.",
            posted_at=datetime(2025, 9, 21, tzinfo=TORONTO),
            account_name="club",
            now=NOW,
        )
        self.assertEqual(candidates[0].event_date, "2025-10-02")
        self.assertIn("past", candidates[0].review_reason)

    def test_story_today_without_a_time_is_flagged(self):
        candidates = extract_events(
            "Come by today for complimentary lunch at our campus event!",
            posted_at=NOW,
            account_name="club",
            now=NOW,
        )
        self.assertEqual(candidates[0].event_date, "2026-09-24")
        self.assertEqual(candidates[0].event_time, "")
        self.assertIn("today", candidates[0].review_reason)

    def test_cancellation_text_is_not_resurfaced_as_an_upcoming_food_event(self):
        candidates = extract_events(
            "Baking social has been cancelled. September 27, 2026. Free pizza was planned.",
            posted_at=POSTED,
            account_name="club",
            now=NOW,
        )
        self.assertEqual(candidates[0].title, "Baking social")
        self.assertEqual(candidates[0].status, "cancelled")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite3"
        initialize(self.db_path)
        with connect(self.db_path) as db:
            self.club_id = upsert_club(db, source_key="manual:test", name="Campus Baking Club")
            self.account_id = link_club_account(db, self.club_id, "bakingclub")

    def tearDown(self):
        self.temp.cleanup()

    def test_manual_correction_survives_rescan(self):
        source = MediaSource(
            "post:abc123", "post", "https://www.instagram.com/p/abc123/",
            "Baking social! Free pizza. September 27 2026.", "", POSTED.isoformat(),
        )
        process_source(self.db_path, self.account_id, "bakingclub", source)
        initial = list_events(db_path=self.db_path)
        self.assertEqual(len(initial), 1)
        patch_event(initial[0]["id"], {"location": "Sidney Smith 213", "review_reason": ""}, self.db_path)
        process_source(self.db_path, self.account_id, "bakingclub", source)
        updated = get_event(initial[0]["id"], self.db_path)
        self.assertEqual(updated["location"], "Sidney Smith 213")
        self.assertEqual(updated["review_reason"], "")

    def test_duplicate_announcements_keep_both_sources(self):
        for shortcode in ("abc123", "def456"):
            source = MediaSource(
                f"post:{shortcode}", "post", f"https://www.instagram.com/p/{shortcode}/",
                "Baking social! Free pizza. September 27 2026.", "", POSTED.isoformat(),
            )
            process_source(self.db_path, self.account_id, "bakingclub", source)
        events = list_events(db_path=self.db_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["supporting_sources"]), 2)

    def test_changing_a_post_moves_old_offer_to_review(self):
        source = MediaSource(
            "post:abc123", "post", "https://www.instagram.com/p/abc123/",
            "Baking social! Free pizza. September 27 2026.", "", POSTED.isoformat(),
        )
        process_source(self.db_path, self.account_id, "bakingclub", source)
        edited = MediaSource(
            "post:abc123", "post", "https://www.instagram.com/p/abc123/",
            "Baking social! September 27 2026. Food will not be served.", "", POSTED.isoformat(),
        )
        process_source(self.db_path, self.account_id, "bakingclub", edited)
        self.assertEqual(list_events(db_path=self.db_path), [])
        review = list_events(view="review", db_path=self.db_path)
        self.assertEqual(len(review), 1)
        self.assertIn("post changed", review[0]["review_reason"])

    def test_clear_cancellation_updates_the_existing_event(self):
        announced = MediaSource(
            "post:announce", "post", "https://www.instagram.com/p/announce/",
            "Baking social\nFree pizza\nSeptember 27, 2026.", "", POSTED.isoformat(),
        )
        cancelled = MediaSource(
            "post:cancel", "post", "https://www.instagram.com/p/cancel/",
            "Baking social has been cancelled. September 27, 2026. Free pizza was planned.", "", POSTED.isoformat(),
        )
        process_source(self.db_path, self.account_id, "bakingclub", announced)
        process_source(self.db_path, self.account_id, "bakingclub", cancelled)
        events = list_events(db_path=self.db_path)
        self.assertEqual(events, [])
        with connect(self.db_path) as db:
            row = db.execute("SELECT status FROM events WHERE title='Baking social'").fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_entry_filters_separate_free_paid_and_unspecified(self):
        sources = [
            ("unknown", "Baking social! Free pizza. September 27 2026."),
            ("free", "Coffee meetup! Free donuts. Free entry. September 28 2026. Open to everyone."),
        ]
        for shortcode, caption in sources:
            source = MediaSource(
                f"post:{shortcode}", "post", f"https://www.instagram.com/p/{shortcode}/",
                caption, "", POSTED.isoformat(),
            )
            process_source(self.db_path, self.account_id, "bakingclub", source)
        self.assertEqual(len(list_events(db_path=self.db_path)), 2)
        free_entries = list_events(free_entry=True, db_path=self.db_path)
        self.assertEqual(len(free_entries), 1)
        self.assertEqual(free_entries[0]["entry_cost"], "Free entry")
        unrestricted = list_events(unrestricted_entry=True, db_path=self.db_path)
        self.assertEqual(len(unrestricted), 1)
        self.assertEqual(unrestricted[0]["eligibility"], "Open to everyone")

    def test_manual_club_can_be_created_updated_and_paused(self):
        club = upsert_manual_club(name="Chemistry Club", username="ChemUofT", db_path=self.db_path)
        self.assertEqual(club["instagram_username"], "chemuoft")
        updated = upsert_manual_club(
            club_id=club["id"], name="Chemistry Club", username="ChemUofT", active=False, db_path=self.db_path
        )
        self.assertEqual(updated["active"], 0)
        with self.assertRaises(ValueError):
            upsert_manual_club(name="Invalid Handle", username="not a valid handle", db_path=self.db_path)

    def test_duplicate_club_handles_share_one_instagram_account(self):
        with connect(self.db_path) as db:
            upsert_club(db, source_key="manual:duplicate", name="Another Club", username="@bakingclub")
            count = db.execute("SELECT COUNT(*) AS n FROM accounts WHERE username='bakingclub'").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_login_challenge_pauses_the_account_queue(self):
        with connect(self.db_path) as db:
            second_club = upsert_club(db, source_key="manual:second", name="Second Campus Club")
            link_club_account(db, second_club, "secondclub")
            db.execute(
                "INSERT INTO kv(key,value) VALUES ('last_discovery',?)",
                (datetime.now().astimezone().isoformat(),),
            )

        challenge = type("LoginRequiredException", (Exception,), {})
        with patch("foodfinder.scanner._make_loader", return_value=(object(), "loggedin")), patch(
            "foodfinder.scanner.iter_account_sources", side_effect=challenge("verify login")
        ) as collect:
            run_scan(self.db_path)

        self.assertEqual(collect.call_count, 1)
        with connect(self.db_path) as db:
            run = db.execute("SELECT status,accounts_total,accounts_checked,message FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["accounts_total"], 2)
        self.assertEqual(run["accounts_checked"], 1)
        self.assertIn("paused because Instagram requires login verification", run["message"])

    def test_wrapped_instagram_429_pauses_the_account_queue(self):
        with connect(self.db_path) as db:
            second_club = upsert_club(db, source_key="manual:second", name="Second Campus Club")
            link_club_account(db, second_club, "secondclub")
            db.execute(
                "INSERT INTO kv(key,value) VALUES ('last_discovery',?)",
                (datetime.now().astimezone().isoformat(),),
            )

        # Instaloader 4.15.3 wraps the exhausted 429 retry in ConnectionException.
        connection_error = type("ConnectionException", (Exception,), {})
        with patch("foodfinder.scanner._make_loader", return_value=(object(), "loggedin")), patch(
            "foodfinder.scanner.iter_account_sources",
            side_effect=connection_error("JSON Query: 429 Too Many Requests"),
        ) as collect:
            run_scan(self.db_path)

        self.assertEqual(collect.call_count, 1)
        with connect(self.db_path) as db:
            run = db.execute(
                "SELECT status,accounts_total,accounts_checked,message FROM scan_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["accounts_total"], 2)
        self.assertEqual(run["accounts_checked"], 1)
        self.assertIn("returned HTTP 429", run["message"])


if __name__ == "__main__":
    unittest.main()
