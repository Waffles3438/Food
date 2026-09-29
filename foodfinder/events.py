"""Conservative local food, date, and eligibility extraction."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from foodfinder.food_keywords import FOOD_KEYWORDS
from foodfinder.progress import report


TORONTO = ZoneInfo("America/Toronto")
FOOD_WORDS = "(?:" + "|".join(
    re.escape(word).replace(r"\ ", r"\s+")
    for word in sorted(FOOD_KEYWORDS, key=len, reverse=True)
) + ")"
FOOD_KEYWORD_PATTERN = re.compile(rf"\b{FOOD_WORDS}\b", re.I)
FOOD_PATTERN = re.compile(
    rf"\b(?:(?:free|complimentary)\s*{FOOD_WORDS}"
    rf"|{FOOD_WORDS}\s+(?:is\s+|are\s+|will\s+be\s+)?(?:free|complimentary|provided|included|on\s+us)"
    rf"|(?:provided|included|on\s+us)\s+(?:is\s+|are\s+)?(?:free\s+)?{FOOD_WORDS})\b",
    re.IGNORECASE,
)
VAGUE_FOOD_PATTERN = re.compile(
    rf"\b(?:refreshments?|light snacks?|{FOOD_WORDS}\s+(?:(?:is|are|will be)\s+)?available)\b", re.I
)
FOOD_NEGATION = re.compile(
    r"\b(?:no|not|isn't|aren't|won't be|will not be|without|never)\b"
    rf"(?:[^\w.!?\n]+\w+){{0,4}}[^\w.!?\n]+(?:free\s*)?(?:{FOOD_WORDS}|provided|included|available)\b",
    re.I,
)
CANCEL_PATTERN = re.compile(r"\b(?:cancelled|canceled|postponed|event is off|will not take place)\b", re.I)
TIME_PATTERN = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b|\b(2[0-3]|[01]?\d):([0-5]\d)\b",
    re.I,
)
DATE_FALLBACK = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b",
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


class OCRUnavailableError(RuntimeError):
    def __init__(self, message: str, partial_text: str = ""):
        super().__init__(message)
        self.partial_text = partial_text


def _ocr_batch_size() -> int:
    size = int(os.getenv("FOODFINDER_OCR_BATCH_SIZE", "1"))
    if not 1 <= size <= 16:
        raise ValueError("FOODFINDER_OCR_BATCH_SIZE must be between 1 and 16.")
    return size


def _read_image_batch(reader: Any, paths: list[str]) -> list[list[str]]:
    """Batch detection; pad mixed image sizes without stretching their text."""
    import numpy as np
    from easyocr.utils import reformat_input

    images = [reformat_input(path) for path in paths]
    height = max(image.shape[0] for image, _ in images)
    width = max(image.shape[1] for image, _ in images)
    if height * width * len(images) > 16_000_000:
        raise ValueError("Padded image batch exceeds the pixel budget.")
    colors = []
    greys = []
    for image, grey in images:
        padding = ((0, height - image.shape[0]), (0, width - image.shape[1]))
        colors.append(np.pad(image, (*padding, (0, 0)), constant_values=255))
        greys.append(np.pad(grey, padding, constant_values=255))
    horizontal, free = reader.detect(np.stack(colors), reformat=False)
    if len(horizontal) != len(paths) or len(free) != len(paths):
        raise ValueError("OCR did not return one detection result per image.")
    # Keep recognition settings and image order identical to single-image OCR.
    return [reader.recognize(grey, boxes, polygons, detail=0, reformat=False)
            for grey, boxes, polygons in zip(greys, horizontal, free)]


def ocr_images(paths: Iterable[str]) -> str:
    """Batch Intel GPU OCR, with individual-image retries and CPU fallback."""
    paths = list(paths)
    if not paths:
        return ""
    report(f"Preparing image OCR for {len(paths)} image(s). The first model load can take longer.")
    try:
        reader = _ocr_reader()
        batch_size = _ocr_batch_size() if getattr(reader, "device", "cpu") == "xpu" else 1
    except Exception as exc:
        raise OCRUnavailableError("Poster OCR could not initialize. Check the OCR configuration and model installation.") from exc
    out: list[str] = []
    failed = False
    offset = 0
    while offset < len(paths):
        size = batch_size if getattr(reader, "device", "cpu") == "xpu" else 1
        chunk = paths[offset:offset + size]
        if len(chunk) > 1:
            report(f"Image OCR batch {offset + 1}-{offset + len(chunk)}/{len(paths)} using xpu.")
            try:
                results = _read_image_batch(reader, chunk)
                if len(results) != len(chunk):
                    raise ValueError("OCR batch result count did not match image count.")
                lines = [str(line) for result in results for line in result if str(line).strip()]
            except Exception:
                report("Image OCR batch failed or was too large; retrying its images individually.")
                batch_size = 1
            else:
                out.extend(lines)
                report(f"Image OCR batch {offset + 1}-{offset + len(chunk)}/{len(paths)} finished.")
                offset += len(chunk)
                continue
        for index, path in enumerate(chunk, offset + 1):
            report(f"Image OCR {index}/{len(paths)} using {getattr(reader, 'device', 'cpu')}.")
            try:
                out.extend(str(line) for line in reader.readtext(path, detail=0) if str(line).strip())
            except Exception:
                if getattr(reader, "device", "cpu") == "xpu":
                    _LOG.warning("Intel GPU OCR failed; retrying this image on CPU.")
                    try:
                        reader = _ocr_reader(force_cpu=True)
                        out.extend(str(line) for line in reader.readtext(path, detail=0) if str(line).strip())
                        report(f"Image OCR {index}/{len(paths)} finished on CPU after GPU failure.")
                        continue
                    except Exception:
                        pass
                failed = True
                report(f"Image OCR {index}/{len(paths)} failed; preserving other readable text.")
            else:
                report(f"Image OCR {index}/{len(paths)} finished.")
        offset += len(chunk)
    if failed:
        raise OCRUnavailableError("Some poster images could not be read.", "\n".join(out))
    return "\n".join(out)


_READER: Any = None
_LOG = logging.getLogger(__name__)


def _ocr_device() -> str:
    requested = os.getenv("FOODFINDER_OCR_DEVICE", "auto").strip().lower()
    if requested not in {"auto", "cpu", "xpu"}:
        raise ValueError("FOODFINDER_OCR_DEVICE must be auto, cpu, or xpu.")
    if requested == "cpu":
        return "cpu"
    import torch

    if hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu"
    if requested == "xpu":
        _LOG.warning("Intel GPU is unavailable; using CPU OCR. Check the XPU PyTorch installation and graphics driver.")
    return "cpu"


def _build_ocr_reader(device: str) -> Any:
    import easyocr

    # EasyOCR 1.7.2's GPU loader assumes CUDA DataParallel. Load ordinary,
    # unquantized models on CPU, then move both models to the single Intel GPU.
    reader = easyocr.Reader(["en"], gpu=False, quantize=device == "cpu", verbose=False)
    if device == "xpu":
        reader.detector = reader.detector.to("xpu")
        reader.recognizer = reader.recognizer.to("xpu")
        reader.device = "xpu"
    return reader


def _ocr_reader(*, force_cpu: bool = False) -> Any:
    global _READER
    if force_cpu:
        _READER = None
    if _READER is None:
        device = "cpu" if force_cpu else _ocr_device()
        try:
            _READER = _build_ocr_reader(device)
        except Exception:
            if device == "cpu":
                raise
            _LOG.warning("Intel GPU OCR could not initialize; using CPU OCR.")
            _READER = _build_ocr_reader("cpu")
        _LOG.info("Poster OCR device: %s", _READER.device)
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
    period = r"(?:a\.?m\.?|p\.?m\.?)"
    # Posters and OCR often use a dot instead of a colon in 24-hour times.
    text = re.sub(r"(?<![\w.$])([01]\d|2[0-3])\.([0-5]\d)(?![\w.])",
                  r"\1:\2", text)
    text = re.sub(r"\b(\d{1,2})[.]([0-5]\d)\s*(a\.?m\.?|p\.?m\.?)\b",
                  r"\1:\2\3", text, flags=re.I)

    def normalize_time_range(match):
        start, end, suffix = match.groups()
        suffix = "pm" if suffix.lower().startswith("p") else "am"
        return f"{start}{suffix}-{end}{suffix}"

    text = re.sub(
        rf"\b(\d{{1,2}}(?::\d{{2}})?)\s*[-–]\s*(\d{{1,2}}(?::\d{{2}})?)\s*({period})\b",
        normalize_time_range,
        text,
        flags=re.I,
    )
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
    # Only explicit calendar expressions; durations are not event dates.
    for match in DATE_FALLBACK.finditer(text):
        if re.search(r"\b(?:published|since|copyright)\s*$", text[max(0, match.start()-30):match.start()], re.I):
            continue
        fragment = match.group()
        cleaned = re.sub(r"(\d)(?:st|nd|rd|th)", r"\1", fragment, flags=re.I)
        cleaned = re.sub(r"\bSept\b", "Sep", cleaned, flags=re.I)
        if not re.search(r"\b\d{4}\b", cleaned):
            cleaned += f" {local_base.year}"
        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
            try:
                parsed.append((datetime.strptime(cleaned, fmt).date(), fragment))
                break
            except ValueError:
                continue
    # Day-first poster dates may lose spaces or punctuation during OCR.
    for match in re.finditer(
        r"\b(\d{1,2})(?:st|nd|rd|th)?[.\s/-]+"
        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
        r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"(?:[.\s/-]*(\d{4}))?\b", text, re.I,
    ):
        day, month, year = match.groups()
        cleaned = f"{day} {month[:3]} {year or local_base.year}"
        try:
            parsed.append((datetime.strptime(cleaned, "%d %b %Y").date(), match.group()))
        except ValueError:
            continue
    for match in re.finditer(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b", text):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                parsed.append((datetime.strptime(match.group(), fmt).date(), match.group()))
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
        if event_day not in [d for d, _ in unique]:
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
    date_text: str | None = None,
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
    date_evidence = full if date_text is None else date_text
    food_paragraphs = [part for part in re.split(r"\n\s*\n", date_evidence)
                       if _valid_food_claim(part) or VAGUE_FOOD_PATTERN.search(part)]
    food_dates = _extract_dates("\n".join(food_paragraphs), posted_at, now_local) if food_paragraphs else []
    dates = food_dates or _extract_dates(date_evidence, posted_at, now_local)
    if not cancelled and not food_match and not EVENT_CUE.search(full) and not dates:
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


def caption_needs_ocr(caption: str, *, posted_at: datetime | str) -> bool:
    """Spend OCR work on blank captions or relevant captions missing details."""
    if not caption.strip():
        return True
    if not (FOOD_KEYWORD_PATTERN.search(caption) or FOOD_PATTERN.search(caption)
            or EVENT_CUE.search(caption) or CANCEL_PATTERN.search(caption)):
        return False
    candidates = extract_events(caption, posted_at=posted_at, account_name="club")
    # Unclear food wording, cancellations, multiple dates, or absent event
    # fields can benefit from the poster. Do not infer missing entry rules.
    return not (
        len(candidates) == 1
        and candidates[0].status == "upcoming"
        and candidates[0].food_confidence == "high"
        and not candidates[0].review_reason
        and candidates[0].event_date
        and candidates[0].event_time
        and candidates[0].location
    )


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
        explicit = re.match(r"(?:free|complimentary)", food, re.I) or re.search(r"\b(?:free|complimentary|on us|included)\b", food, re.I)
        confidence = "high" if explicit else "medium"
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
