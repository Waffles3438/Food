"""Exercise the installed Instaloader with synthetic responses; never contact Instagram."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import instaloader
import requests
from fixed_clock import EventFixtureDateTime

from foodfinder.instagram import (
    InstagramResponseError, TIMELINE_DOC_ID, _download_post_images, _make_loader,
    collection_error_detail, collection_failure_kind, iter_account_sources,
    verify_session, interactive_login,
)
from foodfinder.events import OCRUnavailableError, ocr_images

NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)


def media(code="fixture", *, username="club", posted_at=NOW):
    return {
        "code": code, "pk": "101", "media_type": 1,
        "taken_at": int(posted_at.timestamp()), "caption": {"text": "Free pizza tomorrow!"},
        "has_liked": False, "like_count": 0,
        "image_versions2": {"candidates": [{"url": "https://example.invalid/image.jpg"}]},
        "user": {"pk": 42, "username": username, "is_private": False,
                 "full_name": "Fixture Club", "profile_pic_url": "https://example.invalid/avatar.jpg"},
    }


def page(nodes, *, has_next=False, cursor=None):
    return {"status": "ok", "data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
        "edges": [{"node": node} for node in nodes],
        "page_info": {"has_next_page": has_next, "end_cursor": cursor},
    }}}


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.http = self.enterContext(patch(
            "requests.sessions.Session.request", side_effect=AssertionError("Live network forbidden in tests")
        ))
        self.loader = instaloader.Instaloader(
            sleep=False, quiet=True, max_connection_attempts=1, request_timeout=30,
            fatal_status_codes=[401, 403, 429], iphone_support=False,
        )
        self.loader.context.load_session("fixtureuser", {
            "sessionid": "fixture-session", "csrftoken": "fixture-csrf", "ds_user_id": "999",
        })
        self.addCleanup(self.loader.close)
        self.profile_lookup = self.enterContext(patch.object(
            instaloader.Profile, "from_username", side_effect=AssertionError("Unexpected profile lookup")
        ))
        self.download = self.enterContext(patch("foodfinder.instagram._download_post_images", return_value=["fixture.jpg"]))
        self.enterContext(patch("foodfinder.instagram.ocr_images", return_value=""))
        self.stories = self.enterContext(patch.object(self.loader, "get_stories", return_value=[]))

    def sources(self, **kwargs):
        return list(iter_account_sources("@CLUB", loader=self.loader, now=NOW, **kwargs))

    def test_complete_caption_and_irrelevant_post_skip_download_and_ocr(self):
        for caption in ("Meet our new executive team!", "Free cookies! September 27, 2026 at 6 PM.\nLocation: Student Centre"):
            with self.subTest(caption=caption):
                node = media()
                node["caption"]["text"] = caption
                with patch("foodfinder.events.datetime", EventFixtureDateTime), patch.object(
                    self.loader.context, "doc_id_graphql_query", return_value=page([node])
                ), patch("foodfinder.instagram.ocr_images") as ocr:
                    source = self.sources(limit=1, include_stories=False)[0]
                self.assertEqual(source.caption, caption)
                self.assertEqual(source.media_text, "")
                self.assertEqual(source.collection_warning, "")
                self.download.assert_not_called()
                ocr.assert_not_called()

    def test_incomplete_food_caption_collects_poster_text(self):
        node = media()
        node["caption"]["text"] = "Free cookies!"
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([node])), patch(
            "foodfinder.instagram.ocr_images", return_value="September 27, 2026 at 6 PM. Location: Student Centre"
        ) as ocr:
            source = self.sources(limit=1, include_stories=False)[0]
        self.download.assert_called_once()
        ocr.assert_called_once()
        self.assertIn("Student Centre", source.media_text)

    def test_cached_post_skips_media_but_does_not_hide_newer_post_after_it(self):
        cached = {"post:fixture": {"caption": "Free pizza tomorrow!", "cache_version": 1,
                                   "media_text": "Poster text", "ocr_complete": 1}}
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media(), media("new")])):
            sources = self.sources(saved_sources=cached, include_stories=False)
        self.assertEqual([source.source_key for source in sources], ["post:new"])
        self.download.assert_called_once()

    def test_completed_post_with_edited_caption_is_not_scraped_again(self):
        cached = {"post:fixture": {"caption": "Free cookies tomorrow!", "cache_version": 1,
                                   "media_text": "Student Centre, 6 PM", "ocr_complete": 1}}
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])), patch(
            "foodfinder.instagram.ocr_images"
        ) as ocr:
            sources = self.sources(saved_sources=cached, include_stories=False)
        self.assertEqual(sources, [])
        self.download.assert_not_called()
        ocr.assert_not_called()

    def test_failed_or_legacy_sources_are_retried(self):
        for version in (0, None):
            self.download.reset_mock()
            cached = {"post:fixture": {"caption": "Free pizza tomorrow!", "cache_version": version}}
            with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])):
                sources = self.sources(saved_sources=cached, include_stories=False)
            self.assertEqual(len(sources), 1)
            self.download.assert_called_once()

    def test_cached_post_does_not_read_caption_and_keeps_story_account_id(self):
        class SavedPost:
            shortcode = "fixture"

            @property
            def caption(self):
                raise AssertionError("Completed posts must be skipped before reading captions")

        with patch("foodfinder.instagram._timeline_posts", return_value=iter([(SavedPost(), 42)])):
            self.assertEqual(self.sources(saved_sources={"post:fixture": {"cache_version": 1}}), [])
        self.stories.assert_called_once_with(userids=[42])
        self.download.assert_not_called()

    def test_saved_post_history_survives_restart_and_new_posts_are_collected(self):
        from foodfinder.database import connect, initialize
        from foodfinder.scanner import run_scan, upsert_manual_club

        original = media()
        original["caption"]["text"] = "Free cookies! September 27, 2026 at 6 PM.\nLocation: Student Centre"
        edited = media()
        edited["caption"]["text"] = "A different caption on the same post."
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "posts.sqlite3"
            initialize(path)
            upsert_manual_club(name="Fixture Club", username="club", db_path=path)
            with patch("foodfinder.scanner.due_for_discovery", return_value=False), patch(
                "foodfinder.scanner._make_loader", return_value=(self.loader, "fixtureuser")
            ), patch("foodfinder.events.datetime", EventFixtureDateTime), patch(
                "foodfinder.scanner.datetime", EventFixtureDateTime
            ), patch.object(self.loader.context, "doc_id_graphql_query", side_effect=[
                page([original]), page([edited, media("new")])
            ]):
                run_scan(path)
                initialize(path)
                second = run_scan(path)
            with connect(path) as db:
                saved = db.execute("SELECT source_key,caption FROM sources ORDER BY source_key").fetchall()
                self.assertEqual([row["source_key"] for row in saved], ["post:fixture", "post:new"])
                self.assertEqual(saved[0]["caption"], original["caption"]["text"])
                self.assertEqual(db.execute("SELECT posts_seen FROM scan_runs WHERE id=?", (second,)).fetchone()[0], 1)
            self.download.assert_called_once()

    def test_empty_media_download_is_not_marked_successful_for_caching(self):
        self.download.return_value = []
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])):
            source = self.sources(include_stories=False)[0]
        self.assertTrue(source.collection_warning)
        self.assertFalse(source.ocr_complete)

    def test_caption_edit_does_not_download_a_completed_caption_only_post(self):
        cached = {"post:fixture": {"caption": "Meet the team!", "cache_version": 1,
                                   "media_text": "", "ocr_complete": 0}}
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])):
            sources = self.sources(saved_sources=cached, include_stories=False)
        self.assertEqual(sources, [])
        self.download.assert_not_called()

    def test_successfully_cached_story_skips_download(self):
        item = SimpleNamespace(mediaid=123, caption="")
        self.stories.return_value = [SimpleNamespace(get_items=lambda: iter([item]))]
        cached = {"story:123": {"caption": "", "cache_version": 1}}
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])), patch.object(
            self.loader, "download_storyitem"
        ) as download_story:
            sources = self.sources(saved_sources=cached)
        self.assertEqual([source.kind for source in sources], ["post"])
        download_story.assert_not_called()

    def test_cached_story_with_edited_caption_is_skipped(self):
        item = SimpleNamespace(mediaid=123, caption="An updated caption")
        self.stories.return_value = [SimpleNamespace(get_items=lambda: iter([item]))]
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])), patch.object(
            self.loader, "download_storyitem"
        ) as download_story:
            sources = self.sources(saved_sources={"post:fixture": {"cache_version": 1},
                                                  "story:123": {"caption": "", "cache_version": 1}})
        self.assertEqual(sources, [])
        download_story.assert_not_called()

    def test_one_post_uses_authenticated_timeline_without_profile_or_stories(self):
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])) as query:
            sources = self.sources(limit=1, include_stories=False)
        self.assertEqual(sources[0].caption, "Free pizza tomorrow!")
        self.assertEqual(sources[0].url, "https://www.instagram.com/p/fixture/")
        self.assertTrue(sources[-1].truncated)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(query.call_args.args[0], TIMELINE_DOC_ID)
        self.assertEqual(query.call_args.args[1]["username"], "club")
        self.profile_lookup.assert_not_called()
        self.stories.assert_not_called()
        self.http.assert_not_called()

    def test_pagination_passes_cursor_and_stops_at_post_limit(self):
        pages = [page([media("one")], has_next=True, cursor="next-page"),
                 page([media("two")], has_next=True, cursor="unused")]
        with patch.object(self.loader.context, "doc_id_graphql_query", side_effect=pages) as query:
            sources = self.sources(limit=2, include_stories=False)
        self.assertEqual([s.source_key for s in sources if s.source_key], ["post:one", "post:two"])
        self.assertEqual(query.call_count, 2)
        self.assertEqual(query.call_args.args[1]["after"], "next-page")

    def test_stories_use_account_id_from_timeline(self):
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])):
            self.sources(limit=10)
        self.stories.assert_called_once_with(userids=[42])

    def test_collaborative_post_does_not_scan_another_owners_stories(self):
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media(username="otherclub")])):
            sources = self.sources(limit=10)
        self.stories.assert_not_called()
        self.assertIn("Stories could not be checked", sources[-1].collection_warning)

    def test_empty_timeline_reports_incomplete_coverage(self):
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([])):
            sources = self.sources(limit=10)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].source_key, "")
        self.assertIn("empty timeline", sources[0].collection_warning)
        self.stories.assert_not_called()

    def test_bad_response_is_not_mistaken_for_no_posts(self):
        for response in ({"data": None}, {"data": {}}, {"errors": [{"message": "Failed"}]},
                         page([], has_next=True, cursor=None)):
            with self.subTest(response=response), patch.object(
                self.loader.context, "doc_id_graphql_query", return_value=response
            ) as query:
                with self.assertRaises(InstagramResponseError):
                    self.sources()
                query.assert_called_once()
        self.stories.assert_not_called()

    def test_timeline_error_classification_preserves_restrictions_without_raw_data(self):
        for response, kind in (
            ({"message": "Please wait a few minutes; sessionid=private-cookie", "status": "fail"}, "temporary_limit"),
            ({"errors": [{"message": "feedback_required", "extensions": {"code": 401}}]}, "temporary_limit"),
            ({"errors": [{"message": "Rate limited", "extensions": {"code": 429}}]}, "rate_limit"),
            ({"errors": [{"message": "challenge_required"}]}, "authentication"),
            ({"require_login": True}, "authentication"),
            ({"message": "403 Forbidden"}, "access"),
            ({"data": None, "sessionid": "private-cookie"}, "response"),
        ):
            with self.subTest(kind=kind, response=response), patch.object(
                self.loader.context, "doc_id_graphql_query", return_value=response
            ) as query:
                with self.assertRaises(InstagramResponseError) as caught:
                    self.sources()
                self.assertEqual(collection_failure_kind(caught.exception), kind)
                detail = collection_error_detail(caught.exception)
                self.assertIn(f"category={kind}", detail)
                self.assertNotIn("private-cookie", detail)
                self.assertNotIn("sessionid", detail)
                query.assert_called_once()
        self.stories.assert_not_called()

    def test_readable_timeline_with_unrelated_partial_errors_still_yields_posts(self):
        response = page([media()])
        response["errors"] = [{"message": "Optional field unavailable"}]
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=response):
            self.assertEqual(self.sources(include_stories=False)[0].source_key, "post:fixture")

    def test_graphql_error_code_and_safe_reason_are_visible(self):
        for message, code, reason, kind in (
            ("Rate limit exceeded; sessionid=private-cookie", 1234567, "request_limit", "temporary_limit"),
            ("Variable example was not provided; private-cookie", 7654321, "query_rejected", "response"),
            ("Invalid session: private-cookie", 1234, "login_required", "authentication"),
            ("Something went wrong: private-cookie", 5678, "server_error", "response"),
            ("Unrecognized message: private-cookie", 9876, "unspecified", "response"),
        ):
            response = {"data": None, "errors": [{"message": message, "code": code}]}
            with self.subTest(reason=reason), patch.object(
                self.loader.context, "doc_id_graphql_query", return_value=response
            ):
                with self.assertRaises(InstagramResponseError) as caught:
                    self.sources()
                self.assertEqual(collection_failure_kind(caught.exception), kind)
                detail = collection_error_detail(caught.exception)
                self.assertIn(f"codes={code}", detail)
                self.assertIn(f"reason={reason}", detail)
                self.assertNotIn("private-cookie", detail)
                self.assertNotIn("sessionid", detail)

    def test_readable_timeline_with_access_restriction_stops(self):
        response = page([media()])
        response["errors"] = [{"message": "feedback_required"}]
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=response):
            with self.assertRaises(InstagramResponseError) as caught:
                self.sources()
        self.assertEqual(collection_failure_kind(caught.exception), "temporary_limit")
        self.download.assert_not_called()
        self.stories.assert_not_called()

    def test_old_pinned_post_does_not_hide_recent_post(self):
        nodes = [media("pinned", posted_at=NOW-timedelta(days=90)), media("recent")]
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page(nodes)):
            sources = self.sources(limit=2, include_stories=False)
        self.assertEqual([s.source_key for s in sources if s.source_key], ["post:recent"])
        self.download.assert_called_once()

    def test_media_throttling_and_challenges_abort_before_stories(self):
        for error in (instaloader.exceptions.AbortDownloadException("429 Too Many Requests"),
                      instaloader.exceptions.ConnectionException("429 Too Many Requests"),
                      instaloader.exceptions.AbortDownloadException("challenge_required")):
            with self.subTest(error=error), patch.object(
                self.loader.context, "doc_id_graphql_query", return_value=page([media(), media("second")])
            ):
                self.download.side_effect = error
                with self.assertRaises(type(error)):
                    self.sources()
        self.stories.assert_not_called()
        self.assertEqual(self.download.call_count, 3)

    def test_missing_image_keeps_caption_and_reports_partial_coverage(self):
        self.download.side_effect = FileNotFoundError("unavailable image")
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])):
            sources = self.sources(limit=10)
        self.assertEqual(sources[0].caption, "Free pizza tomorrow!")
        self.assertIn("only the caption", sources[0].collection_warning)

    def test_ocr_initialization_failure_preserves_scraped_caption(self):
        with patch.object(self.loader.context, "doc_id_graphql_query", return_value=page([media()])), patch(
            "foodfinder.instagram.ocr_images", side_effect=OCRUnavailableError("model unavailable")
        ):
            sources = self.sources(limit=1, include_stories=False)
        self.assertEqual(sources[0].caption, "Free pizza tomorrow!")
        self.assertIn("OCR was incomplete", sources[0].collection_warning)

    def test_real_http_path_posts_graphql_with_loaded_session(self):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(page([media()])).encode()
        response.headers["Content-Type"] = "application/json"
        self.http.side_effect = None
        self.http.return_value = response
        sources = self.sources(limit=1, include_stories=False)
        self.assertEqual(sources[0].source_key, "post:fixture")
        self.http.assert_called_once()
        call = self.http.call_args
        self.assertEqual(call.args[:2], ("POST", "https://www.instagram.com/graphql/query"))
        self.assertEqual(call.kwargs["data"]["doc_id"], TIMELINE_DOC_ID)

    def test_real_http_429_is_never_retried_or_followed_by_another_endpoint(self):
        response = requests.Response()
        response.status_code = 429
        response.reason = "Too Many Requests"
        response._content = b'{"status":"fail","message":"Too many requests"}'
        response.headers["Content-Type"] = "application/json"
        self.http.side_effect = None
        self.http.return_value = response
        with self.assertRaises(instaloader.exceptions.AbortDownloadException):
            self.sources(limit=1, include_stories=False)
        self.http.assert_called_once()
        self.download.assert_not_called()
        self.stories.assert_not_called()

    def test_saved_session_loader_configures_single_attempt_and_safe_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sessions").mkdir()
            self.loader.save_session_to_file(str(root / "sessions" / "instaloader-session-fixtureuser"))
            with patch("foodfinder.instagram.data_dir", return_value=root):
                loaded, user = _make_loader(instaloader)
            try:
                self.assertEqual(user, "fixtureuser")
                self.assertTrue(loaded.context.is_logged_in)
                self.assertEqual(loaded.context.max_connection_attempts, 1)
                self.assertEqual(loaded.context.request_timeout, 30)
                self.assertTrue({401, 403, 429}.issubset(loaded.context.fatal_status_codes))
                self.assertFalse(loaded.context.iphone_support)
            finally:
                loaded.close()
        self.http.assert_not_called()


class MediaDownloadTests(unittest.TestCase):
    def test_ocr_model_failure_is_reported(self):
        with patch("foodfinder.events._ocr_reader", side_effect=ValueError("model failure")):
            with self.assertRaises(OCRUnavailableError):
                ocr_images(["fixture.jpg"])

    def test_partial_ocr_text_is_preserved_when_an_image_fails(self):
        reader = SimpleNamespace(readtext=Mock(side_effect=[["Free pizza"], ValueError("bad image")]))
        with patch("foodfinder.events._ocr_reader", return_value=reader):
            with self.assertRaises(OCRUnavailableError) as caught:
                ocr_images(["one.jpg", "two.jpg"])
        self.assertEqual(caught.exception.partial_text, "Free pizza")
    def test_please_wait_401_is_temporary_restriction_not_expired_login(self):
        error = instaloader.exceptions.AbortDownloadException('401 Unauthorized: Please wait a few minutes before you try again.')
        self.assertEqual(collection_failure_kind(error), "temporary_limit")

    def test_session_verification_rejects_different_browser_account(self):
        loader = SimpleNamespace(context=SimpleNamespace(graphql_query=Mock(return_value={"data": {"user": {"username": "otheruser"}}})))
        with self.assertRaisesRegex(ValueError, "different Instagram account"):
            verify_session(loader, "uoftfoodscraper")
        loader.context.graphql_query.assert_called_once()

    def test_verification_does_not_swallow_429(self):
        loader = SimpleNamespace(context=SimpleNamespace(graphql_query=Mock(side_effect=instaloader.exceptions.AbortDownloadException("429 Too Many Requests"))))
        with self.assertRaises(instaloader.exceptions.AbortDownloadException):
            verify_session(loader, "uoftfoodscraper")
        loader.context.graphql_query.assert_called_once()

    def test_failed_browser_verification_never_overwrites_saved_session(self):
        loader = Mock()
        loader.context.fatal_status_codes = []
        with patch("instaloader.Instaloader", return_value=loader), patch(
            "foodfinder.instagram._import_browser_session", side_effect=ValueError("different account")
        ), patch("foodfinder.instagram._session_path") as session_path:
            with self.assertRaises(ValueError):
                interactive_login(browser_cookie="firefox", username="uoftfoodscraper")
        loader.save_session_to_file.assert_not_called()
        session_path.assert_not_called()

    def test_all_carousel_images_are_downloaded(self):
        post = SimpleNamespace(shortcode="fixture", typename="GraphSidecar", date_local=NOW,
                               get_sidecar_nodes=lambda: [SimpleNamespace(display_url=f"https://example.invalid/{i}.jpg") for i in range(20)])
        def save(filename, url, posted_at):
            Path(filename + ".jpg").write_bytes(b"fixture")
        loader = SimpleNamespace(download_pic=Mock(side_effect=save))
        with tempfile.TemporaryDirectory() as directory:
            images = _download_post_images(loader, post, Path(directory))
            self.assertEqual(len(images), 20)
            self.assertEqual(loader.download_pic.call_count, 20)

    def test_reel_cover_failure_is_not_suppressed(self):
        post = SimpleNamespace(shortcode="fixture", typename="GraphVideo", date_local=NOW,
                               url="https://example.invalid/cover.jpg")
        loader = SimpleNamespace(download_pic=Mock(side_effect=instaloader.exceptions.ConnectionException("429 Too Many Requests")))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(instaloader.exceptions.ConnectionException):
                _download_post_images(loader, post, Path(directory))
        loader.download_pic.assert_called_once()

    def test_wrapped_rate_limit_takes_precedence_over_other_failure(self):
        error = RuntimeError("Collection failed")
        error.__cause__ = instaloader.exceptions.ConnectionException("429 Too Many Requests")
        self.assertEqual(collection_failure_kind(error), "rate_limit")


if __name__ == "__main__":
    unittest.main()
