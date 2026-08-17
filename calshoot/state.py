"""Local sync state: calendar links, sync cursors and the event id map.

Google event ids are derived deterministically from TimeTree uuids, so this
database is an optimisation rather than the source of truth. Losing it costs a
full re-scan; it does not create duplicates.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from calshoot.config import state_path

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- high_water is the newest updated_at seen for this calendar. It is not a
-- pagination cursor: TimeTree's feed pages backwards, so the value it hands
-- back is the oldest record, not the newest. See TimeTreeClient.changed_since.
CREATE TABLE IF NOT EXISTS calendar_links (
    tt_calendar_id     INTEGER PRIMARY KEY,
    tt_calendar_name   TEXT,
    google_calendar_id TEXT    NOT NULL,
    high_water         INTEGER NOT NULL DEFAULT 0,
    last_synced_at     INTEGER,
    -- 1 when CalShoot created the Google calendar itself. `reset` will only
    -- delete a calendar it made; anything pre-existing is left alone.
    created_by_calshoot INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event_map (
    tt_calendar_id     INTEGER NOT NULL,
    tt_uuid            TEXT    NOT NULL,
    google_calendar_id TEXT    NOT NULL,
    google_event_id    TEXT    NOT NULL,
    content_hash       TEXT,
    updated_at         INTEGER,
    PRIMARY KEY (tt_calendar_id, tt_uuid)
);

CREATE INDEX IF NOT EXISTS idx_event_map_google
    ON event_map (google_calendar_id, google_event_id);
"""


@dataclass
class CalendarLink:
    """A TimeTree calendar wired to a Google calendar."""

    tt_calendar_id: int
    tt_calendar_name: str
    google_calendar_id: str
    high_water: int = 0
    last_synced_at: int | None = None
    created_by_calshoot: int = 0


class State:
    """SQLite-backed sync state."""

    def __init__(self, path: Path | None = None):
        self.path = path or state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _migrate(self) -> None:
        """Bring an older database up to the current schema.

        v1 called the column `cursor` and stored TimeTree's paging value, which
        turned out to page backwards. v2 stores a high-water mark on
        updated_at instead. The old value is meaningless under the new
        semantics, so it is reset to 0 to force one full re-scan.
        """
        tables = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "calendar_links" not in tables:
            return

        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(calendar_links)")
        }
        if "cursor" in columns and "high_water" not in columns:
            self._conn.execute(
                "ALTER TABLE calendar_links RENAME COLUMN cursor TO high_water"
            )
            self._conn.execute("UPDATE calendar_links SET high_water = 0")

        if "created_by_calshoot" not in columns:
            self._conn.execute(
                "ALTER TABLE calendar_links "
                "ADD COLUMN created_by_calshoot INTEGER NOT NULL DEFAULT 0"
            )

        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- calendar links ---------------------------------------------------

    def link_calendar(
        self,
        tt_calendar_id: int,
        tt_calendar_name: str,
        google_calendar_id: str,
        *,
        created_by_calshoot: bool = False,
    ) -> None:
        """Wire a TimeTree calendar to a Google calendar, preserving sync state."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO calendar_links
                    (tt_calendar_id, tt_calendar_name, google_calendar_id,
                     created_by_calshoot)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tt_calendar_id) DO UPDATE SET
                    tt_calendar_name   = excluded.tt_calendar_name,
                    google_calendar_id = excluded.google_calendar_id,
                    -- Never downgrade a calendar we know we created.
                    created_by_calshoot = MAX(
                        calendar_links.created_by_calshoot,
                        excluded.created_by_calshoot
                    )
                """,
                (tt_calendar_id, tt_calendar_name, google_calendar_id, int(created_by_calshoot)),
            )

    def update_calendar_name(self, tt_calendar_id: int, tt_calendar_name: str) -> None:
        """Record a new TimeTree calendar name. Purely cosmetic; sync is by id."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE calendar_links SET tt_calendar_name = ? WHERE tt_calendar_id = ?",
                (tt_calendar_name, tt_calendar_id),
            )

    def unlink_calendar(self, tt_calendar_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM calendar_links WHERE tt_calendar_id = ?", (tt_calendar_id,))
            conn.execute("DELETE FROM event_map WHERE tt_calendar_id = ?", (tt_calendar_id,))

    def links(self) -> list[CalendarLink]:
        rows = self._conn.execute(
            "SELECT * FROM calendar_links ORDER BY tt_calendar_name"
        ).fetchall()
        return [CalendarLink(**dict(row)) for row in rows]

    def link_for(self, tt_calendar_id: int) -> CalendarLink | None:
        row = self._conn.execute(
            "SELECT * FROM calendar_links WHERE tt_calendar_id = ?", (tt_calendar_id,)
        ).fetchone()
        return CalendarLink(**dict(row)) if row else None

    def save_high_water(self, tt_calendar_id: int, high_water: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE calendar_links SET high_water = ?, last_synced_at = ? "
                "WHERE tt_calendar_id = ?",
                (high_water, int(time.time()), tt_calendar_id),
            )

    def reset_high_water(self, tt_calendar_id: int) -> None:
        """Force the next sync to re-scan the calendar from the beginning."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE calendar_links SET high_water = 0 WHERE tt_calendar_id = ?",
                (tt_calendar_id,),
            )

    # -- event map --------------------------------------------------------

    def record_event(
        self,
        tt_calendar_id: int,
        tt_uuid: str,
        google_calendar_id: str,
        google_event_id: str,
        content_hash: str,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO event_map
                    (tt_calendar_id, tt_uuid, google_calendar_id,
                     google_event_id, content_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tt_calendar_id, tt_uuid) DO UPDATE SET
                    google_calendar_id = excluded.google_calendar_id,
                    google_event_id    = excluded.google_event_id,
                    content_hash       = excluded.content_hash,
                    updated_at         = excluded.updated_at
                """,
                (
                    tt_calendar_id,
                    tt_uuid,
                    google_calendar_id,
                    google_event_id,
                    content_hash,
                    int(time.time()),
                ),
            )

    def forget_event(self, tt_calendar_id: int, tt_uuid: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM event_map WHERE tt_calendar_id = ? AND tt_uuid = ?",
                (tt_calendar_id, tt_uuid),
            )

    def known_hash(self, tt_calendar_id: int, tt_uuid: str) -> str | None:
        row = self._conn.execute(
            "SELECT content_hash FROM event_map WHERE tt_calendar_id = ? AND tt_uuid = ?",
            (tt_calendar_id, tt_uuid),
        ).fetchone()
        return row["content_hash"] if row else None

    def clear_everything(self) -> None:
        """Forget all links and mapped events for this profile."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM event_map")
            conn.execute("DELETE FROM calendar_links")

    def mapped_event_count(self, tt_calendar_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM event_map WHERE tt_calendar_id = ?", (tt_calendar_id,)
        ).fetchone()
        return row["n"]
