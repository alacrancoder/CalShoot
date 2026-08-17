"""Tests for linking calendars, singly and in bulk."""

from __future__ import annotations

import pytest

from calshoot import config
from calshoot.cli import _link_one, build_parser
from calshoot.state import State
from calshoot.timetree.models import TimeTreeCalendar


@pytest.fixture(autouse=True)
def default_profile():
    original = config.current_profile()
    config.set_profile("default")
    yield
    config.set_profile(original)


class FakeGoogle:
    def __init__(self):
        self.created: list[str] = []

    def create_calendar(self, summary, time_zone=None):
        self.created.append(summary)
        return {"id": f"gcal-{len(self.created)}", "summary": summary}


@pytest.fixture
def state(tmp_path):
    with State(path=tmp_path / "state.db") as s:
        yield s


def boat(cid=1, name="Rochi"):
    return TimeTreeCalendar(id=cid, name=name)


class TestLinkingOne:
    def test_creates_a_google_calendar_and_records_it(self, state):
        google = FakeGoogle()
        made = _link_one(
            google, state, boat(), target_id=None, name_override=None, titles={}
        )

        assert made is True
        assert google.created == ["default Rochi TimeTree"]
        link = state.link_for(1)
        assert link.google_calendar_id == "gcal-1"
        assert link.created_by_calshoot == 1

    def test_reuses_an_existing_calendar_of_the_same_name(self, state):
        google = FakeGoogle()
        titles = {"default Rochi TimeTree": "already-there"}
        made = _link_one(
            google, state, boat(), target_id=None, name_override=None, titles=titles
        )

        assert made is False
        assert google.created == []
        assert state.link_for(1).google_calendar_id == "already-there"

    def test_linking_to_a_calendar_you_named_does_not_claim_authorship(self, state):
        google = FakeGoogle()
        _link_one(
            google,
            state,
            boat(),
            target_id="mine@gmail.com",
            name_override=None,
            titles={},
        )
        # Not created by us, so reset must never delete it.
        assert state.link_for(1).created_by_calshoot == 0

    def test_name_override_is_honoured(self, state):
        google = FakeGoogle()
        _link_one(
            google, state, boat(), target_id=None, name_override="Rochi only", titles={}
        )
        assert google.created == ["Rochi only"]


class TestLinkingInBulk:
    def test_each_boat_gets_its_own_calendar(self, state):
        google = FakeGoogle()
        titles: dict[str, str] = {}
        for cid, name in [(1, "Rochi"), (2, "SeaRay"), (3, "Cloud 60ft")]:
            _link_one(
                google,
                state,
                boat(cid, name),
                target_id=None,
                name_override=None,
                titles=titles,
            )

        assert google.created == [
            "default Rochi TimeTree",
            "default SeaRay TimeTree",
            "default Cloud 60ft TimeTree",
        ]
        assert len(state.links()) == 3
        ids = {link.google_calendar_id for link in state.links()}
        assert len(ids) == 3  # no two boats sharing a calendar

    def test_the_shared_title_map_prevents_duplicate_creation(self, state):
        google = FakeGoogle()
        titles: dict[str, str] = {}
        # Same boat linked twice in one run must not create two calendars.
        for _ in range(2):
            _link_one(
                google, state, boat(), target_id=None, name_override=None, titles=titles
            )
        assert google.created == ["default Rochi TimeTree"]


class TestArgumentParsing:
    def test_timetree_calendar_repeats(self):
        args = build_parser().parse_args(
            ["link", "--timetree-calendar", "1", "--timetree-calendar", "2"]
        )
        assert args.timetree_calendar == [1, 2]

    def test_all_flag_parses(self):
        args = build_parser().parse_args(["link", "--all"])
        assert args.all is True
        assert args.timetree_calendar is None

    def test_defaults_are_interactive(self):
        args = build_parser().parse_args(["link"])
        assert args.all is False
        assert args.timetree_calendar is None
        assert args.google_calendar is None

    def test_all_works_with_a_profile(self):
        args = build_parser().parse_args(["link", "--all", "--profile", "fleet2"])
        assert args.all is True
        assert args.profile == "fleet2"


class TestQuotaExhaustion:
    """Google caps calendar creation per day; bulk linking can reach it."""

    @staticmethod
    def _http_error(reason: str):
        from googleapiclient.errors import HttpError

        class Resp(dict):
            status = 403
            reason = "Forbidden"

        body = (
            '{"error":{"errors":[{"reason":"%s"}],"code":403,'
            '"message":"Calendar usage limits exceeded."}}' % reason
        )
        return HttpError(Resp(), body.encode())

    @staticmethod
    def _stub_client():
        from calshoot.gcal.client import GoogleCalendarClient

        class Calendars:
            def insert(self, body=None):
                return object()

        class Service:
            def calendars(self):
                return Calendars()

        client = object.__new__(GoogleCalendarClient)
        client.service = Service()
        return client

    def test_reason_is_extracted_from_the_google_error(self):
        from calshoot.gcal.client import _reason

        assert _reason(self._http_error("quotaExceeded")) == "quotaExceeded"

    def test_daily_quota_becomes_a_clear_error(self, monkeypatch):
        import calshoot.gcal.client as mod
        from calshoot.gcal.client import GoogleQuotaError

        error = self._http_error("quotaExceeded")
        monkeypatch.setattr(mod, "_with_retries", lambda *a, **k: (_ for _ in ()).throw(error))

        with pytest.raises(GoogleQuotaError, match="24 hours"):
            self._stub_client().create_calendar("luxury Rochi TimeTree")

    def test_other_403s_are_not_mistaken_for_a_quota(self, monkeypatch):
        import calshoot.gcal.client as mod
        from calshoot.gcal.client import GoogleQuotaError
        from googleapiclient.errors import HttpError

        error = self._http_error("insufficientPermissions")
        monkeypatch.setattr(mod, "_with_retries", lambda *a, **k: (_ for _ in ()).throw(error))

        with pytest.raises(HttpError):
            self._stub_client().create_calendar("luxury Rochi TimeTree")
        assert not isinstance(error, GoogleQuotaError)

    def test_links_made_before_the_quota_hit_are_kept(self, state):
        # The partial run must commit, so re-running resumes rather than restarts.
        google = FakeGoogle()
        titles: dict[str, str] = {}
        _link_one(google, state, boat(1, "Rochi"), target_id=None,
                  name_override=None, titles=titles)
        _link_one(google, state, boat(2, "SeaRay"), target_id=None,
                  name_override=None, titles=titles)

        assert len(state.links()) == 2
        assert {link.tt_calendar_id for link in state.links()} == {1, 2}
