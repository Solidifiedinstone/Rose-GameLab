"""The library: adding, finding, filtering and updating games.

This is the only module that writes to the games tables. Sources discover
candidates, the scanner groups files, and everything funnels through here so
that duplicate detection and playtime accounting happen in exactly one place.

Duplicate detection is the interesting part. The same game can arrive from
several sources — owned on Steam, dumped as a ROM, installed through Heroic —
and the user wants ONE library entry with several ways to play, not three
entries. Matches are established in descending order of confidence:

  1. identical content hash            (certain: byte-identical files)
  2. same Steam appid                  (certain: Steam's own identifier)
  3. same normalised title AND system  (confident: same game, different dump)

Titles alone are never enough across systems — Tomb Raider on PS1 and Tomb
Raider on PC are genuinely different entries with different art and saves.

Nothing here touches the network.
"""

from __future__ import annotations

import json
import sqlite3

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from rose_gamelab.core.discs import GameGroup, sort_title
from rose_gamelab.core.emulator import GameEntry
from rose_gamelab.db.database import Database, utc_now


@dataclass
class Game:
    """A library entry, as read from the database."""

    id: int
    title: str
    sort_title: str
    system: str
    source_id: Optional[str] = None
    cover_path: Optional[str] = None
    release_date: Optional[str] = None
    developer: Optional[str] = None
    publisher: Optional[str] = None
    rating: Optional[float] = None
    summary: Optional[str] = None
    steam_appid: Optional[int] = None
    igdb_id: Optional[int] = None
    favorite: bool = False
    hidden: bool = False
    play_seconds: int = 0
    play_count: int = 0
    last_played: Optional[str] = None
    added_at: str = ""

    @property
    def playtime_hours(self) -> float:
        return round(self.play_seconds / 3600, 1)

    @property
    def has_cover(self) -> bool:
        return bool(self.cover_path) and Path(self.cover_path).is_file()

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Game":
        keys = set(row.keys())
        return cls(
            id=row["id"],
            title=row["title"],
            sort_title=row["sort_title"],
            system=row["system"],
            source_id=row["source_id"] if "source_id" in keys else None,
            cover_path=row["cover_path"] if "cover_path" in keys else None,
            release_date=row["release_date"] if "release_date" in keys else None,
            developer=row["developer"] if "developer" in keys else None,
            publisher=row["publisher"] if "publisher" in keys else None,
            rating=row["rating"] if "rating" in keys else None,
            summary=row["summary"] if "summary" in keys else None,
            steam_appid=row["steam_appid"] if "steam_appid" in keys else None,
            igdb_id=row["igdb_id"] if "igdb_id" in keys else None,
            favorite=bool(row["favorite"]) if "favorite" in keys else False,
            hidden=bool(row["hidden"]) if "hidden" in keys else False,
            play_seconds=row["play_seconds"] if "play_seconds" in keys else 0,
            play_count=row["play_count"] if "play_count" in keys else 0,
            last_played=row["last_played"] if "last_played" in keys else None,
            added_at=row["added_at"] if "added_at" in keys else "",
        )


@dataclass
class ImportResult:
    """What an import run actually did — reported to the user, never guessed."""

    added: int = 0
    merged: int = 0          # matched an existing game, gained a launch option
    updated: int = 0         # existing game, new file (e.g. an extra disc)
    skipped: int = 0         # already present, nothing to change
    errors: list[str] = field(default_factory=list)

    @property
    def total_seen(self) -> int:
        return self.added + self.merged + self.updated + self.skipped


class Library:
    """Read and write access to the game library."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Reading ───────────────────────────────────────────────────

    def get(self, game_id: int) -> Optional[Game]:
        row = self.db.query_one("SELECT * FROM games WHERE id = ?", (game_id,))
        return Game.from_row(row) if row else None

    def count(self, *, include_hidden: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM games"
        if not include_hidden:
            sql += " WHERE hidden = 0"
        return int(self.db.query_one(sql)["n"])

    def list_games(
        self,
        *,
        system: Optional[str] = None,
        source_id: Optional[str] = None,
        collection_id: Optional[int] = None,
        tag: Optional[str] = None,
        favorites_only: bool = False,
        include_hidden: bool = False,
        search: Optional[str] = None,
        sort: str = "title",
        descending: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[Game]:
        """Filtered, sorted view of the library.

        `search` uses the FTS index when it looks like a word query, so typing
        'zelda' finds 'The Legend of Zelda' without a leading wildcard.
        """
        where: list[str] = []
        params: list[Any] = []
        joins = ""

        if not include_hidden:
            where.append("g.hidden = 0")
        if system:
            where.append("g.system = ?")
            params.append(system)
        if source_id:
            where.append("g.source_id = ?")
            params.append(source_id)
        if favorites_only:
            where.append("g.favorite = 1")

        if collection_id is not None:
            joins += " JOIN collection_games cg ON cg.game_id = g.id"
            where.append("cg.collection_id = ?")
            params.append(collection_id)

        if tag:
            joins += (
                " JOIN game_tags gt ON gt.game_id = g.id"
                " JOIN tags t ON t.id = gt.tag_id"
            )
            where.append("t.name = ?")
            params.append(tag)

        if search:
            query = self._fts_query(search)
            if query:
                joins += " JOIN games_fts f ON f.rowid = g.id"
                where.append("games_fts MATCH ?")
                params.append(query)
            else:
                where.append("g.title LIKE ?")
                params.append(f"%{search}%")

        sort_columns = {
            "title": "g.sort_title",
            "added": "g.added_at",
            "last_played": "g.last_played",
            "playtime": "g.play_seconds",
            "release": "g.release_date",
            "rating": "g.rating",
            "system": "g.system, g.sort_title",
        }
        # Whitelisted rather than interpolated, so a sort key can never inject SQL.
        order = sort_columns.get(sort, "g.sort_title")
        direction = "DESC" if descending else "ASC"

        sql = f"SELECT g.* FROM games g{joins}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # NULLS LAST so games never played sort after played ones, not before.
        sql += f" ORDER BY {order} IS NULL, {order} {direction}"

        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        return [Game.from_row(row) for row in self.db.query(sql, tuple(params))]

    @staticmethod
    def _fts_query(search: str) -> Optional[str]:
        """Turn user input into a safe FTS5 prefix query, or None if unusable.

        FTS5 treats punctuation as syntax and raises on malformed queries, so
        anything that is not alphanumeric is stripped and each surviving word
        becomes a prefix term.
        """
        words = ["".join(c for c in word if c.isalnum()) for word in search.split()]
        words = [w for w in words if w]
        if not words:
            return None
        return " ".join(f'"{w}"*' for w in words)

    def systems_in_library(self) -> list[tuple[str, int]]:
        """(system id, game count) for systems that actually have games.

        The sidebar shows only these — an empty system is noise.
        """
        rows = self.db.query(
            "SELECT system, COUNT(*) AS n FROM games WHERE hidden = 0"
            " GROUP BY system ORDER BY n DESC"
        )
        return [(row["system"], row["n"]) for row in rows]

    def random_game(self, **filters: Any) -> Optional[Game]:
        """One random game — the 'surprise me' button."""
        games = self.list_games(**filters)
        if not games:
            return None
        # random.choice, but seeded from the OS so repeated presses differ.
        import secrets
        return games[secrets.randbelow(len(games))]

    def files_for(self, game_id: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM game_files WHERE game_id = ?"
            " ORDER BY disc_number IS NULL, disc_number, path",
            (game_id,),
        )

    def launch_options_for(self, game_id: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM launch_options WHERE game_id = ?"
            " ORDER BY is_primary DESC, sort_order, id",
            (game_id,),
        )

    # ── Duplicate detection ───────────────────────────────────────

    def find_duplicate(
        self,
        *,
        title: str,
        system: str,
        steam_appid: Optional[int] = None,
        sha1: Optional[str] = None,
    ) -> Optional[int]:
        """Find an existing game that this candidate is a duplicate of.

        Returns the game id, or None. Checks run in descending confidence.
        """
        # 1. Byte-identical file — certain.
        if sha1:
            row = self.db.query_one(
                "SELECT game_id FROM game_files WHERE sha1 = ? LIMIT 1", (sha1,)
            )
            if row:
                return row["game_id"]

        # 2. Steam's own identifier — certain.
        if steam_appid is not None:
            row = self.db.query_one(
                "SELECT id FROM games WHERE steam_appid = ? LIMIT 1", (steam_appid,)
            )
            if row:
                return row["id"]

        # 3. Same title on the same system — confident, but system-scoped:
        #    Tomb Raider on PS1 and on PC are genuinely different entries.
        row = self.db.query_one(
            "SELECT id FROM games WHERE sort_title = ? AND system = ? LIMIT 1",
            (sort_title(title), system),
        )
        return row["id"] if row else None

    # ── Writing ───────────────────────────────────────────────────

    def add_game(
        self,
        *,
        title: str,
        system: str,
        source_id: Optional[str] = None,
        steam_appid: Optional[int] = None,
        **fields: Any,
    ) -> int:
        """Insert a game and return its id. Does not check for duplicates."""
        columns = {
            "title": title,
            "sort_title": sort_title(title),
            "system": system,
            "source_id": source_id,
            "steam_appid": steam_appid,
            "added_at": utc_now(),
        }
        allowed = {
            "igdb_id", "summary", "release_date", "developer", "publisher",
            "rating", "rating_count", "rating_source", "cover_path",
            "hero_path", "logo_path", "hidden", "favorite",
        }
        columns.update({k: v for k, v in fields.items() if k in allowed})

        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO games ({', '.join(columns)}) VALUES ({placeholders})"
        )
        return int(self.db.execute(sql, tuple(columns.values())).lastrowid)

    def add_file(
        self,
        game_id: int,
        path: str | Path,
        *,
        disc_number: Optional[int] = None,
        disc_label: Optional[str] = None,
        size_bytes: Optional[int] = None,
    ) -> Optional[int]:
        """Attach a file to a game. Returns None if the path is already known."""
        try:
            cursor = self.db.execute(
                "INSERT INTO game_files"
                " (game_id, path, disc_number, disc_label, size_bytes, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (game_id, str(path), disc_number, disc_label, size_bytes, utc_now()),
            )
            return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            # UNIQUE(path): the file is already in the library.
            return None

    def add_launch_option(
        self,
        game_id: int,
        *,
        kind: str,
        target: str,
        label: Optional[str] = None,
        emulator: Optional[str] = None,
        args: Optional[str] = None,
        working_dir: Optional[str] = None,
        profile_id: Optional[int] = None,
        is_primary: bool = False,
    ) -> int:
        """Add a way to launch a game.

        The first option added to a game becomes primary automatically, so a
        game always has something to launch even if nobody set a preference.
        """
        existing = self.db.query_one(
            "SELECT COUNT(*) AS n FROM launch_options WHERE game_id = ?", (game_id,)
        )
        if existing["n"] == 0:
            is_primary = True
        elif is_primary:
            self.db.execute(
                "UPDATE launch_options SET is_primary = 0 WHERE game_id = ?", (game_id,)
            )

        cursor = self.db.execute(
            "INSERT INTO launch_options"
            " (game_id, kind, label, emulator, target, args, working_dir,"
            "  profile_id, is_primary, sort_order)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                game_id, kind, label, emulator, target, args, working_dir,
                profile_id, int(is_primary), existing["n"],
            ),
        )
        return int(cursor.lastrowid)

    def has_launch_option(self, game_id: int, kind: str, target: str) -> bool:
        return self.db.query_one(
            "SELECT 1 FROM launch_options WHERE game_id = ? AND kind = ? AND target = ?",
            (game_id, kind, target),
        ) is not None

    def update_game(self, game_id: int, **fields: Any) -> None:
        """Update metadata fields. Silently ignores unknown columns."""
        allowed = {
            "title", "system", "summary", "release_date", "developer",
            "publisher", "rating", "rating_count", "rating_source", "igdb_id",
            "steam_appid", "cover_path", "hero_path", "logo_path", "hidden",
            "favorite", "metadata_locked", "source_id",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return

        # Keep sort_title consistent whenever the title changes.
        if "title" in updates:
            updates["sort_title"] = sort_title(str(updates["title"]))

        assignments = ", ".join(f"{k} = ?" for k in updates)
        self.db.execute(
            f"UPDATE games SET {assignments} WHERE id = ?",
            (*updates.values(), game_id),
        )

    def set_favorite(self, game_id: int, favorite: bool = True) -> None:
        self.db.execute(
            "UPDATE games SET favorite = ? WHERE id = ?", (int(favorite), game_id)
        )

    def set_hidden(self, game_id: int, hidden: bool = True) -> None:
        self.db.execute(
            "UPDATE games SET hidden = ? WHERE id = ?", (int(hidden), game_id)
        )

    def remove_game(self, game_id: int) -> None:
        """Remove a game from the library. Never touches files on disk."""
        self.db.execute("DELETE FROM games WHERE id = ?", (game_id,))

    # ── Importing ─────────────────────────────────────────────────

    def import_entries(
        self, entries: Iterable[GameEntry], *, source_id: Optional[str] = None
    ) -> ImportResult:
        """Import games discovered by a source, merging duplicates.

        Used for source providers that yield one entry per game (Steam, GOG,
        Heroic). ROM folders go through `import_group` instead, because their
        files need disc grouping first.
        """
        result = ImportResult()

        for entry in entries:
            try:
                self._import_one(entry, source_id or entry.source, result)
            except Exception as exc:  # one bad entry must not abort the scan
                result.errors.append(f"{entry.name}: {exc}")

        return result

    def _ensure_source(self, source_id: Optional[str]) -> Optional[str]:
        """Guarantee a sources row exists, so games.source_id stays valid.

        Providers report a source id ('steam', 'heroic') that the user may
        never have registered explicitly. Inserting a stub row keeps the
        foreign key satisfied and preserves the attribution, rather than
        dropping it to NULL and losing track of where a game came from.
        """
        if not source_id:
            return None

        if self.db.query_one("SELECT 1 FROM sources WHERE id = ?", (source_id,)) is None:
            self.db.execute(
                "INSERT INTO sources (id, name, type, added_at) VALUES (?, ?, ?, ?)",
                (source_id, source_id.replace("_", " ").title(), source_id, utc_now()),
            )

        return source_id

    def _import_one(
        self, entry: GameEntry, source_id: Optional[str], result: ImportResult
    ) -> None:
        source_id = self._ensure_source(source_id)
        appid = entry.metadata.get("steam_appid")
        kind = (
            "steam" if entry.is_steam
            else "heroic" if entry.is_heroic
            else "gog" if entry.is_gog
            # The provider's own id is the launch kind for everything else, so
            # Lutris entries get kind='lutris' and reach the right handler
            # rather than falling through to a bare exec of a URL.
            else entry.source if entry.source in ("lutris",)
            else "native"
        )

        existing = self.find_duplicate(
            title=entry.name, system=entry.system, steam_appid=appid
        )

        if existing is None:
            game_id = self.add_game(
                title=entry.name,
                system=entry.system,
                source_id=source_id,
                steam_appid=appid,
            )
            self.add_launch_option(game_id, kind=kind, target=entry.path, label=kind.title())
            result.added += 1
            return

        # Known game. Add this as another way to play it, if it is new.
        if self.has_launch_option(existing, kind, entry.path):
            result.skipped += 1
        else:
            self.add_launch_option(existing, kind=kind, target=entry.path, label=kind.title())
            result.merged += 1

    def import_group(
        self,
        group: GameGroup,
        *,
        system: str,
        source_id: Optional[str] = None,
        emulator: Optional[str] = None,
        playlist: Optional[Path] = None,
    ) -> tuple[int, str]:
        """Import one grouped ROM (possibly multi-disc). Returns (game_id, outcome).

        `outcome` is one of 'added', 'updated' or 'skipped', so callers report
        what actually happened rather than assuming.
        """
        existing = self.find_duplicate(title=group.title, system=system)

        if existing is None:
            game_id = self.add_game(
                title=group.title, system=system, source_id=self._ensure_source(source_id)
            )
            outcome = "added"
        else:
            game_id = existing
            outcome = "skipped"

        added_file = False
        for disc in group.sorted_files:
            file_id = self.add_file(
                game_id,
                disc.path,
                disc_number=disc.disc_number,
                disc_label=disc.disc_label,
                size_bytes=disc.path.stat().st_size if disc.path.exists() else None,
            )
            if file_id is not None:
                added_file = True

        if outcome == "skipped" and added_file:
            outcome = "updated"

        # A multi-disc game launches from its playlist so the emulator can swap
        # discs; a single-disc game launches from the file itself.
        target = str(playlist) if playlist else str(group.primary_file)
        if not self.has_launch_option(game_id, "emulator", target):
            self.add_launch_option(
                game_id, kind="emulator", target=target, emulator=emulator
            )

        return game_id, outcome

    # ── Playtime ──────────────────────────────────────────────────

    def start_session(self, game_id: int, launch_option_id: Optional[int] = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO play_sessions (game_id, launch_option_id, started_at)"
            " VALUES (?, ?, ?)",
            (game_id, launch_option_id, utc_now()),
        )
        return int(cursor.lastrowid)

    def end_session(self, session_id: int) -> int:
        """Close a session and fold its duration into the game's totals.

        Returns the session length in seconds. Sessions shorter than a few
        seconds are recorded but are usually a failed launch, not play.
        """
        row = self.db.query_one(
            "SELECT game_id, started_at FROM play_sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            return 0

        started = datetime.fromisoformat(row["started_at"])
        ended = datetime.now(timezone.utc)
        seconds = max(0, int((ended - started).total_seconds()))

        with self.db.transaction() as cur:
            cur.execute(
                "UPDATE play_sessions SET ended_at = ?, seconds = ? WHERE id = ?",
                (ended.isoformat(timespec="seconds"), seconds, session_id),
            )
            cur.execute(
                "UPDATE games SET play_seconds = play_seconds + ?,"
                " play_count = play_count + 1, last_played = ? WHERE id = ?",
                (seconds, ended.isoformat(timespec="seconds"), row["game_id"]),
            )

        return seconds

    # ── Collections and tags ──────────────────────────────────────

    def create_collection(self, name: str, *, icon: Optional[str] = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO collections (name, icon) VALUES (?, ?)", (name, icon)
        )
        return int(cursor.lastrowid)

    def add_to_collection(self, collection_id: int, game_id: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO collection_games (collection_id, game_id)"
            " VALUES (?, ?)",
            (collection_id, game_id),
        )

    def remove_from_collection(self, collection_id: int, game_id: int) -> None:
        self.db.execute(
            "DELETE FROM collection_games WHERE collection_id = ? AND game_id = ?",
            (collection_id, game_id),
        )

    def list_collections(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT c.*, COUNT(cg.game_id) AS game_count FROM collections c"
            " LEFT JOIN collection_games cg ON cg.collection_id = c.id"
            " GROUP BY c.id ORDER BY c.sort_order, c.name"
        )

    def tag_game(self, game_id: int, tag_name: str, *, kind: str = "user") -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO tags (name, kind) VALUES (?, ?)", (tag_name, kind)
        )
        row = self.db.query_one("SELECT id FROM tags WHERE name = ?", (tag_name,))
        self.db.execute(
            "INSERT OR IGNORE INTO game_tags (game_id, tag_id) VALUES (?, ?)",
            (game_id, row["id"]),
        )

    def untag_game(self, game_id: int, tag_name: str) -> None:
        self.db.execute(
            "DELETE FROM game_tags WHERE game_id = ?"
            " AND tag_id = (SELECT id FROM tags WHERE name = ?)",
            (game_id, tag_name),
        )

    def tags_for(self, game_id: int) -> list[str]:
        rows = self.db.query(
            "SELECT t.name FROM tags t JOIN game_tags gt ON gt.tag_id = t.id"
            " WHERE gt.game_id = ? ORDER BY t.name",
            (game_id,),
        )
        return [row["name"] for row in rows]

    def list_tags(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT t.*, COUNT(gt.game_id) AS game_count FROM tags t"
            " LEFT JOIN game_tags gt ON gt.tag_id = t.id"
            " GROUP BY t.id ORDER BY t.kind, t.name"
        )

    # ── Sources ───────────────────────────────────────────────────

    def register_source(
        self,
        source_id: str,
        *,
        name: str,
        type: str,
        path: Optional[str] = None,
        system: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> None:
        """Record a source, or update it if already registered."""
        self.db.execute(
            "INSERT INTO sources (id, name, type, path, system, config, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "   name = excluded.name, path = excluded.path,"
            "   system = excluded.system, config = excluded.config",
            (source_id, name, type, path, system, json.dumps(config or {}), utc_now()),
        )

    def list_sources(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT s.*, ("
            "  SELECT COUNT(*) FROM games g WHERE g.source_id = s.id"
            ") AS game_count FROM sources s ORDER BY s.name"
        )

    def mark_source_scanned(self, source_id: str) -> None:
        self.db.execute(
            "UPDATE sources SET scanned_at = ? WHERE id = ?", (utc_now(), source_id)
        )

    def remove_source(self, source_id: str, *, remove_games: bool = False) -> None:
        """Remove a source. By default its games stay in the library.

        Deleting a source is usually reconfiguration, not a request to lose
        playtime and artwork, so games are kept unless explicitly asked.
        """
        if remove_games:
            self.db.execute("DELETE FROM games WHERE source_id = ?", (source_id,))
        self.db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
