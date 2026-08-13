"""What shape a game is on disk.

A library holds three different kinds of thing and they are not interchangeable:

  - a **game folder** — a PS3 or Wii U title, a directory tree that must stay
    intact and is launched by pointing an emulator at a file deep inside it
  - a **disc image** — one file that is a whole disc, sometimes with a cuesheet
    and track files beside it, sometimes several of them for a multi-disc game
  - a **ROM file** — a cartridge dump, self-contained, one file one game

Getting this wrong is destructive rather than merely untidy: moving a PS3
game's EBOOT.BIN out of its folder the way a loose ROM would be moved leaves
the user with a broken dump and an entry that no longer launches.

Nothing here asks the user which kind they have. The shape is read off the disk
— a directory that matches a known layout is a folder game, and everything else
is decided by extension — so pointing GameLab at a mixed folder of PS3 rips,
PS2 ISOs and SNES carts just works.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Union

from rose_gamelab.core import folder_games
from rose_gamelab.core.folder_games import FolderGame

#: One file that is an entire optical disc. Note the overlap with cuesheets:
#: when a .cue exists it is the entry point and the .bin tracks beside it are
#: not games — core.discs already knows this, and it is why grouping runs
#: before anything here is asked about a file.
DISC_IMAGE_EXTENSIONS = {
    ".iso", ".bin", ".img", ".mdf", ".nrg", ".cdi", ".chd",
    ".cue", ".ccd", ".gdi", ".mds", ".toc",
    # Console-specific disc containers.
    ".rvz", ".wbfs", ".gcz", ".gcm", ".ciso", ".cso", ".wia", ".wud", ".wux",
}

#: Extensions that are a disc for some systems and a cartridge for others.
#: `.bin` is a Mega Drive ROM as often as it is a disc track, so when the
#: system is known its own answer is used instead of the extension's.
AMBIGUOUS_EXTENSIONS = {".bin", ".img"}

#: A playlist is a game made of several discs, not a disc itself.
PLAYLIST_EXTENSIONS = {".m3u", ".m3u8"}


class MediaKind(str, Enum):
    """How a game is stored. `str` so it survives being put in a database."""

    FOLDER = "folder"
    DISC_IMAGE = "disc_image"
    PLAYLIST = "playlist"
    FILE = "file"

    @property
    def label(self) -> str:
        """Singular, in the words the interface uses."""
        return {
            MediaKind.FOLDER: "game folder",
            MediaKind.DISC_IMAGE: "disc image",
            MediaKind.PLAYLIST: "multi-disc playlist",
            MediaKind.FILE: "ROM file",
        }[self]

    @property
    def plural(self) -> str:
        return {
            MediaKind.FOLDER: "game folders",
            MediaKind.DISC_IMAGE: "disc images",
            MediaKind.PLAYLIST: "multi-disc playlists",
            MediaKind.FILE: "ROM files",
        }[self]

    @property
    def moves_as_a_unit(self) -> bool:
        """Whether organising this must move a whole directory, not a file.

        The one question the ROM organiser has to get right.
        """
        return self is MediaKind.FOLDER


def classify(
    path: Union[str, Path, FolderGame],
    *,
    system_id: Optional[str] = None,
) -> MediaKind:
    """Decide what kind of media `path` is, by looking at it.

    Accepts a `FolderGame` directly so callers holding scan results do not have
    to re-detect anything. `system_id`, when the caller has already worked it
    out, settles the extensions that mean different things on different systems.

    A directory is only a FOLDER game when it matches a known layout; an
    unrecognised directory is NOT one, because a folder of ISOs is a shelf of
    games and must never be moved as a single unit.
    """
    if isinstance(path, FolderGame):
        return MediaKind.FOLDER

    path = Path(path)

    if path.is_dir():
        if folder_games.detect(path) is not None:
            return MediaKind.FOLDER
        return MediaKind.FILE

    suffix = path.suffix.lower()

    if suffix in PLAYLIST_EXTENSIONS:
        return MediaKind.PLAYLIST

    if suffix in DISC_IMAGE_EXTENSIONS:
        if suffix in AMBIGUOUS_EXTENSIONS and system_id:
            from rose_gamelab.core.emulator import get_system

            system = get_system(system_id)
            if system is not None and not system.disc_based:
                return MediaKind.FILE
        return MediaKind.DISC_IMAGE

    return MediaKind.FILE


def folder_game_for(path: Union[str, Path]) -> Optional[FolderGame]:
    """The folder game `path` is or belongs to, if any.

    Works for a file inside a game as well as the game's own directory, so
    anything holding a path — a library entry, a dropped file — can find out
    whether it is part of something bigger.
    """
    return folder_games.game_root_for(path)


def summarise(items: Iterable[Union[str, Path, FolderGame]]) -> dict[MediaKind, int]:
    """Count each kind, for telling the user what a folder actually contains."""
    counts: dict[MediaKind, int] = {}
    for item in items:
        kind = classify(item)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def describe(counts: dict[MediaKind, int]) -> str:
    """A summary line: '12 game folders, 40 disc images'. '' when empty."""
    parts = [
        f"{count} {kind.plural if count != 1 else kind.label}"
        for kind, count in sorted(counts.items(), key=lambda kv: -kv[1])
        if count
    ]
    return ", ".join(parts)
