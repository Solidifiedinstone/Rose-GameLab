"""Connection handling and migration for the library database.

The library lives in a single SQLite file under the user's data directory. It
is deliberately a plain, readable database with no proprietary format: users
can open it with any SQLite browser, back it up by copying one file, and delete
it without leaving anything behind.

Nothing in here talks to the network.
"""

from __future__ import annotations

import os
import sqlite3

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from rose_gamelab.db.migrations import MIGRATIONS


def _data_dir() -> Path:
    """XDG data directory for GameLab, honouring XDG_DATA_HOME."""
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "rose-gamelab"


DEFAULT_DB_PATH = _data_dir() / "library.db"


def utc_now() -> str:
    """Current time as an ISO 8601 UTC string — the format every table uses."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """A migrated connection to the library database.

    Usage:
        db = Database()                 # default location, migrated on open
        with db.transaction() as cur:
            cur.execute(...)

    Use `Database(":memory:")` in tests.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH

        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None puts the driver in autocommit mode so that WE
        # control transactions explicitly, rather than sqlite3 opening implicit
        # ones behind our back and fighting the BEGIN/COMMIT below.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row

        # Foreign keys are OFF by default in SQLite and must be enabled per
        # connection — without this, ON DELETE CASCADE silently does nothing
        # and deleting a game would leave orphaned files, saves and sessions.
        self.conn.execute("PRAGMA foreign_keys = ON")

        # WAL lets the UI read while a scan writes, instead of blocking on it.
        # Unavailable for in-memory databases, which is fine — nothing to share.
        if str(self.path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")

        self.conn.execute("PRAGMA synchronous = NORMAL")

        self.migrate()

    # ── Schema ────────────────────────────────────────────────────

    @property
    def version(self) -> int:
        """Current schema version, stored in SQLite's own user_version field."""
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        """Apply any pending migrations. Returns the number applied.

        Each migration runs in its own transaction, so an interrupted or failing
        upgrade rolls back rather than leaving a half-applied schema.
        """
        applied = 0
        current = self.version

        for version, description, sql in sorted(MIGRATIONS, key=lambda m: m[0]):
            if version <= current:
                continue
            # BEGIN/COMMIT must live INSIDE the script: executescript() issues an
            # implicit COMMIT before it runs, which would discard a transaction
            # opened separately beforehand.
            #
            # PRAGMA does not accept bound parameters; `version` is an int from
            # our own migration table, never user input.
            script = f"BEGIN;\n{sql}\nPRAGMA user_version = {int(version)};\nCOMMIT;"
            try:
                self.conn.executescript(script)
            except Exception as exc:
                # execute(), NOT executescript(): executescript issues an
                # implicit COMMIT before running its payload, so rolling back
                # through it would commit the half-applied migration and then
                # fail with "no transaction is active", masking the real error.
                if self.conn.in_transaction:
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        # Already unwound by SQLite; the original failure is
                        # what matters and is re-raised below.
                        pass
                raise RuntimeError(
                    f"migration {version} ({description}) failed; "
                    f"database left at version {self.version}"
                ) from exc
            applied += 1

        return applied

    # ── Access ────────────────────────────────────────────────────

    class _Transaction:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def __enter__(self) -> sqlite3.Cursor:
            self._cur = self._conn.cursor()
            self._cur.execute("BEGIN")
            return self._cur

        def __exit__(self, exc_type, exc, tb) -> bool:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
            self._cur.close()
            return False  # never swallow the exception

    def transaction(self) -> "Database._Transaction":
        """Context manager that commits on success and rolls back on error."""
        return Database._Transaction(self.conn)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        """Run a single statement and commit it."""
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def iter_query(self, sql: str, params: tuple | dict = ()) -> Iterator[sqlite3.Row]:
        """Stream rows instead of materialising them — for large libraries."""
        cur = self.conn.execute(sql, params)
        try:
            while (row := cur.fetchone()) is not None:
                yield row
        finally:
            cur.close()

    # ── Maintenance ───────────────────────────────────────────────

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def backup_to(self, target: str | Path) -> Path:
        """Copy the live database to `target`, safe to call while in use."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(target))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        return target

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False
