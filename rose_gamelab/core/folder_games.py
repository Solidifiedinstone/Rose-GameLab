"""Games that are a folder rather than a file.

Most systems ship a game as one file. Several do not: a PS3 title is a
directory tree — `PS3_GAME/USRDIR/EBOOT.BIN` plus a few thousand shaders,
localisation blobs and audio banks. Walking such a folder looking for files
with "ROM extensions" finds hundreds of them, and every one looks like a game.
That is exactly what GameLab used to do: forty PS3 games imported as three
hundred entries called things like COALESCED_INT and GLOBALSHADERCACHE-PS3.

So directories are checked BEFORE being descended into. A directory that
matches a known layout is one game, is not searched any further, and the file
the emulator actually wants is recorded as what to launch.

Adding a system here means knowing three things for certain: the marker that
identifies the layout, the file the emulator is pointed at, and that both are
true of real dumps. Anything less is left out — a folder filed under the wrong
system is worse than one not recognised at all.
"""

from __future__ import annotations

import fnmatch
import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _resolve(root: Path, relative: str) -> Optional[Path]:
    """Find `relative` under `root`, ignoring case at every step.

    Dumps do not agree on case. The same release ships as
    `PS3_GAME/USRDIR/EBOOT.BIN`, as `ps3_game/usrdir/eboot.bin`, and as every
    mixture in between — the case is whatever the tool that made the dump, or
    the filesystem it was copied through, happened to produce. Windows and
    macOS never notice; Linux does, and an exact match rejects most real PS3
    folders, which is why they so often imported as nothing at all.

    Returns None when any component is missing.
    """
    current = root

    for part in relative.split("/"):
        if not part or part == ".":
            continue

        candidate = current / part
        if candidate.exists():
            current = candidate
            continue

        # Only scan the directory when the exact name missed, so the common
        # case stays one stat() per component rather than a full listing.
        wanted = part.lower()
        try:
            match = next(
                (child for child in current.iterdir() if child.name.lower() == wanted),
                None,
            )
        except OSError:
            return None

        if match is None:
            return None
        current = match

    return current


@dataclass(frozen=True)
class FolderLayout:
    """One recognised on-disk shape for a folder-based game."""

    system_id: str
    #: Human description of the layout, for the interface and for errors.
    description: str
    #: Relative path that must exist for a directory to match this layout.
    #: Matched case-insensitively — see `_resolve`.
    marker: str
    #: Relative path to the file an emulator is pointed at. When it contains a
    #: '*', the first match in sorted order is used. An empty string means the
    #: game folder itself, for layouts whose emulator takes a directory.
    entry: str
    #: Optional extra check for layouts that share a marker between systems.
    confirm: Optional[Callable[[Path], bool]] = None


@dataclass
class FolderGame:
    """A directory that is one game."""

    root: Path
    system_id: str
    entry: Path
    layout: FolderLayout

    @property
    def title(self) -> str:
        """The folder's name — what the user called it.

        Deliberately not read from the game's own metadata: dumps are named by
        the person who made them, and that name is what the user recognises in
        their file manager.
        """
        return self.root.name


# ── Disc magic ────────────────────────────────────────────────────
#
# GameCube and Wii both extract to sys/ + files/, so the marker alone cannot
# tell them apart. The disc header in sys/boot.bin can: each platform stamps a
# magic word at a fixed offset.

WII_MAGIC = 0x5D1C9EA3       # at offset 0x18
GAMECUBE_MAGIC = 0xC2339F3D  # at offset 0x1C


def _disc_magic(root: Path) -> Optional[int]:
    """Read the magic word out of an extracted disc's sys/boot.bin."""
    boot = _resolve(root, "sys/boot.bin")
    if boot is None:
        return None

    try:
        with boot.open("rb") as handle:
            header = handle.read(0x20)
    except OSError:
        return None

    if len(header) < 0x20:
        return None

    wii, gamecube = struct.unpack_from(">II", header, 0x18)
    if wii == WII_MAGIC:
        return WII_MAGIC
    if gamecube == GAMECUBE_MAGIC:
        return GAMECUBE_MAGIC
    return None


def _is_wii(root: Path) -> bool:
    return _disc_magic(root) == WII_MAGIC


def _is_gamecube(root: Path) -> bool:
    return _disc_magic(root) == GAMECUBE_MAGIC


# ── The registry ──────────────────────────────────────────────────
#
# Order matters: the first layout that matches wins, so the more specific
# marker of a pair must come first.

LAYOUTS: tuple[FolderLayout, ...] = (
    # A PS3 disc dump, and the shape every "JB folder" release uses.
    FolderLayout(
        "ps3", "PS3 disc folder",
        marker="PS3_GAME/USRDIR/EBOOT.BIN",
        entry="PS3_GAME/USRDIR/EBOOT.BIN",
    ),
    # A PS3 title as RPCS3 installs it under dev_hdd0/game/<TITLEID>, and the
    # shape a PSN release unpacks to.
    FolderLayout(
        "ps3", "PS3 installed game",
        marker="USRDIR/EBOOT.BIN",
        entry="USRDIR/EBOOT.BIN",
    ),
    # A disc dump whose EBOOT is missing, differently named, or unreadable —
    # decrypted rips and part-copied folders both land here. PS3_DISC.SFB only
    # ever sits at the top of a PS3 disc, so it identifies one for certain even
    # when the file to launch cannot be found.
    #
    # RPCS3 is pointed at the folder in that case. It is happy to be given a
    # game directory, and a game the user can see and fix beats one that
    # silently did not import.
    FolderLayout(
        "ps3", "PS3 disc folder",
        marker="PS3_DISC.SFB",
        entry="",
    ),
    # An installed title with no EBOOT where we expect one. PARAM.SFO inside
    # PS3_GAME is the game's own metadata and is present in every real dump.
    FolderLayout(
        "ps3", "PS3 game folder",
        marker="PS3_GAME/PARAM.SFO",
        entry="",
    ),
    # An extracted UMD.
    FolderLayout(
        "psp", "PSP game folder",
        marker="PSP_GAME/SYSDIR/EBOOT.BIN",
        entry="PSP_GAME/SYSDIR/EBOOT.BIN",
    ),
    # A Vita title as Vita3K lays it out.
    FolderLayout(
        "psvita", "Vita app folder",
        marker="sce_sys/param.sfo",
        entry="eboot.bin",
    ),
    # Loadiine / extracted Wii U title.
    FolderLayout(
        "wiiu", "Wii U game folder",
        marker="meta/meta.xml",
        entry="code/*.rpx",
    ),
    # Extracted Xbox 360 disc or a GOD install.
    FolderLayout(
        "xbox360", "Xbox 360 game folder",
        marker="default.xex",
        entry="default.xex",
    ),
    # Original Xbox. Note that xemu wants a disc image, so these import but
    # will not launch until the user points a profile at something that can
    # run them — which is honest, and better than hiding the game.
    FolderLayout(
        "xbox", "Xbox game folder",
        marker="default.xbe",
        entry="default.xbe",
    ),
    # Extracted GameCube and Wii discs share a shape; the disc header decides.
    FolderLayout(
        "wii", "Extracted Wii disc",
        marker="sys/main.dol", entry="sys/main.dol", confirm=_is_wii,
    ),
    FolderLayout(
        "gc", "Extracted GameCube disc",
        marker="sys/main.dol", entry="sys/main.dol", confirm=_is_gamecube,
    ),
)

#: Directory names that are part of a game rather than a game themselves.
#: Used to stop a nested layout being reported twice — PS3_GAME sits inside a
#: directory that has already matched.
INTERNAL_DIRECTORIES = {
    "PS3_GAME", "PS3_UPDATE", "PSP_GAME", "USRDIR", "SYSDIR", "TROPDIR",
    "sce_sys", "sce_module", "code", "content", "meta", "sys", "files",
}

#: Compared case-insensitively for the same reason markers are — see `_resolve`.
_INTERNAL_UPPER = {name.upper() for name in INTERNAL_DIRECTORIES}


def _resolve_entry(root: Path, pattern: str) -> Optional[Path]:
    """Find the file to launch, expanding a single '*' if the layout uses one.

    An empty pattern means the folder itself, for the layouts whose emulator
    takes a game directory rather than a file inside it.
    """
    if pattern == "":
        return root if root.is_dir() else None

    if "*" not in pattern:
        found = _resolve(root, pattern)
        return found if found is not None and found.is_file() else None

    directory, _, glob = pattern.rpartition("/")
    search = _resolve(root, directory) if directory else root
    if search is None or not search.is_dir():
        return None

    try:
        matches = sorted(
            child for child in search.iterdir()
            if child.is_file() and fnmatch.fnmatch(child.name.lower(), glob.lower())
        )
    except OSError:
        return None
    return matches[0] if matches else None


def detect(directory: str | Path) -> Optional[FolderGame]:
    """Identify `directory` as a game folder, or return None.

    None means "not a game folder", never "probably one" — callers descend into
    anything this does not claim.
    """
    root = Path(directory)

    # A game's own innards can match a shorter layout — PS3_GAME contains
    # USRDIR/EBOOT.BIN, which is exactly the installed-game marker. Naming the
    # internal directories keeps the game folder itself the answer.
    if root.name.upper() in _INTERNAL_UPPER:
        return None

    for layout in LAYOUTS:
        try:
            if _resolve(root, layout.marker) is None:
                continue
            if layout.confirm is not None and not layout.confirm(root):
                continue
        except OSError as exc:
            logger.debug("could not check %s against %s: %s", root, layout.marker, exc)
            continue

        entry = _resolve_entry(root, layout.entry)
        if entry is None:
            # The marker matched but the file to launch is missing, so the dump
            # is incomplete. Reported by the caller rather than silently fixed.
            logger.debug("%s looks like %s but has no %s", root, layout.system_id, layout.entry)
            continue

        return FolderGame(
            root=root, system_id=layout.system_id, entry=entry, layout=layout
        )

    return None


def game_root_for(path: str | Path) -> Optional[FolderGame]:
    """Walk up from a file to the game folder containing it, if any.

    Lets anything holding a path — a library entry imported before folder games
    were understood, a file the user dropped on the organiser — find out which
    game it is really part of.
    """
    current = Path(path)
    if current.is_file():
        current = current.parent

    # Bounded: game layouts nest a handful of directories deep, and walking to
    # the filesystem root would claim files that merely live under a game.
    for _ in range(6):
        found = detect(current)
        if found is not None:
            return found
        if current.parent == current:
            break
        current = current.parent

    return None


# ── PARAM.SFO ─────────────────────────────────────────────────────

SFO_MAGIC = b"\x00PSF"


def read_param_sfo(path: str | Path) -> dict[str, str | int]:
    """Read a PARAM.SFO into a plain dictionary.

    Sony's format, used by the PS3, PSP and Vita, and the only place a dump
    carries its real title and title id. Returns {} for anything unreadable
    rather than raising: metadata is a bonus, and a corrupt file must not stop
    a game being imported.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return {}

    if len(raw) < 20 or raw[:4] != SFO_MAGIC:
        return {}

    try:
        key_table, data_table, count = struct.unpack_from("<III", raw, 8)

        values: dict[str, str | int] = {}
        for index in range(count):
            offset = 20 + index * 16
            key_offset, fmt, length, _max_length, data_offset = struct.unpack_from(
                "<HHIII", raw, offset
            )

            start = key_table + key_offset
            end = raw.index(b"\x00", start)
            key = raw[start:end].decode("utf-8", "replace")

            blob = raw[data_table + data_offset: data_table + data_offset + length]
            if fmt == 0x0404:                      # integer
                values[key] = struct.unpack("<I", blob[:4])[0]
            else:                                  # utf-8, null-terminated
                values[key] = blob.split(b"\x00", 1)[0].decode("utf-8", "replace")

        return values
    except (struct.error, ValueError, IndexError) as exc:
        logger.debug("malformed PARAM.SFO at %s: %s", path, exc)
        return {}


def _sfo_value(game: FolderGame, key: str) -> Optional[str]:
    """Read one string field from whichever PARAM.SFO this layout carries."""
    for relative in (
        "PS3_GAME/PARAM.SFO",
        "PARAM.SFO",
        "PSP_GAME/PARAM.SFO",
        "sce_sys/param.sfo",
    ):
        candidate = _resolve(game.root, relative)
        if candidate is not None and candidate.is_file():
            value = read_param_sfo(candidate).get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return None


#: Artwork every dump of these systems carries inside itself, best first.
#:
#: This is the one art source that cannot miss: it is not a lookup, a guess or a
#: name match — it is the publisher's own image, shipped in the game, sitting on
#: the user's disk. The archives cover a few dozen PS3 titles between them; this
#: covers every dump that exists, including the exclusives no archive has.
INTERNAL_ARTWORK: dict[str, dict[str, tuple[str, ...]]] = {
    "ps3": {
        # ICON0 is the icon the PS3 dashboard shows for the game.
        "cover": ("PS3_GAME/ICON0.PNG", "ICON0.PNG"),
        # PIC1 is the full-screen background behind it.
        "hero": ("PS3_GAME/PIC1.PNG", "PIC1.PNG", "PS3_GAME/PIC0.PNG"),
    },
    "psp": {
        "cover": ("PSP_GAME/ICON0.PNG", "ICON0.PNG"),
        "hero": ("PSP_GAME/PIC1.PNG", "PIC1.PNG"),
    },
    "psvita": {
        "cover": ("sce_sys/icon0.png",),
        "hero": ("sce_sys/pic0.png", "sce_sys/livearea/contents/bg.png"),
    },
    "wiiu": {
        # TGA, which Qt reads; the .png forms appear in some repacks.
        "cover": ("meta/iconTex.tga", "meta/iconTex.png"),
        "hero": ("meta/bootTvTex.tga", "meta/bootTvTex.png"),
    },
}


def artwork_in(game: FolderGame, kind: str = "cover") -> Optional[Path]:
    """The game's own artwork, from inside the dump. None when it has none."""
    for relative in INTERNAL_ARTWORK.get(game.system_id, {}).get(kind, ()):
        found = _resolve(game.root, relative)
        if found is not None and found.is_file():
            return found

    return None


def title_id_for(game: FolderGame) -> Optional[str]:
    """The publisher's own id for a game folder — BLUS30443 and the like.

    Worth having because it identifies a game exactly, where a folder name is
    whoever-dumped-it's opinion.
    """
    return _sfo_value(game, "TITLE_ID")


def title_for(game: FolderGame) -> Optional[str]:
    """The game's own name, as the publisher wrote it into the dump.

    Not used for the library entry — the folder name is what the user
    recognises — but it is the best possible key for looking up artwork, since
    a folder called `BLUS30443` or `[PS3] Demons Souls [EUR]` says nothing an
    art archive would match.
    """
    return _sfo_value(game, "TITLE")
