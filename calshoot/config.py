"""Filesystem paths for CalShoot.

Everything secret lives under a single private directory so it is easy to
audit and easy to delete. Override the location with CALSHOOT_HOME.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

APP_NAME = "calshoot"
DEFAULT_HOME = Path.home() / ".calshoot"
DEFAULT_PROFILE = "default"

# One profile per TimeTree account. Each keeps its own session cookie and sync
# state; the Google token is shared, because several TimeTree accounts usually
# feed a single Google account.
_current_profile = os.environ.get("CALSHOOT_PROFILE") or DEFAULT_PROFILE
_PROFILE_SAFE = re.compile(r"[^A-Za-z0-9._-]")

# TimeTree's internal web API. Not a public/supported surface: it can change
# without notice, which is the tradeoff for the official API being shut down.
TIMETREE_API_V1 = "https://timetreeapp.com/api/v1"
TIMETREE_API_V2 = "https://timetreeapp.com/api/v2"
TIMETREE_USER_AGENT = "web/2.1.0/en"

# TimeTree allows 600 requests per 10 minutes per IP/token.
TIMETREE_RATE_LIMIT_REQUESTS = 600
TIMETREE_RATE_LIMIT_WINDOW_SECONDS = 600

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ---------------------------------------------------------------------------
# Naming of the Google calendars CalShoot creates.
#
# CHANGE THIS LINE to rename them. Two placeholders are available:
#   {profile}  the TimeTree account, e.g. "default" or "fleet2"
#   {calendar} the TimeTree calendar name, e.g. "Rochi"
# Either may be left out. Examples:
#   "{profile} {calendar} TimeTree"  ->  "default Rochi TimeTree"
#   "TimeTree: {calendar}"           ->  "TimeTree: Rochi"
#   "{calendar} (charter)"           ->  "Rochi (charter)"
#
# Or set CALSHOOT_CALENDAR_NAME in the environment to override without editing.
#
# `reset` recognises its own calendars by matching this same template, so the
# two can never drift apart. Renaming only affects calendars created after the
# change; rename existing ones in Google yourself, or reset and re-link.
# ---------------------------------------------------------------------------
CALENDAR_NAME_TEMPLATE = (
    os.environ.get("CALSHOOT_CALENDAR_NAME") or "{profile} {calendar} TimeTree"
)

_PLACEHOLDER = re.compile(r"(\{profile\}|\{calendar\})")


def calendar_display_name(calendar: str, profile: str | None = None) -> str:
    """Render the Google calendar name for a TimeTree calendar."""
    return CALENDAR_NAME_TEMPLATE.format(
        profile=profile or current_profile(), calendar=calendar
    ).strip()


def looks_like_our_calendar(summary: str | None) -> bool:
    """True when a Google calendar name matches the configured template.

    This is a whole-name structural match, which is stricter than a substring
    test but is not proof of authorship: with a template ending in a literal,
    any name ending the same way still matches. It exists only as a fallback
    for links created before `created_by_calshoot` was recorded. That flag,
    not this, is the authoritative signal.
    """
    if not summary:
        return False
    pattern = "".join(
        ".+" if part in ("{profile}", "{calendar}") else re.escape(part)
        for part in _PLACEHOLDER.split(CALENDAR_NAME_TEMPLATE)
        if part
    )
    try:
        return re.fullmatch(pattern, summary.strip()) is not None
    except re.error:
        return False


def app_home() -> Path:
    """Return the private directory holding session, token and state files."""
    override = os.environ.get("CALSHOOT_HOME")
    root = Path(override).expanduser() if override else DEFAULT_HOME
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def set_profile(name: str | None) -> None:
    """Select the profile all later path lookups resolve against."""
    global _current_profile
    _current_profile = name or DEFAULT_PROFILE


def current_profile() -> str:
    return _current_profile


def _safe_profile(name: str) -> str:
    """Reduce a profile name to something safe to use as a directory."""
    cleaned = _PROFILE_SAFE.sub("_", name).strip("._-")
    return cleaned or DEFAULT_PROFILE


def profile_home(name: str | None = None) -> Path:
    """Private directory for one TimeTree account."""
    root = app_home() / "profiles" / _safe_profile(name or current_profile())
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def list_profiles() -> list[str]:
    """Names of profiles that have been set up."""
    container = app_home() / "profiles"
    if not container.exists():
        return []
    return sorted(p.name for p in container.iterdir() if p.is_dir())


def migrate_legacy_layout() -> list[str]:
    """Move pre-profile files into the default profile.

    Earlier versions kept a single session and state file at the root. Moving
    them preserves an existing setup instead of silently starting over.
    """
    root = app_home()
    target = root / "profiles" / DEFAULT_PROFILE
    moved = []
    for name in ("timetree_session.json", "state.db"):
        legacy = root / name
        if legacy.exists() and not (target / name).exists():
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o700)
            legacy.rename(target / name)
            moved.append(name)
    return moved


def session_path() -> Path:
    """Cached TimeTree session cookie (never the password)."""
    return profile_home() / "timetree_session.json"


def google_token_path() -> Path:
    """Cached Google OAuth token, shared across profiles."""
    return app_home() / "google_token.json"


def google_client_secret_path() -> Path:
    """OAuth client secret downloaded from Google Cloud Console."""
    override = os.environ.get("CALSHOOT_GOOGLE_CLIENT_SECRET")
    if override:
        return Path(override).expanduser()
    return app_home() / "google_client_secret.json"


def state_path() -> Path:
    """SQLite database holding sync state and the event id map."""
    return profile_home() / "state.db"


def write_private(path: Path, text: str) -> None:
    """Write a file only the current user can read, atomically.

    Written to a temporary file alongside the target and then renamed, so a
    concurrent reader sees either the old contents or the new ones and never a
    half-written file. The scheduled job refreshes the Google token on its own
    schedule, which can overlap a sync you start by hand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
