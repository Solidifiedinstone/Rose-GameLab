"""Finding the screenshots emulators have already taken.

Every emulator saves screenshots somewhere, and every one of them picks a
different somewhere and a different filename. Nobody goes looking through
`~/.config/retroarch/screenshots` to find the shot they took of a boss fight
three months ago, so the shots may as well not exist.

They are not catalogued or copied — they are found, on demand, by looking in the
places emulators actually write to and matching the game's name. That means a
screenshot taken a minute ago appears without a rescan, and deleting one from
disk makes it disappear, which is what someone browsing a folder would expect.

Nothing here writes anything.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

#: How deep to descend. RetroArch nests one level per system; nobody nests
#: screenshots five deep, and walking a whole home directory would be rude.
MAX_DEPTH = 3

#: Never look at more than this many files in one directory tree. A misconfigured
#: emulator pointed at a media library should not stall the interface.
MAX_FILES_SCANNED = 20000


def _home() -> Path:
    return Path.home()


def default_directories() -> list[Path]:
    """Where the emulators GameLab knows about write screenshots.

    Flatpak installs keep their own home, so both are listed. Missing ones cost
    nothing — they are filtered out before anything is walked.
    """
    home = _home()
    flatpak = home / ".var" / "app"

    candidates = [
        home / ".config" / "retroarch" / "screenshots",
        home / ".var" / "app" / "org.libretro.RetroArch" / "config" / "retroarch" / "screenshots",
        home / ".config" / "rpcs3" / "screenshots",
        flatpak / "net.rpcs3.RPCS3" / "config" / "rpcs3" / "screenshots",
        home / ".config" / "PCSX2" / "snaps",
        flatpak / "net.pcsx2.PCSX2" / "config" / "PCSX2" / "snaps",
        home / ".local" / "share" / "duckstation" / "screenshots",
        flatpak / "org.duckstation.DuckStation" / "data" / "duckstation" / "screenshots",
        home / ".local" / "share" / "dolphin-emu" / "ScreenShots",
        flatpak / "org.DolphinEmu.dolphin-emu" / "data" / "dolphin-emu" / "ScreenShots",
        home / ".config" / "Ryujinx" / "screenshots",
        home / ".local" / "share" / "Cemu" / "screenshots",
        home / ".config" / "ppsspp" / "PSP" / "SCREENSHOT",
        home / ".local" / "share" / "Steam" / "userdata",
        home / "Pictures" / "Screenshots",
        home / "Pictures" / "GameLab",
    ]

    extra = os.environ.get("ROSE_SCREENSHOT_DIRS", "")
    candidates.extend(Path(p).expanduser() for p in extra.split(os.pathsep) if p.strip())

    return [p for p in candidates if p.is_dir()]


@dataclass(frozen=True)
class Screenshot:
    """One image found on disk."""

    path: Path
    taken_at: float          # POSIX mtime, which is when the shot was taken

    @property
    def name(self) -> str:
        return self.path.name


def match_key(name: str) -> str:
    """Reduce a name to what a filename and a game title have in common.

    Emulators write `Super Metroid (USA)-250102-140233.png`, or replace spaces
    with underscores, or drop punctuation entirely. None of that changes which
    game the shot is of.
    """
    bare = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", " ", name).lower()
    return re.sub(r"[^a-z0-9]+", "", bare)


def _is_match(filename: str, keys: Iterable[str]) -> bool:
    """Whether a screenshot filename belongs to a game with these names."""
    stem = match_key(Path(filename).stem)
    if not stem:
        return False

    # The filename usually carries a timestamp after the title, so the game's
    # key is a PREFIX of it rather than the whole thing. Requiring a reasonable
    # length stops a two-letter title matching everything on disk.
    return any(
        len(key) >= 4 and (stem.startswith(key) or key in stem) for key in keys
    )


def find_for_game(
    names: Iterable[str],
    *,
    directories: Optional[list[Path]] = None,
    limit: int = 60,
) -> list[Screenshot]:
    """Screenshots belonging to a game, newest first.

    `names` is every name the game goes by — its title, and for a folder game
    the name of its folder — because emulators name the file after whichever
    one they were launched with.
    """
    keys = [match_key(name) for name in names if name]
    keys = [key for key in keys if len(key) >= 4]
    if not keys:
        return []

    found: list[Screenshot] = []
    scanned = 0

    for root in (directories if directories is not None else default_directories()):
        for path in _walk(root, MAX_DEPTH):
            scanned += 1
            if scanned > MAX_FILES_SCANNED:
                logger.debug("stopped scanning %s after %d files", root, scanned)
                break

            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if not _is_match(path.name, keys):
                continue

            try:
                found.append(Screenshot(path=path, taken_at=path.stat().st_mtime))
            except OSError:
                continue

    found.sort(key=lambda shot: shot.taken_at, reverse=True)
    return found[:limit]


def _walk(root: Path, depth: int) -> Iterable[Path]:
    """Files under `root`, no deeper than `depth`. Never follows symlinks."""
    if depth < 0:
        return

    try:
        entries = list(root.iterdir())
    except (OSError, PermissionError):
        return

    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                yield from _walk(entry, depth - 1)
            elif entry.is_file():
                yield entry
        except OSError:
            continue
