"""Tests for the Google calendar naming template.

The name and the reset guard are derived from the same template, so these also
check that changing the name keeps the two in step.
"""

from __future__ import annotations

import pytest

from calshoot import config


@pytest.fixture(autouse=True)
def restore_template():
    original = config.CALENDAR_NAME_TEMPLATE
    original_profile = config.current_profile()
    yield
    config.CALENDAR_NAME_TEMPLATE = original
    config.set_profile(original_profile)


def use_template(template: str) -> None:
    config.CALENDAR_NAME_TEMPLATE = template


class TestRendering:
    def test_default_template_puts_the_profile_first(self):
        config.set_profile("fleet2")
        assert config.calendar_display_name("Rochi") == "fleet2 Rochi TimeTree"

    def test_profile_can_be_passed_explicitly(self):
        assert config.calendar_display_name("Rochi", profile="miami") == (
            "miami Rochi TimeTree"
        )

    @pytest.mark.parametrize(
        "template,expected",
        [
            ("{profile} {calendar} TimeTree", "default Rochi TimeTree"),
            ("TimeTree: {calendar}", "TimeTree: Rochi"),
            ("{calendar} (charter)", "Rochi (charter)"),
            ("{calendar}", "Rochi"),
        ],
    )
    def test_templates_render_as_written(self, template, expected):
        config.set_profile("default")
        use_template(template)
        assert config.calendar_display_name("Rochi") == expected

    def test_names_with_regex_characters_survive(self):
        config.set_profile("default")
        assert "Dolce Vita 4 65ft" in config.calendar_display_name("Dolce Vita 4 65ft")


class TestRecognition:
    def test_a_name_we_rendered_is_recognised(self):
        config.set_profile("fleet2")
        rendered = config.calendar_display_name("Rochi")
        assert config.looks_like_our_calendar(rendered)

    @pytest.mark.parametrize(
        "template", ["{profile} {calendar} TimeTree", "TimeTree: {calendar}", "{calendar} x"]
    )
    def test_recognition_follows_whatever_template_is_set(self, template):
        use_template(template)
        assert config.looks_like_our_calendar(config.calendar_display_name("Rochi"))

    def test_the_previous_template_stops_being_recognised(self):
        # Changing the template is what makes old names foreign, which is why
        # renaming means re-linking rather than a silent takeover.
        use_template("TimeTree: {calendar}")
        old_name = config.calendar_display_name("Rochi")
        use_template("{profile} {calendar} TimeTree")
        assert not config.looks_like_our_calendar(old_name)

    @pytest.mark.parametrize("summary", [None, "", "   ", "Family Holidays", "Rochi"])
    def test_unrelated_names_are_not_recognised(self, summary):
        assert not config.looks_like_our_calendar(summary)

    def test_surrounding_whitespace_is_ignored(self):
        config.set_profile("default")
        assert config.looks_like_our_calendar("  default Rochi TimeTree  ")

    def test_a_broken_template_fails_closed(self):
        use_template("{calendar} [unclosed")
        # Must not raise, and must not claim an unrelated calendar.
        assert config.looks_like_our_calendar("Family Holidays") is False


class TestEnvironmentOverride:
    def test_documented_env_var_name_is_read_at_import(self, monkeypatch):
        # Guards the documented override from being renamed accidentally.
        import importlib

        monkeypatch.setenv("CALSHOOT_CALENDAR_NAME", "{calendar} boat")
        reloaded = importlib.reload(config)
        try:
            assert reloaded.CALENDAR_NAME_TEMPLATE == "{calendar} boat"
        finally:
            monkeypatch.delenv("CALSHOOT_CALENDAR_NAME", raising=False)
            importlib.reload(config)
