"""Tests for the TimeTree -> Google Calendar mapping."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from calshoot.mapping import (
    MappingError,
    content_hash,
    google_event_id,
    to_google_event,
)
from calshoot.timetree.models import TimeTreeEvent

CAL_ID = 12345


def epoch_ms(year, month, day, hour=0, minute=0) -> int:
    """Epoch milliseconds for a UTC wall time, the way TimeTree stores them."""
    return int(
        datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000
    )


def make_event(**overrides) -> TimeTreeEvent:
    data = {
        "uuid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "title": "Standup",
        "start_at": epoch_ms(2026, 3, 15, 14, 30),
        "end_at": epoch_ms(2026, 3, 15, 15, 0),
        "all_day": False,
        "start_timezone": "UTC",
        "end_timezone": "UTC",
    }
    data.update(overrides)
    return TimeTreeEvent.from_dict(data)


class TestEventIds:
    def test_is_deterministic(self):
        event = make_event()
        assert google_event_id(event.uuid) == google_event_id(event.uuid)

    def test_uses_only_google_legal_characters(self):
        # Google requires base32hex: [a-v0-9], 5-1024 chars.
        for uuid in [
            "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            "ZZZ",  # nothing legal survives cleaning -> hash fallback
            "xyz-wxyz",  # w,x,y,z are outside the allowed range
            "12",
        ]:
            event_id = google_event_id(uuid)
            assert re.fullmatch(r"[a-v0-9]{5,1024}", event_id), (uuid, event_id)

    def test_distinct_uuids_do_not_collide(self):
        a = google_event_id("3f2504e0-4f89-11d3-9a0c-0305e82c3301")
        b = google_event_id("3f2504e0-4f89-11d3-9a0c-0305e82c3302")
        assert a != b

    def test_rejects_missing_uuid(self):
        with pytest.raises(MappingError):
            google_event_id("")


class TestTimedEvents:
    def test_converts_to_the_events_own_timezone(self):
        event = make_event(start_timezone="America/New_York", end_timezone="America/New_York")
        body = to_google_event(event, timetree_calendar_id=CAL_ID)

        # 14:30 UTC on 2026-03-15 is 10:30 EDT (DST began 2026-03-08).
        assert body["start"]["dateTime"] == "2026-03-15T10:30:00-04:00"
        assert body["start"]["timeZone"] == "America/New_York"
        assert body["end"]["dateTime"] == "2026-03-15T11:00:00-04:00"

    def test_supports_different_start_and_end_timezones(self):
        event = make_event(
            start_timezone="Europe/Madrid",
            end_timezone="America/New_York",
        )
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert body["start"]["timeZone"] == "Europe/Madrid"
        assert body["end"]["timeZone"] == "America/New_York"

    def test_unknown_timezone_falls_back_to_utc(self):
        event = make_event(start_timezone="Mars/Olympus", end_timezone="Mars/Olympus")
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert body["start"]["dateTime"].endswith("+00:00")

    def test_end_before_start_is_clamped(self):
        event = make_event(end_at=epoch_ms(2026, 3, 15, 13, 0))
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert body["end"]["dateTime"] == body["start"]["dateTime"]


class TestAllDayEvents:
    def test_end_date_is_exclusive(self):
        event = make_event(
            all_day=True,
            start_at=epoch_ms(2026, 3, 15),
            end_at=epoch_ms(2026, 3, 15),
        )
        body = to_google_event(event, timetree_calendar_id=CAL_ID)

        # A one-day all-day event spans [2026-03-15, 2026-03-16).
        assert body["start"] == {"date": "2026-03-15"}
        assert body["end"] == {"date": "2026-03-16"}
        assert "dateTime" not in body["start"]

    def test_multi_day_span(self):
        event = make_event(
            all_day=True,
            start_at=epoch_ms(2026, 3, 15),
            end_at=epoch_ms(2026, 3, 18),
        )
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert body["start"] == {"date": "2026-03-15"}
        assert body["end"] == {"date": "2026-03-19"}

    def test_end_never_precedes_start(self):
        event = make_event(
            all_day=True,
            start_at=epoch_ms(2026, 3, 15),
            end_at=epoch_ms(2026, 3, 10),
        )
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert body["end"]["date"] > body["start"]["date"]


class TestRecurrence:
    def test_passes_through_supported_lines(self):
        event = make_event(
            recurrences=["RRULE:FREQ=WEEKLY;BYDAY=MO,WE", "EXDATE;TZID=UTC:20260323T143000Z"]
        )
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert body["recurrence"] == [
            "RRULE:FREQ=WEEKLY;BYDAY=MO,WE",
            "EXDATE;TZID=UTC:20260323T143000Z",
        ]

    def test_strips_dtstart(self):
        # Google rejects DTSTART inside the recurrence array.
        event = make_event(recurrences=["DTSTART:20260315T143000Z", "RRULE:FREQ=DAILY"])
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert body["recurrence"] == ["RRULE:FREQ=DAILY"]

    def test_rejects_rrule_with_both_count_and_until(self):
        event = make_event(recurrences=["RRULE:FREQ=DAILY;COUNT=5;UNTIL=20260401T000000Z"])
        with pytest.raises(MappingError, match="COUNT and UNTIL"):
            to_google_event(event, timetree_calendar_id=CAL_ID)

    def test_non_recurring_event_omits_the_field(self):
        body = to_google_event(make_event(), timetree_calendar_id=CAL_ID)
        assert "recurrence" not in body


class TestReminders:
    def test_maps_alerts_to_overrides(self):
        event = make_event(alerts=[10, 60])
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert body["reminders"]["overrides"] == [
            {"method": "popup", "minutes": 10},
            {"method": "popup", "minutes": 60},
        ]
        assert body["reminders"]["useDefault"] is False

    def test_no_alerts_means_explicitly_none(self):
        body = to_google_event(make_event(), timetree_calendar_id=CAL_ID)
        assert body["reminders"] == {"useDefault": False, "overrides": []}

    def test_drops_out_of_range_and_deduplicates(self):
        event = make_event(alerts=[10, 10, -5, 999999, 30])
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        minutes = [o["minutes"] for o in body["reminders"]["overrides"]]
        assert minutes == [10, 30]

    def test_respects_googles_five_reminder_cap(self):
        event = make_event(alerts=[1, 2, 3, 4, 5, 6, 7])
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert len(body["reminders"]["overrides"]) == 5


class TestLabelsAndColors:
    def test_snaps_label_color_to_nearest_google_color(self):
        event = make_event(label_id=3)
        labels = {3: {"name": "Work", "color": "#d50000"}}  # exact Tomato
        body = to_google_event(event, timetree_calendar_id=CAL_ID, labels=labels)
        assert body["colorId"] == "11"

    def test_approximate_color_still_matches(self):
        event = make_event(label_id=1)
        labels = {1: {"name": "Personal", "color": "#0a8140"}}  # near Basil
        body = to_google_event(event, timetree_calendar_id=CAL_ID, labels=labels)
        assert body["colorId"] == "10"

    def test_no_label_means_no_color(self):
        body = to_google_event(make_event(), timetree_calendar_id=CAL_ID)
        assert "colorId" not in body


class TestContentAndProvenance:
    def test_records_timetree_provenance(self):
        event = make_event()
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        private = body["extendedProperties"]["private"]
        assert private["calshootUuid"] == event.uuid
        assert private["calshootCalendarId"] == str(CAL_ID)

    def test_combines_note_and_url_into_description(self):
        event = make_event(note="Bring slides", url="https://example.com/doc")
        body = to_google_event(event, timetree_calendar_id=CAL_ID)
        assert "Bring slides" in body["description"]
        assert "https://example.com/doc" in body["description"]

    def test_missing_title_gets_a_placeholder(self):
        body = to_google_event(make_event(title=None), timetree_calendar_id=CAL_ID)
        assert body["summary"] == "(no title)"

    def test_hash_is_stable_for_identical_events(self):
        a = to_google_event(make_event(), timetree_calendar_id=CAL_ID)
        b = to_google_event(make_event(), timetree_calendar_id=CAL_ID)
        assert content_hash(a) == content_hash(b)

    def test_hash_changes_when_content_changes(self):
        a = to_google_event(make_event(), timetree_calendar_id=CAL_ID)
        b = to_google_event(make_event(title="Different"), timetree_calendar_id=CAL_ID)
        assert content_hash(a) != content_hash(b)


class TestRejections:
    def test_memos_are_rejected(self):
        event = make_event(category=2)
        with pytest.raises(MappingError, match="memo"):
            to_google_event(event, timetree_calendar_id=CAL_ID)

    def test_missing_times_are_rejected(self):
        event = make_event(start_at=None, end_at=None)
        with pytest.raises(MappingError, match="no start or end"):
            to_google_event(event, timetree_calendar_id=CAL_ID)
