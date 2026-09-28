"""Small, read-only Instaloader integration with private local session files."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urljoin, urlsplit

from foodfinder.events import OCRUnavailableError, caption_needs_ocr, ocr_images
from foodfinder.settings import POST_AGE_DAYS, POST_LIMIT, data_dir
from foodfinder.progress import report

# Same authenticated timeline query as Profile.get_posts() in pinned Instaloader 4.15.3.
# Start here instead of doing the unreliable web_profile_info lookup first.
TIMELINE_DOC_ID = "7898261790222653"


class InstagramResponseError(RuntimeError):
    """Instagram returned an incompatible or unavailable timeline, not an empty feed."""


def collection_failure_kind(exc: BaseException) -> str:
    """Classify access failures without exposing response bodies or session information."""
    pending = [exc]
    seen: set[int] = set()
    result = ""
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        name, message = type(current).__name__, str(current).casefold()
        if name in {"TooManyRequestsException", "QueryReturnedTooManyRequestsException"} or re.search(r"\b429\b", message):
            return "rate_limit"
        if "please wait" in message or "feedback_required" in message:
            result = "temporary_limit"
        elif result == "temporary_limit":
            pass
        elif name in {"LoginRequiredException", "LoginException", "TwoFactorAuthRequiredException", "BadCredentialsException"} or any(
            term in message for term in ("checkpoint_required", "challenge_required", "login_required", "logged out", "redirected to login")
        ) or re.search(r"\b401\b", message):
            result = "authentication"
        elif not result and (name == "AbortDownloadException" or "feedback_required" in message or re.search(r"\b403\b", message)):
            result = "access"
        elif not result and isinstance(current, InstagramResponseError):
            result = "response"
        elif not result and name == "QueryReturnedBadRequestException":
            result = "response"
        elif not result and name == "ConnectionException":
            result = "connection"
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return result


def collection_error_detail(exc: BaseException) -> str:
    """Keep exception types and recognizable HTTP codes, never response text."""
    pending, seen, codes = [exc], set(), set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        match = re.search(r"\b([45]\d{2})\s+(?:Unauthorized|Forbidden|Too Many Requests|Bad Request|Not Found|Server Error|Bad Gateway|Service Unavailable)", str(current), re.I)
        if match:
            codes.add(match.group(1))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    suffix = ", HTTP " + "/".join(sorted(codes)) if codes else ""
    return type(exc).__name__ + suffix


@dataclass(frozen=True)
class MediaSource:
    source_key: str
    kind: str
    url: str
    caption: str
    media_text: str
    posted_at: str
    expires_at: str = ""
    evidence_path: str = ""
    truncated: bool = False
    collection_warning: str = ""
    ocr_complete: bool = False


def _session_path(username: str) -> Path:
    directory = data_dir() / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "", username)
    return directory / f"instaloader-session-{safe_name}"


def _checkpoint_url(error_message: str) -> str:
    match = re.search(r"Point your browser to\s+(.+?)\s+-\s+follow the instructions", error_message, re.I)
    if not match:
        return "https://www.instagram.com/"
    candidate = match.group(1).strip()
    url = urljoin("https://www.instagram.com/", candidate)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"instagram.com", "www.instagram.com"}:
        return "https://www.instagram.com/"
    return url


def verify_session(loader: object, expected_username: str = "") -> str:
    """Verify identity without test_login(), which swallows throttling exceptions."""
    response = loader.context.graphql_query("d6f4427fbe92d846298cf93df0b937d3", {})
    user = (response.get("data") or {}).get("user") or {}
    username = str(user.get("username") or "").lower()
    if not username:
        raise RuntimeError("Instagram did not confirm a signed-in account. Complete browser login before importing.")
    if expected_username and username != expected_username.strip().lstrip("@").lower():
        raise ValueError(f"The browser is signed into a different Instagram account. Sign into @{expected_username} before importing.")
    return username


def _import_browser_session(loader: object, browser: str, expected_username: str = "") -> str:
    """Import a user-selected browser's Instagram session after direct login is rejected."""
    try:
        import browser_cookie3
    except ImportError as exc:
        raise RuntimeError("Browser-cookie import requires browser-cookie3. Rerun setup.ps1 first.") from exc
    reader = getattr(browser_cookie3, browser, None)
    if not callable(reader):
        raise ValueError(f"Unsupported browser: {browser}.")
    try:
        browser_cookies = reader(domain_name="instagram.com")
    except Exception as exc:
        if "cookie decryption" in str(exc).lower() or "unable to get key" in str(exc).lower():
            raise RuntimeError(
                f"Windows could not decrypt {browser}'s protected cookies. Keep browser encryption enabled; "
                "sign in to Instagram in Firefox, then retry with -BrowserCookie firefox."
            ) from None
        raise RuntimeError(
            f"Could not read cookies from {browser}. Confirm you are signed in there, then try again. ({exc})"
        ) from None
    cookies = {
        cookie.name: cookie.value
        for cookie in browser_cookies
        if (str(cookie.domain).lower().lstrip(".") == "instagram.com"
            or str(cookie.domain).lower().endswith(".instagram.com"))
        and not cookie.is_expired()
    }
    if not all(cookies.get(key) for key in ("sessionid", "csrftoken", "ds_user_id")):
        raise RuntimeError(f"No complete, unexpired Instagram login was found in {browser}. Sign in to Instagram there first.")
    loader.context.update_cookies(cookies)
    loader.context._session.headers["X-CSRFToken"] = cookies["csrftoken"]
    username = verify_session(loader, expected_username)
    loader.context.username = username
    return username


def interactive_login(*, browser_cookie: str = "", username: str = "") -> str:
    """Create a local Instaloader session using password login or an explicitly selected browser."""
    try:
        import instaloader
    except ImportError as exc:
        raise RuntimeError("Instagram collection requires Instaloader. Run setup.ps1 first.") from exc
    username = username.strip().lstrip("@").lower()
    if username and not re.fullmatch(r"[a-z0-9_.]{1,30}", username):
        raise ValueError("Provide a valid Instagram username.")
    loader = instaloader.Instaloader(quiet=False, max_connection_attempts=1, request_timeout=30, fatal_status_codes=[401, 403])
    _fail_fast_on_429(loader)
    if browser_cookie:
        try:
            username = _import_browser_session(loader, browser_cookie.lower(), username)
        except Exception as exc:
            if collection_failure_kind(exc) in {"rate_limit", "temporary_limit"}:
                raise RuntimeError("Instagram temporarily restricted session verification. Wait before retrying; the saved session was not replaced.") from exc
            raise
    else:
        username = username or input("Instagram username (not your password): ").strip().lstrip("@")
        if not username:
            raise ValueError("A username is required.")
        try:
            loader.interactive_login(username)
        except instaloader.exceptions.LoginException as exc:
            if "checkpoint required" in str(exc).lower():
                checkpoint = _checkpoint_url(str(exc))
                raise RuntimeError(
                    "Instagram requires a security checkpoint. Open this Instagram link in your browser, "
                    f"approve the sign-in for @{username}, then run login-instagram.ps1 again:\n{checkpoint}"
                ) from None
            raise RuntimeError(
                f"Instagram could not complete the login: {exc} "
                "If you can sign in through your browser, retry with login-instagram.ps1 -BrowserCookie <browser>."
            ) from None
        except instaloader.exceptions.ConnectionException as exc:
            raise RuntimeError(f"Instagram could not be reached during login: {exc}") from None
    target = _session_path(username)
    loader.save_session_to_file(str(target))
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass  # Windows uses its user-profile ACL rather than POSIX file modes.
    return username.lower()


def _fail_fast_on_429(loader: object) -> None:
    """Make Instagram throttling abort this request instead of Instaloader sleeping and retrying."""
    context = getattr(loader, "context")
    fatal_codes = list(getattr(context, "fatal_status_codes", ()))
    if 429 not in fatal_codes:
        fatal_codes.append(429)
    context.fatal_status_codes = fatal_codes


def _make_loader(instaloader: object, *, allow_login: bool = False) -> tuple[object, str]:
    session_files = sorted((data_dir() / "sessions").glob("instaloader-session-*"))
    if not session_files:
        raise RuntimeError("No Instagram session is configured. Run login-instagram.ps1 first.")
    # One account, one profile queue and one rate controller per scan.
    session_path = session_files[0]
    username = session_path.name.removeprefix("instaloader-session-")
    class ReportingRateController(instaloader.RateController):
        def sleep(self, secs):
            report(f"Instagram pacing: waiting {secs:.0f}s before the next request.")
            super().sleep(secs)
            report("Instagram pacing wait finished; resuming request.")

    loader = instaloader.Instaloader(
        quiet=True,
        max_connection_attempts=1,
        request_timeout=30,
        fatal_status_codes=[401, 403],
        iphone_support=False,
        download_pictures=True,
        download_videos=False,
        download_video_thumbnails=True,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        rate_controller=ReportingRateController,
    )
    _fail_fast_on_429(loader)
    loader.load_session_from_file(username, str(session_path))
    return loader, username


def _timeline_posts(loader: object, username: str):
    """Read the authenticated timeline using Instaloader's pagination and rate controller."""
    import instaloader

    if not loader.context.is_logged_in:
        raise instaloader.exceptions.LoginRequiredException("A saved Instagram login session is required.")

    def edges(response):
        try:
            connection = response["data"]["xdt_api__v1__feed__user_timeline_graphql_connection"]
            if response.get("errors") or not isinstance(connection["edges"], list):
                raise ValueError
            page = connection["page_info"]
            if not isinstance(page["has_next_page"], bool) or (page["has_next_page"] and not page.get("end_cursor")):
                raise ValueError
            report(f"@{username}: received timeline page ({len(connection['edges'])} entries).")
            return connection
        except (KeyError, TypeError, ValueError):
            raise InstagramResponseError(
                "Instagram did not return a readable post timeline. Check the saved browser login; the API may have changed."
            ) from None

    def wrap(node):
        try:
            post = instaloader.Post.from_iphone_struct(loader.context, node)
            user = node.get("user") or {}
            # A collaborative post can belong to another account. Never read its owner's Stories.
            user_id = user.get("pk") or user.get("id")
            account_id = int(user_id) if user_id and str(user.get("username", "")).casefold() == username else None
            return post, account_id
        except (KeyError, TypeError, ValueError):
            raise InstagramResponseError("Instagram returned an unsupported post format; the scan was paused.") from None

    return instaloader.NodeIterator(
        context=loader.context,
        query_hash=None,
        doc_id=TIMELINE_DOC_ID,
        edge_extractor=edges,
        node_wrapper=wrap,
        query_variables={
            "data": {"count": 12, "include_relationship_info": True,
                     "latest_besties_reel_media": True, "latest_reel_media": True},
            "username": username,
        },
        query_referer=f"https://www.instagram.com/{username}/",
    )


def _download_post_images(loader: object, post: object, scratch: Path) -> list[str]:
    """Download image/carousel and video-cover media to a temporary folder."""
    target = scratch / re.sub(r"[^A-Za-z0-9_-]", "_", str(post.shortcode))
    target.mkdir(parents=True, exist_ok=True)
    urls = (node.display_url for node in post.get_sidecar_nodes()) if post.typename == "GraphSidecar" else [post.url]
    # download_post() suppresses errors for Reel covers. Download media directly so
    # 429/authentication failures always reach the scanner and pause its queue.
    for index, url in enumerate(urls):
        report(f"Post {post.shortcode}: downloading image {index + 1}.")
        loader.download_pic(str(target / f"image-{index:02d}"), url, post.date_local)
    return [str(path) for path in sorted(target.iterdir()) if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]


def _story_video_frames(path: str, scratch: Path) -> list[str]:
    try:
        import cv2
    except ImportError:
        return []
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        capture.release()
        return []
    try:
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if count < 1:
            return []
        indices = sorted({0, max(0, count // 2), max(0, count - 1)})
        paths: list[str] = []
        for index in indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            if not ok:
                continue
            path = scratch / f"story-frame-{index}.jpg"
            if cv2.imwrite(str(path), frame):
                paths.append(str(path))
        return paths
    finally:
        capture.release()


def iter_account_sources(
    username: str,
    *,
    limit: int = POST_LIMIT,
    age_days: int = POST_AGE_DAYS,
    now: datetime | None = None,
    loader: object | None = None,
    include_stories: bool = True,
    saved_sources: dict[str, dict] | None = None,
) -> Iterator[MediaSource]:
    """Yield bounded recent posts and accessible Stories for one public club account."""
    try:
        import instaloader
    except ImportError as exc:
        raise RuntimeError("Instagram collection requires Instaloader. Run setup.ps1 first.") from exc
    if loader is None:
        loader, _ = _make_loader(instaloader)
    username = username.strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_.]{1,30}", username) or limit < 1:
        raise ValueError("Provide a valid Instagram username and a positive post limit.")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=age_days)
    report(f"@{username}: requesting Instagram timeline.")
    posts = _timeline_posts(loader, username)
    account_id = None
    saved_sources = saved_sources or {}

    with tempfile.TemporaryDirectory(prefix="uoft-food-posts-") as scratch_name:
        scratch = Path(scratch_name)
        posts_seen = 0
        for post, owner_id in islice(posts, limit):
            account_id = account_id or owner_id
            posts_seen += 1
            post_date = post.date_utc
            if post_date.tzinfo is None:
                post_date = post_date.replace(tzinfo=timezone.utc)
            if post_date < cutoff:
                # Pinned posts and collaborative announcements need not be in date order.
                report(f"@{username}: post {posts_seen}/{limit} ({post.shortcode}) is older than {age_days} days; skipped.")
                continue
            report(f"@{username}: reading caption for post {posts_seen}/{limit} - https://www.instagram.com/p/{post.shortcode}/")
            images: list[str] = []
            warning = ""
            caption = (post.caption or "").strip()
            cached = saved_sources.get(f"post:{post.shortcode}", {})
            if cached.get("cache_version") == 1 and cached.get("caption") == caption:
                report(f"@{username}: post {post.shortcode} unchanged; skipping saved post.")
                continue
            media_text = ""
            ocr_complete = False
            if caption_needs_ocr(caption, posted_at=post_date):
                if cached.get("cache_version") == 1 and cached.get("ocr_complete"):
                    media_text = cached["media_text"]
                    ocr_complete = True
                    report(f"@{username}: caption edited for {post.shortcode}; reusing saved image text.")
                else:
                    report(f"@{username}: post {post.shortcode} needs image text; downloading artwork.")
                    try:
                        images = _download_post_images(loader, post, scratch)
                        if not images:
                            warning = "Post images could not be read; only the caption was checked."
                    except Exception as exc:
                        if collection_failure_kind(exc):
                            raise
                        images = []
                        warning = "Post images could not be read; only the caption was checked."
                    try:
                        media_text = ocr_images(images)
                        ocr_complete = not warning
                    except OCRUnavailableError as exc:
                        media_text = exc.partial_text
                        warning = "Poster OCR was incomplete; the caption and any readable poster text were kept. Check the local OCR models."
            else:
                report(f"@{username}: post {post.shortcode} checked from caption; image OCR skipped.")
            yield MediaSource(
                source_key=f"post:{post.shortcode}",
                kind="post",
                url=f"https://www.instagram.com/p/{post.shortcode}/",
                caption=caption,
                media_text=media_text,
                posted_at=post_date.astimezone(timezone.utc).isoformat(timespec="seconds"),
                collection_warning=warning,
                ocr_complete=ocr_complete,
            )

        if posts_seen == limit:
            yield MediaSource("", "post", "", "", "", "", truncated=True)

        if not include_stories:
            return
        if account_id is None:
            yield MediaSource("", "coverage", "", "", "", "", collection_warning=(
                "Stories could not be checked: the timeline did not supply this account's ID. "
                "An empty timeline may mean no posts, a restricted account, or unavailable data."
            ))
            return

        # Story access and Story video downloads both use the same authenticated session.
        report(f"@{username}: checking accessible Stories.")
        for story in loader.get_stories(userids=[account_id]):
            for item in story.get_items():
                stable_item_id = str(getattr(item, "mediaid", None) or getattr(item, "id", None) or "")
                cached = saved_sources.get(f"story:{stable_item_id}", {}) if stable_item_id else {}
                if cached.get("cache_version") == 1 and cached.get("caption") == (getattr(item, "caption", None) or "").strip():
                    report(f"@{username}: Story {stable_item_id} already processed; skipping saved Story.")
                    continue
                report(f"@{username}: downloading Story media.")
                with tempfile.TemporaryDirectory(prefix="uoft-food-story-") as story_tmp:
                    story_path = Path(story_tmp)
                    loader.dirname_pattern = str(story_path / "{target}")
                    previous_download_videos = loader.download_videos
                    loader.download_videos = True
                    try:
                        loader.download_storyitem(item, target="story")
                    finally:
                        loader.download_videos = previous_download_videos
                        loader.dirname_pattern = "{target}/{date_utc}"
                    files = [
                        path for path in story_path.rglob("*")
                        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4", ".webm"}
                    ]
                    videos = [path for path in files if path.suffix.lower() in {".mp4", ".webm"}]
                    images = [str(path) for path in files if path.suffix.lower() in {".jpg", ".jpeg", ".png"}]
                    for video in videos:
                        report(f"@{username}: extracting sample frames from Story video.")
                        images.extend(_story_video_frames(str(video), story_path))
                    story_warning = "" if images else "Story media could not be read; retry collection."
                    try:
                        media_text = ocr_images(images)
                    except OCRUnavailableError as exc:
                        media_text = exc.partial_text
                        story_warning = "Story OCR was incomplete; the caption and any readable text were kept. Check the local OCR models."
                    caption = (getattr(item, "caption", None) or "").strip()
                    item_id = str(getattr(item, "mediaid", None) or getattr(item, "id", None) or "")
                    if not item_id:
                        item_id = f"{int(item.date_utc.timestamp())}-{len(files)}"
                    created = item.date_utc
                    expires = getattr(item, "expiring_utc", None)
                    first_evidence = ""
                    # Keep a modest, locally served image only when text indicates an event.
                    combined = f"{caption}\n{media_text}"
                    if images and re.search(r"\b(?:food|pizza|event|workshop|tomorrow|today|complimentary|free)\b", combined, re.I):
                        first_evidence = str(images[0])
                    yield MediaSource(
                        source_key=f"story:{item_id}",
                        kind="story",
                        url=f"https://www.instagram.com/stories/{username}/{item_id}/",
                        caption=caption,
                        media_text=media_text,
                        posted_at=created.astimezone(timezone.utc).isoformat(timespec="seconds"),
                        expires_at=expires.astimezone(timezone.utc).isoformat(timespec="seconds") if expires else "",
                        evidence_path=first_evidence,
                        collection_warning=story_warning,
                        ocr_complete=not story_warning,
                    )


def _standalone_session_dir() -> Path:
    return data_dir() / "sessions"
