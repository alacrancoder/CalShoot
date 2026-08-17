"""TimeTree sign-in.

CalShoot never stores your TimeTree password and never writes it to disk.
The password is read straight from an interactive prompt (or from an
environment variable you set yourself), exchanged once for a session cookie,
and then dropped. Only the session cookie is cached.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import time
import uuid as uuid_lib

import requests

from calshoot.config import (
    TIMETREE_API_V1,
    TIMETREE_USER_AGENT,
    session_path,
    write_private,
)

logger = logging.getLogger(__name__)

# TimeTree error codes seen on the sign-in endpoint.
_ERR_BAD_CREDENTIALS = -702
_ERR_RATE_LIMITED = -495


class AuthenticationError(Exception):
    """TimeTree refused the sign-in."""


class InvalidCredentialsError(AuthenticationError):
    """Wrong email or password."""


class RateLimitedError(AuthenticationError):
    """Too many sign-in attempts."""


class SessionExpiredError(AuthenticationError):
    """The cached session cookie is no longer accepted."""


def _error_code(response: requests.Response) -> int | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error")
    return error.get("code") if isinstance(error, dict) else None


def login(email: str, password: str) -> str:
    """Exchange credentials for a session id. The password is not retained."""
    response = requests.put(
        f"{TIMETREE_API_V1}/auth/email/signin",
        json={
            "uid": email,
            "password": password,
            "uuid": uuid_lib.uuid4().hex,
        },
        headers={
            "Content-Type": "application/json",
            "X-Timetreea": TIMETREE_USER_AGENT,
        },
        timeout=30,
    )

    if response.status_code != 200:
        code = _error_code(response)
        if code == _ERR_BAD_CREDENTIALS:
            raise InvalidCredentialsError("TimeTree rejected that email or password.")
        if code == _ERR_RATE_LIMITED:
            raise RateLimitedError("TimeTree is rate limiting sign-ins. Try again later.")
        raise AuthenticationError(f"TimeTree sign-in failed (HTTP {response.status_code}).")

    session_id = response.cookies.get("_session_id")
    if not session_id:
        raise AuthenticationError("TimeTree accepted the sign-in but returned no session cookie.")
    return session_id


def save_session(session_id: str, email: str) -> None:
    """Cache the session cookie with owner-only permissions."""
    write_private(
        session_path(),
        json.dumps({"session_id": session_id, "email": email, "created_at": int(time.time())}),
    )


def load_session() -> str | None:
    """Return the cached session cookie, or None if there isn't one."""
    path = session_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("session_id")
    except (ValueError, OSError):
        logger.warning("Cached TimeTree session is unreadable; ignoring it.")
        return None


def clear_session() -> None:
    session_path().unlink(missing_ok=True)


def obtain_session(*, interactive: bool = True, force: bool = False) -> str:
    """Return a usable session id, signing in only when necessary.

    Credentials are taken from TIMETREE_EMAIL / TIMETREE_PASSWORD when set,
    otherwise from an interactive prompt. Set them yourself; CalShoot will
    not ask anyone else for them.
    """
    if not force:
        cached = load_session()
        if cached:
            return cached

    email = os.environ.get("TIMETREE_EMAIL")
    password = os.environ.get("TIMETREE_PASSWORD")

    if not email or not password:
        if not interactive:
            raise AuthenticationError(
                "No cached TimeTree session and no credentials in the environment. "
                "Run `calshoot login` once from a terminal, or set TIMETREE_EMAIL "
                "and TIMETREE_PASSWORD."
            )
        email = email or input("TimeTree email: ").strip()
        password = password or getpass.getpass("TimeTree password (not stored): ")

    session_id = login(email, password)
    del password
    save_session(session_id, email)
    return session_id
