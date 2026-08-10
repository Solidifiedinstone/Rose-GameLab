"""Scraping: fill in metadata and artwork for library games.

Providers are consulted best-first for each game and their results merged, so a
partial answer from one does not block a better answer from another. Everything
is cached, everything is resumable, and a game the user has edited by hand is
never overwritten.

Scraping is explicitly interruptible. A library of several thousand games takes
a long time to scrape at a polite request rate, and the user must be able to
stop, quit, and pick up later without losing progress.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from rose_gamelab.core.library import Library
from rose_gamelab.metadata.base import GameMetadata, ProviderError
from rose_gamelab.metadata.cache import ArtCache
from rose_gamelab.metadata.libretro_art import LibretroArtProvider
from rose_gamelab.metadata.openvgdb import OpenVGDBProvider
from rose_gamelab.metadata.steam_store import SteamStoreProvider

logger = logging.getLogger(__name__)


@dataclass
class ScrapeProgress:
    """Live counters for a scrape run. Reported verbatim, never estimated."""

    total: int = 0
    processed: int = 0
    metadata_found: int = 0
    art_found: int = 0
    not_found: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.processed)


class Scraper:
    """Fills in metadata and artwork for library games."""

    def __init__(
        self,
        library: Library,
        *,
        cache: Optional[ArtCache] = None,
        steam: Optional[SteamStoreProvider] = None,
        libretro: Optional[LibretroArtProvider] = None,
        openvgdb: Optional[OpenVGDBProvider] = None,
    ) -> None:
        self.library = library
        self.cache = cache or ArtCache()
        self.steam = steam or SteamStoreProvider()
        self.libretro = libretro or LibretroArtProvider()
        # Optional: only used once the user has downloaded the offline
        # database. Everything still works without it, just less precisely.
        self.openvgdb = openvgdb or OpenVGDBProvider()
        self._cancel = threading.Event()

    # ── Control ───────────────────────────────────────────────────

    def cancel(self) -> None:
        """Ask an in-flight scrape to stop after the current game."""
        self._cancel.set()

    def reset(self) -> None:
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ── Identity ──────────────────────────────────────────────────

    @staticmethod
    def cache_key(game) -> str:
        """Stable cache identity for a game.

        Uses the database id, so artwork survives a retitle and two games with
        the same name never share a cover.
        """
        return f"game:{game.id}"

    # ── Single game ───────────────────────────────────────────────

    def scrape_game(self, game_id: int, *, overwrite: bool = False) -> bool:
        """Scrape one game. Returns True if anything was found.

        Games the user has edited by hand are skipped unless `overwrite` is
        set: hand-corrected metadata is more trustworthy than a scraper's
        guess, and silently clobbering it is the fastest way to lose a user.
        """
        game = self.library.get(game_id)
        if game is None:
            return False

        row = self.library.db.query_one(
            "SELECT metadata_locked FROM games WHERE id = ?", (game_id,)
        )
        if row and row["metadata_locked"] and not overwrite:
            return False

        found_anything = False

        metadata = self._fetch_metadata(game)
        if metadata and not metadata.is_empty:
            self._apply_metadata(game, metadata, overwrite=overwrite)
            found_anything = True

        if self._fetch_cover(game, overwrite=overwrite):
            found_anything = True

        return found_anything

    def _fetch_metadata(self, game) -> Optional[GameMetadata]:
        """Ask each applicable provider, merging what they return."""
        result: Optional[GameMetadata] = None

        if game.steam_appid:
            try:
                steam_data = self.steam.fetch(game.steam_appid)
                if steam_data:
                    result = steam_data
            except ProviderError as exc:
                logger.warning("Steam metadata failed for %s: %s", game.title, exc)

        # Emulated games: identify by content hash. This is the only source of
        # metadata (as opposed to art) for retro titles, and it is exact where
        # filename matching is a guess.
        identification = self._identify_rom(game)
        if identification is not None:
            result = result.merge(identification.metadata) if result else identification.metadata

            # An exact hash match knows the real title better than the
            # filename does. A filename match does not, so it never renames.
            if identification.exact and identification.title != game.title:
                self.library.update_game(game.id, title=identification.title)

        return result

    def _identify_rom(self, game):
        """Look this game's files up in the offline database by hash."""
        if not self.openvgdb.available():
            return None

        for row in self.library.files_for(game.id):
            if not (row["sha1"] or row["md5"] or row["crc32"]):
                continue
            try:
                found = self.openvgdb.identify(
                    sha1=row["sha1"],
                    md5=row["md5"],
                    crc32=row["crc32"],
                    filename=Path(row["path"]).name,
                )
            except ProviderError as exc:
                logger.warning("offline lookup failed for %s: %s", game.title, exc)
                return None

            if found is not None:
                return found

        return None

    def _apply_metadata(self, game, metadata: GameMetadata, *, overwrite: bool) -> None:
        """Write metadata into the library, without clobbering existing values.

        Unless overwriting, a field is only filled when it is currently empty,
        so re-running a scrape enriches rather than resets.
        """
        updates: dict[str, object] = {}

        def maybe(field_name: str, value: object, current: object) -> None:
            if value in (None, "", []):
                return
            if overwrite or current in (None, "", 0):
                updates[field_name] = value

        maybe("summary", metadata.summary, game.summary)
        maybe("release_date", metadata.release_date, game.release_date)
        maybe("developer", metadata.developer, game.developer)
        maybe("publisher", metadata.publisher, game.publisher)
        maybe("rating", metadata.rating, game.rating)

        if metadata.rating is not None and ("rating" in updates):
            updates["rating_source"] = metadata.rating_source

        if updates:
            self.library.update_game(game.id, **updates)

        # Genres become tags, so they work with the existing filter system
        # rather than being a separate parallel concept.
        for genre in metadata.genres:
            self.library.tag_game(game.id, genre, kind="genre")

    def _fetch_cover(self, game, *, overwrite: bool) -> bool:
        """Download and cache a cover for one game. Returns True on success."""
        key = self.cache_key(game)

        if not overwrite and self.cache.has(key, "cover"):
            # Already cached; make sure the database points at it.
            cached = self.cache.find(key, "cover")
            if cached and game.cover_path != str(cached):
                self.library.update_game(game.id, cover_path=str(cached))
            return False

        data: Optional[bytes] = None

        if game.steam_appid:
            data = self.steam.download_artwork(game.steam_appid, "cover")
        elif self.libretro.supports_system(game.system):
            # The ROM's filename is usually a better lookup key than the
            # cleaned-up library title, because the archive uses dat naming.
            files = self.library.files_for(game.id)
            stem = Path(files[0]["path"]).stem if files else None
            data = self.libretro.download_artwork(
                game.system, game.title, filename_stem=stem
            )

        if not data:
            return False

        stored = self.cache.store(key, "cover", data)
        if not stored:
            return False

        self.library.update_game(game.id, cover_path=str(stored))
        return True

    # ── Whole library ─────────────────────────────────────────────

    def scrape_library(
        self,
        *,
        only_missing: bool = True,
        overwrite: bool = False,
        limit: Optional[int] = None,
        progress: Optional[Callable[[ScrapeProgress, str], None]] = None,
    ) -> ScrapeProgress:
        """Scrape every game, or only those missing data.

        Safe to interrupt: each game is committed as it completes, so a
        cancelled run keeps everything it already found.
        """
        self.reset()

        games = self.library.list_games(include_hidden=True)
        if only_missing:
            games = [
                g for g in games
                if not g.cover_path or not g.summary or not g.release_date
            ]
        if limit:
            games = games[:limit]

        state = ScrapeProgress(total=len(games))

        for game in games:
            if self.cancelled:
                logger.info("scrape cancelled after %d games", state.processed)
                break

            try:
                before_cover = bool(self.library.get(game.id).cover_path)
                found = self.scrape_game(game.id, overwrite=overwrite)
                after = self.library.get(game.id)

                if found:
                    if after.summary or after.release_date:
                        state.metadata_found += 1
                    if after.cover_path and not before_cover:
                        state.art_found += 1
                else:
                    state.not_found += 1

            except Exception as exc:
                state.errors.append(f"{game.title}: {exc}")
                logger.exception("scrape failed for %s", game.title)

            state.processed += 1

            if progress:
                progress(state, game.title)

        return state
