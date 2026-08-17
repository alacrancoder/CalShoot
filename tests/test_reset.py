"""Tests for the guards on `calshoot reset`.

Deleting a Google calendar is irreversible and takes every event with it, so
each guard gets its own test. The bias is always toward refusing.
"""

from __future__ import annotations

import pytest

from calshoot.cli import deletion_plan
from calshoot.gcal.client import GoogleCalendarClient, GoogleCalendarError
from calshoot.state import CalendarLink


def link(**overrides) -> CalendarLink:
    data = {
        "tt_calendar_id": 1,
        "tt_calendar_name": "Rochi",
        "google_calendar_id": "abc@group.calendar.google.com",
        "created_by_calshoot": 1,
    }
    data.update(overrides)
    return CalendarLink(**data)


class FakeGoogle:
    def __init__(self, entry):
        self.entry = entry

    def get_calendar_entry(self, calendar_id):
        return self.entry


def plan_for(entry, the_link=None):
    return deletion_plan(FakeGoogle(entry), [the_link or link()])


OWNED_CREATED = {
    "id": "abc@group.calendar.google.com",
    "summary": "default Rochi TimeTree",  # the configured naming template
    "accessRole": "owner",
}


class TestDeletable:
    def test_a_calendar_calshoot_created_is_deletable(self):
        deletable, protected = plan_for(OWNED_CREATED)
        assert len(deletable) == 1
        assert protected == []

    def test_name_prefix_alone_is_enough_for_older_links(self):
        # Links made before the created_by flag existed still carry the name.
        deletable, _ = plan_for(OWNED_CREATED, link(created_by_calshoot=0))
        assert len(deletable) == 1


class TestGuards:
    def test_primary_calendar_is_never_deleted(self):
        entry = dict(OWNED_CREATED, primary=True)
        deletable, protected = plan_for(entry)
        assert deletable == []
        assert "primary" in protected[0][1]

    def test_primary_by_id_is_never_deleted(self):
        deletable, protected = plan_for(
            OWNED_CREATED, link(google_calendar_id="primary")
        )
        assert deletable == []
        assert "primary" in protected[0][1]

    @pytest.mark.parametrize("role", ["reader", "writer", "freeBusyReader"])
    def test_calendars_you_do_not_own_are_protected(self, role):
        deletable, protected = plan_for(dict(OWNED_CREATED, accessRole=role))
        assert deletable == []
        assert "own" in protected[0][1]

    def test_a_calendar_calshoot_did_not_create_is_protected(self):
        entry = {
            "id": "abc@group.calendar.google.com",
            "summary": "Family Holidays",
            "accessRole": "owner",
        }
        deletable, protected = plan_for(entry, link(created_by_calshoot=0))
        assert deletable == []
        assert "did not create" in protected[0][1]

    def test_a_missing_calendar_is_reported_not_deleted(self):
        deletable, protected = plan_for(None)
        assert deletable == []
        assert "already gone" in protected[0][1]

    @pytest.mark.parametrize(
        "summary",
        [
            "TimeTree: Rochi",  # the old naming scheme, no longer ours
            "Rochi",
            "TimeTree",
            "Charters TimeTree Archive",
        ],
    )
    def test_names_outside_the_template_do_not_qualify(self, summary):
        entry = {
            "id": "abc@group.calendar.google.com",
            "summary": summary,
            "accessRole": "owner",
        }
        deletable, _ = plan_for(entry, link(created_by_calshoot=0))
        assert deletable == []

    def test_the_flag_still_wins_when_the_name_was_changed_by_hand(self):
        # Renaming a calendar in Google must not strand it beyond reset.
        entry = {
            "id": "abc@group.calendar.google.com",
            "summary": "renamed by me",
            "accessRole": "owner",
        }
        deletable, _ = plan_for(entry, link(created_by_calshoot=1))
        assert len(deletable) == 1


class TestClientRefusal:
    def test_delete_calendar_refuses_primary_outright(self):
        # Guard of last resort: calendars().delete on 'primary' would wipe
        # every event in the account's main calendar.
        # Built without __init__ so the test needs no Google credentials.
        client = object.__new__(GoogleCalendarClient)
        for bad in ("primary", ""):
            with pytest.raises(GoogleCalendarError, match="[Rr]efusing"):
                client.delete_calendar(bad)


class TestMixedPlans:
    def test_splits_a_mixed_set_correctly(self):
        class MultiGoogle:
            def get_calendar_entry(self, calendar_id):
                return {
                    "a": dict(OWNED_CREATED, id="a"),
                    "b": {"id": "b", "summary": "My Own", "accessRole": "owner"},
                    "c": None,
                }[calendar_id]

        links = [
            link(tt_calendar_id=1, google_calendar_id="a"),
            link(tt_calendar_id=2, google_calendar_id="b", created_by_calshoot=0),
            link(tt_calendar_id=3, google_calendar_id="c"),
        ]
        deletable, protected = deletion_plan(MultiGoogle(), links)
        assert [link_.google_calendar_id for link_, _ in deletable] == ["a"]
        assert len(protected) == 2
