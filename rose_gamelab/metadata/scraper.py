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

from rose_gamelab.core import folder_games
from rose_gamelab.core.library import Library
from rose_gamelab.metadata.base import GameMetadata, ProviderError
from rose_gamelab.metadata.cache import ArtCache
from rose_gamelab.metadata.libretro_art import LibretroArtProvider
from rose_gamelab.metadata.openvgdb import OpenVGDBProvider
from rose_gamelab.metadata.steam_store import SteamStoreProvider
from rose_gamelab.metadata.steamgriddb import SteamGridDBProvider
from rose_gamelab.metadata.wikidata import WikidataProvider

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
        griddb: Optional[SteamGridDBProvider] = None,
        wikidata: Optional[WikidataProvider] = None,
    ) -> None:
        self.library = library
        self.cache = cache or ArtCache()
        self.steam = steam or SteamStoreProvider()
        self.libretro = libretro or LibretroArtProvider()
        # Needs a free API key, so it is frequently unavailable. Everything
        # else works without it; it is the fallback that covers what the
        # keyless sources cannot — launchers, fan games, obscure dumps.
        self.griddb = griddb if griddb is not None else SteamGridDBProvider()
        # Keyless, and the only metadata source that covers the disc and HD era
        # at all. Consulted after the exact sources, never instead of them.
        self.wikidata = wikidata if wikidata is not None else WikidataProvider()
        # Optional: only used once the user has downloaded the offline
        # database. Everything still works without it, just less precisely.
        self.openvgdb = openvgdb or OpenVGDBProvider()
        self._cancel = threading.Event()
        #: game id -> appid found by name, so one search serves both the
        #: metadata pass and the artwork pass.
        self._appid_cache: dict[int, Optional[int]] = {}

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

        # Not just games that arrived through Steam: a PC game found by name
        # gets its description, genres and release date from there too, which
        # is the difference between a blank detail panel and a full one for
        # everything installed through Heroic, GOG or by hand.
        appid = self._steam_appid_for(game)
        if appid:
            try:
                steam_data = self.steam.fetch(appid)
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

        # Everything above is exact but narrow: Steam covers Steam, and the
        # hash lookup covers ROMs that are a single hashable file. Neither can
        # say anything at all about a PS3 or Wii U title, which is a folder.
        # Wikidata is keyed on name and platform, so it answers for those — and
        # it only fills the fields the exact sources left empty.
        if result is None or result.is_empty or not result.summary:
            wikidata = self._fetch_wikidata(game)
            if wikidata is not None:
                result = result.merge(wikidata) if result else wikidata

        return result

    def _fetch_wikidata(self, game) -> Optional[GameMetadata]:
        """Look this game up by name and platform. None when not found."""
        if self.wikidata is None or not self.wikidata.supports_system(game.system):
            return None

        # A folder game's PARAM.SFO title is the publisher's own name for it,
        # which beats a folder called `BLUS30443` as a search key.
        _stem, extra = self._lookup_names(game)

        try:
            return self.wikidata.fetch([*extra, game.title], game.system)
        except ProviderError as exc:
            logger.debug("Wikidata lookup failed for %s: %s", game.title, exc)
            return None

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

    def _lookup_names(self, game) -> tuple[Optional[str], list[str]]:
        """(filename stem, extra names) worth trying for this game's art.

        For an ordinary ROM the filename is the best key, because art archives
        use the same dat naming the ROM does.

        A folder game needs different treatment: its recorded path is
        `…/PS3_GAME/USRDIR/EBOOT.BIN`, so the "filename" is EBOOT — a name
        every PS3 game on earth shares and no archive lists. The folder's name
        is the useful one, and the dump's own PARAM.SFO title is better still.
        """
        files = self.library.files_for(game.id)
        if not files:
            return None, []

        path = Path(files[0]["path"])
        folder = folder_games.game_root_for(path)

        if folder is None:
            return path.stem, []

        extra: list[str] = []
        real_title = folder_games.title_for(folder)
        if real_title:
            extra.append(real_title)

        return folder.root.name, extra

    def _openvgdb_cover(self, game) -> Optional[bytes]:
        """The cover OpenVGDB already knows about.

        The offline database carries a cover URL alongside every identification
        and it was simply never used — a free, exact source of art for any ROM
        the hash lookup matched.
        """
        found = self._identify_rom(game)
        if found is None or not found.cover_url:
            return None

        try:
            response = self.steam.session.get(found.cover_url, timeout=20)
        except Exception as exc:
            logger.debug("OpenVGDB cover failed for %s: %s", game.title, exc)
            return None

        return response.content if response.status_code == 200 else None

    def _steam_appid_for(self, game, *, art: bool = False) -> Optional[int]:
        """This game's Steam appid, looked up by name when it is not known.

        Steam is the best art source for PC games generally, not only for the
        ones that arrived through Steam — a Heroic install, a GOG copy or a
        launcher added by hand all have covers there.

        The result is deliberately NOT written back to the library: the appid
        column drives duplicate-merging and the "not on Steam" filters, and a
        searched-for id is a guess. It is good enough to choose a picture and
        not good enough to merge two library entries.
        """
        if game.steam_appid:
            return game.steam_appid
        if game.system != "pc" and not art:
            # For metadata, a PC port is a different release with its own dates
            # and is not worth the risk. For ART it is the same game, and the
            # cover is what the user is trying to see.
            return None

        # Metadata and art both ask; one search per game is enough.
        if game.id in self._appid_cache:
            return self._appid_cache[game.id]

        try:
            found = self.steam.search(game.title)
        except Exception as exc:                     # never fatal
            logger.debug("Steam search failed for %s: %s", game.title, exc)
            found = None

        self._appid_cache[game.id] = found
        return found

    def _cover_sources(self, game):
        """Ways to get a cover for this game, best first.

        Yields (source name, callable). Every one is tried until a real image
        comes back — the previous version picked a single source and gave up if
        it missed, which is why a game Steam had no art for ended up with no
        art at all even when the archive had it.
        """
        stem, extra = self._lookup_names(game)
        appid = game.steam_appid

        if appid:
            yield "steam", lambda: self.steam.download_artwork(appid, "cover")

        if self.libretro.supports_system(game.system):
            yield "libretro", lambda: self.libretro.download_artwork(
                game.system, game.title, filename_stem=stem, extra_names=extra,
            )

        if self.openvgdb.available():
            yield "openvgdb", lambda: self._openvgdb_cover(game)

        # Console games are on Steam too, and its catalogue dwarfs the
        # archive's — the libretro PS3 shelf holds about sixty covers. The art
        # is the same game's art even when the release is the PC one.
        if not appid:
            yield "steam by name", lambda: self._steam_cover_by_name(game, extra)

        # The dump's own artwork. Last of the automatic sources because the
        # archives give a proper portrait box art where they have one, and this
        # is the game's dashboard icon rather than its cover — but it is the
        # only source that cannot miss, since the image is already on disk.
        internal = self._internal_artwork(game)
        if internal is not None:
            yield "the dump itself", lambda: internal.read_bytes()

        if self.griddb is not None and self.griddb.available():
            # The catch-all: it carries art for launchers, fan projects and
            # console games alike, which is what everything above cannot do.
            names = [game.title, *extra]
            yield "steamgriddb", lambda: self.griddb.download_artwork(
                names, kind="cover", steam_appid=appid,
            )

    def _internal_artwork(self, game, kind: str = "cover"):
        """The artwork shipped inside a folder game, if it has any."""
        files = self.library.files_for(game.id)
        if not files:
            return None

        found = folder_games.game_root_for(files[0]["path"])
        if found is None:
            return None

        return folder_games.artwork_in(found, kind)

    def _steam_cover_by_name(self, game, extra: list[str]) -> Optional[bytes]:
        """Find the game on Steam by name and use its cover."""
        for name in [*extra, game.title]:
            appid = None
            try:
                # Editions allowed here and nowhere else: a remaster's cover is
                # the right cover, but its release date is not the right date.
                appid = self.steam.search(name, allow_editions=True)
            except Exception as exc:
                logger.debug("Steam search failed for %r: %s", name, exc)

            if appid:
                return self.steam.download_artwork(appid, "cover")

        return None

    def _fetch_cover(self, game, *, overwrite: bool) -> bool:
        """Download and cache a cover for one game. Returns True on success."""
        key = self.cache_key(game)

        if not overwrite and self.cache.has(key, "cover"):
            # Already cached; make sure the database points at it.
            cached = self.cache.find(key, "cover")
            if cached and game.cover_path != str(cached):
                self.library.update_game(game.id, cover_path=str(cached))
            return False

        # Art the user chose by hand is theirs; a scrape must not replace it.
        # This is deliberately narrower than `metadata_locked`, which gates the
        # whole entry — picking a cover should not stop a game ever getting a
        # description.
        if game.cover_locked and not overwrite:
            return False

        data: Optional[bytes] = None

        for source, fetch in self._cover_sources(game):
            try:
                data = fetch()
            except ProviderError as exc:
                logger.debug("%s had no cover for %s: %s", source, game.title, exc)
                continue
            except Exception as exc:
                # One provider being broken must not stop the others, nor
                # abort a scrape of several thousand games.
                logger.warning("%s failed for %s: %s", source, game.title, exc)
                continue

            if data:
                logger.debug("cover for %r from %s", game.title, source)
                break

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
