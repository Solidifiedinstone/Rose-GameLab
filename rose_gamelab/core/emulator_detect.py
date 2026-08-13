"""Finding the emulators actually installed on this machine.

Two things make this less obvious than "is it on PATH".

A large share of Linux emulators are installed as Flatpaks, which put nothing
on PATH at all — the command is `flatpak run net.pcsx2.PCSX2`. Checking only
PATH reports "PCSX2 is not installed" on a machine where PCSX2 is installed and
working, which is worse than useless because it sends the user off to install
something they already have.

The same is true of AppImages, which some emulators (Xenia Edge, for one) ship
as their only Linux build. An AppImage is a single executable file sitting in
whatever folder the user dropped it in, so it is found by looking there rather
than on PATH.

And RetroArch covers most retro systems through libretro cores, so a system can
be perfectly playable with no dedicated emulator installed at all.

When nothing is found, the point is to say what to install and how, rather than
to report an empty result and leave the user guessing.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from rose_gamelab.core.emulator import SYSTEMS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmulatorOption:
    """One emulator that can run a system, and how to invoke it."""

    system_id: str
    name: str
    #: 'native' | 'flatpak' | 'appimage' | 'retroarch'
    kind: str
    command: tuple[str, ...] = ()
    #: Package names, for telling the user what to install.
    arch_package: str = ""
    flatpak_id: str = ""
    #: AUR package, for emulators no official repository carries.
    aur_package: str = ""
    #: Where to download an AppImage from, for emulators shipping only that.
    download_url: str = ""
    #: Where the emulator sits when several can run the same system.
    preference: int = 0

    @property
    def installed(self) -> bool:
        return bool(self.command)

    @property
    def install_hint(self) -> str:
        """How to install this, phrased so it can be pasted into a terminal."""
        if self.flatpak_id:
            return f"flatpak install flathub {self.flatpak_id}"
        if self.arch_package:
            return f"sudo pacman -S {self.arch_package}"
        if self.aur_package:
            return f"{aur_helper()} -S {self.aur_package}"
        if self.download_url:
            return f"download the AppImage from {self.download_url}"
        return f"install {self.name}"


# Candidate emulators per system, best first. `binaries` are checked on PATH;
# `flatpak` is checked against installed Flatpak applications.
#
# This table is the honest, verifiable part: a system listed here with no
# candidate installed is reported as missing, with instructions.
CANDIDATES: dict[str, list[dict]] = {
    # ── Sony ──────────────────────────────────────────────────────
    "ps1":        [{"name": "DuckStation", "binaries": ("duckstation-qt", "duckstation-nogui", "duckstation"), "flatpak": "org.duckstation.DuckStation", "arch": "duckstation"},
                   {"name": "Mednafen", "binaries": ("mednafen",), "flatpak": "", "arch": "mednafen"}],
    "ps2":        [{"name": "PCSX2", "binaries": ("pcsx2-qt", "pcsx2"), "flatpak": "net.pcsx2.PCSX2", "arch": "pcsx2"},
                   {"name": "Play!", "binaries": ("Play", "play"), "flatpak": "org.purei.Play", "arch": ""}],
    "ps3":        [{"name": "RPCS3", "binaries": ("rpcs3",), "flatpak": "net.rpcs3.RPCS3", "arch": "rpcs3"}],
    "ps4":        [{"name": "shadPS4", "binaries": ("shadps4", "shadPS4"), "flatpak": "net.shadps4.shadPS4", "arch": ""}],
    "psp":        [{"name": "PPSSPP", "binaries": ("PPSSPPQt", "PPSSPPSDL", "ppsspp"), "flatpak": "org.ppsspp.PPSSPP", "arch": "ppsspp"}],
    "psvita":     [{"name": "Vita3K", "binaries": ("Vita3K", "vita3k"), "flatpak": "org.vita3k.Vita3K", "arch": ""}],

    # ── Nintendo: home ────────────────────────────────────────────
    "nes":        [{"name": "Mesen", "binaries": ("Mesen", "mesen"), "flatpak": "", "arch": ""},
                   {"name": "FCEUX", "binaries": ("fceux",), "flatpak": "com.fceux.fceux", "arch": "fceux"}],
    "fds":        [{"name": "Mesen", "binaries": ("Mesen", "mesen"), "flatpak": "", "arch": ""},
                   {"name": "FCEUX", "binaries": ("fceux",), "flatpak": "com.fceux.fceux", "arch": "fceux"}],
    "snes":       [{"name": "Snes9x", "binaries": ("snes9x-gtk", "snes9x"), "flatpak": "com.snes9x.Snes9x", "arch": "snes9x-gtk"},
                   {"name": "bsnes", "binaries": ("bsnes",), "flatpak": "dev.bsnes.bsnes", "arch": "bsnes"}],
    "n64":        [{"name": "simple64", "binaries": ("simple64-gui",), "flatpak": "io.github.simple64.simple64", "arch": ""},
                   {"name": "Mupen64Plus", "binaries": ("mupen64plus",), "flatpak": "", "arch": "mupen64plus"}],
    "gc":         [{"name": "Dolphin", "binaries": ("dolphin-emu", "dolphin-emu-nogui"), "flatpak": "org.DolphinEmu.dolphin-emu", "arch": "dolphin-emu"}],
    "wii":        [{"name": "Dolphin", "binaries": ("dolphin-emu", "dolphin-emu-nogui"), "flatpak": "org.DolphinEmu.dolphin-emu", "arch": "dolphin-emu"}],
    "wiiu":       [{"name": "Cemu", "binaries": ("Cemu", "cemu"), "flatpak": "info.cemu.Cemu", "arch": ""}],
    "switch":     [{"name": "Ryujinx", "binaries": ("Ryujinx", "ryujinx"), "flatpak": "", "arch": ""}],

    # ── Nintendo: handheld ────────────────────────────────────────
    "gb":         [{"name": "mGBA", "binaries": ("mgba-qt", "mgba"), "flatpak": "io.mgba.mGBA", "arch": "mgba-qt"},
                   {"name": "SameBoy", "binaries": ("sameboy",), "flatpak": "", "arch": "sameboy"}],
    "gbc":        [{"name": "mGBA", "binaries": ("mgba-qt", "mgba"), "flatpak": "io.mgba.mGBA", "arch": "mgba-qt"},
                   {"name": "SameBoy", "binaries": ("sameboy",), "flatpak": "", "arch": "sameboy"}],
    "gba":        [{"name": "mGBA", "binaries": ("mgba-qt", "mgba"), "flatpak": "io.mgba.mGBA", "arch": "mgba-qt"}],
    "nds":        [{"name": "melonDS", "binaries": ("melonDS", "melonds"), "flatpak": "net.kuribo64.melonDS", "arch": "melonds"},
                   {"name": "DeSmuME", "binaries": ("desmume",), "flatpak": "org.desmume.DeSmuME", "arch": "desmume"}],
    "3ds":        [{"name": "Azahar", "binaries": ("azahar", "citra-qt", "citra"), "flatpak": "org.azahar_emu.Azahar", "arch": ""}],
    "virtualboy": [{"name": "Mednafen", "binaries": ("mednafen",), "flatpak": "", "arch": "mednafen"}],

    # ── Sega ──────────────────────────────────────────────────────
    "master_system": [{"name": "Kega Fusion", "binaries": ("Fusion", "fusion"), "flatpak": "", "arch": ""},
                      {"name": "Mednafen", "binaries": ("mednafen",), "flatpak": "", "arch": "mednafen"}],
    "gamegear":   [{"name": "Mednafen", "binaries": ("mednafen",), "flatpak": "", "arch": "mednafen"}],
    "megadrive":  [{"name": "BlastEm", "binaries": ("blastem",), "flatpak": "", "arch": "blastem"},
                   {"name": "Kega Fusion", "binaries": ("Fusion", "fusion"), "flatpak": "", "arch": ""}],
    "segacd":     [{"name": "Kega Fusion", "binaries": ("Fusion", "fusion"), "flatpak": "", "arch": ""}],
    "sega32x":    [{"name": "Kega Fusion", "binaries": ("Fusion", "fusion"), "flatpak": "", "arch": ""}],
    "saturn":     [{"name": "Yabause", "binaries": ("yabause", "kronos"), "flatpak": "", "arch": ""},
                   {"name": "Flycast", "binaries": ("flycast",), "flatpak": "org.flycast.Flycast", "arch": ""}],
    "dreamcast":  [{"name": "Flycast", "binaries": ("flycast",), "flatpak": "org.flycast.Flycast", "arch": ""},
                   {"name": "Redream", "binaries": ("redream",), "flatpak": "io.github.redream.Redream", "arch": ""}],

    # ── Microsoft ─────────────────────────────────────────────────
    "xbox":       [{"name": "xemu", "binaries": ("xemu",), "flatpak": "app.xemu.xemu", "arch": "xemu"}],
    # Xenia Edge is a Canary fork whose stated aim is Vulkan and Linux, where
    # upstream Xenia is a Windows project run under Wine. It is listed first
    # because on Linux it is the build that actually runs. Upstream ships it as
    # an AppImage only; the AUR package wraps that same AppImage and symlinks
    # /usr/bin/xenia_edge, so both forms are checked.
    "xbox360":    [{"name": "Xenia Edge", "binaries": ("xenia_edge", "xenia-edge"),
                    "appimage": ("xenia_edge*.AppImage", "xenia-edge*.AppImage"),
                    "flatpak": "", "arch": "", "aur": "xenia-edge-bin",
                    "url": "https://github.com/has207/xenia-edge/releases"},
                   {"name": "Xenia Canary", "binaries": ("xenia_canary", "xenia-canary"),
                    "appimage": ("xenia_canary*.AppImage", "xenia-canary*.AppImage"),
                    "flatpak": "", "arch": "", "aur": "xenia-canary-bin"},
                   {"name": "Xenia", "binaries": ("xenia",), "flatpak": "", "arch": "",
                    "aur": "xenia-git"}],

    # ── Other consoles ────────────────────────────────────────────
    "atari2600":  [{"name": "Stella", "binaries": ("stella",), "flatpak": "io.github.stella_emu.Stella", "arch": "stella"}],
    "atari7800":  [{"name": "Stella", "binaries": ("stella",), "flatpak": "io.github.stella_emu.Stella", "arch": "stella"}],
    "lynx":       [{"name": "Mednafen", "binaries": ("mednafen",), "flatpak": "", "arch": "mednafen"}],
    "jaguar":     [{"name": "BigPEmu", "binaries": ("bigpemu",), "flatpak": "", "arch": ""}],
    "pc_engine":  [{"name": "Mednafen", "binaries": ("mednafen",), "flatpak": "", "arch": "mednafen"}],
    "pc_engine_cd": [{"name": "Mednafen", "binaries": ("mednafen",), "flatpak": "", "arch": "mednafen"}],
    "neogeo":     [{"name": "MAME", "binaries": ("mame",), "flatpak": "org.mamedev.MAME", "arch": "mame"},
                   {"name": "FinalBurn Neo", "binaries": ("fbneo",), "flatpak": "", "arch": ""}],
    "ngp":        [{"name": "Mednafen", "binaries": ("mednafen",), "flatpak": "", "arch": "mednafen"}],
    "wonderswan": [{"name": "Mednafen", "binaries": ("mednafen",), "flatpak": "", "arch": "mednafen"}],
    "3do":        [{"name": "Phoenix", "binaries": ("phoenix",), "flatpak": "", "arch": ""}],

    # ── Arcade and computers ──────────────────────────────────────
    "arcade":     [{"name": "MAME", "binaries": ("mame",), "flatpak": "org.mamedev.MAME", "arch": "mame"},
                   {"name": "FinalBurn Neo", "binaries": ("fbneo",), "flatpak": "", "arch": ""}],
    "msx":        [{"name": "openMSX", "binaries": ("openmsx",), "flatpak": "org.openmsx.openMSX", "arch": "openmsx"},
                   {"name": "blueMSX", "binaries": ("bluemsx",), "flatpak": "", "arch": ""}],
    "c64":        [{"name": "VICE", "binaries": ("x64sc", "x64"), "flatpak": "net.sf.VICE", "arch": "vice"}],
    "amiga":      [{"name": "FS-UAE", "binaries": ("fs-uae-launcher", "fs-uae"), "flatpak": "net.fsuae.FSUAE", "arch": "fs-uae"}],
    "dos":        [{"name": "DOSBox Staging", "binaries": ("dosbox-staging", "dosbox"), "flatpak": "io.github.dosbox-staging", "arch": "dosbox-staging"},
                   {"name": "DOSBox-X", "binaries": ("dosbox-x",), "flatpak": "com.dosbox_x.DOSBox-X", "arch": "dosbox-x"}],
    "scummvm":    [{"name": "ScummVM", "binaries": ("scummvm",), "flatpak": "org.scummvm.ScummVM", "arch": "scummvm"}],
}

RETROARCH = {"binaries": ("retroarch",), "flatpak": "org.libretro.RetroArch", "arch": "retroarch"}

# `EmulatorDef.default_core` is documented as "RetroArch core OR standalone
# emulator id", and for the modern systems it holds the latter — there is no
# `xenia_libretro.so`, and RetroArch cannot run an Xbox 360 game by any means.
# Offering it anyway told the user a system was playable, counted it in "you
# can play N systems", and then failed at launch with no core to load.
NO_LIBRETRO_CORE = frozenset({
    "3ds", "ps3", "ps4", "psvita", "switch", "wiiu", "xbox", "xbox360",
})

# Where people actually keep AppImages. Searched top-level only: an AppImage is
# a single file the user dropped somewhere, and walking whole home directories
# to find one would cost far more than it is worth.
APPIMAGE_DIRS = (
    "~/Applications",
    "~/AppImages",
    "~/Apps",
    "~/.local/bin",
    "~/.local/share/applications",
    "~/Downloads",
    "~/Games",
    "/opt",
    "/usr/local/bin",
)

# AUR helpers, best first. Only used to phrase an install hint.
AUR_HELPERS = ("paru", "yay", "pikaur", "trizen")


@lru_cache(maxsize=512)
def _which(binary: str) -> Optional[str]:
    """`shutil.which`, remembered.

    Detection asks about the same binaries repeatedly — `summary()` alone walks
    every system, and each `shutil.which` stats every directory on PATH. The
    answer is already treated as fixed for the lifetime of a run (see
    `installed_flatpaks`), so it is cached the same way and cleared the same way.
    """
    return shutil.which(binary)


@lru_cache(maxsize=1)
def aur_helper() -> str:
    """The AUR helper on this machine, or 'yay' as the thing to suggest."""
    for helper in AUR_HELPERS:
        if shutil.which(helper):
            return helper
    return "yay"


@lru_cache(maxsize=1)
def installed_appimages() -> tuple[Path, ...]:
    """Executable *.AppImage files in the usual places.

    Cached alongside the Flatpak list, and cleared by `refresh()`, because this
    touches the filesystem and the answer does not change while GameLab runs.

    Newest first. People keep the AppImage they downloaded last month next to
    the one they downloaded yesterday — Xenia Edge builds are named after the
    commit they came from, so several accumulate — and directory order is
    arbitrary, which would otherwise mean picking an old build at random.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    for entry in APPIMAGE_DIRS:
        directory = Path(entry).expanduser()
        try:
            children = list(directory.iterdir())
        except OSError:
            # Missing, unreadable, or not a directory: all uninteresting.
            continue

        for child in children:
            if not child.name.lower().endswith(".appimage"):
                continue
            # An AppImage without the executable bit cannot be launched, and
            # reporting it as installed would fail at launch instead of here.
            if not os.access(child, os.X_OK):
                logger.debug("ignoring non-executable AppImage: %s", child)
                continue
            try:
                key = child.resolve()
            except OSError:
                key = child
            if key not in seen:
                seen.add(key)
                found.append(child)

    def newest_first(path: Path):
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return (-modified, path.name)

    return tuple(sorted(found, key=newest_first))


def _find_appimage(patterns) -> Optional[Path]:
    """The first AppImage on this machine matching any of `patterns`."""
    images = installed_appimages()
    for pattern in patterns:
        lowered = pattern.lower()
        for image in images:
            if fnmatch.fnmatch(image.name.lower(), lowered):
                return image
    return None


@lru_cache(maxsize=1)
def installed_flatpaks() -> frozenset[str]:
    """Application ids of installed Flatpaks. Empty if Flatpak is absent.

    Cached because this shells out, and the answer does not change while
    GameLab is running. Call `refresh()` after the user installs something.
    """
    if shutil.which("flatpak") is None:
        return frozenset()

    try:
        result = subprocess.run(
            ["flatpak", "list", "--app", "--columns=application"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not list flatpaks: %s", exc)
        return frozenset()

    if result.returncode != 0:
        return frozenset()

    return frozenset(
        line.strip() for line in result.stdout.splitlines() if line.strip()
    )


def refresh() -> None:
    """Forget cached detection, after the user installs an emulator."""
    installed_flatpaks.cache_clear()
    installed_appimages.cache_clear()
    aur_helper.cache_clear()
    _which.cache_clear()


def _resolve(candidate: dict, system_id: str, preference: int) -> EmulatorOption:
    """Turn one candidate into an option, resolved against this machine."""
    packaging = {
        "arch_package": candidate.get("arch", ""),
        "flatpak_id": candidate.get("flatpak", ""),
        "aur_package": candidate.get("aur", ""),
        "download_url": candidate.get("url", ""),
    }

    for binary in candidate.get("binaries", ()):
        path = _which(binary)
        if path:
            return EmulatorOption(
                system_id=system_id, name=candidate["name"], kind="native",
                command=(path,), preference=preference, **packaging,
            )

    flatpak_id = packaging["flatpak_id"]
    if flatpak_id and flatpak_id in installed_flatpaks():
        return EmulatorOption(
            system_id=system_id, name=f"{candidate['name']} (Flatpak)", kind="flatpak",
            command=("flatpak", "run", flatpak_id),
            preference=preference, **packaging,
        )

    # Some emulators ship as an AppImage and nothing else, so a loose file in
    # ~/Applications or ~/Downloads is a real installation, not a leftover.
    appimage = _find_appimage(candidate.get("appimage", ()))
    if appimage is not None:
        return EmulatorOption(
            system_id=system_id, name=f"{candidate['name']} (AppImage)", kind="appimage",
            command=(str(appimage),), preference=preference, **packaging,
        )

    # Not installed. Returned anyway, so the interface can say what to get.
    return EmulatorOption(
        system_id=system_id, name=candidate["name"], kind="native", command=(),
        preference=preference, **packaging,
    )


def retroarch_command() -> tuple[str, ...]:
    """How to run RetroArch here, or an empty tuple if it is not installed."""
    for binary in RETROARCH["binaries"]:
        path = _which(binary)
        if path:
            return (path,)
    if RETROARCH["flatpak"] in installed_flatpaks():
        return ("flatpak", "run", RETROARCH["flatpak"])
    return ()


def options_for(system_id: str) -> list[EmulatorOption]:
    """Every emulator that could run a system, installed ones first."""
    options = [
        _resolve(candidate, system_id, index)
        for index, candidate in enumerate(CANDIDATES.get(system_id, []))
    ]

    # RetroArch covers most retro systems through cores, so it is a genuine
    # fallback rather than a suggestion.
    system = SYSTEMS.get(system_id)
    if (
        system
        and system.default_core
        and system_id != "pc"
        and system_id not in NO_LIBRETRO_CORE
    ):
        command = retroarch_command()
        options.append(EmulatorOption(
            system_id=system_id,
            name=f"RetroArch ({system.default_core})",
            kind="retroarch",
            command=command,
            arch_package=RETROARCH["arch"],
            flatpak_id=RETROARCH["flatpak"],
            preference=99,
        ))

    return sorted(options, key=lambda o: (not o.installed, o.preference))


def best_for(system_id: str) -> Optional[EmulatorOption]:
    """The emulator GameLab would use for a system, or None if none is installed."""
    for option in options_for(system_id):
        if option.installed:
            return option
    return None


def missing_for(system_ids) -> dict[str, list[EmulatorOption]]:
    """Systems with no emulator installed, mapped to what would fix that."""
    missing: dict[str, list[EmulatorOption]] = {}
    for system_id in system_ids:
        if best_for(system_id) is None:
            options = [o for o in options_for(system_id) if not o.installed]
            if options:
                missing[system_id] = options
    return missing


def summary() -> list[tuple[str, str, Optional[EmulatorOption]]]:
    """(system id, system name, emulator or None) for every emulated system."""
    rows = []
    for system_id, system in SYSTEMS.items():
        if system_id == "pc":
            continue
        rows.append((system_id, system.name, best_for(system_id)))
    return sorted(rows, key=lambda r: (r[2] is None, r[1]))
