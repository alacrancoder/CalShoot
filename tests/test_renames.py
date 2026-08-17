"""Tests for following TimeTree calendar renames.

The headline guarantee: a rename never affects which events sync where,
because everything is keyed on ids. These also cover the cosmetic follow-up.
"""

from __future__ import annotations

import pytest

from calshoot import config
from calshoot.state import State
from calshoot.sync import Syncer
from calshoot.timetree.models import TimeTreeCalendar
from tests.test_sync import CAL_ID, GOOGLE_CAL, FakeGoogle, FakeTimeTree, raw_event


@pytest.fixture(autouse=True)
def default_profile():
    original = config.current_profile()
    config.set_profile("default")
    yield
    config.set_profile(original)


class RenamingTimeTree(FakeTimeTree):
    """A fake whose calendar list can be changed between runs."""

    def __init__(self, batches, calendar_name="Rochi"):
        super().__init__(batches)
        self.calendar_name = calendar_name

    def calendars(self):
        return [TimeTreeCalendar(id=CAL_ID, name=self.calendar_name)]


class RenamingGoogle(FakeGoogle):
    def __init__(self, title):
        super().__init__()
        self.title = title
        self.renames: list[str] = []

    def list_calendars(self):
        return [{"id": GOOGLE_CAL, "summary": self.title}]

    def rename_calendar(self, calendar_id, summary):
        self.renames.append(summary)
        self.title = summary
        return {"id": calendar_id, "summary": summary}


@pytest.fixture
def linked(tmp_path):
    with State(path=tmp_path / "state.db") as s:
        s.link_calendar(CAL_ID, "Rochi", GOOGLE_CAL, created_by_calshoot=True)
        yield s


class TestSyncIsUnaffected:
    def test_events_still_sync_after_a_rename(self, linked):
        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
        google = RenamingGoogle("default Rochi TimeTree")

        reports = Syncer(timetree, google, linked).sync_all()

        assert reports[0].created == 1
        assert google.upserts == 1

    def test_the_link_still_points_at_the_same_google_calendar(self, linked):
        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
        Syncer(timetree, RenamingGoogle("default Rochi TimeTree"), linked).sync_all()

        assert linked.link_for(CAL_ID).google_calendar_id == GOOGLE_CAL

    def test_no_duplicate_link_row_is_created(self, linked):
        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
        Syncer(timetree, RenamingGoogle("default Rochi TimeTree"), linked).sync_all()
        assert len(linked.links()) == 1


class TestNameFollowing:
    def test_the_stored_name_is_updated(self, linked):
        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
        Syncer(timetree, RenamingGoogle("default Rochi TimeTree"), linked).sync_all()

        assert linked.link_for(CAL_ID).tt_calendar_name == "Rochi II"

    def test_the_google_calendar_is_retitled(self, linked):
        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
        google = RenamingGoogle("default Rochi TimeTree")
        Syncer(timetree, google, linked).sync_all()

        assert google.renames == ["default Rochi II TimeTree"]

    def test_nothing_happens_when_the_name_is_unchanged(self, linked):
        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi")
        google = RenamingGoogle("default Rochi TimeTree")
        Syncer(timetree, google, linked).sync_all()

        assert google.renames == []


class TestRespectingManualEdits:
    def test_a_title_you_chose_yourself_is_left_alone(self, linked):
        # The Google title is not the generated one, so it was set by hand.
        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
        google = RenamingGoogle("Rochi — charters only")

        Syncer(timetree, google, linked).sync_all()

        assert google.renames == []
        assert google.title == "Rochi — charters only"
        # The local record still follows, since that is display-only.
        assert linked.link_for(CAL_ID).tt_calendar_name == "Rochi II"

    def test_calendars_calshoot_did_not_create_are_left_alone(self, tmp_path):
        with State(path=tmp_path / "s.db") as state:
            state.link_calendar(CAL_ID, "Rochi", GOOGLE_CAL, created_by_calshoot=False)
            timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
            google = RenamingGoogle("default Rochi TimeTree")

            Syncer(timetree, google, state).sync_all()

            assert google.renames == []

    def test_no_rename_flag_disables_retitling(self, linked):
        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
        google = RenamingGoogle("default Rochi TimeTree")

        Syncer(timetree, google, linked, rename_calendars=False).sync_all()

        assert google.renames == []
        assert linked.link_for(CAL_ID).tt_calendar_name == "Rochi II"

    def test_dry_run_changes_nothing(self, linked):
        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
        google = RenamingGoogle("default Rochi TimeTree")

        Syncer(timetree, google, linked, dry_run=True).sync_all()

        assert google.renames == []
        assert linked.link_for(CAL_ID).tt_calendar_name == "Rochi"


class TestResilience:
    def test_a_failure_to_list_timetree_calendars_does_not_stop_the_sync(self, linked):
        class NoCalendars(FakeTimeTree):
            def calendars(self):
                raise RuntimeError("timetree unavailable")

        timetree = NoCalendars([([raw_event()], 500)])
        reports = Syncer(timetree, RenamingGoogle("x"), linked).sync_all()

        assert reports[0].created == 1
        assert reports[0].ok

    def test_a_failed_retitle_does_not_fail_the_sync(self, linked):
        class BrokenRename(RenamingGoogle):
            def rename_calendar(self, calendar_id, summary):
                raise RuntimeError("google said no")

        timetree = RenamingTimeTree([([raw_event()], 500)], calendar_name="Rochi II")
        reports = Syncer(timetree, BrokenRename("default Rochi TimeTree"), linked).sync_all()

        assert reports[0].created == 1
        assert reports[0].ok
