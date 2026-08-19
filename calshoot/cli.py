"""Command line interface for CalShoot."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone

from calshoot import config
from calshoot.gcal.auth import GoogleAuthError, clear_credentials, load_credentials
from calshoot.gcal.client import GoogleCalendarClient, GoogleQuotaError
from calshoot.state import State
from calshoot.sync import Syncer
from calshoot.timetree import auth as tt_auth
from calshoot.timetree.client import TimeTreeClient

logger = logging.getLogger("calshoot")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("googleapiclient").setLevel(logging.ERROR)


def _timetree(interactive: bool = True) -> TimeTreeClient:
    return TimeTreeClient(tt_auth.obtain_session(interactive=interactive))


def _google(interactive: bool = True) -> GoogleCalendarClient:
    return GoogleCalendarClient(load_credentials(interactive=interactive))


# -- commands -------------------------------------------------------------


def cmd_login(args) -> int:
    profile = config.current_profile()
    print(f"Signing in to the {profile!r} profile.")
    session_id = tt_auth.obtain_session(force=True)
    calendars = TimeTreeClient(session_id).calendars()
    print(f"Signed in. {len(calendars)} calendar(s) visible on this account.")
    return 0


def cmd_logout(args) -> int:
    tt_auth.clear_session()
    if args.google:
        clear_credentials()
        print("Cleared TimeTree session and Google token.")
    else:
        print("Cleared TimeTree session.")
    return 0


def cmd_google_login(args) -> int:
    client = _google()
    calendars = client.list_calendars()
    print(f"Authorised with Google. {len(calendars)} calendar(s) visible.")
    return 0


def cmd_calendars(args) -> int:
    print("TimeTree calendars:")
    for calendar in _timetree().calendars():
        print(f"  {calendar.id:<12} {calendar.name}")

    if args.google:
        print("\nGoogle calendars:")
        for calendar in _google().list_calendars():
            access = calendar.get("accessRole", "")
            writable = access in ("owner", "writer")
            marker = " " if writable else "  (read-only)"
            print(f"  {calendar['id']:<45} {calendar.get('summary', '')}{marker}")
    return 0


def _link_one(google, state, source, *, target_id, name_override, titles) -> bool:
    """Link one TimeTree calendar. Returns True if a Google calendar was created.

    `titles` maps existing Google calendar names to ids, fetched once by the
    caller so linking twenty boats costs one listing rather than twenty.
    """
    made_by_us = False
    if target_id in (None, "new"):
        name = name_override or config.calendar_display_name(source.name)
        existing_id = titles.get(name)
        if existing_id:
            target_id = existing_id
            print(f"  {source.name}: reusing {name!r}")
        else:
            target_id = google.create_calendar(name)["id"]
            titles[name] = target_id
            made_by_us = True
            print(f"  {source.name}: created {name!r}")
    else:
        print(f"  {source.name}: linked to {target_id}")

    state.link_calendar(source.id, source.name, target_id, created_by_calshoot=made_by_us)
    return made_by_us


def cmd_link(args) -> int:
    timetree = _timetree()
    google = _google()
    calendars = {calendar.id: calendar for calendar in timetree.calendars()}

    requested = list(args.timetree_calendar or [])

    if args.all:
        if requested:
            print(
                "--all cannot be combined with --timetree-calendar.", file=sys.stderr
            )
            return 2
        requested = list(calendars)
    elif not requested:
        print("TimeTree calendars:")
        for calendar in calendars.values():
            print(f"  {calendar.id:<12} {calendar.name}")
        print()
        try:
            requested = [int(input("TimeTree calendar id: ").strip())]
        except ValueError:
            print("Not a valid calendar id.", file=sys.stderr)
            return 2

    if len(requested) > 1 and (args.google_calendar or args.name):
        print(
            "--google-calendar and --name apply to a single calendar only.",
            file=sys.stderr,
        )
        return 2

    unknown = [cid for cid in requested if cid not in calendars]
    if unknown:
        joined = ", ".join(str(cid) for cid in unknown)
        print(f"No TimeTree calendar with id {joined}.", file=sys.stderr)
        return 2

    titles = {
        entry.get("summary"): entry["id"]
        for entry in google.list_calendars()
        if entry.get("summary")
    }

    with State() as state:
        linked_already = {link.tt_calendar_id for link in state.links()}
        # Only skip in bulk mode. An explicitly named calendar is re-linked, so
        # you can repoint one at a different Google calendar.
        todo, skipped = [], []
        for cid in requested:
            if args.all and cid in linked_already:
                skipped.append(calendars[cid])
            else:
                todo.append(calendars[cid])

        for source in skipped:
            print(f"  {source.name}: already linked, left alone")

        if not todo:
            print("Nothing new to link.")
            return 0

        print(f"Linking {len(todo)} calendar(s) on profile {config.current_profile()!r}:")
        created = 0
        done = 0
        for source in todo:
            try:
                created += _link_one(
                    google,
                    state,
                    source,
                    target_id=args.google_calendar,
                    name_override=args.name,
                    titles=titles,
                )
            except GoogleQuotaError as exc:
                # Everything linked so far is already committed, and --all
                # skips what is linked, so re-running resumes cleanly.
                remaining = len(todo) - done
                print(f"\n{exc}", file=sys.stderr)
                print(
                    f"Linked {done} of {len(todo)} before stopping; "
                    f"{remaining} left for the next run.",
                    file=sys.stderr,
                )
                return 1
            done += 1

    print(f"\nLinked {len(todo)} calendar(s); created {created} in Google.")
    print("Next: calshoot sync --future-only --dry-run")
    return 0


def cmd_unlink(args) -> int:
    with State() as state:
        state.unlink_calendar(args.timetree_calendar)
    print(f"Unlinked TimeTree calendar {args.timetree_calendar}.")
    print("Events already copied into Google were left in place.")
    return 0


def cmd_profiles(args) -> int:
    names = config.list_profiles()
    if not names:
        print("No profiles yet. Run `calshoot login` to create the default one.")
        return 0

    print("TimeTree accounts:")
    for name in names:
        config.set_profile(name)
        signed_in = "signed in" if tt_auth.load_session() else "needs login"
        with State() as state:
            count = len(state.links())
        print(f"  {name:<16} {signed_in:<12} {count} calendar(s) linked")
    print("\nUse `--profile NAME` on any command to pick one.")
    return 0


def cmd_status(args) -> int:
    print(f"Config directory: {config.app_home()}")
    print(f"Google token:     {'present' if config.google_token_path().exists() else 'missing'}")

    profiles = config.list_profiles() if args.all else [config.current_profile()]
    for name in profiles or [config.DEFAULT_PROFILE]:
        config.set_profile(name)
        print(f"\n--- profile: {name} ---")
        print(f"TimeTree session: {'present' if tt_auth.load_session() else 'missing'}")
        _print_links()
    return 0


def _print_links() -> None:
    with State() as state:
        links = state.links()
        if not links:
            print("No calendars linked. Run `calshoot link`.")
            return
        print("Linked calendars:")
        for link in links:
            count = state.mapped_event_count(link.tt_calendar_id)
            when = link.last_synced_at
            stamp = (
                datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M") if when else "never"
            )
            print(f"  {link.tt_calendar_name}")
            print(f"    -> {link.google_calendar_id}")
            watermark = (
                datetime.fromtimestamp(link.high_water / 1000).strftime("%Y-%m-%d %H:%M")
                if link.high_water
                else "never synced"
            )
            print(f"    {count} events mapped, changes seen up to {watermark}")
            print(f"    last run {stamp}")


def _skip_before_ms(value: str | None) -> int | None:
    """Parse a YYYY-MM-DD cutoff into epoch milliseconds."""
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"--skip-before needs a YYYY-MM-DD date, got {value!r}")
    return int(parsed.timestamp() * 1000)


def notify(title: str, message: str) -> None:
    """Best-effort macOS notification. Never raises, never blocks a sync."""
    if sys.platform != "darwin":
        return
    script = (
        f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script], check=False, timeout=10, capture_output=True
        )
    except Exception as exc:  # notification failure must never matter
        logger.debug("Could not post notification: %s", exc)


def _sync_one_profile(args, google, skip_before, only) -> tuple[list, list[str]]:
    """Sync the profile currently selected. Returns (reports, problems)."""
    with State() as state:
        links = state.links()
        if not links:
            return [], ["nothing linked"]

        selected = [link for link in links if only is None or link.tt_calendar_id in only]
        if not selected:
            return [], ["none of the requested calendars are linked"]

        if args.full:
            for link in selected:
                state.reset_high_water(link.tt_calendar_id)
            print(f"Reset {len(selected)} calendar(s); re-scanning in full.")

        syncer = Syncer(
            _timetree(interactive=not args.non_interactive),
            google,
            state,
            dry_run=args.dry_run,
            skip_before=skip_before,
            rename_calendars=not args.no_rename,
        )
        return syncer.sync_all(only=only), []


def cmd_sync(args) -> int:
    if args.future_only:
        # Resolved per run, so a scheduled job keeps advancing with the date.
        skip_before = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    else:
        skip_before = _skip_before_ms(args.skip_before)
    only = set(args.calendar) if args.calendar else None

    if args.all_profiles and only:
        print(
            "--calendar cannot be combined with --all-profiles: calendar ids "
            "belong to one account.",
            file=sys.stderr,
        )
        return 2

    if args.all_profiles:
        profiles = config.list_profiles() or [config.DEFAULT_PROFILE]
    else:
        profiles = [config.current_profile()]

    prefix = "[dry run] " if args.dry_run else ""
    google = None
    problems: list[str] = []
    synced_any = False

    for profile in profiles:
        config.set_profile(profile)
        if len(profiles) > 1:
            print(f"\n=== {profile} ===")

        try:
            if google is None:
                google = _google(interactive=not args.non_interactive)
            reports, notes = _sync_one_profile(args, google, skip_before, only)
        except (tt_auth.AuthenticationError, GoogleAuthError) as exc:
            # One broken account must not stop the others.
            print(f"  {exc}", file=sys.stderr)
            problems.append(f"{profile}: {exc}")
            continue

        for note in notes:
            print(f"  {note}")

        for report in reports:
            synced_any = True
            print(f"{prefix}{report.calendar_name}: {report.summary()}")
            for note in report.skipped[: args.max_notes]:
                print(f"    skipped: {note}")
            for error in report.errors[: args.max_notes]:
                print(f"    error:   {error}")
            if not report.ok:
                problems.append(f"{profile}/{report.calendar_name}: {report.errors[0]}")

    if problems:
        if args.notify:
            headline = problems[0]
            extra = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
            notify("CalShoot sync failed", f"{headline}{extra}")
        return 1

    if args.notify and not synced_any:
        notify("CalShoot sync did nothing", "No calendars are linked. Run `calshoot link`.")
    return 0


def deletion_plan(google, links) -> tuple[list, list]:
    """Split links into calendars safe to delete and ones to leave alone.

    A calendar is only deletable when every guard passes: it still exists, it
    is not the primary calendar, you own it, and CalShoot created it (recorded
    in state, or recognisable by the name it uses).
    """
    deletable, protected = [], []
    for link in links:
        entry = google.get_calendar_entry(link.google_calendar_id)
        if entry is None:
            protected.append((link, "already gone from Google"))
        elif entry.get("primary") or link.google_calendar_id in ("primary", ""):
            protected.append((link, "this is your primary calendar"))
        elif entry.get("accessRole") != "owner":
            protected.append((link, f"you do not own it ({entry.get('accessRole')})"))
        elif not (
            link.created_by_calshoot or config.looks_like_our_calendar(entry.get("summary"))
        ):
            protected.append((link, "CalShoot did not create it"))
        else:
            deletable.append((link, entry))
    return deletable, protected


def cmd_reset(args) -> int:
    """Delete the Google calendars CalShoot created and clear local state."""
    profile = config.current_profile()

    with State() as state:
        links = state.links()
        if not links:
            print(f"Nothing linked on profile {profile!r}. Already clean.")
            return 0

        google = _google()
        deletable, protected = deletion_plan(google, links)

        print(f"Profile {profile!r}\n")
        if deletable:
            print("Google calendars to DELETE, with all their events:")
            for link, entry in deletable:
                print(f"  {entry.get('summary')}  ({link.tt_calendar_name})")
        if protected:
            print("\nLeft untouched:")
            for link, reason in protected:
                print(f"  {link.tt_calendar_name} — {reason}")

        if not args.yes:
            print(
                f"\nThis was a preview. Re-run with --yes to delete "
                f"{len(deletable)} calendar(s). This cannot be undone."
            )
            return 0

        failures = 0
        for link, entry in deletable:
            try:
                google.delete_calendar(link.google_calendar_id)
                print(f"Deleted {entry.get('summary')!r}.")
            except Exception as exc:
                failures += 1
                print(f"Could not delete {entry.get('summary')!r}: {exc}", file=sys.stderr)

        state.clear_everything()
        print(f"\nCleared local sync state for profile {profile!r}.")
        if protected:
            print(
                f"{len(protected)} calendar(s) were left in Google and are now unlinked."
            )
        return 1 if failures else 0


def cmd_doctor(args) -> int:
    """Check credentials and report what the live API actually returns.

    Some details of TimeTree's internal API cannot be confirmed without a real
    account, most importantly whether removed events come back through the
    sync feed as tombstones. This reports what your data shows.
    """
    session = tt_auth.load_session()
    if not session:
        print("No TimeTree session. Run `calshoot login`.", file=sys.stderr)
        return 2

    timetree = TimeTreeClient(session)
    if not timetree.check_session():
        print("TimeTree session is no longer valid. Run `calshoot login`.", file=sys.stderr)
        return 2
    print("TimeTree session: OK")

    try:
        google = _google(interactive=False)
        print(f"Google auth:      OK ({len(google.list_calendars())} calendars)")
    except GoogleAuthError as exc:
        print(f"Google auth:      MISSING ({exc})")

    calendars = timetree.calendars()
    print(f"\nTimeTree calendars: {len(calendars)}")

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    totals = {"events": 0, "future": 0}

    for calendar in calendars:
        events, high_water = timetree.changed_since(calendar.id, 0)
        keys: set[str] = set()
        for event in events:
            keys.update(event.raw.keys())
        all_day = sum(1 for e in events if e.all_day)
        recurring = sum(1 for e in events if e.is_recurrence_master)
        children = sum(1 for e in events if e.is_recurrence_child)
        memos = sum(1 for e in events if e.is_memo)
        tombstones = sum(1 for e in events if e.deleted)
        future = sum(
            1
            for e in events
            if e.is_recurrence_master
            or (e.end_at or e.start_at or 0) >= now_ms
        )
        totals["events"] += len(events)
        totals["future"] += future

        last_change = (
            datetime.fromtimestamp(high_water / 1000).strftime("%Y-%m-%d")
            if high_water
            else "unknown"
        )
        print(f"\n  {calendar.name} (id {calendar.id})")
        print(f"    {len(events)} events, {future} of them today or later")
        print(f"    last changed {last_change}")
        print(f"    {all_day} all-day, {recurring} recurring masters,")
        print(f"    {children} edited occurrences, {memos} memos, {tombstones} tombstones")
        if args.verbose:
            print(f"    fields present: {', '.join(sorted(keys))}")

        if args.dump:
            path = config.app_home() / f"doctor_calendar_{calendar.id}.json"
            config.write_private(
                path,
                json.dumps([e.raw for e in events], indent=2, ensure_ascii=False, default=str),
            )
            print(f"    raw payload written to {path}")

    print(
        f"\nTotal: {totals['events']} events, {totals['future']} today or later "
        f"(what --future-only would sync)."
    )
    return 0


# -- argument parsing -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calshoot",
        description="One-way sync from TimeTree calendars to Google Calendar.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    # Accepted by every subcommand, so `calshoot sync --profile work` works.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--profile",
        metavar="NAME",
        help="which TimeTree account to act on (default: 'default')",
    )

    p = sub.add_parser(
        "login", parents=[common], help="sign in to TimeTree (password is never stored)"
    )
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", parents=[common], help="clear cached credentials")
    p.add_argument("--google", action="store_true", help="also clear the Google token")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("google-login", help="authorise Google Calendar access")
    p.set_defaults(func=cmd_google_login)

    p = sub.add_parser("profiles", help="list configured TimeTree accounts")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("calendars", parents=[common], help="list calendars")
    p.add_argument("--google", action="store_true", help="also list Google calendars")
    p.set_defaults(func=cmd_calendars)

    p = sub.add_parser(
        "link", parents=[common], help="link a TimeTree calendar to a Google calendar"
    )
    p.add_argument(
        "--timetree-calendar",
        type=int,
        action="append",
        metavar="ID",
        help="TimeTree calendar id (repeat to link several)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="link every calendar on this account that is not linked yet",
    )
    p.add_argument(
        "--google-calendar",
        help="Google calendar id, or 'new' to create one (default: create)",
    )
    p.add_argument("--name", help="name for a newly created Google calendar")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", parents=[common], help="stop syncing a TimeTree calendar")
    p.add_argument("timetree_calendar", type=int)
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser(
        "reset",
        parents=[common],
        help="delete the Google calendars CalShoot created and clear local state",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="actually delete; without it you get a preview only",
    )
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("status", parents=[common], help="show credentials and linked calendars")
    p.add_argument("--all", action="store_true", help="show every profile")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sync", parents=[common], help="sync linked calendars")
    p.add_argument("--dry-run", action="store_true", help="report changes without writing")
    p.add_argument("--full", action="store_true", help="reset cursors and re-scan everything")
    p.add_argument(
        "--calendar",
        type=int,
        action="append",
        metavar="ID",
        help="sync only this TimeTree calendar (repeat for several)",
    )
    cutoff = p.add_mutually_exclusive_group()
    cutoff.add_argument(
        "--skip-before",
        metavar="YYYY-MM-DD",
        help="skip events that finished before this date (recurring series are kept)",
    )
    cutoff.add_argument(
        "--future-only",
        action="store_true",
        help="skip events that already finished, recalculated on every run",
    )
    p.add_argument(
        "--all-profiles",
        action="store_true",
        help="sync every TimeTree account, not just one",
    )
    p.add_argument(
        "--notify",
        action="store_true",
        help="post a macOS notification if the run fails (for scheduled runs)",
    )
    p.add_argument(
        "--no-rename",
        action="store_true",
        help="do not retitle Google calendars when a TimeTree calendar is renamed",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="fail instead of prompting (use for scheduled runs)",
    )
    p.add_argument("--max-notes", type=int, default=10, help="notes to print per calendar")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser(
        "doctor", parents=[common], help="check credentials and inspect live TimeTree data"
    )
    p.add_argument("--dump", action="store_true", help="write raw API payloads for inspection")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    config.set_profile(getattr(args, "profile", None))
    if config.trim_log():
        logger.info("Trimmed the log file; it had grown past its size limit.")
    moved = config.migrate_legacy_layout()
    if moved:
        logger.info("Moved %s into the 'default' profile.", ", ".join(moved))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (tt_auth.AuthenticationError, GoogleAuthError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
