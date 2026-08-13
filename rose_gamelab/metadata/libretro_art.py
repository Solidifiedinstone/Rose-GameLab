"""Box art for emulated games, from the libretro thumbnail archive.

libretro maintains a public archive of box art, title screens and in-game
screenshots for retro systems, served over plain HTTP with no key and no
account. Files are named after the No-Intro / Redump title for the game, which
is exactly the naming our ROM filenames already follow.

That naming dependency is also the limitation: a ROM named `smb3.nes` will not
match, because the archive knows it as
`Super Mario Bros. 3 (USA).png`. Hash-based identification (see
core/hashing.py) is what eventually closes that gap; until then we try the
filename and a few normalised variants, and report honestly when nothing hits.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote, unquote

import requests

from rose_gamelab.metadata.base import USER_AGENT, MetadataProvider
from rose_gamelab.metadata.steam_store import RateLimiter

logger = logging.getLogger(__name__)

BASE = "https://thumbnails.libretro.com"

REQUEST_TIMEOUT = 15
RATE_LIMIT = 0.5

# GameLab system id -> libretro's directory name for that system.
# These strings are the archive's own naming and must match exactly.
SYSTEM_DIRECTORIES = {
    "nes": "Nintendo - Nintendo Entertainment System",
    "fds": "Nintendo - Family Computer Disk System",
    "snes": "Nintendo - Super Nintendo Entertainment System",
    "n64": "Nintendo - Nintendo 64",
    "gc": "Nintendo - GameCube",
    "wii": "Nintendo - Wii",
    "gb": "Nintendo - Game Boy",
    "gbc": "Nintendo - Game Boy Color",
    "gba": "Nintendo - Game Boy Advance",
    "nds": "Nintendo - Nintendo DS",
    "3ds": "Nintendo - Nintendo 3DS",
    "virtualboy": "Nintendo - Virtual Boy",
    "wiiu": "Nintendo - Wii U",
    "ps1": "Sony - PlayStation",
    "ps2": "Sony - PlayStation 2",
    # The archive has covered the HD-era consoles for years. Leaving them out
    # of this map was why a PS3 collection scraped to an empty grid: the
    # scraper asked whether the system was supported, got "no", and stopped.
    "ps3": "Sony - PlayStation 3",
    "ps4": "Sony - PlayStation 4",
    "psvita": "Sony - PlayStation Vita",
    "psp": "Sony - PlayStation Portable",
    "xbox": "Microsoft - Xbox",
    "xbox360": "Microsoft - Xbox 360",
    "dos": "DOS",
    "scummvm": "ScummVM",
    # Arcade ROMs are named by the MAME/FBNeo set they came from, and the two
    # archives disagree about which sets they hold — so both are tried.
    "arcade": ("MAME", "FBNeo - Arcade Games"),
    "master_system": "Sega - Master System - Mark III",
    "megadrive": "Sega - Mega Drive - Genesis",
    "segacd": "Sega - Mega-CD - Sega CD",
    "sega32x": "Sega - 32X",
    "saturn": "Sega - Saturn",
    "dreamcast": "Sega - Dreamcast",
    "gamegear": "Sega - Game Gear",
    "atari2600": "Atari - 2600",
    "atari7800": "Atari - 7800",
    "lynx": "Atari - Lynx",
    "jaguar": "Atari - Jaguar",
    "pc_engine": "NEC - PC Engine - TurboGrafx 16",
    "pc_engine_cd": "NEC - PC Engine CD - TurboGrafx-CD",
    "neogeo": "SNK - Neo Geo",
    "ngp": "SNK - Neo Geo Pocket",
    "wonderswan": "Bandai - WonderSwan",
    "3do": "The 3DO Company - 3DO",
    "msx": "Microsoft - MSX",
    "c64": "Commodore - 64",
    "amiga": "Commodore - Amiga",
}

# Archive subdirectory per artwork kind.
KIND_DIRECTORIES = {
    "cover": "Named_Boxarts",
    "hero": "Named_Snaps",
    "logo": "Named_Titles",
}

# Characters the archive replaces in filenames, because they are illegal on
# some filesystems. The archive applies these substitutions to its own names,
# so we must apply them to our lookups too.
def index_key(name: str) -> str:
    """Reduce a name to what two spellings of the same game share.

    Region tags, punctuation, articles and case all vary between a dump's folder
    name and an archive's filename; none of them change which game it is.
    """
    bare = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", " ", name).lower()
    # Dat files write "Legend of Zelda, The"; people write "The Legend of Zelda".
    bare = re.sub(r"^(the|a|an)\s+", "", bare.strip())
    bare = re.sub(r",\s*(the|a|an)\s*$", "", bare.strip())
    return re.sub(r"[^a-z0-9]+", "", bare)


_ILLEGAL = str.maketrans({"&": "_", "*": "_", "/": "_", ":": "_", "`": "_", "<": "_", ">": "_", "?": "_", "|": "_", '"': "_"})


def candidate_names(
    title: str,
    filename_stem: Optional[str] = None,
    *,
    extra: Optional[list[str]] = None,
) -> list[str]:
    """Names to try against the archive, most likely first.

    The archive uses full No-Intro titles including region tags, so the raw
    filename stem is usually the best match. Falls back to progressively more
    normalised forms.

    `extra` is for names a caller knows from the game itself rather than from
    its filename — the TITLE field of a PS3 dump's PARAM.SFO, say. Those are
    tried early because they are the publisher's own name for the game, which
    beats anything derived from whatever the folder was called.
    """
    candidates: list[str] = []

    def add(name: str) -> None:
        name = (name or "").strip()
        if name and name not in candidates:
            candidates.append(name)

    if filename_stem:
        add(filename_stem)

    for name in extra or []:
        add(name)

    add(title)

    def move_article(text: str) -> str:
        """'The Legend of Zelda' -> 'Legend of Zelda, The', as dat files file it."""
        match = re.match(r"^(The|A|An)\s+(.*)$", text, re.I)
        return f"{match.group(2)}, {match.group(1)}" if match else text

    def variants_of(seed: str) -> list[str]:
        """Every dat-style spelling of one name.

        Applied to each seed rather than only to the library title: a PS3
        folder is often named `BLES01143`, so the useful name is the one from
        the dump's PARAM.SFO — and it needs the same colon-to-dash and article
        rewriting that any other title does.
        """
        bare = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", seed).strip()
        if not bare:
            return []

        # Dat naming splits subtitles with " - " rather than a colon, and moves
        # a leading article to the end of the MAIN title only — so
        #   "The Legend of Zelda: A Link to the Past"
        # is filed as
        #   "Legend of Zelda, The - A Link to the Past (USA)"
        main, _, subtitle = bare.partition(":")
        main, subtitle = main.strip(), subtitle.strip()

        forms = [bare]
        if subtitle:
            forms.append(f"{main} - {subtitle}")
            forms.append(f"{move_article(main)} - {subtitle}")
        forms.append(move_article(bare if not subtitle else main))

        out: list[str] = []
        for form in forms:
            out.append(form)
            # Common regional variants the archive does carry.
            out.extend(f"{form} {region}" for region in
                       ("(USA)", "(Europe)", "(Japan)", "(World)"))
        return out

    # Seeds in the order they are worth trusting: what the game calls itself,
    # then what the library calls it, then what the file is called.
    for seed in [*(extra or []), title, *([filename_stem] if filename_stem else [])]:
        for name in variants_of(seed):
            add(name)

    return candidates


class LibretroArtProvider(MetadataProvider):
    """Box art for emulated games. No key required."""

    name = "libretro thumbnails"
    requires_key = False

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        rate_limit: float = RATE_LIMIT,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # Tests pass rate_limit=0 to run against fakes without sleeping.
        self.limiter = RateLimiter(rate_limit)
        #: (system, kind) -> {normalised name: url}. One listing per system.
        self._index: dict[tuple[str, str], dict[str, str]] = {}

    def available(self) -> bool:
        return True

    def supports_system(self, system_id: str) -> bool:
        return system_id in SYSTEM_DIRECTORIES

    @staticmethod
    def directories_for(system_id: str) -> tuple[str, ...]:
        """Archive directories to search for a system, best first.

        Usually one. Arcade is the exception: a set may be in MAME's archive,
        FBNeo's, or both, and which one holds it is not knowable in advance.
        """
        found = SYSTEM_DIRECTORIES.get(system_id)
        if found is None:
            return ()
        return (found,) if isinstance(found, str) else tuple(found)

    def urls_for(self, system_id: str, name: str, kind: str = "cover") -> tuple[str, ...]:
        """Every archive URL worth trying for one candidate name."""
        subdirectory = KIND_DIRECTORIES.get(kind)
        if not subdirectory:
            return ()

        safe = quote(name.translate(_ILLEGAL))
        return tuple(
            f"{BASE}/{quote(directory)}/{subdirectory}/{safe}.png"
            for directory in self.directories_for(system_id)
        )

    def url_for(self, system_id: str, name: str, kind: str = "cover") -> Optional[str]:
        """The best archive URL for one candidate name, or None if unsupported."""
        urls = self.urls_for(system_id, name, kind)
        return urls[0] if urls else None

    # ── Matching by name ──────────────────────────────────────────

    def index_for(self, system_id: str, kind: str = "cover") -> dict[str, str]:
        """Every artwork file the archive holds for a system, by normalised name.

        Guessing filenames only works when a dump is named the way the archive's
        dat files are. Real folders are called things like
        `[PS3] Demon's Souls [BLES00932]`, and no amount of variant-generation
        turns that into `Demon's Souls (USA).png`.

        So the listing is fetched once per system and matched on the name alone,
        with region tags, punctuation and case thrown away on both sides. One
        request buys a match for every game on that system.
        """
        cached = self._index.get((system_id, kind))
        if cached is not None:
            return cached

        index: dict[str, str] = {}
        subdirectory = KIND_DIRECTORIES.get(kind)

        for directory in self.directories_for(system_id):
            if not subdirectory:
                continue

            url = f"{BASE}/{quote(directory)}/{subdirectory}/"
            self.limiter.wait()
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.debug("could not list %s: %s", url, exc)
                continue

            if response.status_code != 200:
                continue

            for match in re.finditer(r'href="([^"]+\.png)"', response.text):
                filename = unquote(match.group(1))
                key = index_key(filename[:-4])
                # First writer wins, so the earlier archive in the list is
                # preferred where MAME and FBNeo both hold a set.
                index.setdefault(key, f"{url}{match.group(1)}")

        self._index[(system_id, kind)] = index
        logger.debug("libretro index for %s: %d entries", system_id, len(index))
        return index

    def find_by_name(
        self, system_id: str, names: list[str], kind: str = "cover"
    ) -> Optional[str]:
        """The archive URL for a game, matched on its name alone."""
        index = self.index_for(system_id, kind)
        if not index:
            return None

        for name in names:
            key = index_key(name)
            if key and key in index:
                return index[key]

        return None

    def download_artwork(
        self,
        system_id: str,
        title: str,
        *,
        filename_stem: Optional[str] = None,
        extra_names: Optional[list[str]] = None,
        kind: str = "cover",
    ) -> Optional[bytes]:
        """Try each candidate name until one hits. None if the game is not in
        the archive, which is common and is not an error."""
        if not self.supports_system(system_id):
            return None

        # Exact filename guesses first: they cost one request and hit whenever
        # a dump is named the way the archive expects.
        found = self._try_names(
            system_id, candidate_names(title, filename_stem, extra=extra_names), kind
        )
        if found is not None:
            return found

        # Otherwise match on the name alone against the archive's own listing.
        url = self.find_by_name(system_id, [*(extra_names or []), title], kind)
        if url is not None:
            self.limiter.wait()
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
                if response.status_code == 200 and response.content:
                    logger.debug("libretro art found for %r by name", title)
                    return response.content
            except requests.RequestException as exc:
                logger.debug("libretro request failed for %s: %s", url, exc)

        return None

    def _try_names(self, system_id: str, names: list[str], kind: str) -> Optional[bytes]:
        for name in names:
            for url in self.urls_for(system_id, name, kind):
                self.limiter.wait()
                try:
                    response = self.session.get(url, timeout=REQUEST_TIMEOUT)
                except requests.RequestException as exc:
                    logger.debug("libretro request failed for %s: %s", url, exc)
                    continue

                if response.status_code == 200 and response.content:
                    logger.debug("libretro art found as %r", name)
                    return response.content

        return None
