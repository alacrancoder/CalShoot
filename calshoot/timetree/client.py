"""Client for TimeTree's internal web API.

The official TimeTree API (developers.timetreeapp.com) was shut down on
2023-12-22 and its docs now redirect to the termination notice, so this talks
to the same endpoints the TimeTree web app uses. That surface is unsupported
and may change without warning.

The one thing it gives us that a plain scrape would not is a real incremental
sync cursor: /calendar/<id>/events/sync?since=<cursor> returns only what
changed, which is what makes repeated syncing cheap.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import requests

from calshoot.config import (
    TIMETREE_API_V1,
    TIMETREE_API_V2,
    TIMETREE_RATE_LIMIT_REQUESTS,
    TIMETREE_RATE_LIMIT_WINDOW_SECONDS,
    TIMETREE_USER_AGENT,
)
from calshoot.timetree.auth import SessionExpiredError
from calshoot.timetree.models import TimeTreeCalendar, TimeTreeEvent

logger = logging.getLogger(__name__)

# Stay under TimeTree's documented ceiling rather than at it.
_SAFETY_MARGIN = 0.9


class TimeTreeError(Exception):
    """A TimeTree request failed."""


class _RateLimiter:
    """Sliding-window throttle shared across threads."""

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max(1, int(max_requests * _SAFETY_MARGIN))
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] > self.window_seconds:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                sleep_for = self.window_seconds - (now - self._timestamps[0]) + 0.1
            logger.info("TimeTree rate limit reached; pausing %.0fs.", sleep_for)
            time.sleep(sleep_for)


class TimeTreeClient:
    """Read-only access to the signed-in user's TimeTree calendars."""

    def __init__(self, session_id: str, *, timeout: int = 30, max_retries: int = 3):
        self.session = requests.Session()
        self.session.cookies.set("_session_id", session_id)
        self.timeout = timeout
        self.max_retries = max_retries
        self._limiter = _RateLimiter(
            TIMETREE_RATE_LIMIT_REQUESTS, TIMETREE_RATE_LIMIT_WINDOW_SECONDS
        )

    def _get(self, url: str, params: dict | None = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "X-Timetreea": TIMETREE_USER_AGENT,
        }
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self._limiter.acquire()
            try:
                response = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("TimeTree request error (%s); retrying.", exc)
                time.sleep(2**attempt)
                continue

            if response.status_code in (401, 403):
                raise SessionExpiredError(
                    "TimeTree rejected the cached session. Run `calshoot login` again."
                )
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", 60))
                logger.warning("TimeTree returned 429; waiting %ss.", wait)
                time.sleep(wait)
                continue
            if response.status_code >= 500:
                last_error = TimeTreeError(f"TimeTree returned HTTP {response.status_code}")
                time.sleep(2**attempt)
                continue
            if response.status_code != 200:
                raise TimeTreeError(
                    f"TimeTree returned HTTP {response.status_code} for {url}: {response.text[:300]}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise TimeTreeError(f"TimeTree returned non-JSON for {url}") from exc

        raise TimeTreeError(f"TimeTree request to {url} failed after retries: {last_error}")

    def check_session(self) -> bool:
        """Return True when the current session cookie still works."""
        try:
            self._get(f"{TIMETREE_API_V1}/calendars", params={"since": 0})
            return True
        except SessionExpiredError:
            return False

    def calendars(self, *, include_inactive: bool = False) -> list[TimeTreeCalendar]:
        """List the user's calendars."""
        payload = self._get(f"{TIMETREE_API_V1}/calendars", params={"since": 0})
        calendars = [TimeTreeCalendar.from_dict(item) for item in payload.get("calendars", [])]
        if include_inactive:
            return calendars
        return [calendar for calendar in calendars if calendar.active]

    def labels(self, calendar_id: int) -> dict[int, dict]:
        """Return {label_id: {"name": str, "color": "#rrggbb"}} for a calendar."""
        try:
            payload = self._get(f"{TIMETREE_API_V1}/calendar/{calendar_id}/labels")
        except TimeTreeError:
            logger.warning("Could not read labels for calendar %s.", calendar_id)
            return {}

        labels: dict[int, dict] = {}
        for label in payload.get("calendar_labels", []):
            label_id = label.get("id")
            if label_id is None:
                continue
            color = label.get("color", "")
            labels[label_id] = {
                "name": label.get("name") or "",
                "color": f"#{color:06x}" if isinstance(color, int) else color,
            }
        return labels

    def changed_since(
        self, calendar_id: int, high_water: int = 0, *, max_pages: int = 500
    ) -> tuple[list[TimeTreeEvent], int]:
        """Fetch events updated after `high_water`.

        The `since` parameter is easy to misread. It does NOT mean "changed
        after this time" — it means "updated *before* this time", and the feed
        is ordered newest-first. The `since` value in each response is the
        *oldest* updated_at in that batch, meant to be fed back to page
        further into the past. Feeding back the final value returns nothing.

        So incremental sync works by walking backwards from the newest record
        and stopping as soon as a page reaches past the previous run's
        high-water mark. In practice that is a single request per calendar.

        Returns the changed events and the new high-water mark to store.
        """
        collected: dict[str, TimeTreeEvent] = {}
        cursor = 0
        new_high_water = high_water
        pages = 0

        for _ in range(max_pages):
            payload = self._get(
                f"{TIMETREE_API_V1}/calendar/{calendar_id}/events/sync",
                params={"since": cursor},
            )
            pages += 1
            batch = payload.get("events") or []
            if not batch:
                break

            for item in batch:
                event = TimeTreeEvent.from_dict(item)
                updated = event.updated_at or 0
                if updated > new_high_water:
                    new_high_water = updated
                if updated > high_water and event.uuid:
                    collected[event.uuid] = event

            next_cursor = payload.get("since")
            if next_cursor is None or next_cursor == cursor:
                break
            # This page already reaches back past everything we have seen, so
            # every older page is known too.
            if high_water and next_cursor <= high_water:
                break
            if not payload.get("chunk"):
                break
            cursor = next_cursor
        else:
            logger.warning(
                "Hit the %s-page ceiling for calendar %s; more history may remain.",
                max_pages,
                calendar_id,
            )

        logger.info(
            "Calendar %s: %d changed event(s) in %d request(s); high-water %s -> %s.",
            calendar_id,
            len(collected),
            pages,
            high_water,
            new_high_water,
        )
        return list(collected.values()), new_high_water

    def public_calendar_events(self, calendar_id: int) -> list[TimeTreeEvent]:
        """Fetch events from a public calendar (cursor pagination, no sync)."""
        events: list[TimeTreeEvent] = []
        cursor = None
        while True:
            params = {"from": 0}
            if cursor:
                params["cursor"] = cursor
            payload = self._get(
                f"{TIMETREE_API_V2}/public_calendars/{calendar_id}/public_events", params=params
            )
            events.extend(
                TimeTreeEvent.from_dict(item) for item in payload.get("public_events", [])
            )
            paging = payload.get("paging") or {}
            if not paging.get("next"):
                break
            cursor = paging.get("next_cursor")
            if not cursor:
                break
        return events
