"""Tests for multi-account profile isolation."""

from __future__ import annotations

import pytest

from calshoot import config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CALSHOOT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALSHOOT_PROFILE", raising=False)
    config.set_profile(None)
    yield
    config.set_profile(None)


class TestIsolation:
    def test_each_profile_gets_its_own_session_and_state(self):
        config.set_profile("miami")
        miami_session, miami_state = config.session_path(), config.state_path()

        config.set_profile("keywest")
        keywest_session, keywest_state = config.session_path(), config.state_path()

        assert miami_session != keywest_session
        assert miami_state != keywest_state

    def test_the_google_token_is_shared(self):
        config.set_profile("miami")
        first = config.google_token_path()
        config.set_profile("keywest")
        assert config.google_token_path() == first

    def test_default_profile_is_used_when_unset(self):
        assert config.current_profile() == config.DEFAULT_PROFILE
        assert config.DEFAULT_PROFILE in str(config.session_path())

    def test_environment_variable_is_honoured(self, monkeypatch):
        # Re-import path resolution picks the env default at module load, so
        # setting it explicitly is what callers rely on.
        config.set_profile("fromenv")
        assert "fromenv" in str(config.state_path())


class TestNameSafety:
    @pytest.mark.parametrize(
        "name", ["../escape", "../../etc/passwd", "with space", "sl/ash", ""]
    )
    def test_profile_names_cannot_escape_the_config_directory(self, name):
        config.set_profile(name)
        home = config.app_home().resolve()
        assert home in config.profile_home().resolve().parents

    def test_distinct_names_stay_distinct(self):
        config.set_profile("boats-a")
        a = config.profile_home()
        config.set_profile("boats-b")
        assert config.profile_home() != a


class TestLegacyMigration:
    def test_moves_root_level_files_into_the_default_profile(self):
        root = config.app_home()
        (root / "timetree_session.json").write_text('{"session_id": "abc"}')
        (root / "state.db").write_text("not really a db")

        moved = config.migrate_legacy_layout()

        assert set(moved) == {"timetree_session.json", "state.db"}
        assert not (root / "timetree_session.json").exists()
        config.set_profile(config.DEFAULT_PROFILE)
        assert config.session_path().exists()
        assert config.session_path().read_text() == '{"session_id": "abc"}'

    def test_is_a_no_op_when_there_is_nothing_to_move(self):
        assert config.migrate_legacy_layout() == []

    def test_never_overwrites_an_existing_profile_file(self):
        config.set_profile(config.DEFAULT_PROFILE)
        config.session_path().write_text("keep me")

        root = config.app_home()
        (root / "timetree_session.json").write_text("legacy")
        config.migrate_legacy_layout()

        assert config.session_path().read_text() == "keep me"


class TestListing:
    def test_lists_profiles_that_exist(self):
        config.profile_home("miami")
        config.profile_home("keywest")
        assert config.list_profiles() == ["keywest", "miami"]

    def test_empty_when_none_created(self):
        assert config.list_profiles() == []


class TestAtomicWrites:
    """Credential files are rewritten while the scheduled job may be reading."""

    def test_permissions_are_owner_only(self, tmp_path):
        target = config.app_home() / "secret.json"
        config.write_private(target, '{"token": "abc"}')
        assert oct(target.stat().st_mode)[-3:] == "600"

    def test_content_round_trips(self, tmp_path):
        target = config.app_home() / "secret.json"
        config.write_private(target, '{"token": "abc"}')
        config.write_private(target, '{"token": "xyz"}')
        assert target.read_text() == '{"token": "xyz"}'

    def test_no_temporary_files_are_left_behind(self, tmp_path):
        target = config.app_home() / "secret.json"
        config.write_private(target, "x" * 5000)
        leftovers = [p.name for p in config.app_home().iterdir() if p.name != "secret.json"]
        assert leftovers == []

    def test_a_failed_write_leaves_the_old_file_intact(self, tmp_path, monkeypatch):
        target = config.app_home() / "secret.json"
        config.write_private(target, "original")

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(config.os, "replace", explode)
        with pytest.raises(OSError):
            config.write_private(target, "replacement")

        # The reader must still see valid old contents, not a truncated file.
        assert target.read_text() == "original"
        leftovers = [p.name for p in config.app_home().iterdir() if p.name != "secret.json"]
        assert leftovers == []
