"""Small, read-only Instaloader integration with private local session files."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urljoin, urlsplit

from foodfinder.events import ocr_images
from foodfinder.settings import POST_AGE_DAYS, POST_LIMIT, data_dir


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


def _import_browser_session(loader: object, browser: str) -> str:
    """Import a user-selected browser's Instagram session after direct login is rejected."""
    try:
        import browser_cookie3
    except ImportError as exc:
        raise RuntimeError("Browser-cookie import requires browser-cookie3. Rerun setup.ps1 first.") from exc
    reader = getattr(browser_cookie3, browser, None)
    if not callable(reader):
        raise ValueError(f"Unsupported browser: {browser}.")
    try:
        browser_cookies = reader()
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
        if "instagram.com" in str(cookie.domain).lower()
    }
    if not cookies:
        raise RuntimeError(f"No Instagram cookies were found in {browser}. Sign in to Instagram there first.")
    loader.context.update_cookies(cookies)
    username = loader.test_login()
    if not username:
        raise RuntimeError(f"The {browser} Instagram session could not be verified. Sign in to Instagram in that browser first.")
    loader.context.username = username
    return username


def interactive_login(*, browser_cookie: str = "") -> str:
    """Create a local Instaloader session using password login or an explicitly selected browser."""
    try:
        import instaloader
    except ImportError as exc:
        raise RuntimeError("Instagram collection requires Instaloader. Run setup.ps1 first.") from exc
    loader = instaloader.Instaloader(quiet=False)
    if browser_cookie:
        username = _import_browser_session(loader, browser_cookie.lower())
    else:
        username = input("Instagram username (not your password): ").strip().lstrip("@")
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
    loader = instaloader.Instaloader(
        quiet=True,
        download_pictures=True,
        download_videos=False,
        download_video_thumbnails=True,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
    )
    _fail_fast_on_429(loader)
    loader.load_session_from_file(username, str(session_path))
    return loader, username


def _download_post_images(loader: object, post: object, scratch: Path) -> list[str]:
    """Download image/carousel and video-cover media to a temporary folder."""
    images: list[str] = []
    old_cwd = Path.cwd()
    target = scratch / re.sub(r"[^A-Za-z0-9_-]", "_", str(post.shortcode))
    target.mkdir(parents=True, exist_ok=True)
    try:
        # Instaloader chooses its filename root from dirname_pattern; keep per-post paths isolated.
        loader.dirname_pattern = str(target / "{target}" / "{date_utc}")
        loader.filename_pattern = "{date_utc}_UTC"
        before = set(target.rglob("*.jpg")) | set(target.rglob("*.jpeg")) | set(target.rglob("*.png"))
        loader.download_post(post, target="post")
        after = set(target.rglob("*.jpg")) | set(target.rglob("*.jpeg")) | set(target.rglob("*.png"))
        images = [str(path) for path in sorted(after - before)]
        # A reused shortcode directory can contain all files before the scan.
        if not images:
            images = [str(path) for path in sorted(after)]
        return images[:12]
    finally:
        loader.dirname_pattern = "{target}/{date_utc}"


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
) -> Iterator[MediaSource]:
    """Yield bounded recent posts and accessible Stories for one public club account."""
    try:
        import instaloader
    except ImportError as exc:
        raise RuntimeError("Instagram collection requires Instaloader. Run setup.ps1 first.") from exc
    if loader is None:
        loader, _ = _make_loader(instaloader)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=age_days)
    profile = instaloader.Profile.from_username(loader.context, username)

    with tempfile.TemporaryDirectory(prefix="uoft-food-posts-") as scratch_name:
        scratch = Path(scratch_name)
        posts_seen = 0
        for post in profile.get_posts():
            post_date = post.date_utc
            if post_date.tzinfo is None:
                post_date = post_date.replace(tzinfo=timezone.utc)
            if post_date < cutoff:
                break
            if posts_seen >= limit:
                # Yield a harmless truncation marker that the scanner records as account status.
                yield MediaSource("", "post", "", "", "", "", truncated=True)
                break
            posts_seen += 1
            images: list[str] = []
            try:
                images = _download_post_images(loader, post, scratch)
            except Exception:
                # Keep and analyse captions even if one media item is unavailable.
                images = []
            media_text = ocr_images(images)
            caption = (post.caption or "").strip()
            yield MediaSource(
                source_key=f"post:{post.shortcode}",
                kind="post",
                url=f"https://www.instagram.com/p/{post.shortcode}/",
                caption=caption,
                media_text=media_text,
                posted_at=post_date.astimezone(timezone.utc).isoformat(timespec="seconds"),
            )

        # Story access and Story video downloads both use the same authenticated session.
        for story in loader.get_stories(userids=[profile.userid]):
            for item in story.get_items():
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
                        images.extend(_story_video_frames(str(video), story_path))
                    media_text = ocr_images(images)
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
                    )


def _standalone_session_dir() -> Path:
    return data_dir() / "sessions"
