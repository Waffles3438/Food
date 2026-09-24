"""Polite scraper for public U of T club directories."""

from __future__ import annotations

import re
import time
import unicodedata
from html.parser import HTMLParser
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from html import unescape
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup as _BeautifulSoup
except ImportError:  # Keep pure parsing/core tests useful before the optional web dependencies are installed.
    _BeautifulSoup = None

SOP_GROUPS = "https://sop.utoronto.ca/groups/"
MYUTSU_GROUPS = "https://myutsu.ca/groups/join/"
_IG_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}


class _Element:
    def __init__(self, tag: str = "", attrs: dict[str, str] | None = None, parent: "_Element | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.children: list[_Element | str] = []

    def find_all(self, tag: str, **filters: object) -> list["_Element"]:
        matches: list[_Element] = []
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            if not isinstance(node, _Element):
                continue
            if node.tag == tag and all(node.attrs.get(key) == value or value is True and key in node.attrs for key, value in filters.items()):
                matches.append(node)
            stack.extend(reversed(node.children))
        return matches

    def get_text(self, separator: str = "", strip: bool = False) -> str:
        pieces: list[str] = []
        stack: list[_Element | str] = list(reversed(self.children))
        while stack:
            node = stack.pop()
            if isinstance(node, _Element):
                stack.extend(reversed(node.children))
            elif node:
                pieces.append(node)
        text = separator.join(pieces)
        return " ".join(text.split()) if strip else text

    def __getitem__(self, key: str) -> str:
        return self.attrs[key]


class _TreeBuilder(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Element("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        item = _Element(tag, {key: value or "" for key, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(item)
        if tag not in self.VOID:
            self.stack.append(item)

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.stack) - 1, 0, -1):
            if self.stack[position].tag == tag:
                del self.stack[position:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _soup(html: str) -> object:
    if _BeautifulSoup:
        return _BeautifulSoup(html, "html.parser")
    parser = _TreeBuilder()
    parser.feed(html)
    return parser.root


@dataclass(frozen=True)
class Club:
    source_key: str
    name: str
    campus: str = "St George"
    portal_url: str = ""
    website: str = ""
    instagram_username: str = ""


def fetch_html(url: str) -> str:
    """Fetch a public directory page and obey short, explicit backoffs."""
    import requests

    for attempt in range(3):
        response = requests.get(
            url,
            timeout=(10, 25),
            headers={
                "User-Agent": "UofTFreeFoodFinder/0.1 (personal campus event finder; contact: local user agent)"
            },
        )
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 2:
                response.raise_for_status()
            try:
                retry_after = min(float(response.headers.get("Retry-After", "")), 30)
            except ValueError:
                retry_after = 2 ** (attempt + 1)
            time.sleep(max(0, retry_after))
            continue
        response.raise_for_status()
        return response.text
    raise RuntimeError(f"Could not fetch public directory page: {url}")


def _socials(soup: BeautifulSoup, base_url: str) -> tuple[str, str]:
    website = ""
    username = ""
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, unescape(anchor["href"]).strip())
        parsed = urlparse(href)
        if parsed.hostname and parsed.hostname.lower() in _IG_HOSTS:
            segments = [segment for segment in parsed.path.split("/") if segment]
            if segments and segments[0].lower() not in {
                "p", "reel", "reels", "stories", "explore", "accounts", "direct"
            }:
                username = re.sub(r"[^A-Za-z0-9_.]", "", segments[0]).lower()
        elif parsed.scheme in {"http", "https"} and parsed.hostname:
            host = parsed.hostname.lower()
            if host not in {"sop.utoronto.ca", "www.sop.utoronto.ca"} and not website:
                website = href
    return website, username


def _club_cards(soup: BeautifulSoup, base_url: str, *, st_george_only: bool) -> list[Club]:
    by_key: dict[str, Club] = {}
    anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        href = urljoin(base_url, anchor["href"])
        parsed = urlparse(href)
        if not parsed.path.rstrip("/").startswith("/group/"):
            continue
        name = anchor.get_text(" ", strip=True)
        slug = parsed.path.strip("/").split("/", 1)[-1]
        if not name or not slug:
            continue
        campus_text = name
        node = anchor
        for _ in range(5):
            if node.parent is None:
                break
            node = node.parent
            group_links = [a for a in node.find_all("a", href=True) if "/group/" in urlparse(urljoin(base_url, a["href"])).path]
            if len(group_links) == 1:
                campus_text = node.get_text(" ", strip=True)
                break
        campus_text = re.sub(r"\s+", " ", campus_text)
        campuses = re.findall(r"\b(?:St\s*George|UTM|UTSC)\b", campus_text, flags=re.IGNORECASE)
        campus = "St George" if re.search(r"\bSt\s*George\b", campus_text, re.I) else ""
        if st_george_only and not campus:
            continue
        if not campus:
            campus = campuses[0] if campuses else "St George"
        source_key = f"sop:{slug.lower()}"
        by_key.setdefault(source_key, Club(source_key, name, campus, href))
    return list(by_key.values())


def parse_sop_page(html: str, base_url: str = SOP_GROUPS) -> list[Club]:
    return _club_cards(_soup(html), base_url, st_george_only=True)


def _page_numbers(soup: BeautifulSoup) -> list[int]:
    pages = {1}
    for anchor in soup.find_all("a", href=True):
        query = dict(pair.split("=", 1) for pair in urlparse(anchor["href"]).query.split("&") if "=" in pair)
        if query.get("pg", "").isdigit():
            pages.add(int(query["pg"]))
    return sorted(pages)


def fetch_sop_clubs(fetch: Callable[[str], str] = fetch_html, *, enrich_profiles: bool = True) -> list[Club]:
    first_html = fetch(SOP_GROUPS)
    first = _soup(first_html)
    pages = _page_numbers(first)
    clubs: dict[str, Club] = {}
    for page in range(1, min(max(pages), 60) + 1):
        html = first_html if page == 1 else fetch(f"{SOP_GROUPS}?pg={page}")
        for club in parse_sop_page(html, f"{SOP_GROUPS}?pg={page}"):
            clubs.setdefault(club.source_key, club)
    if not enrich_profiles:
        return list(clubs.values())

    enriched: list[Club] = []
    for club in clubs.values():
        try:
            page = _soup(fetch(club.portal_url))
            website, username = _socials(page, club.portal_url)
            if not website:
                website = next(
                    (
                        anchor["href"]
                        for anchor in page.find_all("a", href=True)
                        if urlparse(anchor["href"]).scheme in {"http", "https"}
                        and urlparse(anchor["href"]).hostname
                        and urlparse(anchor["href"]).hostname.lower() not in {"sop.utoronto.ca", "www.sop.utoronto.ca"}
                        and not (urlparse(anchor["href"]).hostname or "").lower().endswith("instagram.com")
                    ),
                    "",
                )
            if website and not username:
                site = _soup(fetch(website))
                _, username = _socials(site, website)
            enriched.append(Club(**{**asdict(club), "website": website, "instagram_username": username}))
        except Exception:
            # Keep directory records even when an individual club site is offline.
            enriched.append(club)
        time.sleep(0.12)
    return enriched


def _normal_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"\b(?:university of toronto|at u\s?of\s?t|uoft|student chapter|st\.? george campus)\b", " ", name)
    return " ".join(re.findall(r"[a-z0-9]+", name))


def fetch_myutsu_clubs(fetch: Callable[[str], str] = fetch_html) -> list[Club]:
    """Collect UTSU gallery names; group profile links can be manually resolved in the UI."""
    html = fetch(MYUTSU_GROUPS)
    soup = _soup(html)
    clubs: dict[str, Club] = {}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(MYUTSU_GROUPS, anchor["href"])
        path = urlparse(href).path.rstrip("/")
        match = re.search(r"/groups/id/(\d+)$", path)
        name = anchor.get_text(" ", strip=True)
        if not match or not name or len(name) > 180:
            continue
        source_key = f"myutsu:{match.group(1)}"
        clubs.setdefault(source_key, Club(source_key, name, "St George", href))
    return list(clubs.values())


def merge_directory_clubs(primary: Iterable[Club], supplementary: Iterable[Club]) -> list[Club]:
    """Attach UTSU results to a strong exact-name SOP match; keep unmatched UTSU entries distinct."""
    result = {club.source_key: club for club in primary}
    by_name: dict[str, list[Club]] = {}
    for club in primary:
        by_name.setdefault(_normal_name(club.name), []).append(club)
    for club in supplementary:
        key = _normal_name(club.name)
        matches = by_name.get(key, [])
        if matches:
            match = matches[0]
            result[match.source_key] = Club(
                **{
                    **asdict(match),
                    "website": match.website or club.portal_url,
                }
            )
        else:
            result.setdefault(club.source_key, club)
    return list(result.values())
