"""CLI wiring tests.

These are deliberately shallow. Their job is to import the CLI module and
exercise argument parsing, so syntax errors and broken flag wiring fail here
rather than in front of a user.
"""

from __future__ import annotations

import pytest

from calshoot.cli import _skip_before_ms, build_parser


class TestParser:
    def test_every_subcommand_is_reachable(self):
        parser = build_parser()
        for command in (
            "login",
            "logout",
            "google-login",
            "calendars",
            "link",
            "unlink",
            "status",
            "sync",
            "doctor",
        ):
            args = parser.parse_args([command] if command != "unlink" else [command, "1"])
            assert hasattr(args, "func")

    def test_sync_defaults_are_conservative(self):
        args = build_parser().parse_args(["sync"])
        assert args.dry_run is False
        assert args.full is False
        assert args.future_only is False
        assert args.calendar is None
        assert args.skip_before is None

    def test_calendar_flag_repeats(self):
        args = build_parser().parse_args(
            ["sync", "--calendar", "111", "--calendar", "222"]
        )
        assert args.calendar == [111, 222]

    def test_future_only_and_skip_before_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["sync", "--future-only", "--skip-before", "2025-01-01"]
            )


class TestSkipBeforeParsing:
    def test_parses_an_iso_date(self):
        assert _skip_before_ms("2026-01-01") == 1767225600000

    def test_none_means_no_cutoff(self):
        assert _skip_before_ms(None) is None

    def test_rejects_junk(self):
        with pytest.raises(SystemExit):
            _skip_before_ms("last tuesday")
