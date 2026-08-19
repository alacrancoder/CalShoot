"""Detection for silent breakage of TimeTree's unofficial API.

Loud failures already surface: a 500 raises, a 401 says the session died.
What this module watches for is the dangerous class — responses that look
successful but are not:

  * a field renamed or dropped, so events map to nonsense or vanish
  * auth quietly degraded to "200 with no events" instead of a 401
  * paging semantics changed again, so changes stop being noticed

The second and third are indistinguishable from a quiet week, which is why
prolonged total silence is treated as a signal rather than trusted.

Pure functions, no network and no state, so all of it is testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Keys every event payload must carry. Absence means the shape changed under
# us. Presence is what's checked, not truthiness — a null title is fine, a
# missing `title` key is not.
REQUIRED_EVENT_KEYS = frozenset(
    {"uuid", "title", "start_at", "end_at", "updated_at", "all_day"}
)

# Fields TimeTree sent when this was written. New ones appearing is normal and
# harmless; this exists only to log drift, never to alarm.
KNOWN_EVENT_KEYS = frozenset(
    {
        "alerts", "all_day", "attachment", "attendees", "author_id", "author_type",
        "calendar_id", "category", "created_at", "deactivated_at", "end_at",
        "end_timezone", "files", "id", "label_id", "like_count", "link_object_id",
        "link_object_id_string", "location", "location_lat", "location_lon", "lunar",
        "media_content_count", "note", "parent_id", "pinned_at", "primary_id",
        "recurrences", "recurring_uuid", "row_order", "start_at", "start_timezone",
        "title", "type", "updated_at", "url", "uuid",
    }
)

# Above this share of events missing a required key, assume the API changed
# rather than that a few records are odd.
BROKEN_THRESHOLD = 0.20

# Total silence across every calendar for longer than this is suspicious on an
# account whose bookings move daily. Tunable, because a quiet fleet is real.
DEFAULT_QUIET_DAYS = 3

MS_PER_DAY = 86_400_000


@dataclass
class SchemaFinding:
    """What a sample of event payloads looked like."""

    checked: int = 0
    missing: dict[str, int] = field(default_factory=dict)
    unknown: set[str] = field(default_factory=set)

    @property
    def worst_missing_rate(self) -> float:
        if not self.checked or not self.missing:
            return 0.0
        return max(self.missing.values()) / self.checked

    @property
    def broken(self) -> bool:
        """True when enough events lack a required key to suspect a change."""
        return self.worst_missing_rate > BROKEN_THRESHOLD

    def describe(self) -> str:
        parts = []
        for name, count in sorted(self.missing.items(), key=lambda kv: -kv[1]):
            parts.append(f"{name} missing from {count}/{self.checked} events")
        return "; ".join(parts)


def check_event_schema(raw_events) -> SchemaFinding:
    """Compare raw TimeTree payloads against the shape we rely on."""
    finding = SchemaFinding()
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        finding.checked += 1
        keys = raw.keys()
        for required in REQUIRED_EVENT_KEYS:
            if required not in keys:
                finding.missing[required] = finding.missing.get(required, 0) + 1
        finding.unknown.update(set(keys) - KNOWN_EVENT_KEYS)
    return finding


def silence_finding(
    high_waters,
    now_ms: int,
    quiet_days: int = DEFAULT_QUIET_DAYS,
) -> str | None:
    """Flag when no calendar has reported a change for an implausibly long time.

    This is the dead-man's switch. It does not care *how* the API broke — a
    silently emptied response, a changed cursor, a degraded session all look
    the same from here, and all stop the high-water marks advancing.

    Returns a message, or None when things look alive. Calendars that have
    never synced are ignored, since they have nothing to be stale about.
    """
    if quiet_days <= 0:  # disabled
        return None

    seen = [hw for hw in high_waters if hw]
    if not seen:
        return None

    newest = max(seen)
    quiet_ms = now_ms - newest
    if quiet_ms <= quiet_days * MS_PER_DAY:
        return None

    days = quiet_ms / MS_PER_DAY
    return (
        f"No change on any calendar for {days:.1f} days. Either genuinely quiet, "
        f"or TimeTree's API changed and syncs are silently doing nothing. "
        f"Check with: calshoot doctor"
    )
