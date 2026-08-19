"""Tests for silent-breakage detection.

The failures these guard against are the ones that look like success. Each
test names a specific way TimeTree could change under us.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from calshoot.canary import (
    BROKEN_THRESHOLD,
    MS_PER_DAY,
    check_event_schema,
    silence_finding,
)


def event(**overrides) -> dict:
    data = {
        "uuid": "e1",
        "title": "Charter",
        "start_at": 1_700_000_000_000,
        "end_at": 1_700_003_600_000,
        "updated_at": 1_700_000_000_000,
        "all_day": False,
    }
    data.update(overrides)
    return data


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


class TestHealthySchema:
    def test_normal_payloads_are_not_flagged(self):
        finding = check_event_schema([event(uuid=f"e{i}") for i in range(50)])
        assert finding.broken is False
        assert finding.missing == {}
        assert finding.checked == 50

    def test_a_null_field_is_fine_only_the_key_matters(self):
        # A booking with no title is ordinary; a payload with no `title` key
        # means the shape changed.
        finding = check_event_schema([event(title=None), event(note=None)])
        assert finding.broken is False

    def test_new_unknown_fields_are_noted_but_never_alarm(self):
        finding = check_event_schema([event(brand_new_field="x") for _ in range(10)])
        assert finding.broken is False
        assert "brand_new_field" in finding.unknown

    def test_an_empty_sample_is_not_breakage(self):
        finding = check_event_schema([])
        assert finding.broken is False
        assert finding.worst_missing_rate == 0.0

    def test_non_dict_entries_are_ignored(self):
        finding = check_event_schema([event(), None, "garbage", 42])
        assert finding.checked == 1


class TestSchemaBreakage:
    def test_a_renamed_field_is_caught(self):
        # The realistic failure: start_at becomes startAt.
        broken = [
            {k: v for k, v in event(uuid=f"e{i}").items() if k != "start_at"}
            | {"startAt": 1_700_000_000_000}
            for i in range(20)
        ]
        finding = check_event_schema(broken)

        assert finding.broken is True
        assert finding.missing["start_at"] == 20
        assert "startAt" in finding.unknown
        assert "start_at missing from 20/20 events" in finding.describe()

    @pytest.mark.parametrize(
        "dropped", ["uuid", "title", "start_at", "end_at", "updated_at", "all_day"]
    )
    def test_every_required_field_is_actually_required(self, dropped):
        payloads = [
            {k: v for k, v in event(uuid=f"e{i}").items() if k != dropped}
            for i in range(10)
        ]
        assert check_event_schema(payloads).broken is True

    def test_a_few_odd_records_do_not_trip_it(self):
        # One weird event among many is not an API change.
        payloads = [event(uuid=f"e{i}") for i in range(100)]
        payloads[0].pop("start_at")
        finding = check_event_schema(payloads)

        assert finding.broken is False
        assert finding.missing["start_at"] == 1

    def test_the_threshold_is_where_it_claims_to_be(self):
        total = 100
        over = int(total * BROKEN_THRESHOLD) + 5
        payloads = [event(uuid=f"e{i}") for i in range(total)]
        for p in payloads[:over]:
            p.pop("end_at")

        assert check_event_schema(payloads).broken is True


class TestSilenceDetection:
    def test_recent_activity_is_healthy(self):
        recent = now_ms() - MS_PER_DAY  # yesterday
        assert silence_finding([recent], now_ms(), quiet_days=3) is None

    def test_prolonged_silence_is_flagged(self):
        stale = now_ms() - (10 * MS_PER_DAY)
        message = silence_finding([stale], now_ms(), quiet_days=3)

        assert message is not None
        assert "10.0 days" in message
        assert "calshoot doctor" in message

    def test_the_newest_calendar_decides(self):
        # One busy boat is enough to prove the API still works.
        stale = now_ms() - (30 * MS_PER_DAY)
        fresh = now_ms() - MS_PER_DAY
        assert silence_finding([stale, stale, fresh], now_ms(), quiet_days=3) is None

    def test_never_synced_calendars_are_ignored(self):
        # A high-water of 0 means "no data yet", not "stale".
        assert silence_finding([0, 0, 0], now_ms(), quiet_days=3) is None

    def test_no_calendars_at_all_is_not_a_finding(self):
        assert silence_finding([], now_ms(), quiet_days=3) is None

    def test_the_window_is_configurable(self):
        stale = now_ms() - (5 * MS_PER_DAY)
        assert silence_finding([stale], now_ms(), quiet_days=3) is not None
        assert silence_finding([stale], now_ms(), quiet_days=7) is None

    def test_zero_days_disables_the_check(self):
        ancient = now_ms() - (365 * MS_PER_DAY)
        assert silence_finding([ancient], now_ms(), quiet_days=0) is None

    def test_exactly_at_the_boundary_is_not_flagged(self):
        boundary = now_ms() - (3 * MS_PER_DAY)
        assert silence_finding([boundary], now_ms(), quiet_days=3) is None
