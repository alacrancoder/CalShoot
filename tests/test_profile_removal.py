"""Tests for removing profiles, and for not creating them by accident.

The bug this guards against: resolving a profile path used to create the
directory, so any command run without --profile resurrected 'default'.
"""

from __future__ import annotations

import pytest

from calshoot import config
from calshoot.state import State


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CALSHOOT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALSHOOT_PROFILE", raising=False)
    config.set_profile(None)
    yield
    config.set_profile(None)


class TestPathsDoNotCreate:
    def test_resolving_a_profile_path_creates_nothing(self):
        config.set_profile("ghost")
        config.profile_home()
        config.session_path()
        config.state_path()
        assert config.list_profiles() == []

    def test_profile_exists_is_false_until_something_is_written(self):
        config.set_profile("ghost")
        assert config.profile_exists() is False
        config.ensure_profile_home()
        assert config.profile_exists() is True

    def test_ensure_creates_it_owner_only(self):
        config.set_profile("real")
        home = config.ensure_profile_home()
        assert home.is_dir()
        assert oct(home.stat().st_mode)[-3:] == "700"

    def test_writing_state_does_create_the_profile(self):
        # Actually using a profile should bring it into existence.
        config.set_profile("working")
        with State() as state:
            state.link_calendar(1, "Rochi", "gcal-1")
        assert config.list_profiles() == ["working"]


class TestRemoval:
    def test_removes_the_directory_and_its_contents(self):
        config.set_profile("stale")
        config.ensure_profile_home()
        (config.profile_home() / "state.db").write_text("x")

        assert config.remove_profile("stale") is True
        assert config.profile_exists("stale") is False
        assert config.list_profiles() == []

    def test_removing_something_absent_is_not_an_error(self):
        assert config.remove_profile("never-existed") is False

    def test_other_profiles_are_untouched(self):
        for name in ("keep", "drop"):
            config.set_profile(name)
            config.ensure_profile_home()

        config.remove_profile("drop")
        assert config.list_profiles() == ["keep"]

    def test_a_traversal_style_name_cannot_escape(self, tmp_path):
        # Names are sanitised, so this can only ever hit a subdirectory.
        outside = config.app_home().parent / "do-not-touch"
        outside.mkdir(parents=True, exist_ok=True)

        config.remove_profile("../../do-not-touch")

        assert outside.is_dir()


class TestLinkCounting:
    def test_counting_links_does_not_create_the_profile(self):
        from calshoot.cli import _link_count

        assert _link_count("ghost") == 0
        assert config.list_profiles() == []

    def test_counts_real_links(self):
        from calshoot.cli import _link_count

        config.set_profile("working")
        with State() as state:
            state.link_calendar(1, "Rochi", "gcal-1")
            state.link_calendar(2, "SeaRay", "gcal-2")

        assert _link_count("working") == 2
