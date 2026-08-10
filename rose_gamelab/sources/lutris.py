"""Lutris importer.

Lutris keeps its library in a SQLite database at
`~/.local/share/lutris/pga.db` ("personal game archive"). The `games` table is
the whole library; the columns this module uses are:

    id            integer primary key — the number Lutris launches by
    name          display name
    slug          url-safe id, unique per game
    runner        wine / linux / retroarch / steam / ... ; empty until installed
    directory     install directory, may be empty for runners that need none
    installed     0/1
    hidden        0/1 — the user has hidden it from their own library

The database is opened read-only through a `file:...?mode=ro` URI. Lutris may
be running while GameLab scans, and a read-only handle cannot take a write lock
or leave a `-wal` file behind in someone else's data directory.

Games are launched with `lutris lutris:rungameid/<id>`. Lutris resolves the
runner, the Wine prefix, the per-game environment and the pre-launch scripts;
running the executable in `directory` directly gets none of that, and for the
majority of Lutris games (Wine) it does not start at all.

── Launching ─────────────────────────────────────────────────────────
`core/launcher.py` has no 'lutris' launch kind, so these entries import as
kind='native', for which it builds `[target]` — that cannot execute a URI.
The entry's `path` is still the URI, because that is the correct launch
target, and `metadata["launch_command"]` holds the argv that works. Wiring the
launcher up to it is a change to launcher.py and is not done here.

── What is verified and what is assumed ──────────────────────────────
Written without a populated `pga.db` to test against (the machine it was
written on has Lutris installed but has never run it), so the schema is not
assumed to be fixed: the column list is read from `PRAGMA table_info(games)`
and only columns that exist are selected. A Lutris version that renames or
drops a column loses that one field with a logged warning instead of raising.
Only `id` and `name` are required; without them there is no game to import.

Nothing here touches the network.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3

from pathlib import Path
from typing import Optional

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import GameEntry
from rose_gamelab.sources.base import SourceDef, SourceProvider

logger = logging.getLogger(__name__)

# Candidate locations for pga.db, in priority order.
LUTRIS_DB_PATHS = (
    "~/.local/share/lutris/pga.db",
    "~/.var/app/net.lutris.Lutris/data/lutris/pga.db",   # Flatpak
)

# Columns required for a usable entry. Anything else is a bonus.
REQUIRED_COLUMNS = ("id", "name")

# Columns read when present. Missing ones are logged once per scan and the
# corresponding metadata is simply absent — never filled with a placeholder.
OPTIONAL_COLUMNS = (
    "slug", "runner", "directory", "installed", "hidden", "platform",
    "playtime", "lastplayed", "steamid", "service", "service_id", "year",
)


class LutrisProvider(SourceProvider):
    """Discovers installed games from the Lutris database."""

    def __init__(self, config: Optional[Config] = None, db_path: Optional[str] = None) -> None:
        self.config = config
        self.db_path: Optional[Path] = (
            Path(db_path).expanduser() if db_path else self.find_db()
        )

    # ── Discovery of Lutris itself ────────────────────────────────

    @staticmethod
    def find_db() -> Optional[Path]:
        """Locate pga.db, or None if Lutris has never built a library.

        Note that the `lutris` binary being on PATH is not the same thing:
        an installed-but-never-run Lutris has no database at all, and this
        returns None for it rather than pretending the source is ready.
        """
        for candidate in LUTRIS_DB_PATHS:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path.resolve()
        return None

    def connect(self) -> Optional[sqlite3.Connection]:
        """Open pga.db read-only, or None with a logged reason."""
        if self.db_path is None or not self.db_path.is_file():
            return None

        # mode=ro fails loudly on a missing file instead of creating an empty
        # database, which is exactly what we want.
        uri = f"file:{self.db_path}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            logger.warning("could not open %s read-only: %s", self.db_path, exc)
            return None

        connection.row_factory = sqlite3.Row
        return connection

    def _columns(self, connection: sqlite3.Connection) -> set[str]:
        """Column names on the `games` table, or empty if there is no such table."""
        try:
            rows = connection.execute("PRAGMA table_info(games)").fetchall()
        except sqlite3.Error as exc:
            logger.warning("could not inspect the games table in %s: %s", self.db_path, exc)
            return set()

        if not rows:
            logger.warning(
                "%s has no `games` table; this does not look like a Lutris "
                "database", self.db_path,
            )
        return {row["name"] for row in rows}

    # ── Discovery of games ────────────────────────────────────────

    def discover(self) -> list[GameEntry]:
        """Return every installed, non-hidden game in the Lutris library."""
        connection = self.connect()
        if connection is None:
            return []

        try:
            return self._discover(connection)
        except sqlite3.DatabaseError as exc:
            # A corrupt or truncated database. Report it; do not return a
            # partial list that looks like a small library.
            logger.error("could not read the Lutris library at %s: %s", self.db_path, exc)
            return []
        finally:
            connection.close()

    def _discover(self, connection: sqlite3.Connection) -> list[GameEntry]:
        available = self._columns(connection)

        missing_required = [c for c in REQUIRED_COLUMNS if c not in available]
        if missing_required:
            logger.error(
                "the games table in %s has no %s column(s); Lutris's schema has "
                "changed and this importer needs updating",
                self.db_path, ", ".join(missing_required),
            )
            return []

        selected = list(REQUIRED_COLUMNS) + [c for c in OPTIONAL_COLUMNS if c in available]
        for column in OPTIONAL_COLUMNS:
            if column not in available:
                logger.info("Lutris games table has no `%s` column; skipping it", column)

        # Filtering in SQL only on columns that exist; `installed` and `hidden`
        # are treated as "assume visible and installed" when absent, which is
        # what a schema without them means.
        where = []
        if "installed" in available:
            where.append("installed = 1")
        if "hidden" in available:
            where.append("(hidden IS NULL OR hidden = 0)")

        query = f"SELECT {', '.join(selected)} FROM games"  # noqa: S608 - names are from a fixed list
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY id"

        rows = connection.execute(query).fetchall()

        entries: dict[str, GameEntry] = {}
        for row in rows:
            entry = self.build_entry(dict(row))
            if entry is not None:
                entries.setdefault(entry.id, entry)

        return list(entries.values())

    def build_entry(self, row: dict) -> Optional[GameEntry]:
        """Build one GameEntry from a `games` row, or None if unlaunchable."""
        game_id = row.get("id")
        if not isinstance(game_id, int):
            logger.debug("skipping Lutris row with non-integer id: %r", game_id)
            return None

        name = str(row.get("name") or "").strip()
        slug = str(row.get("slug") or "").strip()
        if not name:
            # Lutris will not show a nameless game either; without a name there
            # is nothing to put in the library.
            logger.warning("skipping Lutris game %s: it has no name", game_id)
            return None

        runner = str(row.get("runner") or "").strip()
        if not runner:
            # Lutris leaves runner empty for library entries that were never
            # installed. `lutris:rungameid/N` on one of those opens the
            # installer, not the game.
            logger.info(
                "skipping Lutris game %r (id %s): no runner, so it is not "
                "actually installed", name, game_id,
            )
            return None

        directory = str(row.get("directory") or "")
        if directory and not Path(directory).is_dir():
            # Worth knowing about — usually an unmounted drive — but Lutris is
            # still the thing that decides whether it can launch, so the game
            # is kept.
            logger.info(
                "Lutris game %r points at %s, which does not exist", name, directory,
            )

        uri = f"lutris:rungameid/{game_id}"

        metadata = {
            "lutris_id": game_id,
            "lutris_slug": slug,
            "runner": runner,
            "install_path": directory,
            "launch_command": self.launch_command(uri),
            "launch_target_is_url": True,
        }

        # A Lutris-managed Steam game carries its appid; passing it through lets
        # the library merge it with the same game found by the Steam importer
        # instead of listing it twice.
        steam_appid = self._steam_appid(row)
        if steam_appid is not None:
            metadata["steam_appid"] = steam_appid

        for column, key in (
            ("platform", "platform"),
            ("playtime", "playtime_hours"),
            ("lastplayed", "last_played"),
            ("service", "service"),
            ("service_id", "service_id"),
            ("year", "year"),
        ):
            if row.get(column) not in (None, ""):
                metadata[key] = row[column]

        return GameEntry(
            id=f"lutris:{game_id}",
            name=name,
            system="pc",
            # Launch target, not a filesystem path.
            path=uri,
            source="lutris",
            metadata=metadata,
        )

    @staticmethod
    def _steam_appid(row: dict) -> Optional[int]:
        """The Steam appid for a Lutris entry, if it has one.

        Lutris has stored it in `steamid` and, for games added through the
        Steam service integration, in `service_id`. Both are text columns in
        some schema versions, so both are parsed rather than cast blindly.
        """
        candidates = [row.get("steamid")]
        if str(row.get("service") or "").strip().lower() == "steam":
            candidates.append(row.get("service_id"))

        for value in candidates:
            if isinstance(value, int) and value > 0:
                return value
            if isinstance(value, str) and value.strip().isdigit():
                appid = int(value)
                if appid > 0:
                    return appid
        return None

    @staticmethod
    def launch_command(uri: str) -> list[str]:
        """The argv that actually starts a Lutris game.

        Falls back to the Flatpak invocation when `lutris` is not on PATH; the
        command is returned either way so the caller has something to show the
        user rather than an empty list that reads as success.
        """
        binary = shutil.which("lutris")
        if binary:
            return [binary, uri]
        return ["flatpak", "run", "net.lutris.Lutris", uri]

    # ── SourceProvider interface ──────────────────────────────────

    def validate(self) -> bool:
        """True when pga.db exists and has a readable `games` table."""
        connection = self.connect()
        if connection is None:
            return False

        try:
            return not set(REQUIRED_COLUMNS) - self._columns(connection)
        finally:
            connection.close()

    def get_def(self) -> SourceDef:
        return SourceDef(
            id="lutris",
            name="Lutris",
            type="lutris",
            path=str(self.db_path) if self.db_path else None,
            system="pc",
            enabled=True,
        )
