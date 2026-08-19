"""Tests for trimming the scheduled job's log file.

The log is appended to forever by launchd. Trimming happens in place rather
than by renaming, because launchd holds the file open across a run.
"""

from __future__ import annotations

import pytest

from calshoot import config


@pytest.fixture
def log(tmp_path):
    return tmp_path / "calshoot.log"


class TestLeavesSmallLogsAlone:
    def test_a_short_log_is_untouched(self, log):
        log.write_text("one run\ntwo runs\n")
        assert config.trim_log(log, max_bytes=1000) is False
        assert log.read_text() == "one run\ntwo runs\n"

    def test_a_log_exactly_at_the_limit_is_untouched(self, log):
        log.write_bytes(b"x" * 100)
        assert config.trim_log(log, max_bytes=100) is False
        assert log.stat().st_size == 100

    def test_a_missing_log_is_not_an_error(self, tmp_path):
        assert config.trim_log(tmp_path / "nope.log", max_bytes=10) is False

    def test_a_directory_in_the_way_is_not_an_error(self, tmp_path):
        (tmp_path / "adir").mkdir()
        assert config.trim_log(tmp_path / "adir", max_bytes=10) is False


class TestTrimming:
    def test_an_oversized_log_is_shrunk(self, log):
        log.write_text("".join(f"line {i}\n" for i in range(20_000)))
        before = log.stat().st_size

        assert config.trim_log(log, max_bytes=1000, keep_bytes=500) is True

        after = log.stat().st_size
        assert after < before
        assert after < 1000

    def test_the_most_recent_entries_survive(self, log):
        log.write_text("".join(f"line {i}\n" for i in range(20_000)))
        config.trim_log(log, max_bytes=1000, keep_bytes=500)
        text = log.read_text()

        # Recent runs are what you debug from, so the tail is what matters.
        assert "line 19999" in text
        assert "line 0\n" not in text

    def test_it_says_it_trimmed(self, log):
        log.write_text("x\n" * 50_000)
        config.trim_log(log, max_bytes=1000, keep_bytes=500)
        assert "trimmed by calshoot" in log.read_text()

    def test_no_partial_first_line_is_left(self, log):
        log.write_text("".join(f"entry number {i}\n" for i in range(20_000)))
        config.trim_log(log, max_bytes=1000, keep_bytes=500)

        lines = log.read_text().splitlines()
        # First line is the marker; every other line must be whole.
        for line in lines[1:]:
            assert line.startswith("entry number"), line

    def test_appending_after_a_trim_still_works(self, log):
        """launchd holds the handle in append mode across the trim."""
        log.write_text("".join(f"line {i}\n" for i in range(20_000)))
        with log.open("a") as still_open:
            config.trim_log(log, max_bytes=1000, keep_bytes=500)
            still_open.write("a later run\n")
            still_open.flush()

        text = log.read_text()
        assert text.endswith("a later run\n")
        assert len(text) < 2000  # did not balloon back up

    def test_trimming_twice_is_stable(self, log):
        log.write_text("".join(f"line {i}\n" for i in range(20_000)))
        config.trim_log(log, max_bytes=1000, keep_bytes=500)
        size_once = log.stat().st_size

        assert config.trim_log(log, max_bytes=1000, keep_bytes=500) is False
        assert log.stat().st_size == size_once


class TestConfiguration:
    def test_default_location_is_the_mac_log_folder(self, monkeypatch):
        monkeypatch.delenv("CALSHOOT_LOG", raising=False)
        assert config.log_path().name == "calshoot.log"
        assert "Logs" in str(config.log_path())

    def test_the_location_can_be_overridden(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CALSHOOT_LOG", str(tmp_path / "custom.log"))
        assert config.log_path() == tmp_path / "custom.log"
