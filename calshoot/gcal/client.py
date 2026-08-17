"""Google Calendar write client.

Because event ids are derived from TimeTree uuids, writes are idempotent:
re-running a sync converges on the same events instead of duplicating them.
"""

from __future__ import annotations

import logging
import random
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = (403, 429, 500, 502, 503, 504)
_RETRYABLE_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "backendError")
_MAX_ATTEMPTS = 5


class GoogleCalendarError(Exception):
    """A Google Calendar request failed."""


class GoogleQuotaError(GoogleCalendarError):
    """A daily Google quota is exhausted.

    Distinct from a rate limit: waiting seconds does not help, so this is
    never retried. Google caps how many calendars an account may create per
    day, which bulk linking can reach.
    """


def _reason(error: HttpError) -> str:
    try:
        details = error.error_details
        if isinstance(details, list) and details:
            return details[0].get("reason", "")
    except (AttributeError, KeyError, TypeError):
        pass
    return ""


def _should_retry(error: HttpError) -> bool:
    status = getattr(error.resp, "status", None)
    if status in (500, 502, 503, 504, 429):
        return True
    # 403 is only retryable when it is a rate limit, not a permission problem.
    if status == 403:
        return _reason(error) in _RETRYABLE_REASONS
    return False


def _with_retries(description: str, call):
    """Execute a Google API request with exponential backoff."""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return call.execute()
        except HttpError as error:
            if not _should_retry(error) or attempt == _MAX_ATTEMPTS - 1:
                raise
            delay = (2**attempt) + random.uniform(0, 1)
            logger.warning(
                "%s failed (HTTP %s); retrying in %.1fs.",
                description,
                getattr(error.resp, "status", "?"),
                delay,
            )
            time.sleep(delay)
    raise GoogleCalendarError(f"{description} exhausted retries")


class GoogleCalendarClient:
    """Thin wrapper over the Calendar v3 API."""

    def __init__(self, credentials):
        self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    # -- calendars --------------------------------------------------------

    def list_calendars(self) -> list[dict]:
        calendars: list[dict] = []
        page_token = None
        while True:
            response = _with_retries(
                "calendarList.list",
                self.service.calendarList().list(pageToken=page_token, maxResults=250),
            )
            calendars.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return calendars

    def find_calendar_by_summary(self, summary: str) -> dict | None:
        for calendar in self.list_calendars():
            if calendar.get("summary") == summary:
                return calendar
        return None

    def rename_calendar(self, calendar_id: str, summary: str) -> dict:
        """Change a calendar's display name, leaving its events untouched."""
        return _with_retries(
            "calendars.patch",
            self.service.calendars().patch(
                calendarId=calendar_id, body={"summary": summary}
            ),
        )

    def get_calendar_entry(self, calendar_id: str) -> dict | None:
        """Fetch one calendarList entry, including accessRole and primary flag."""
        try:
            return _with_retries(
                "calendarList.get",
                self.service.calendarList().get(calendarId=calendar_id),
            )
        except HttpError as error:
            if getattr(error.resp, "status", None) in (404, 410):
                return None
            raise

    def delete_calendar(self, calendar_id: str) -> bool:
        """Permanently delete a secondary calendar. Returns False if absent.

        Refuses the primary calendar outright: calendars().delete on 'primary'
        does not remove it, it wipes every event in the account's main
        calendar. That must never be reachable from here.
        """
        if calendar_id in ("primary", ""):
            raise GoogleCalendarError("Refusing to delete the primary calendar.")
        try:
            _with_retries(
                "calendars.delete", self.service.calendars().delete(calendarId=calendar_id)
            )
            return True
        except HttpError as error:
            if getattr(error.resp, "status", None) in (404, 410):
                return False
            raise

    def create_calendar(self, summary: str, time_zone: str | None = None) -> dict:
        body = {"summary": summary}
        if time_zone:
            body["timeZone"] = time_zone
        try:
            return _with_retries(
                "calendars.insert", self.service.calendars().insert(body=body)
            )
        except HttpError as error:
            if _reason(error) == "quotaExceeded":
                raise GoogleQuotaError(
                    "Google's daily limit for creating calendars is exhausted. "
                    "It resets after about 24 hours; re-run the same command "
                    "then to pick up where this left off."
                ) from error
            raise

    # -- events -----------------------------------------------------------

    def get_event(self, calendar_id: str, event_id: str) -> dict | None:
        try:
            return _with_retries(
                "events.get",
                self.service.events().get(calendarId=calendar_id, eventId=event_id),
            )
        except HttpError as error:
            if getattr(error.resp, "status", None) in (404, 410):
                return None
            raise

    def upsert_event(self, calendar_id: str, body: dict, *, expect_existing: bool = False) -> dict:
        """Create or update an event, keyed on the id already in `body`.

        Tries the operation we expect to succeed first, then falls back, so the
        common path costs a single API call.
        """
        event_id = body.get("id")
        if not event_id:
            raise GoogleCalendarError("Cannot upsert an event without an id.")

        operations = (
            [self._update, self._insert] if expect_existing else [self._insert, self._update]
        )

        last_error: HttpError | None = None
        for operation in operations:
            try:
                return operation(calendar_id, event_id, body)
            except HttpError as error:
                status = getattr(error.resp, "status", None)
                # 409: id already exists -> update instead.
                # 404/410: not there (or purged) -> insert instead.
                if status in (404, 409, 410):
                    last_error = error
                    continue
                raise

        raise GoogleCalendarError(
            f"Could not upsert event {event_id} on {calendar_id}: {last_error}"
        )

    def _insert(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return _with_retries(
            "events.insert",
            self.service.events().insert(calendarId=calendar_id, body=body),
        )

    def _update(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return _with_retries(
            "events.update",
            self.service.events().update(calendarId=calendar_id, eventId=event_id, body=body),
        )

    def delete_event(self, calendar_id: str, event_id: str) -> bool:
        """Delete an event. Returns False when it was already gone."""
        try:
            _with_retries(
                "events.delete",
                self.service.events().delete(calendarId=calendar_id, eventId=event_id),
            )
            return True
        except HttpError as error:
            if getattr(error.resp, "status", None) in (404, 410):
                return False
            raise

    def find_instance(
        self, calendar_id: str, master_event_id: str, original_start: str
    ) -> dict | None:
        """Find one occurrence of a recurring event by its original start time."""
        try:
            response = _with_retries(
                "events.instances",
                self.service.events().instances(
                    calendarId=calendar_id,
                    eventId=master_event_id,
                    originalStart=original_start,
                    maxResults=2,
                ),
            )
        except HttpError as error:
            if getattr(error.resp, "status", None) in (400, 404, 410):
                return None
            raise

        items = response.get("items", [])
        return items[0] if items else None

    def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return _with_retries(
            "events.patch",
            self.service.events().patch(calendarId=calendar_id, eventId=event_id, body=body),
        )
