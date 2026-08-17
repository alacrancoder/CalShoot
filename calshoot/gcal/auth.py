"""Google Calendar OAuth.

You create the OAuth client in your own Google Cloud project and approve the
consent screen in your own browser. CalShoot only ever handles the resulting
token, which is cached with owner-only permissions.
"""

from __future__ import annotations

import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from calshoot.config import (
    GOOGLE_SCOPES,
    google_client_secret_path,
    google_token_path,
    write_private,
)

logger = logging.getLogger(__name__)


class GoogleAuthError(Exception):
    """Google authorisation is missing or unusable."""


def load_credentials(*, interactive: bool = True) -> Credentials:
    """Return usable Google credentials, refreshing or prompting as needed."""
    token_path = google_token_path()
    creds: Credentials | None = None

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), GOOGLE_SCOPES)
        except (ValueError, OSError) as exc:
            logger.warning("Cached Google token is unreadable (%s); re-authorising.", exc)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            return creds
        except Exception as exc:
            logger.warning("Google token refresh failed (%s); re-authorising.", exc)

    if not interactive:
        raise GoogleAuthError(
            "No valid Google token and not running interactively. "
            "Run `calshoot google-login` once from a terminal."
        )

    secret_path = google_client_secret_path()
    if not secret_path.exists():
        raise GoogleAuthError(
            f"No OAuth client secret at {secret_path}.\n"
            "Create a Desktop-app OAuth client in Google Cloud Console, download "
            "the JSON, and save it there (or point CALSHOOT_GOOGLE_CLIENT_SECRET at it)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    _save(creds)
    return creds


def _save(creds: Credentials) -> None:
    write_private(google_token_path(), creds.to_json())


def clear_credentials() -> None:
    google_token_path().unlink(missing_ok=True)
