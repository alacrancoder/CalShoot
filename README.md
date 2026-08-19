# CalShoot

One-way calendar sync: **TimeTree → Google Calendar**.

TimeTree is the source of truth. Each TimeTree calendar is mirrored into a
Google calendar, and repeated runs push only what changed.

## Why it works this way

TimeTree's official API was shut down on 2023-12-22 — `developers.timetreeapp.com`
now redirects to the termination notice, so a personal access token is no longer
an option. CalShoot therefore talks to the same internal endpoints the TimeTree
web app uses.

That surface is unofficial and can change without notice — see
**Breakage detection** below for how CalShoot notices when it does.

### The `since` parameter is a trap

`/calendar/<id>/events/sync?since=…` looks like a "changed since" cursor. It is
not. It means **"updated *before* this timestamp"**, and the feed is ordered
newest-first — `since` pages *backwards* into history. The `since` value in
each response is the **oldest** record in that batch.

Feeding that value back as a "what changed?" cursor returns zero events
forever: the first sync looks perfect and every later one silently does
nothing. CalShoot instead keeps a **high-water mark** on `updated_at`, walks
back from the newest record, and stops as soon as a page reaches past it —
one request per calendar in the steady state. See
`TimeTreeClient.changed_since` and `tests/test_timetree_client.py`.

## Install

```bash
cd ~/CalShoot && python3.10 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Setup

### 1. Google OAuth client

You create this in your own Google Cloud project, so the token belongs to you:

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create
   (or pick) a project.
2. **APIs & Services → Library →** enable **Google Calendar API**.
3. **APIs & Services → OAuth consent screen →** choose **External**, fill in the
   required fields, and add your own address under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID →**
   application type **Desktop app**.
5. Download the JSON and save it as `~/.calshoot/google_client_secret.json`.

### 2. Sign in

```bash
calshoot login && calshoot google-login
```

`login` prompts for your TimeTree password, exchanges it once for a session
cookie, and discards it. The password is never written to disk. Only the session
cookie and the Google token are cached, both `0600` in `~/.calshoot/`.

### 3. Check what's there

```bash
calshoot doctor
```

This reports what your live TimeTree data actually contains — event counts,
all-day and recurring breakdowns, and which fields the API returns. Worth
running once before the first sync.

### 4. Link calendars

```bash
calshoot link
```

Interactive: it lists your TimeTree calendars and creates a matching Google
calendar. To link the whole account at once:

```bash
calshoot link --all
```

That skips anything already linked, so it is safe to re-run whenever the fleet
grows.

**Google caps how many calendars an account may create per day** — around 40 in
practice, then `Calendar usage limits exceeded` until it resets about 24 hours
later. Linking stops cleanly, reports how many remain, and keeps everything it
already linked. Because `--all` skips linked calendars, re-running the same
command the next day resumes where it stopped. Spread a large fleet over a few
days.

Repeat `--timetree-calendar` to pick a specific handful, or target an
existing Google calendar for a single boat:

```bash
calshoot link --timetree-calendar 12345 --google-calendar you@gmail.com
```

Linking to a dedicated calendar rather than your main one is strongly
recommended — it keeps synced events separable from everything else.

### 5. Sync

```bash
calshoot sync --dry-run
```

Then, once the numbers look right:

```bash
calshoot sync
```

## Scheduling

`com.calshoot.sync.plist` is a launchd user agent that runs

```
calshoot sync --all-profiles --future-only --non-interactive --notify
```

every 30 minutes, logging to `~/Library/Logs/calshoot.log`. Install it with:

```bash
cp com.calshoot.sync.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.calshoot.sync.plist
```

Run it immediately instead of waiting for the timer:

```bash
launchctl kickstart -p gui/$(id -u)/com.calshoot.sync
```

Stop it:

```bash
launchctl bootout gui/$(id -u)/com.calshoot.sync
```

What to expect:

- **Cheap.** A calendar with no changes costs one TimeTree request, because of
  the high-water mark. Google is only called when something actually changed.
- **Silent when healthy.** `--notify` posts a macOS banner only on failure.
- **Independent accounts.** One broken profile is reported and skipped; the
  others still sync.
- **Sleep-safe.** If the Mac is asleep when a run is due, launchd fires once
  after wake rather than replaying every missed interval. It runs while you are
  logged in, not at the login screen.

One thing it cannot do for you: the TimeTree session eventually expires and
only you can renew it with `calshoot login` — the banner is the cue. The log
looks after itself, trimming to its most recent entries once it passes 1 MB.

If you edit the plist, note that **XML comments cannot contain two consecutive
dashes**, so flags are written there without their leading dashes. Validate any
change with `plutil -lint com.calshoot.sync.plist` before loading it;
`tests/test_scheduling.py` also parses it on every test run.

## Commands

| Command | Purpose |
| --- | --- |
| `calshoot login` | Sign in to TimeTree |
| `calshoot profiles` | List configured TimeTree accounts |
| `calshoot google-login` | Authorise Google Calendar |
| `calshoot calendars --google` | List calendars on both sides |
| `calshoot link` | Wire a TimeTree calendar to a Google calendar (`--all` for every one) |
| `calshoot unlink <id>` | Stop syncing a calendar (leaves Google alone) |
| `calshoot reset` | Delete the calendars CalShoot created, clear state |
| `calshoot status` | Credentials, links, last sync (`--all` for every profile) |
| `calshoot sync` | Sync (`--dry-run`, `--full`, `--calendar`, `--skip-before`, `--non-interactive`) |
| `calshoot doctor` | Check credentials, inspect live data (`--dump`) |

## How it stays safe to re-run

- **Deterministic ids.** Google event ids are derived from TimeTree uuids, so
  every write is idempotent. Deleting `~/.calshoot/state.db` costs a full
  re-scan; it cannot produce duplicate events.
- **High-water mark held on failure.** It only advances when a run finishes
  without hard errors, so a partial failure is retried rather than silently
  skipped. Re-reads are cheap: unchanged events are detected by content hash
  and cost no Google API calls.
- **Scoped deletes.** CalShoot only deletes events it created, identified by
  provenance stamped into each event's private extended properties.
- **Rate limits respected.** TimeTree requests are throttled below the
  documented 600-per-10-minutes ceiling; Google calls retry with backoff.

## What it maps

| TimeTree | Google |
| --- | --- |
| Title, note, location, URL | `summary`, `description`, `location` |
| Start/end (epoch ms, per-event IANA zone) | `start`/`end` with `timeZone` |
| All-day events | `date` values, end made exclusive |
| Recurrence (`RRULE`/`EXDATE`/`RDATE`) | `recurrence` array |
| Alerts (minutes before) | `reminders.overrides` (max 5) |
| Label colour | Nearest of Google's 11 event colours |

## Multiple TimeTree accounts

Each TimeTree account is a **profile** with its own session and sync state.
The Google token is shared, so several TimeTree accounts feed one Google
account.

```bash
calshoot login --profile fleet2
```

Then pass `--profile fleet2` to any command — `calendars`, `link`, `sync`,
`doctor`, `status`. Without it you get the `default` profile.

```bash
calshoot profiles
```

lists the accounts set up and whether each is still signed in. `calshoot status
--all` shows linked calendars across all of them.

Sign in to each account yourself; CalShoot never handles a stored password.
An existing pre-profile setup is moved into `default` automatically the next
time any command runs.

## Working with a large account

An account with many calendars and years of history is a bulk migration, not a
routine sync. Three flags keep it manageable:

- `--future-only` skips events that already finished, **recalculated on every
  run** so a scheduled job keeps advancing with the date. On a booking account
  this is the difference between 676 events and 14,971.
- `--skip-before YYYY-MM-DD` is the fixed-date version, for backfilling a
  specific window.
- `--calendar ID` syncs one calendar instead of everything linked. Repeat the
  flag for several.

Recurring series are never age-filtered by either cutoff, because a weekly
booking started years ago may still be producing occurrences today.

```bash
calshoot sync --calendar 1071966567 --future-only --dry-run
```

Google throttles bulk writes, so a run of several thousand events is paced by
backoff and takes a while. `--future-only` usually avoids that entirely.

## When a TimeTree calendar gets renamed

Nothing breaks. Links are keyed on the TimeTree calendar id and the Google
calendar id, and events on the TimeTree event uuid — names are never part of
any lookup. A calendar renamed mid-season keeps syncing to the same place,
with no duplicates and no re-linking.

Each sync also follows the rename cosmetically: the stored name is updated, and
the Google calendar is retitled to match. That retitle is deliberately
conservative — it happens only when CalShoot created the calendar **and** its
current title is still exactly the generated one. If you retitled it yourself,
your title stands. `--no-rename` turns the retitling off entirely while still
keeping the local record current.

## Renaming the Google calendars

New calendars are named `<profile> <calendar> TimeTree`, e.g.
`fleet2 Rochi TimeTree`. To change that, edit one line — `CALENDAR_NAME_TEMPLATE`
near the top of [calshoot/config.py](calshoot/config.py):

```python
CALENDAR_NAME_TEMPLATE = "{profile} {calendar} TimeTree"
```

`{profile}` is the TimeTree account, `{calendar}` the TimeTree calendar name.
Either can be dropped. `"TimeTree: {calendar}"` gives `TimeTree: Rochi`;
`"{calendar} (charter)"` gives `Rochi (charter)`.

For a one-off without editing anything:

```bash
CALSHOOT_CALENDAR_NAME="{calendar} charters" calshoot link --timetree-calendar 1071966567
```

Or name a single calendar directly with `calshoot link --name "Whatever I like"`.

Two things to know. Renaming only affects calendars created **after** the
change — existing ones keep their names, so rename them in Google yourself or
`reset` and re-link. And `reset` recognises its own calendars by matching this
same template, which is why the two live in one place; calendars created before
a rename are still safely deletable because linking also records
`created_by_calshoot` in the database.

## Starting over

After a round of testing, to clear a profile completely:

```bash
calshoot reset --profile default
```

That prints a plan and deletes nothing. Add `--yes` to carry it out.

`reset` deletes Google calendars **and every event in them**, so it refuses
unless all four guards pass: the calendar still exists, it is not your primary
calendar, you own it, and CalShoot created it — recorded in state, or
recognisable by the `TimeTree: ` name it uses. Anything else is listed as
untouched and simply unlinked. The guards are covered by
`tests/test_reset.py`, including a last-resort refusal to call delete on
`primary`, which would otherwise wipe your main calendar's events.

To drop a single calendar without deleting anything in Google, use
`calshoot unlink <id>` instead.

## Breakage detection

The endpoints this depends on are unofficial and can change without notice.
Loud failures already surface — a 500 raises, a 401 says the session died. The
dangerous ones look like success, so two checks watch for them.

**Payload shape.** Every sync verifies that events still carry the fields the
mapping relies on (`uuid`, `title`, `start_at`, `end_at`, `updated_at`,
`all_day`). If more than 20% of a calendar's events are missing one — a renamed
or dropped field — that calendar is **refused rather than synced**, and its
high-water mark is held so nothing is skipped once the cause is understood. A
handful of odd records does not trip it. New unknown fields are logged, never
alarmed: TimeTree adding a field is harmless.

**Silence.** If no calendar on an account has reported a change for more than
three days, the run warns. This is the dead-man's switch, and it is deliberately
indifferent to *how* things broke — a silently emptied response, a degraded
session, changed paging semantics all look identical from the outside, and all
stop the high-water marks advancing. Tune it with `--quiet-days N`, or `0` to
switch it off on a genuinely quiet fleet.

A warning notifies but still exits 0; a shape change is an error and exits 1.

## Known limits

- **One-way.** Edits made in Google to a synced event are overwritten the next
  time that event changes in TimeTree. Nothing flows back to TimeTree.
- **Memos are skipped.** TimeTree "Keeps" have no time range, so they have no
  Google equivalent. They're reported as skipped, not silently dropped.
- **Session expiry needs a human.** When the TimeTree cookie expires, a
  scheduled run fails into the log and you re-run `calshoot login`. There is no
  way around this without storing your password, which CalShoot won't do.
- **Deletion** relies on TimeTree marking removed events rather than dropping
  them. `calshoot doctor` confirmed `deactivated_at` is present on this
  account's events, which is the field CalShoot keys on. A from-scratch read
  returns no tombstones by design; they appear on incremental runs. Worth
  confirming once in practice by deleting a test event and re-syncing.
- **Edited occurrences** of recurring events are matched to Google instances by
  original start time. When a match can't be made the occurrence is **skipped
  rather than duplicated**, and reported. Rare in practice — 1 occurrence
  across ~15,000 events on this account.

## Tests

```bash
.venv/bin/python -m pytest
```

204 tests cover breakage detection, the mapping (timezones, all-day boundaries, recurrence,
reminders, colours, id generation), the sync engine (create/update/delete,
high-water handling, failure paths, age filtering, calendar selection, dry
run), TimeTree's backwards paging semantics, profile isolation, linking in
bulk, reset guards, calendar naming, rename following, the scheduled job's
plist, and CLI wiring — using fake clients, so no credentials are needed.

## Not affiliated with TimeTree

CalShoot uses unofficial, reverse-engineered TimeTree web endpoints and is not
endorsed by TimeTree, Inc. It reads only calendars your own account can already
see.
