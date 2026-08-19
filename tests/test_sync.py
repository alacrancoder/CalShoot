"""Tests for the sync engine, using fake TimeTree and Google clients."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from calshoot.state import State
from calshoot.sync import Syncer, _latest_revision
from calshoot.timetree.models import TimeTreeEvent

CAL_ID = 999
GOOGLE_CAL = "cal@group.calendar.google.com"


def epoch_ms(year, month, day, hour=0, minute=0) -> int:
    return int(
        datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000
    )


def raw_event(uuid="e1", title="Meeting", **overrides) -> dict:
    data = {
        "uuid": uuid,
        "title": title,
        "start_at": epoch_ms(2026, 4, 1, 9, 0),
        "end_at": epoch_ms(2026, 4, 1, 10, 0),
        "all_day": False,
        "start_timezone": "UTC",
        "end_timezone": "UTC",
        "updated_at": 1000,
    }
    data.update(overrides)
    return data


class FakeTimeTree:
    def __init__(self, batches):
        # batches: list of (events, next_cursor), consumed one per call
        self.batches = list(batches)
        self.calls = []

    def changed_since(self, calendar_id, high_water):
        self.calls.append(high_water)
        if not self.batches:
            return [], high_water
        events, cursor = self.batches.pop(0)
        return [TimeTreeEvent.from_dict(e) for e in events], cursor

    def labels(self, calendar_id):
        return {}


class FakeGoogle:
    def __init__(self, *, fail_on=None):
        self.events: dict[str, dict] = {}
        self.upserts = 0
        self.deletes = 0
        self.patches = 0
        self.fail_on = fail_on or set()
        self.instances: dict[str, dict] = {}

    def upsert_event(self, calendar_id, body, *, expect_existing=False):
        if body.get("summary") in self.fail_on:
            raise RuntimeError("google rejected this event")
        self.upserts += 1
        self.events[body["id"]] = body
        return body

    def delete_event(self, calendar_id, event_id):
        self.deletes += 1
        return self.events.pop(event_id, None) is not None

    def find_instance(self, calendar_id, master_event_id, original_start):
        return self.instances.get(master_event_id)

    def patch_event(self, calendar_id, event_id, body):
        self.patches += 1
        return body


@pytest.fixture
def state(tmp_path):
    with State(path=tmp_path / "state.db") as s:
        s.link_calendar(CAL_ID, "Family", GOOGLE_CAL)
        yield s


def link_of(state):
    return state.link_for(CAL_ID)


class TestFirstSync:
    def test_creates_events(self, state):
        timetree = FakeTimeTree([([raw_event(), raw_event("e2", "Dinner")], 500)])
        google = FakeGoogle()
        report = Syncer(timetree, google, state).sync_calendar(link_of(state))

        assert report.created == 2
        assert report.updated == 0
        assert google.upserts == 2
        assert report.ok

    def test_advances_the_cursor(self, state):
        timetree = FakeTimeTree([([raw_event()], 500)])
        Syncer(timetree, FakeGoogle(), state).sync_calendar(link_of(state))
        assert link_of(state).high_water == 500


class TestIncrementalSync:
    def test_unchanged_events_cost_no_writes(self, state):
        google = FakeGoogle()
        first = FakeTimeTree([([raw_event()], 500)])
        Syncer(first, google, state).sync_calendar(link_of(state))
        assert google.upserts == 1

        # Same event comes back in the next feed; nothing should be written.
        second = FakeTimeTree([([raw_event()], 600)])
        report = Syncer(second, google, state).sync_calendar(link_of(state))

        assert report.unchanged == 1
        assert report.created == 0
        assert google.upserts == 1

    def test_changed_event_is_updated_not_duplicated(self, state):
        google = FakeGoogle()
        Syncer(FakeTimeTree([([raw_event()], 500)]), google, state).sync_calendar(link_of(state))

        changed = raw_event(title="Meeting (moved)", updated_at=2000)
        report = Syncer(FakeTimeTree([([changed], 600)]), google, state).sync_calendar(
            link_of(state)
        )

        assert report.updated == 1
        assert report.created == 0
        assert len(google.events) == 1

    def test_second_run_asks_timetree_for_only_new_changes(self, state):
        Syncer(FakeTimeTree([([raw_event()], 500)]), FakeGoogle(), state).sync_calendar(
            link_of(state)
        )
        timetree = FakeTimeTree([([], 500)])
        Syncer(timetree, FakeGoogle(), state).sync_calendar(link_of(state))
        assert timetree.calls == [500]


class TestDeletion:
    def test_tombstone_removes_the_google_event(self, state):
        google = FakeGoogle()
        Syncer(FakeTimeTree([([raw_event()], 500)]), google, state).sync_calendar(link_of(state))
        assert len(google.events) == 1

        tombstone = raw_event(deactivated_at=123456, updated_at=2000)
        report = Syncer(FakeTimeTree([([tombstone], 600)]), google, state).sync_calendar(
            link_of(state)
        )

        assert report.deleted == 1
        assert google.events == {}

    def test_deleted_event_is_forgotten_from_state(self, state):
        google = FakeGoogle()
        Syncer(FakeTimeTree([([raw_event()], 500)]), google, state).sync_calendar(link_of(state))
        tombstone = raw_event(deactivated_at=1, updated_at=2000)
        Syncer(FakeTimeTree([([tombstone], 600)]), google, state).sync_calendar(link_of(state))
        assert state.known_hash(CAL_ID, "e1") is None


class TestFailureHandling:
    def test_cursor_is_held_back_when_an_event_fails(self, state):
        timetree = FakeTimeTree([([raw_event(), raw_event("e2", "Bad")], 700)])
        google = FakeGoogle(fail_on={"Bad"})
        report = Syncer(timetree, google, state).sync_calendar(link_of(state))

        assert not report.ok
        assert report.created == 1
        # Holding the cursor means the failed event is retried next run.
        assert link_of(state).high_water == 0

    def test_timetree_failure_is_reported_not_raised(self, state):
        class Broken:
            def changed_since(self, calendar_id, high_water):
                raise RuntimeError("network down")

            def labels(self, calendar_id):
                return {}

        report = Syncer(Broken(), FakeGoogle(), state).sync_calendar(link_of(state))
        assert not report.ok
        assert "network down" in report.errors[0]


class TestSkipping:
    def test_memos_are_skipped_without_failing_the_run(self, state):
        events = [raw_event(), raw_event("m1", "Note", category=2)]
        report = Syncer(FakeTimeTree([(events, 500)]), FakeGoogle(), state).sync_calendar(
            link_of(state)
        )
        assert report.created == 1
        assert len(report.skipped) == 1
        assert report.ok

    def test_unmatched_occurrence_is_skipped_not_duplicated(self, state):
        google = FakeGoogle()  # no instances registered
        child = raw_event("c1", "Moved occurrence", recurring_uuid="master-1")
        report = Syncer(FakeTimeTree([([child], 500)]), google, state).sync_calendar(
            link_of(state)
        )

        assert report.instances_patched == 0
        assert len(report.skipped) == 1
        assert google.upserts == 0  # crucially, no standalone duplicate

    def test_matched_occurrence_is_patched(self, state):
        google = FakeGoogle()
        from calshoot.mapping import google_event_id

        google.instances[google_event_id("master-1")] = {"id": "instance-1"}
        child = raw_event("c1", "Moved occurrence", recurring_uuid="master-1")
        report = Syncer(FakeTimeTree([([child], 500)]), google, state).sync_calendar(
            link_of(state)
        )

        assert report.instances_patched == 1
        assert google.patches == 1


class TestDryRun:
    def test_writes_nothing_and_leaves_the_cursor_alone(self, state):
        google = FakeGoogle()
        timetree = FakeTimeTree([([raw_event(), raw_event("e2", "Dinner")], 500)])
        report = Syncer(timetree, google, state, dry_run=True).sync_calendar(link_of(state))

        assert report.created == 2
        assert google.upserts == 0
        assert google.deletes == 0
        assert link_of(state).high_water == 0


class TestAgeFilter:
    def test_old_events_are_not_copied(self, state):
        google = FakeGoogle()
        old = raw_event("old", "Charter 2023", start_at=epoch_ms(2023, 5, 1),
                        end_at=epoch_ms(2023, 5, 1, 6))
        events = [raw_event(), old]
        report = Syncer(
            FakeTimeTree([(events, 500)]), google, state, skip_before=epoch_ms(2026, 1, 1)
        ).sync_calendar(link_of(state))

        assert report.created == 1
        assert report.filtered == 1
        assert google.upserts == 1

    def test_recurring_series_survive_the_filter(self, state):
        # A weekly series started in 2021 is still producing occurrences now,
        # so age-filtering it away would lose live events.
        google = FakeGoogle()
        old_series = raw_event(
            "series",
            "Weekly maintenance",
            start_at=epoch_ms(2021, 6, 1),
            end_at=epoch_ms(2021, 6, 1, 2),
            recurrences=["RRULE:FREQ=WEEKLY"],
        )
        report = Syncer(
            FakeTimeTree([([old_series], 500)]), google, state, skip_before=epoch_ms(2026, 1, 1)
        ).sync_calendar(link_of(state))

        assert report.created == 1
        assert report.filtered == 0

    def test_no_filter_copies_everything(self, state):
        google = FakeGoogle()
        old = raw_event("old", "Charter 2023", end_at=epoch_ms(2023, 5, 1))
        report = Syncer(FakeTimeTree([([raw_event(), old], 500)]), google, state).sync_calendar(
            link_of(state)
        )
        assert report.created == 2
        assert report.filtered == 0


class TestCalendarSelection:
    def test_only_syncs_the_requested_calendar(self, state):
        state.link_calendar(1234, "Other boat", "other@group.calendar.google.com")
        timetree = FakeTimeTree([([raw_event()], 500), ([raw_event("x")], 900)])
        reports = Syncer(timetree, FakeGoogle(), state).sync_all(only={CAL_ID})

        assert len(reports) == 1
        assert reports[0].calendar_name == "Family"

    def test_no_selection_syncs_all_links(self, state):
        state.link_calendar(1234, "Other boat", "other@group.calendar.google.com")
        timetree = FakeTimeTree([([raw_event()], 500), ([raw_event("x")], 900)])
        reports = Syncer(timetree, FakeGoogle(), state).sync_all()
        assert len(reports) == 2


class TestChangeFeedDeduplication:
    def test_keeps_only_the_newest_revision_of_an_event(self):
        events = [
            TimeTreeEvent.from_dict(raw_event(title="old", updated_at=1)),
            TimeTreeEvent.from_dict(raw_event(title="new", updated_at=9)),
        ]
        collapsed = _latest_revision(events)
        assert len(collapsed) == 1
        assert collapsed[0].title == "new"

    def test_drops_records_without_a_uuid(self):
        events = [TimeTreeEvent.from_dict(raw_event(uuid=None))]
        assert _latest_revision(events) == []


class TestBreakageDetection:
    """The canary must stop a sync, not merely mention a problem."""

    def _shape_changed(self, count=20):
        # start_at renamed: the realistic silent-breakage case.
        out = []
        for i in range(count):
            e = raw_event(f"e{i}")
            e["startAt"] = e.pop("start_at")
            out.append(e)
        return out

    def test_a_changed_payload_shape_blocks_writes(self, state):
        google = FakeGoogle()
        report = Syncer(
            FakeTimeTree([(self._shape_changed(), 900)]), google, state
        ).sync_calendar(link_of(state))

        assert not report.ok
        assert "payload shape changed" in report.errors[0]
        assert google.upserts == 0  # nothing garbage written to Google

    def test_the_high_water_mark_is_held_so_nothing_is_skipped(self, state):
        report = Syncer(
            FakeTimeTree([(self._shape_changed(), 900)]), FakeGoogle(), state
        ).sync_calendar(link_of(state))

        assert not report.ok
        # Once the cause is fixed, the same window is re-read rather than lost.
        assert link_of(state).high_water == 0

    def test_a_few_odd_events_still_sync(self, state):
        events = [raw_event(f"e{i}") for i in range(50)]
        events[0].pop("start_at")  # one weird record, not an API change
        google = FakeGoogle()

        report = Syncer(FakeTimeTree([(events, 900)]), google, state).sync_calendar(
            link_of(state)
        )

        assert report.ok
        assert report.created == 49
        assert len(report.skipped) == 1

    def test_healthy_data_raises_no_warning(self, state):
        # A realistic high-water mark: TimeTree reports epoch milliseconds, so
        # a live account's value is always near "now".
        recent = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        syncer = Syncer(
            FakeTimeTree([([raw_event(updated_at=recent)], recent)]), FakeGoogle(), state
        )
        syncer.sync_all()
        assert syncer.warnings == []

    def test_stale_high_water_marks_raise_a_warning(self, state):
        from calshoot.canary import MS_PER_DAY

        ancient = int(
            (datetime.now(tz=timezone.utc).timestamp() * 1000) - 30 * MS_PER_DAY
        )
        state.save_high_water(CAL_ID, ancient)

        syncer = Syncer(FakeTimeTree([([], ancient)]), FakeGoogle(), state)
        syncer.sync_all()

        assert len(syncer.warnings) == 1
        assert "silently doing nothing" in syncer.warnings[0]

    def test_the_warning_can_be_switched_off(self, state):
        from calshoot.canary import MS_PER_DAY

        ancient = int(
            (datetime.now(tz=timezone.utc).timestamp() * 1000) - 30 * MS_PER_DAY
        )
        state.save_high_water(CAL_ID, ancient)

        syncer = Syncer(
            FakeTimeTree([([], ancient)]), FakeGoogle(), state, quiet_days=0
        )
        syncer.sync_all()
        assert syncer.warnings == []
