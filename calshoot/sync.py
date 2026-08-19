"""One-way sync engine: TimeTree -> Google Calendar.

TimeTree is treated as the source of truth. Changes made directly in Google on
a synced event are overwritten the next time that event changes in TimeTree.

Safety properties worth knowing:
  * Google event ids are derived from TimeTree uuids, so every write is
    idempotent and a lost state database cannot produce duplicates.
  * The sync cursor only advances when a calendar completes without hard
    errors, so a partial failure is retried rather than silently skipped.
  * Deletes only ever touch events CalShoot created, identified by the
    provenance stamped into each event's private extended properties.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from calshoot import config
from calshoot.canary import (
    DEFAULT_QUIET_DAYS,
    check_event_schema,
    silence_finding,
)
from calshoot.gcal.client import GoogleCalendarClient
from calshoot.mapping import (
    MappingError,
    content_hash,
    google_event_id,
    to_google_event,
)
from calshoot.state import CalendarLink, State
from calshoot.timetree.client import TimeTreeClient
from calshoot.timetree.models import TimeTreeEvent

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    """What a sync run did."""

    calendar_name: str = ""
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    instances_patched: int = 0
    filtered: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.created + self.updated + self.deleted + self.instances_patched

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"{self.created} created",
            f"{self.updated} updated",
            f"{self.deleted} deleted",
            f"{self.unchanged} unchanged",
        ]
        if self.instances_patched:
            parts.append(f"{self.instances_patched} occurrences patched")
        if self.filtered:
            parts.append(f"{self.filtered} too old")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)


def _latest_revision(events: list[TimeTreeEvent]) -> list[TimeTreeEvent]:
    """Collapse a change feed to one record per uuid, keeping the newest."""
    newest: dict[str, TimeTreeEvent] = {}
    for event in events:
        if not event.uuid:
            continue
        existing = newest.get(event.uuid)
        if existing is None or (event.updated_at or 0) >= (existing.updated_at or 0):
            newest[event.uuid] = event
    return list(newest.values())


def _rfc3339(epoch_ms: int) -> str:
    return (
        datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class Syncer:
    """Applies TimeTree changes to Google Calendar."""

    def __init__(
        self,
        timetree: TimeTreeClient,
        google: GoogleCalendarClient,
        state: State,
        *,
        dry_run: bool = False,
        skip_before: int | None = None,
        rename_calendars: bool = True,
        quiet_days: int = DEFAULT_QUIET_DAYS,
    ):
        self.timetree = timetree
        self.google = google
        self.state = state
        self.dry_run = dry_run
        self.rename_calendars = rename_calendars
        self.quiet_days = quiet_days
        # Findings that apply to the run as a whole rather than one calendar.
        self.warnings: list[str] = []
        # Epoch ms. Events that finished before this are not copied, which
        # matters on calendars carrying years of history nobody needs.
        self.skip_before = skip_before

    def sync_all(self, only: set[int] | None = None) -> list[SyncReport]:
        links = self.state.links()
        if only is not None:
            links = [link for link in links if link.tt_calendar_id in only]
            if not links:
                logger.warning("None of the requested calendars are linked.")
        if not links:
            logger.warning("No calendars are linked yet. Run `calshoot link` first.")
            return []

        self._reconcile_names(links)
        # Re-read so reports carry the current names.
        fresh = {link.tt_calendar_id: link for link in self.state.links()}
        links = [fresh.get(link.tt_calendar_id, link) for link in links]
        reports = [self.sync_calendar(link) for link in links]

        # Dead-man's switch. Checked after syncing, against the marks this run
        # just wrote, so a run that genuinely saw changes never trips it.
        quiet = silence_finding(
            [link.high_water for link in self.state.links()],
            int(datetime.now(tz=timezone.utc).timestamp() * 1000),
            self.quiet_days,
        )
        if quiet:
            logger.warning(quiet)
            self.warnings.append(quiet)

        return reports

    def _reconcile_names(self, links: list[CalendarLink]) -> None:
        """Follow TimeTree calendar renames.

        Sync itself is keyed on ids, so a rename never affects correctness.
        This only keeps the names humans read in step: the local record, and
        the Google calendar's own title.

        A Google calendar is only retitled when CalShoot created it AND its
        current title is still exactly what CalShoot would have generated for
        the old name. If you retitled it yourself, that is left alone.
        """
        try:
            current = {c.id: c.name for c in self.timetree.calendars()}
        except Exception as exc:
            logger.debug("Could not refresh calendar names: %s", exc)
            return

        google_titles: dict[str, str] | None = None

        for link in links:
            new_name = current.get(link.tt_calendar_id)
            if not new_name or new_name == link.tt_calendar_name:
                continue

            logger.info(
                "TimeTree calendar %s renamed: %r -> %r",
                link.tt_calendar_id,
                link.tt_calendar_name,
                new_name,
            )
            if self.dry_run:
                continue
            self.state.update_calendar_name(link.tt_calendar_id, new_name)

            if not (self.rename_calendars and link.created_by_calshoot):
                continue

            if google_titles is None:
                try:
                    google_titles = {
                        c["id"]: c.get("summary") or ""
                        for c in self.google.list_calendars()
                    }
                except Exception as exc:
                    logger.debug("Could not read Google calendar titles: %s", exc)
                    google_titles = {}

            actual = google_titles.get(link.google_calendar_id)
            expected_old = config.calendar_display_name(link.tt_calendar_name)
            if actual is None or actual != expected_old:
                logger.info(
                    "Leaving Google title %r alone; it is not the generated name.", actual
                )
                continue

            expected_new = config.calendar_display_name(new_name)
            try:
                self.google.rename_calendar(link.google_calendar_id, expected_new)
                logger.info("Renamed Google calendar to %r.", expected_new)
            except Exception as exc:
                logger.warning("Could not rename Google calendar: %s", exc)

    def _too_old(self, event: TimeTreeEvent) -> bool:
        """True when an event ended before the cutoff.

        Recurrence masters are never filtered: a series that began years ago
        can still be producing occurrences today.
        """
        if self.skip_before is None or event.is_recurrence_master:
            return False
        reference = event.end_at if event.end_at is not None else event.start_at
        return reference is not None and reference < self.skip_before

    def sync_calendar(self, link: CalendarLink) -> SyncReport:
        report = SyncReport(calendar_name=link.tt_calendar_name)
        logger.info(
            "Syncing %r (high-water %s) -> %s",
            link.tt_calendar_name,
            link.high_water,
            link.google_calendar_id,
        )

        try:
            raw_events, new_high_water = self.timetree.changed_since(
                link.tt_calendar_id, link.high_water
            )
        except Exception as exc:
            report.errors.append(f"Could not read TimeTree calendar: {exc}")
            return report

        events = _latest_revision(raw_events)
        if not events:
            logger.info("No changes for %r.", link.tt_calendar_name)
            self._advance_high_water(link, new_high_water, report)
            return report

        # Before trusting any of this, confirm the payloads still have the
        # shape we rely on. If they don't, stop rather than write nonsense
        # into Google — and leave the high-water mark alone so nothing is
        # skipped once the cause is understood.
        schema = check_event_schema([event.raw for event in events])
        if schema.broken:
            report.errors.append(
                f"TimeTree payload shape changed: {schema.describe()}. "
                f"Refusing to sync this calendar until it is understood."
            )
            return report
        if schema.unknown:
            logger.debug(
                "TimeTree sent fields we do not know about (harmless): %s",
                ", ".join(sorted(schema.unknown)),
            )

        labels = self._labels(link.tt_calendar_id)

        tombstones = [e for e in events if e.deleted]
        children = [e for e in events if not e.deleted and e.is_recurrence_child]
        primary = [e for e in events if not e.deleted and not e.is_recurrence_child]

        if self.skip_before is not None:
            kept = [e for e in primary if not self._too_old(e)]
            report.filtered = len(primary) - len(kept)
            primary = kept

        for event in primary:
            self._sync_event(link, event, labels, report)
        for event in children:
            self._sync_instance(link, event, report)
        for event in tombstones:
            self._delete_event(link, event, report)

        self._advance_high_water(link, new_high_water, report)
        logger.info("%s: %s", link.tt_calendar_name, report.summary())
        return report

    # -- individual operations -------------------------------------------

    def _labels(self, tt_calendar_id: int) -> dict:
        try:
            return self.timetree.labels(tt_calendar_id)
        except Exception as exc:
            logger.warning("Could not read labels (%s); colours will be skipped.", exc)
            return {}

    def _sync_event(
        self, link: CalendarLink, event: TimeTreeEvent, labels: dict, report: SyncReport
    ) -> None:
        try:
            body = to_google_event(
                event, timetree_calendar_id=link.tt_calendar_id, labels=labels
            )
        except MappingError as exc:
            report.skipped.append(str(exc))
            return

        new_hash = content_hash(body)
        known_hash = self.state.known_hash(link.tt_calendar_id, event.uuid)

        if known_hash == new_hash:
            report.unchanged += 1
            return

        if self.dry_run:
            if known_hash is None:
                report.created += 1
            else:
                report.updated += 1
            return

        try:
            self.google.upsert_event(
                link.google_calendar_id, body, expect_existing=known_hash is not None
            )
        except Exception as exc:
            report.errors.append(f"{event.title or event.uuid}: {exc}")
            return

        self.state.record_event(
            link.tt_calendar_id,
            event.uuid,
            link.google_calendar_id,
            body["id"],
            new_hash,
        )
        if known_hash is None:
            report.created += 1
        else:
            report.updated += 1

    def _sync_instance(
        self, link: CalendarLink, event: TimeTreeEvent, report: SyncReport
    ) -> None:
        """Apply an edited occurrence of a recurring event.

        TimeTree returns a modified occurrence as its own record pointing back
        at the master. It has to be written as a patch to the matching Google
        instance; creating it as a standalone event would show the occurrence
        twice. When the matching instance cannot be identified the event is
        skipped rather than duplicated.
        """
        master_uuid = event.master_uuid
        if not master_uuid or event.start_at is None:
            report.skipped.append(f"Occurrence {event.uuid} has no master or start time.")
            return

        if self.dry_run:
            report.instances_patched += 1
            return

        master_id = google_event_id(master_uuid)
        try:
            instance = self.google.find_instance(
                link.google_calendar_id, master_id, _rfc3339(event.start_at)
            )
        except Exception as exc:
            report.errors.append(f"Occurrence {event.uuid}: {exc}")
            return

        if instance is None:
            report.skipped.append(
                f"Could not match occurrence {event.uuid} to an instance of {master_uuid}; "
                "left untouched to avoid a duplicate."
            )
            return

        patch = {"summary": (event.title or "(no title)").strip()}
        if event.note:
            patch["description"] = event.note.strip()
        if event.location:
            patch["location"] = event.location.strip()

        try:
            self.google.patch_event(link.google_calendar_id, instance["id"], patch)
            report.instances_patched += 1
        except Exception as exc:
            report.errors.append(f"Occurrence {event.uuid}: {exc}")

    def _delete_event(
        self, link: CalendarLink, event: TimeTreeEvent, report: SyncReport
    ) -> None:
        if self.dry_run:
            report.deleted += 1
            return

        event_id = google_event_id(event.uuid)
        try:
            existed = self.google.delete_event(link.google_calendar_id, event_id)
        except Exception as exc:
            report.errors.append(f"Deleting {event.uuid}: {exc}")
            return

        self.state.forget_event(link.tt_calendar_id, event.uuid)
        if existed:
            report.deleted += 1

    def _advance_high_water(
        self, link: CalendarLink, high_water: int, report: SyncReport
    ) -> None:
        """Move the high-water mark only when the calendar synced cleanly.

        Holding it back on failure means the next run re-reads the same window.
        That is cheap, because unchanged events are detected by hash and cost
        no Google API calls.
        """
        if self.dry_run:
            return
        if not report.ok:
            logger.warning(
                "Holding high-water for %r at %s because the run had errors.",
                link.tt_calendar_name,
                link.high_water,
            )
            return
        if high_water != link.high_water:
            self.state.save_high_water(link.tt_calendar_id, high_water)
