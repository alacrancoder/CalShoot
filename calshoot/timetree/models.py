"""Typed views over TimeTree's internal API payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# TimeTree event "type" values.
TYPE_NORMAL = 0
TYPE_BIRTHDAY = 1

# TimeTree event "category" values. Memos have no meaningful time range.
CATEGORY_NORMAL = 1
CATEGORY_MEMO = 2

# Keys TimeTree has been observed to use, or might plausibly use, to mark an
# event as removed. The sync engine treats a non-null value in any of them as a
# tombstone. Verified against live data by `calshoot doctor`.
_TOMBSTONE_KEYS = ("deactivated_at", "deleted_at", "removed_at")


@dataclass(frozen=True)
class TimeTreeCalendar:
    """A TimeTree calendar the signed-in user can see."""

    id: int
    name: str
    deactivated_at: Any = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def active(self) -> bool:
        return self.deactivated_at is None

    @classmethod
    def from_dict(cls, data: dict) -> "TimeTreeCalendar":
        return cls(
            id=data.get("id"),
            name=data.get("name") or f"Calendar {data.get('id')}",
            deactivated_at=data.get("deactivated_at"),
            raw=data,
        )


@dataclass(frozen=True)
class TimeTreeEvent:
    """A single TimeTree event.

    Timestamps are epoch milliseconds. Each event carries its own IANA
    timezone for start and end, which need not match.
    """

    uuid: str
    title: str | None
    note: str | None
    location: str | None
    url: str | None
    start_at: int | None
    end_at: int | None
    all_day: bool
    start_timezone: str
    end_timezone: str
    alerts: list = field(default_factory=list)
    recurrences: list = field(default_factory=list)
    parent_id: str | None = None
    recurring_uuid: str | None = None
    event_type: int = TYPE_NORMAL
    category: int = CATEGORY_NORMAL
    label_id: int | None = None
    updated_at: int | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_recurrence_master(self) -> bool:
        """True when this event defines a recurrence rule of its own."""
        return bool(self.recurrences)

    @property
    def is_recurrence_child(self) -> bool:
        """True when this is a modified instance of another recurring event.

        TimeTree returns edited occurrences as standalone events that point
        back at their master. They must be applied to Google as instance
        patches, not as separate events, or every edit becomes a duplicate.
        """
        return bool(self.recurring_uuid or self.parent_id) and not self.recurrences

    @property
    def master_uuid(self) -> str | None:
        """The uuid of the recurrence master this event modifies, if any."""
        return self.recurring_uuid or self.parent_id

    @property
    def deleted(self) -> bool:
        """True when TimeTree has marked this event as removed."""
        return any(self.raw.get(key) is not None for key in _TOMBSTONE_KEYS)

    @property
    def is_memo(self) -> bool:
        """Memos ("keeps") carry no usable time range."""
        return self.category == CATEGORY_MEMO

    @staticmethod
    def _label_id(data: dict) -> int | None:
        """Pull the label id out of either the flat or JSON:API shape."""
        label_id = data.get("label_id")
        if label_id is not None:
            return label_id
        try:
            rel_id = data["relationships"]["label"]["data"]["id"]
        except (KeyError, TypeError):
            return None
        try:
            # Sometimes formatted "calendar_id,label_number".
            if isinstance(rel_id, str) and "," in rel_id:
                return int(rel_id.split(",")[-1])
            return int(rel_id)
        except (TypeError, ValueError):
            return None

    @classmethod
    def from_dict(cls, data: dict) -> "TimeTreeEvent":
        # An event can legitimately omit a timezone; UTC is the safe default
        # because TimeTree stores instants, not wall-clock times.
        start_tz = data.get("start_timezone") or "UTC"
        end_tz = data.get("end_timezone") or start_tz
        return cls(
            uuid=data.get("uuid"),
            title=data.get("title"),
            note=data.get("note"),
            location=data.get("location"),
            url=data.get("url"),
            start_at=data.get("start_at"),
            end_at=data.get("end_at"),
            all_day=bool(data.get("all_day")),
            start_timezone=start_tz,
            end_timezone=end_tz,
            alerts=data.get("alerts") or [],
            recurrences=data.get("recurrences") or [],
            parent_id=data.get("parent_id"),
            recurring_uuid=data.get("recurring_uuid"),
            event_type=data.get("type", TYPE_NORMAL),
            category=data.get("category", CATEGORY_NORMAL),
            label_id=cls._label_id(data),
            updated_at=data.get("updated_at"),
            raw=data,
        )
