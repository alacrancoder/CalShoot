"""Tests for the pieces the scheduled job depends on.

A scheduled run is unattended, so the failure modes that matter are the quiet
ones: a broken account stopping the others, or a notification crash taking the
sync down with it.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from calshoot.cli import build_parser, notify

PLIST = Path(__file__).resolve().parent.parent / "com.calshoot.sync.plist"


class TestNotifications:
    def test_never_raises_on_a_broken_osascript(self, monkeypatch):
        def explode(*args, **kwargs):
            raise OSError("osascript missing")

        monkeypatch.setattr("calshoot.cli.subprocess.run", explode)
        notify("title", "message")  # must not raise

    def test_is_a_no_op_off_macos(self, monkeypatch):
        calls = []
        monkeypatch.setattr("calshoot.cli.sys.platform", "linux")
        monkeypatch.setattr("calshoot.cli.subprocess.run", lambda *a, **k: calls.append(a))
        notify("title", "message")
        assert calls == []

    def test_quotes_are_escaped_rather_than_breaking_the_script(self, monkeypatch):
        captured = {}

        def capture(cmd, **kwargs):
            captured["script"] = cmd[-1]

        monkeypatch.setattr("calshoot.cli.sys.platform", "darwin")
        monkeypatch.setattr("calshoot.cli.subprocess.run", capture)
        notify("CalShoot", 'boat "Rochi" failed')

        # The inner quotes must be escaped, not left to terminate the string.
        assert '\\"Rochi\\"' in captured["script"]


class TestSyncFlags:
    def test_all_profiles_and_notify_parse(self):
        args = build_parser().parse_args(
            ["sync", "--all-profiles", "--future-only", "--non-interactive", "--notify"]
        )
        assert args.all_profiles is True
        assert args.notify is True
        assert args.future_only is True
        assert args.non_interactive is True

    def test_defaults_stay_off_for_manual_runs(self):
        args = build_parser().parse_args(["sync"])
        assert args.all_profiles is False
        assert args.notify is False


class TestPlist:
    @pytest.fixture
    def plist(self):
        with PLIST.open("rb") as handle:
            return plistlib.load(handle)

    def test_is_valid_plist_with_the_expected_label(self, plist):
        assert plist["Label"] == "com.calshoot.sync"

    def test_runs_every_thirty_minutes(self, plist):
        assert plist["StartInterval"] == 1800

    def test_command_is_safe_to_run_unattended(self, plist):
        args = plist["ProgramArguments"]
        # Unattended means: never prompt, cover every account, say so on failure.
        assert "--non-interactive" in args
        assert "--all-profiles" in args
        assert "--notify" in args
        assert args[1] == "sync"

    def test_points_at_an_executable_that_exists(self, plist):
        assert Path(plist["ProgramArguments"][0]).exists()

    def test_does_not_run_at_load(self, plist):
        # Loading the job should not kick off a sync before you are ready.
        assert plist["RunAtLoad"] is False
