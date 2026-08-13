"""Installing RetroArch, and fetching the cores that make it useful.

RetroArch on its own plays nothing. It is a frontend; the emulation lives in
*cores* — one shared library per system — and a fresh install has none of them.
Getting from "RetroArch is installed" to "I can play a SNES game" means finding
the right core, downloading it, and putting it where RetroArch looks. RetroArch
can do this itself through its own online updater, buried a few menus deep in an
interface designed for a television.

GameLab already knows which core each system needs, because it needs that to
launch anything. So it can offer the same thing as a list of systems with a box
beside each: tick the consoles you own, get exactly those cores.

Two honest limits shape this.

Installing RetroArch itself needs a package manager, and a package manager needs
root. GameLab will not ask for your password and will not run sudo — a game
launcher acquiring root to install software is not a thing that should exist.
Flatpak is the exception: it installs per-user with no privileges at all, so
that path really can be done from inside the application. Everywhere else,
GameLab hands you the exact command.

Cores are different: they are plain files in your own directory, published by
the libretro buildbot, and fetching one needs no privileges at all. That part
works properly.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from rose_gamelab.core import emulator_detect
from rose_gamelab.core.emulator import SYSTEMS, get_system
from rose_gamelab.core.emulator_detect import NO_LIBRETRO_CORE

logger = logging.getLogger(__name__)

#: Where the libretro project publishes nightly Linux builds. One zip per core.
BUILDBOT = "https://buildbot.libretro.com/nightly/linux/x86_64/latest"

#: Flatpak application id, and the only route that can install RetroArch itself
#: without asking for a password.
FLATPAK_ID = "org.libretro.RetroArch"

#: Where cores are looked for and written, best first. The Flatpak location is
#: separate because a Flatpak RetroArch reads only from inside its own sandbox.
CORE_DIRECTORIES = (
    "~/.config/retroarch/cores",
    "~/.var/app/org.libretro.RetroArch/config/retroarch/cores",
    "~/.local/share/retroarch/cores",
)

DOWNLOAD_TIMEOUT = 120

#: A core smaller than this is not a core — it is an error page that was saved
#: with a .zip name, and writing it would leave RetroArch failing confusingly.
MIN_CORE_BYTES = 50 * 1024


@dataclass(frozen=True)
class Core:
    """One libretro core, and the systems it runs.

    Systems, plural, on purpose. `genesis_plus_gx` covers the Mega Drive, the
    Master System, the Game Gear and the Sega CD; `dolphin` covers GameCube and
    Wii. Listing a row per system would show the same download four times, and
    an earlier attempt at exactly that left four boxes fighting over one entry
    so that three of them did nothing when clicked.
    """

    #: Core name without the `_libretro` suffix, as the buildbot publishes it.
    name: str
    #: Every system this core runs, in the order they are worth naming.
    systems: tuple[str, ...] = ()
    system_ids: tuple[str, ...] = ()
    installed: bool = False
    #: Games in the library across all of this core's systems, so the interface
    #: can put the ones actually needed first.
    game_count: int = 0

    @property
    def system_name(self) -> str:
        """How to name this core's coverage in one line."""
        if len(self.systems) <= 2:
            return " and ".join(self.systems)
        return f"{self.systems[0]} and {len(self.systems) - 1} more"

    @property
    def filename(self) -> str:
        return f"{self.name}_libretro.so"

    @property
    def archive_url(self) -> str:
        return f"{BUILDBOT}/{self.filename}.zip"


def installed() -> bool:
    """Whether RetroArch itself is available, in any form."""
    return bool(emulator_detect.retroarch_command())


def is_flatpak() -> bool:
    return FLATPAK_ID in emulator_detect.installed_flatpaks()


def can_install_without_root() -> bool:
    """Whether GameLab can genuinely install RetroArch itself.

    Only through Flatpak. Everything else needs a package manager, which needs
    root, which a game launcher has no business asking for.
    """
    return shutil.which("flatpak") is not None


def install_command() -> str:
    """The exact command to install RetroArch on this machine."""
    if can_install_without_root():
        return f"flatpak install -y flathub {FLATPAK_ID}"
    return (emulator_detect.RETROARCH["arch"] and (
        f"sudo pacman -S {emulator_detect.RETROARCH['arch']}"
    )) or "install retroarch with your package manager"


def install_retroarch(
    *, progress: Optional[Callable[[str], None]] = None
) -> tuple[bool, str]:
    """Install RetroArch through Flatpak. Returns (succeeded, message).

    Never runs sudo, and never asks for a password. When Flatpak is not
    available this does nothing and returns the command to run instead, which
    is the honest answer rather than a spinner that fails at the end.
    """
    if installed():
        return True, "RetroArch is already installed."

    if not can_install_without_root():
        return False, (
            "RetroArch has to be installed with your package manager, which "
            f"needs a password GameLab will not ask you for. Run:\n\n    "
            f"{install_command()}"
        )

    if progress:
        progress("Installing RetroArch through Flatpak…")

    try:
        result = subprocess.run(
            ["flatpak", "install", "-y", "flathub", FLATPAK_ID],
            capture_output=True, text=True, timeout=900, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run flatpak: {exc}"

    emulator_detect.refresh()

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, "Flatpak refused: " + (detail[-1] if detail else "unknown error")

    return True, "RetroArch installed."


# ── Cores ─────────────────────────────────────────────────────────

def core_directory(*, create: bool = False) -> Optional[Path]:
    """Where cores belong on this machine.

    A Flatpak RetroArch reads only from inside its own sandbox, so an install
    into the ordinary location would download perfectly and then be invisible.
    """
    if is_flatpak():
        candidates = [CORE_DIRECTORIES[1], *CORE_DIRECTORIES]
    else:
        candidates = list(CORE_DIRECTORIES)

    for entry in candidates:
        path = Path(entry).expanduser()
        if path.is_dir():
            return path

    if not create:
        return None

    path = Path(candidates[0]).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("could not create the core directory %s: %s", path, exc)
        return None
    return path


def installed_cores(directory: Optional[Path] = None) -> set[str]:
    """Core names already present, without the `_libretro.so` suffix."""
    folder = directory or core_directory()
    if folder is None:
        return set()

    try:
        return {
            path.name.replace("_libretro.so", "")
            for path in folder.glob("*_libretro.so")
        }
    except OSError:
        return set()


def available_cores(library=None, directory: Optional[Path] = None) -> list[Core]:
    """Every core GameLab knows how to use, most useful first.

    Ordered by how many games the user actually owns for that system, so the
    boxes worth ticking are the ones at the top. Systems with no libretro core
    at all — the modern consoles — are left out rather than listed as
    unavailable, because they are not a choice anyone can make.
    """
    counts: dict[str, int] = {}
    if library is not None:
        try:
            counts = dict(library.systems_in_library())
        except Exception:
            logger.exception("could not count games per system")

    present = installed_cores(directory)

    # Grouped by core, because that is what gets downloaded. Systems whose
    # games are in the library come first within each core's name list, so a
    # core covering four consoles is labelled with the one actually owned.
    grouped: dict[str, list[tuple[int, str, str]]] = {}
    for system_id, system in SYSTEMS.items():
        if system_id == "pc" or system_id in NO_LIBRETRO_CORE:
            continue
        if not system.default_core:
            continue
        grouped.setdefault(system.default_core, []).append(
            (int(counts.get(system_id, 0)), system.name, system_id)
        )

    cores = []
    for name, entries in grouped.items():
        entries.sort(key=lambda entry: (-entry[0], entry[1]))
        cores.append(Core(
            name=name,
            systems=tuple(entry[1] for entry in entries),
            system_ids=tuple(entry[2] for entry in entries),
            installed=name in present,
            game_count=sum(entry[0] for entry in entries),
        ))

    # Owned systems first, so the boxes worth ticking are at the top.
    cores.sort(key=lambda core: (-core.game_count, core.systems[0]))
    return cores


@dataclass
class InstallResult:
    """What a core installation actually did."""

    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = []
        if self.installed:
            parts.append(f"{len(self.installed)} core(s) installed")
        if self.skipped:
            parts.append(f"{len(self.skipped)} already present")
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        return ", ".join(parts) or "nothing to do"


def install_cores(
    names: Iterable[str],
    *,
    directory: Optional[Path] = None,
    session=None,
    progress: Optional[Callable[[str, int, int], None]] = None,
    overwrite: bool = False,
) -> InstallResult:
    """Download and unpack cores by name.

    Each core is written to a temporary file and moved into place, so an
    interrupted download never leaves a half-written library that RetroArch
    will try to load and crash on.
    """
    import requests

    result = InstallResult()
    wanted = list(names)

    folder = Path(directory).expanduser() if directory else core_directory(create=True)
    if folder is None:
        result.errors.append(
            "Could not find or create a directory for cores. Is RetroArch installed?"
        )
        return result

    # Created whether it was chosen here or handed in. Only the discovered path
    # was being created, so an explicit destination failed at the write with a
    # "no such file or directory" that read as a download problem.
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.errors.append(f"Could not create {folder}: {exc}")
        return result

    present = installed_cores(folder)
    session = session or requests.Session()
    session.headers["User-Agent"] = _user_agent()

    for index, name in enumerate(wanted, start=1):
        if name in present and not overwrite:
            result.skipped.append(name)
            continue

        if progress:
            progress(name, index, len(wanted))

        url = f"{BUILDBOT}/{name}_libretro.so.zip"
        try:
            response = session.get(url, timeout=DOWNLOAD_TIMEOUT)
            response.raise_for_status()
            payload = response.content
        except Exception as exc:
            result.errors.append(f"{name}: could not download ({exc})")
            continue

        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
                members = [n for n in bundle.namelist() if n.endswith(".so")]
                if not members:
                    result.errors.append(f"{name}: the archive contained no core")
                    continue
                data = bundle.read(members[0])
        except zipfile.BadZipFile:
            result.errors.append(f"{name}: the download was not a valid archive")
            continue

        if len(data) < MIN_CORE_BYTES:
            result.errors.append(f"{name}: the download was implausibly small")
            continue

        target = folder / f"{name}_libretro.so"
        # Built by name rather than with_suffix, which takes a single suffix and
        # would mangle a name containing dots.
        temporary = folder / f"{name}_libretro.so.part"
        try:
            temporary.write_bytes(data)
            temporary.replace(target)
        except OSError as exc:
            result.errors.append(f"{name}: could not write it ({exc})")
            temporary.unlink(missing_ok=True)
            continue

        result.installed.append(name)
        logger.info("installed libretro core %s", name)

    return result


def cores_for_library(library) -> list[Core]:
    """Cores for the systems the user actually owns games for."""
    return [core for core in available_cores(library) if core.game_count]


def missing_for_library(library) -> list[Core]:
    """Owned systems whose core is not installed — what to tick by default."""
    return [core for core in cores_for_library(library) if not core.installed]


def _user_agent() -> str:
    from rose_gamelab.metadata.base import USER_AGENT

    return USER_AGENT


def describe_system(system_id: str) -> str:
    system = get_system(system_id)
    return system.name if system else system_id
