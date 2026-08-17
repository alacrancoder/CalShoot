"""Translate TimeTree events into Google Calendar event resources.

This module is pure: no network, no state. That keeps the part most likely to
be wrong — timezones, all-day boundaries, recurrence rules — testable offline.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from calshoot.timetree.models import TimeTreeEvent

logger = logging.getLogger(__name__)

# Google event ids must be 5-1024 chars from base32hex: [a-v0-9].
_GOOGLE_ID_ALLOWED = re.compile(r"[^a-v0-9]")
_ID_PREFIX = "tt"

# Only these iCalendar properties belong in Google's `recurrence` array.
# DTSTART/DTEND are carried by start/end instead and are rejected here.
_ALLOWED_RECURRENCE_PROPS = ("RRULE", "EXDATE", "RDATE")

# Google caps reminder overrides per event, and minutes must fit 4 weeks.
_MAX_REMINDERS = 5
_MAX_REMINDER_MINUTES = 40320

# Google's eleven event colours, as displayed hex values.
_GOOGLE_EVENT_COLORS = {
    "1": (0x79, 0x86, 0xCB),  # Lavender
    "2": (0x33, 0xB6, 0x79),  # Sage
    "3": (0x8E, 0x24, 0xAA),  # Grape
    "4": (0xE6, 0x7C, 0x73),  # Flamingo
    "5": (0xF6, 0xBF, 0x26),  # Banana
    "6": (0xF4, 0x51, 0x1E),  # Tangerine
    "7": (0x03, 0x9B, 0xE5),  # Peacock
    "8": (0x61, 0x61, 0x61),  # Graphite
    "9": (0x3F, 0x51, 0xB5),  # Blueberry
    "10": (0x0B, 0x80, 0x43),  # Basil
    "11": (0xD5, 0x00, 0x00),  # Tomato
}

PRIVATE_UUID_KEY = "calshootUuid"
PRIVATE_CALENDAR_KEY = "calshootCalendarId"
PRIVATE_HASH_KEY = "calshootHash"


class MappingError(Exception):
    """The event cannot be represented as a Google Calendar event."""


def google_event_id(tt_uuid: str) -> str:
    """Derive a stable Google event id from a TimeTree uuid.

    Deterministic ids make creates idempotent: re-running a sync after a lost
    state database updates the existing Google events instead of duplicating
    them. TimeTree uuids are hex, which already fits Google's base32hex
    charset; anything else falls back to a hash.
    """
    if not tt_uuid:
        raise MappingError("TimeTree event has no uuid.")

    cleaned = _GOOGLE_ID_ALLOWED.sub("", tt_uuid.lower())
    if len(cleaned) >= 5:
        return f"{_ID_PREFIX}{cleaned}"[:1024]

    digest = hashlib.sha1(tt_uuid.encode("utf-8")).digest()
    encoded = base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")
    return f"{_ID_PREFIX}{encoded}"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("Unknown timezone %r; falling back to UTC.", name)
        return ZoneInfo("UTC")


def _to_datetime(epoch_ms: int, tz_name: str) -> datetime:
    """Convert TimeTree's epoch milliseconds to a zoned datetime."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).astimezone(_zone(tz_name))


def _time_fields(event: TimeTreeEvent) -> tuple[dict, dict]:
    """Build Google's start and end objects."""
    if event.start_at is None or event.end_at is None:
        raise MappingError(f"Event {event.uuid} has no start or end time.")

    start = _to_datetime(event.start_at, event.start_timezone)
    end = _to_datetime(event.end_at, event.end_timezone)

    if event.all_day:
        # Google's all-day end date is exclusive, same as RFC 5545 DTEND.
        start_date = start.date()
        end_date = end.date() + timedelta(days=1)
        if end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        return {"date": start_date.isoformat()}, {"date": end_date.isoformat()}

    if end < start:
        logger.warning("Event %s ends before it starts; clamping to start.", event.uuid)
        end = start

    return (
        {"dateTime": start.isoformat(), "timeZone": event.start_timezone},
        {"dateTime": end.isoformat(), "timeZone": event.end_timezone},
    )


def _recurrence(event: TimeTreeEvent) -> list[str]:
    """Pass TimeTree's iCalendar contentlines through to Google.

    TimeTree already stores recurrence as RRULE/EXDATE/RDATE lines, so this is
    close to a straight copy. Google rejects an RRULE carrying both COUNT and
    UNTIL, and rejects DTSTART inside the recurrence array.
    """
    lines: list[str] = []
    for raw in event.recurrences or []:
        if not isinstance(raw, str):
            continue
        line = raw.strip()
        if not line:
            continue

        prop = line.split(":", 1)[0].split(";", 1)[0].upper()
        if prop not in _ALLOWED_RECURRENCE_PROPS:
            if prop not in ("DTSTART", "DTEND"):
                logger.warning("Dropping unsupported recurrence line %r.", line)
            continue

        if prop == "RRULE" and "COUNT=" in line.upper() and "UNTIL=" in line.upper():
            raise MappingError(
                f"Event {event.uuid} has an RRULE with both COUNT and UNTIL: {line}"
            )

        lines.append(line)
    return lines


def _reminders(event: TimeTreeEvent) -> dict:
    """Map TimeTree alerts (minutes before) to Google reminder overrides."""
    overrides = []
    for alert in event.alerts or []:
        if not isinstance(alert, (int, float)):
            continue
        minutes = int(alert)
        if minutes < 0 or minutes > _MAX_REMINDER_MINUTES:
            logger.warning("Dropping out-of-range reminder %s on %s.", minutes, event.uuid)
            continue
        overrides.append({"method": "popup", "minutes": minutes})

    if not overrides:
        # Explicitly no reminders, rather than inheriting Google's defaults,
        # so the Google copy matches what TimeTree actually says.
        return {"useDefault": False, "overrides": []}

    # Deduplicate while preserving order, then respect Google's cap.
    seen = set()
    unique = []
    for override in overrides:
        if override["minutes"] in seen:
            continue
        seen.add(override["minutes"])
        unique.append(override)

    return {"useDefault": False, "overrides": unique[:_MAX_REMINDERS]}


def _color_id(hex_color: str | None) -> str | None:
    """Snap a TimeTree label colour to the nearest Google event colour."""
    if not hex_color:
        return None
    value = hex_color.lstrip("#")
    if len(value) != 6:
        return None
    try:
        target = tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None

    return min(
        _GOOGLE_EVENT_COLORS,
        key=lambda cid: sum(
            (channel - other) ** 2 for channel, other in zip(target, _GOOGLE_EVENT_COLORS[cid])
        ),
    )


def _description(event: TimeTreeEvent) -> str:
    parts = []
    if event.note:
        parts.append(event.note.strip())
    if event.url:
        parts.append(event.url.strip())
    comments = event.raw.get("comments")
    if comments:
        parts.append("Comments:\n" + "\n".join(str(c) for c in comments))
    return "\n\n".join(parts)


def content_hash(body: dict) -> str:
    """Fingerprint the meaningful parts of a Google event body.

    Used to skip no-op updates. The hash field itself is excluded so that
    storing it does not change it.
    """
    payload = {key: value for key, value in body.items() if key != "extendedProperties"}
    private = (body.get("extendedProperties") or {}).get("private") or {}
    payload["_uuid"] = private.get(PRIVATE_UUID_KEY)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def to_google_event(
    event: TimeTreeEvent,
    *,
    timetree_calendar_id: int,
    labels: dict[int, dict] | None = None,
) -> dict:
    """Build a Google Calendar event resource from a TimeTree event.

    Raises MappingError when the event cannot be represented.
    """
    if event.is_memo:
        raise MappingError(f"Event {event.uuid} is a TimeTree memo and has no time range.")

    start, end = _time_fields(event)

    body: dict = {
        "id": google_event_id(event.uuid),
        "summary": (event.title or "(no title)").strip(),
        "start": start,
        "end": end,
        "reminders": _reminders(event),
        "status": "confirmed",
    }

    description = _description(event)
    if description:
        body["description"] = description
    if event.location:
        body["location"] = event.location.strip()

    recurrence = _recurrence(event)
    if recurrence:
        body["recurrence"] = recurrence

    label = (labels or {}).get(event.label_id)
    color_id = _color_id(label.get("color")) if label else None
    if color_id:
        body["colorId"] = color_id

    body["extendedProperties"] = {
        "private": {
            PRIVATE_UUID_KEY: event.uuid,
            PRIVATE_CALENDAR_KEY: str(timetree_calendar_id),
        }
    }
    body["extendedProperties"]["private"][PRIVATE_HASH_KEY] = content_hash(body)
    return body
