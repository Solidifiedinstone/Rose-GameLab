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


#: Where a user-scope flathub remote is added from, when one is missing.
FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"


def _run(command: list[str], *, timeout: int = 60):
    """Run a command, returning None instead of raising."""
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not run %s: %s", command[0], exc)
        return None


def flatpak_remotes() -> dict[str, str]:
    """Configured remotes, mapped to 'user' or 'system'.

    The scope matters more than it looks. A system remote installs system-wide,
    which needs authentication through polkit — and a game launcher that
    triggers a password prompt from a settings screen, while blocking, is
    indistinguishable from one that has frozen.
    """
    result = _run(["flatpak", "remotes", "--columns=name,options"], timeout=20)
    if result is None or result.returncode != 0:
        return {}

    remotes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        options = " ".join(parts[1:]).lower()
        remotes[parts[0]] = "user" if "user" in options else "system"
    return remotes


def has_user_flathub() -> bool:
    return flatpak_remotes().get("flathub") == "user"


def ensure_user_flathub() -> bool:
    """Add flathub for this user if it is missing. Needs no authentication.

    A system-only flathub — which is what a distribution's flatpak package
    usually sets up — cannot serve a `--user` install at all: it fails with
    "no remote refs found". Adding the same remote at user scope is free, needs
    no password, and leaves the system one alone.
    """
    if has_user_flathub():
        return True

    result = _run(
        ["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_REPO],
        timeout=60,
    )
    return result is not None and result.returncode == 0


def can_install_without_root() -> bool:
    """Whether GameLab can genuinely install RetroArch itself.

    Only through a *user-scope* Flatpak install. Everything else — a system
    Flatpak, a package manager — needs authentication, and a game launcher
    has no business asking for a password.
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
        progress("Adding the Flathub repository for your user…")

    if not ensure_user_flathub():
        return False, (
            "Flathub is only set up system-wide here, and adding it for your "
            "user failed. Install RetroArch yourself with:\n\n    "
            f"flatpak install flathub {FLATPAK_ID}"
        )

    if progress:
        progress("Downloading RetroArch… this takes a few minutes.")

    # --user so no authentication is ever required, and --noninteractive so a
    # question flatpak cannot ask makes it exit rather than wait forever for an
    # answer that is never coming.
    result = _run(
        [
            "flatpak", "install", "--user", "--noninteractive",
            "flathub", FLATPAK_ID,
        ],
        timeout=1800,
    )

    emulator_detect.refresh()

    if result is None:
        return False, (
            "Flatpak could not be run, or took too long. Install RetroArch "
            f"yourself with:\n\n    flatpak install flathub {FLATPAK_ID}"
        )

    if result.returncode != 0:
        detail = [
            line for line in
            (result.stderr or result.stdout or "").strip().splitlines()
            if line.strip()
        ]
        return False, (
            "Flatpak refused: " + (detail[-1] if detail else "unknown error")
        )

    if not installed():
        return False, (
            "Flatpak reported success but RetroArch still cannot be found. "
            "Try running it once from your applications menu."
        )

    return True, "RetroArch installed."


# ── Cores ─────────────────────────────────────────────────────────

#: Where RetroArch keeps its own configuration, which is the only authority on
#: where it looks for cores and BIOS files.
CONFIG_FILES = (
    "~/.var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg",
    "~/.config/retroarch/retroarch.cfg",
)


def config_file() -> Optional[Path]:
    """RetroArch's own configuration file, if it has one."""
    order = CONFIG_FILES if is_flatpak() else tuple(reversed(CONFIG_FILES))
    for entry in order:
        path = Path(entry).expanduser()
        if path.is_file():
            return path
    return None


def configured_directory(setting: str) -> Optional[Path]:
    """Read a directory out of RetroArch's configuration.

    Asking RetroArch where it looks beats guessing. Guessing put cores in
    ~/.config/retroarch/cores on a machine whose RetroArch is a Flatpak and
    reads only from inside its sandbox: they downloaded perfectly, RetroArch
    never saw one of them, and nothing said why.
    """
    path = config_file()
    if path is None:
        return None

    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == setting:
                value = value.strip().strip('"')
                if value and value not in (":", "default"):
                    return Path(value).expanduser()
    except OSError as exc:
        logger.warning("could not read %s: %s", path, exc)

    return None


def core_directory(*, create: bool = False) -> Optional[Path]:
    """Where cores belong on this machine.

    RetroArch's own `libretro_directory` first, because that is the answer;
    the known locations are only a fallback for an installation that has not
    written a configuration yet.
    """
    configured = configured_directory("libretro_directory")
    if configured is not None:
        if configured.is_dir():
            return configured
        if create:
            try:
                configured.mkdir(parents=True, exist_ok=True)
                return configured
            except OSError as exc:
                logger.warning("could not create %s: %s", configured, exc)

    if is_flatpak():
        # Never the host path for a Flatpak: it cannot read cores from there.
        candidates = [CORE_DIRECTORIES[1]]
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


def system_directory() -> Optional[Path]:
    """Where RetroArch looks for BIOS files."""
    configured = configured_directory("system_directory")
    if configured is not None:
        return configured

    cores = core_directory()
    return cores.parent / "system" if cores else None


#: Cores that will not run without a BIOS, and the files they want. Without
#: one, SwanStation does not start — and "crashes on launch" is what that
#: looks like from the outside, which is why this is checked and said out
#: loud rather than left for the user to discover.
BIOS_REQUIRED: dict[str, tuple[str, ...]] = {
    "swanstation": ("scph5500.bin", "scph5501.bin", "scph5502.bin"),
    "mednafen_psx": ("scph5500.bin", "scph5501.bin", "scph5502.bin"),
    "pcsx_rearmed": ("scph5500.bin", "scph5501.bin", "scph5502.bin"),
    "mednafen_saturn": ("sega_101.bin", "mpr-17933.bin"),
    "mednafen_pce": ("syscard3.pce",),
    "flycast": ("dc_boot.bin", "dc_flash.bin"),
    "opera": ("panafz1.bin", "panafz10.bin", "goldstar.bin"),
    "pcsx2": ("ps2-0230a-20080220.bin",),
}


@dataclass(frozen=True)
class BiosNeed:
    """One core's firmware requirement, and whether it is met."""

    core: str
    systems: tuple[str, ...]
    filenames: tuple[str, ...]
    satisfied: bool

    @property
    def summary(self) -> str:
        names = " or ".join(self.filenames)
        return f"{', '.join(self.systems)} — {names}"


def bios_needs(library=None) -> list[BiosNeed]:
    """Firmware wanted by the cores worth caring about, most useful first.

    Every core that needs firmware is listed, not only the installed ones: the
    point is to be able to put the file in place *before* discovering a game
    will not start, which is the whole reason this is hard to diagnose.
    """
    present = installed_cores()
    counts: dict[str, int] = {}
    if library is not None:
        try:
            counts = dict(library.systems_in_library())
        except Exception:
            logger.exception("could not count games per system")

    # The core list is walked once and the sort keys built as we go: looking
    # each core up again from inside a sort key turns this into a quadratic
    # scan of the whole catalogue for no benefit.
    ordered = []
    for core in available_cores(library):
        wanted = BIOS_REQUIRED.get(core.name)
        if not wanted:
            continue

        owned = sum(counts.get(system_id, 0) for system_id in core.system_ids)
        need = BiosNeed(
            core=core.name,
            systems=core.systems,
            filenames=wanted,
            satisfied=not missing_bios(core.name),
        )
        # Unsatisfied first, then whatever the user owns games for, so the
        # thing standing between them and a game they have is at the top.
        ordered.append(((need.satisfied, -owned, core.name not in present,
                         core.systems[0]), need))

    ordered.sort(key=lambda entry: entry[0])
    return [need for _key, need in ordered]


@dataclass
class BiosInstallResult:
    """What copying firmware in actually did."""

    added: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.errors and not (self.added or self.replaced):
            return "; ".join(self.errors)
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.replaced:
            parts.append(f"{len(self.replaced)} replaced")
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        return ", ".join(parts) or "nothing to do"


def install_bios(paths, *, directory: Optional[Path] = None) -> BiosInstallResult:
    """Copy firmware files into the directory RetroArch reads them from.

    Copied, never moved: these are the user's dumps of their own hardware, and
    a tool that relocates them out of wherever they keep their backups is a
    tool that loses them.

    Names are kept exactly as given. Every emulator that wants firmware looks
    for it by a specific filename, so "helpfully" renaming anything here would
    guarantee it is never found.
    """
    result = BiosInstallResult()

    folder = Path(directory).expanduser() if directory else system_directory()
    if folder is None:
        result.errors.append(
            "RetroArch has no system directory yet. Install it, run it once, "
            "and try again."
        )
        return result

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.errors.append(f"Could not create {folder}: {exc}")
        return result

    for entry in paths:
        source = Path(entry).expanduser()
        if not source.is_file():
            result.errors.append(f"{source.name}: not a file")
            continue

        target = folder / source.name
        existed = target.exists()
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            result.errors.append(f"{source.name}: {exc}")
            continue

        (result.replaced if existed else result.added).append(source.name)

    return result


def missing_bios(core: str) -> tuple[str, ...]:
    """BIOS files a core needs and RetroArch cannot find. Empty when happy.

    A core needing *any one of* its listed files is the usual case, so this
    reports nothing when at least one is present.
    """
    wanted = BIOS_REQUIRED.get(core)
    if not wanted:
        return ()

    folder = system_directory()
    if folder is None or not folder.is_dir():
        return wanted

    try:
        present = {path.name.lower() for path in folder.iterdir()}
    except OSError:
        return wanted

    if any(name.lower() in present for name in wanted):
        return ()
    return wanted


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
