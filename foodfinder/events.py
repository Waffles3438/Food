"""Conservative local food, date, and eligibility extraction."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable
from zoneinfo import ZoneInfo


TORONTO = ZoneInfo("America/Toronto")
FOOD_PATTERN = re.compile(
    r"\b(?:free\s+(?:food|pizza|snacks?|refreshments?|lunch|dinner|breakfast|coffee|tea|donuts?|pastries|bagels?|cookies?|meal|meals)"
    r"|complimentary\s+(?:food|pizza|snacks?|refreshments?|lunch|dinner|breakfast|coffee|tea|bagels?|cookies?|meal|meals)"
    r"|(?:food|pizza|snacks?|refreshments?|lunch|dinner|breakfast|coffee|tea|donuts?|pastries|bagels?|cookies?|meal|meals)"
    r"\s+(?:is\s+|are\s+|will\s+be\s+)?(?:free|complimentary|provided|included|available|on\s+us)"
    r"|(?:provided|included|on\s+us)\s+(?:is\s+|are\s+)?(?:free\s+)?(?:food|pizza|snacks?|refreshments?|lunch|dinner|breakfast|coffee|tea|bagels?|cookies?|meal|meals))\b",
    re.IGNORECASE,
)
VAGUE_FOOD_PATTERN = re.compile(
    r"\b(?:refreshments?|light snacks?|food will be available|food available)\b", re.I
)
FOOD_NEGATION = re.compile(
    r"\b(?:no|not|isn't|aren't|won't be|will not be|without|never)\b"
    r"(?:\W+\w+){0,4}\W+(?:free\s+)?(?:food|pizza|snacks?|refreshments?|lunch|dinner|breakfast|coffee|tea|meal|meals|provided|included|available)\b",
    re.I,
)
CANCEL_PATTERN = re.compile(r"\b(?:cancelled|canceled|postponed|event is off|will not take place)\b", re.I)
TIME_PATTERN = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b|\b(2[0-3]|[01]?\d):([0-5]\d)\b",
    re.I,
)
DATE_FALLBACK = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b",
    re.I,
)
DATE_TOKEN = re.compile(
    r"\b(?:today|tonight|tomorrow|yesterday|mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|"
    r"thu(?:rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|jan(?:uary)?|feb(?:ruary)?|"
    r"mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b",
    re.I,
)
EVENT_CUE = re.compile(
    r"\b(?:event|workshop|meeting|social|mixer|seminar|panel|info\s+session|info-session|"
    r"fair|gala|screening|reception|open house|meetup|gathering|hangout|come join|join us|RSVP|register|tickets?|"
    r"tomorrow|today|tonight|on\s+(?:mon|tues|wednes|thurs|fri|satur|sun)day|\d{1,2}\s?(?:-|to)\s?\d{1,2})\b",
    re.I,
)
ENTRY_PATTERN = re.compile(
    r"\b(?:tickets?|admission|entry|cover(?:\s+charge)?)\b[^\n.!]{0,24}"
    r"(?:\$\s?\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s?(?:CAD|dollars?)\b)"
    r"|\$\s?\d+(?:\.\d{1,2})?[^\n.!]{0,12}\b(?:tickets?|admission|entry|cover)\b"
    r"|\b(?:paid\s+(?:entry|admission)|ticketed event)\b",
    re.I,
)
FREE_ENTRY_PATTERN = re.compile(
    r"\b(?:free entry|free admission|entry is free|admission is free|no (?:entry|admission|cover) fee|no cover charge|no tickets? required)\b",
    re.I,
)
LOCATION_PATTERN = re.compile(r"\b(?:where|location|venue|room|address)\s*[:@-]\s*([^\n|;]{3,100})", re.I)
RSVP_PATTERN = re.compile(r"\b(?:RSVP|register|registration|sign up|sign-up|reserve|ticket link)\b", re.I)
MEMBERS_PATTERN = re.compile(r"\b(?:members? only|for members only|must be (?:a |an )?member|exclusive to members|club members? only)\b", re.I)
OPEN_PATTERN = re.compile(r"\b(?:open to everyone|open to all|all are welcome|everyone welcome|all students welcome|no membership required)\b", re.I)


@dataclass(frozen=True)
class EventCandidate:
    event_key: str
    title: str
    event_date: str = ""
    event_time: str = ""
    location: str = ""
    food: str = ""
    entry_cost: str = ""
    restrictions: str = ""
    eligibility: str = ""
    rsvp: str = ""
    food_confidence: str = "low"
    review_reason: str = ""
    status: str = "upcoming"
    evidence_excerpt: str = ""


def ocr_images(paths: Iterable[str]) -> str:
    """Read event artwork using EasyOCR (English only, CPU only)."""
    paths = list(paths)
    if not paths:
        return ""
    try:
        import easyocr
    except ImportError:
        return ""
    reader = _ocr_reader()
    out: list[str] = []
    for path in paths:
        try:
            out.extend(str(line) for line in reader.readtext(path, detail=0) if str(line).strip())
        except Exception:
            continue
    return "\n".join(out)


_READER: Any = None


def _ocr_reader() -> Any:
    global _READER
    if _READER is None:
        import easyocr

        _READER = easyocr.Reader(["en"], gpu=False)
    return _READER


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", s))


def _valid_food_claim(text: str) -> re.Match[str] | None:
    for match in FOOD_PATTERN.finditer(text):
        context = text[max(0, match.start() - 42):match.start()]
        # “Gluten-free pizza” describes a dietary option, not an offer with no charge.
        if re.search(r"\b[a-z][a-z-]*-$", context, re.I):
            continue
        previous_sentence = context.rsplit(".", 1)[-1].rsplit("!", 1)[-1].rsplit("?", 1)[-1]
        if re.search(r"\b(?:no|not|isn't|aren't|won't|never|without)\s+(?:any\s+)?(?:free\s+)?$", previous_sentence, re.I):
            continue
        after = text[match.end():match.end() + 24]
        if re.match(r"\s+(?:will\s+)?not\s+be\b", after, re.I):
            continue
        return match
    return None


def _extract_times(text: str) -> list[str]:
    found: list[str] = []
    for m in TIME_PATTERN.finditer(text):
        if m.group(1):
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            period = (m.group(3) or "").replace(".", "").lower()
            if not 1 <= hour <= 12 or minute > 59:
                continue
            hour = hour % 12 + (12 if period.startswith("p") else 0)
        else:
            hour, minute = int(m.group(4)), int(m.group(5))
        value = f"{hour:02d}:{minute:02d}"
        if value not in found:
            found.append(value)
    return found[:2]


def _extract_dates(text: str, posted_at: datetime, now: datetime) -> list[tuple[date, str]]:
    base = posted_at
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    local_base = base.astimezone(TORONTO)
    parsed: list[tuple[date, str]] = []
    try:
        from dateparser.search import search_dates

        results = search_dates(
            text,
            languages=["en"],
            settings={
                "RELATIVE_BASE": local_base,
                "TIMEZONE": "America/Toronto",
                "TO_TIMEZONE": "America/Toronto",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "current_period",
                "DATE_ORDER": "MDY",
                "STRICT_PARSING": True,
            },
        ) or []
        for fragment, dt in results:
            if not isinstance(dt, datetime):
                continue
            if not DATE_TOKEN.search(fragment):
                continue
            local_dt = dt.astimezone(TORONTO) if dt.tzinfo else dt.replace(tzinfo=TORONTO)
            if (local_dt.date(), fragment.lower()) not in [(d, f.lower()) for d, f in parsed]:
                parsed.append((local_dt.date(), fragment))
    except (ImportError, ValueError, OverflowError):
        # The deterministic fallback makes core extraction usable during minimal installs.
        pass
    if not parsed:
        for match in DATE_FALLBACK.finditer(text):
            fragment = match.group(0)
            cleaned = re.sub(r"(\d)(?:st|nd|rd|th)", r"\1", fragment, flags=re.I)
            formats = ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%B %d", "%b %d")
            for fmt in formats:
                try:
                    parsed_date = datetime.strptime(cleaned, fmt).date()
                    if "%Y" not in fmt:
                        parsed_date = parsed_date.replace(year=local_base.year)
                    parsed.append((parsed_date, fragment))
                    break
                except ValueError:
                    continue
    if not parsed:
        relative = re.search(r"\b(today|tonight|tomorrow|yesterday)\b", text, re.I)
        if relative:
            day = local_base.date()
            word = relative.group(1).lower()
            shift = {"today": 0, "tonight": 0, "tomorrow": 1, "yesterday": -1}[word]
            parsed.append((day + timedelta(days=shift), relative.group(0)))
    if not parsed:
        weekday_names = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        weekday_pattern = re.search(
            r"\b(?:this|next)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            text,
            re.I,
        )
        if weekday_pattern:
            target = weekday_names[weekday_pattern.group(1).lower()]
            delta = (target - local_base.weekday()) % 7
            if delta == 0 and not re.search(r"\bthis\s+" + weekday_pattern.group(1) + r"\b", text, re.I):
                delta = 7
            parsed.append((local_base.date() + timedelta(days=delta), weekday_pattern.group(0).strip()))
    unique: list[tuple[date, str]] = []
    for event_day, fragment in parsed:
        if (event_day, fragment.lower()) not in [(d, f.lower()) for d, f in unique]:
            unique.append((event_day, fragment))
    return unique[:5]


def _title_from_text(text: str, account_name: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip(" -|#*•\t") for line in text.splitlines()]
    if len(lines) == 1:
        lines = [part.strip() for part in re.split(r"(?<=[.!?])\s+", lines[0]) if part.strip()]
    bad = re.compile(r"(?:^|\b)(?:free\s+food|free\s+pizza|RSVP|register now|link in bio|"
                     r"all are welcome|date|time|location|where|when)\b", re.I)
    for line in lines[:12]:
        line = re.sub(r"https?://\S+|@\w+", "", line).strip()
        line = re.sub(r"\s+(?:(?:has\s+been|is)\s+)?(?:cancelled|canceled|postponed|rescheduled)\b", "", line, flags=re.I).strip(" .!—-")
        if 4 <= len(line) <= 110 and not bad.search(line) and len(re.findall(r"\w+", line)) <= 13:
            if _normalize(line) == _normalize(account_name):
                continue
            return line[:110]
    return "Club event"


def extract_events(
    text: str,
    *,
    posted_at: datetime | str,
    account_name: str,
    now: datetime | None = None,
) -> list[EventCandidate]:
    """Find independently dated food events; ambiguous claims go to review."""
    if isinstance(posted_at, str):
        try:
            posted_at = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        except ValueError:
            posted_at = datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(TORONTO)
    now_local = now.astimezone(TORONTO) if now.tzinfo else now.replace(tzinfo=TORONTO)
    full = text[:16000].strip()
    if not full:
        return []
    cancelled = bool(CANCEL_PATTERN.search(full))
    negative_food = bool(FOOD_NEGATION.search(full))
    food_match = _valid_food_claim(full)
    vague_match = VAGUE_FOOD_PATTERN.search(full)
    if not food_match and not vague_match and not cancelled and not negative_food:
        return []
    dates = _extract_dates(full, posted_at, now_local)
    if not cancelled and not EVENT_CUE.search(full) and not dates:
        return []
    times = _extract_times(full)
    cost_match = ENTRY_PATTERN.search(full)
    location_match = LOCATION_PATTERN.search(full)
    rsvp_match = RSVP_PATTERN.search(full)
    members_match = MEMBERS_PATTERN.search(full)
    open_match = OPEN_PATTERN.search(full)
    free_entry_match = FREE_ENTRY_PATTERN.search(full)
    title = _title_from_text(full, account_name)
    food_match = food_match if food_match and not negative_food else None
    if cancelled:
        status = "cancelled"
    else:
        status = "upcoming"

    # With no confidently parsed date, retain the result for a human to review.
    if not dates:
        reasons = ["Could not confidently read an event date."]
        if not food_match:
            reasons.append("Food offer is ambiguous or contradicted by the post.")
        return [
            _candidate(
                title, "", times[0] if times else "", location_match, food_match, vague_match,
                cost_match, rsvp_match, members_match, open_match, free_entry_match,
                "low", "; ".join(reasons), status, full,
                posted_at,
            )
        ]

    candidates: list[EventCandidate] = []
    for event_date, date_fragment in dates:
        reasons: list[str] = []
        is_today_without_time = event_date == now_local.date() and not times
        if event_date < now_local.date():
            reasons.append("The parsed event date is in the past; check for an old or cancelled announcement.")
        if event_date.year not in (now_local.year - 1, now_local.year, now_local.year + 1):
            reasons.append("The detected year is outside the current one-year window.")
        if not food_match:
            reasons.append("Food wording is vague or explicitly contradicted; confirm it is complimentary.")
        if is_today_without_time:
            reasons.append("Event is today, but the announcement does not give a start time.")
        review = bool(reasons)
        this_time = times[0] if times else ""
        candidates.append(
            _candidate(
                title, event_date.isoformat(), this_time, location_match, food_match, vague_match,
                cost_match, rsvp_match, members_match, open_match, free_entry_match,
                "review" if review else "high",
                "; ".join(reasons), status, date_fragment, posted_at,
                needs_review=review,
            )
        )
    return candidates


def _candidate(
    title: str,
    event_day: str,
    event_time: str,
    location_match: re.Match[str] | None,
    food_match: re.Match[str] | None,
    vague_match: re.Match[str] | None,
    cost_match: re.Match[str] | None,
    rsvp_match: re.Match[str] | None,
    members_match: re.Match[str] | None,
    open_match: re.Match[str] | None,
    free_entry_match: re.Match[str] | None,
    confidence: str,
    reason: str,
    status: str,
    excerpt: str,
    posted_at: datetime,
    *,
    needs_review: bool = True,
) -> EventCandidate:
    if food_match:
        food = food_match.group(0).strip()
        confidence = "high" if re.search(r"\b(?:free|complimentary|on us|included)\b", food, re.I) else "medium"
    elif vague_match:
        food = vague_match.group(0).strip()
        confidence = "low"
    else:
        food = ""
    cost = "Free entry" if free_entry_match else cost_match.group(0).strip() if cost_match else ""
    restrictions = members_match.group(0).strip() if members_match else ""
    eligibility = "Open to everyone" if open_match else "Members only" if members_match else ""
    canonical = "|".join((
        _normalize(title), event_day, event_time,
    ))
    key = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return EventCandidate(
        event_key=key,
        title=title,
        event_date=event_day,
        event_time=event_time,
        location=location_match.group(1).strip() if location_match else "",
        food=food,
        entry_cost=cost,
        restrictions=restrictions,
        eligibility=eligibility,
        rsvp="RSVP/registration mentioned" if rsvp_match else "",
        food_confidence=confidence,
        review_reason=reason if needs_review else "",
        status=status,
        evidence_excerpt=excerpt[:700].strip(),
    )


def event_dict(candidate: EventCandidate) -> dict[str, str]:
    return asdict(candidate)


def deduplicate_candidate(existing: Iterable[dict[str, Any]], candidate: EventCandidate) -> str | None:
    """Find an existing title/date match without joining different club events."""
    needle = _normalize(candidate.title)
    for row in existing:
        if row.get("event_date") != candidate.event_date:
            continue
        title = _normalize(str(row.get("title", "")))
        if title and needle and SequenceMatcher(None, title, needle).ratio() >= 0.90:
            return str(row["id"])
    return None
