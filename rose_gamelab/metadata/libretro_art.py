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
from urllib.parse import quote

import requests

from rose_gamelab.metadata.base import MetadataProvider
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
    "ps1": "Sony - PlayStation",
    "ps2": "Sony - PlayStation 2",
    "psp": "Sony - PlayStation Portable",
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
_ILLEGAL = str.maketrans({"&": "_", "*": "_", "/": "_", ":": "_", "`": "_", "<": "_", ">": "_", "?": "_", "|": "_", '"': "_"})


def candidate_names(title: str, filename_stem: Optional[str] = None) -> list[str]:
    """Names to try against the archive, most likely first.

    The archive uses full No-Intro titles including region tags, so the raw
    filename stem is usually the best match. Falls back to progressively more
    normalised forms.
    """
    candidates: list[str] = []

    def add(name: str) -> None:
        name = name.strip()
        if name and name not in candidates:
            candidates.append(name)

    if filename_stem:
        add(filename_stem)

    add(title)

    # Without region/dump tags.
    bare = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title).strip()
    add(bare)

    # Common regional variants the archive does carry.
    for region in ("(USA)", "(Europe)", "(Japan)", "(World)"):
        add(f"{bare} {region}")

    # Dat naming splits subtitles with " - " rather than a colon, and moves a
    # leading article to the end of the MAIN title only — so
    #   "The Legend of Zelda: A Link to the Past"
    # is filed as
    #   "Legend of Zelda, The - A Link to the Past (USA)"
    main, _, subtitle = bare.partition(":")
    main = main.strip()
    subtitle = subtitle.strip()

    def move_article(text: str) -> str:
        match = re.match(r"^(The|A|An)\s+(.*)$", text, re.I)
        return f"{match.group(2)}, {match.group(1)}" if match else text

    variants = []
    if subtitle:
        variants.append(f"{main} - {subtitle}")
        variants.append(f"{move_article(main)} - {subtitle}")
    variants.append(move_article(main if not subtitle else bare))

    for variant in variants:
        add(variant)
        for region in ("(USA)", "(Europe)", "(Japan)"):
            add(f"{variant} {region}")

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
        self.session.headers.setdefault("User-Agent", "Rose-GameLab/0.1")
        # Tests pass rate_limit=0 to run against fakes without sleeping.
        self.limiter = RateLimiter(rate_limit)

    def available(self) -> bool:
        return True

    def supports_system(self, system_id: str) -> bool:
        return system_id in SYSTEM_DIRECTORIES

    def url_for(self, system_id: str, name: str, kind: str = "cover") -> Optional[str]:
        """The archive URL for one candidate name, or None for unsupported input."""
        directory = SYSTEM_DIRECTORIES.get(system_id)
        subdirectory = KIND_DIRECTORIES.get(kind)

        if not directory or not subdirectory:
            return None

        safe = name.translate(_ILLEGAL)
        return f"{BASE}/{quote(directory)}/{subdirectory}/{quote(safe)}.png"

    def download_artwork(
        self,
        system_id: str,
        title: str,
        *,
        filename_stem: Optional[str] = None,
        kind: str = "cover",
    ) -> Optional[bytes]:
        """Try each candidate name until one hits. None if the game is not in
        the archive, which is common and is not an error."""
        if not self.supports_system(system_id):
            return None

        for name in candidate_names(title, filename_stem):
            url = self.url_for(system_id, name, kind)
            if not url:
                continue

            self.limiter.wait()
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.debug("libretro request failed for %s: %s", url, exc)
                continue

            if response.status_code == 200 and response.content:
                logger.debug("libretro art found for %r as %r", title, name)
                return response.content

        return None
