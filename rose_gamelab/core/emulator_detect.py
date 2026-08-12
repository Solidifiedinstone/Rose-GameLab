"""Finding the emulators actually installed on this machine.

Two things make this less obvious than "is it on PATH".

A large share of Linux emulators are installed as Flatpaks, which put nothing
on PATH at all — the command is `flatpak run net.pcsx2.PCSX2`. Checking only
PATH reports "PCSX2 is not installed" on a machine where PCSX2 is installed and
working, which is worse than useless because it sends the user off to install
something they already have.

And RetroArch covers most retro systems through libretro cores, so a system can
be perfectly playable with no dedicated emulator installed at all.

When nothing is found, the point is to say what to install and how, rather than
to report an empty result and leave the user guessing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from rose_gamelab.core.emulator import SYSTEMS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmulatorOption:
    """One emulator that can run a system, and how to invoke it."""

    system_id: str
    name: str
    #: 'native' | 'flatpak' | 'retroarch'
    kind: str
    command: tuple[str, ...] = ()
    #: Package names, for telling the user what to install.
    arch_package: str = ""
    flatpak_id: str = ""
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
        return f"install {self.name}"


# Candidate emulators per system, best first. `binaries` are checked on PATH;
# `flatpak` is checked against installed Flatpak applications.
#
# This table is the honest, verifiable part: a system listed here with no
# candidate installed is reported as missing, with instructions.
CANDIDATES: dict[str, list[dict]] = {
    "ps2":        [{"name": "PCSX2", "binaries": ("pcsx2-qt", "pcsx2"), "flatpak": "net.pcsx2.PCSX2", "arch": "pcsx2"}],
    "ps1":        [{"name": "DuckStation", "binaries": ("duckstation-qt", "duckstation-nogui", "duckstation"), "flatpak": "org.duckstation.DuckStation", "arch": "duckstation"}],
    "ps3":        [{"name": "RPCS3", "binaries": ("rpcs3",), "flatpak": "net.rpcs3.RPCS3", "arch": "rpcs3"}],
    "psp":        [{"name": "PPSSPP", "binaries": ("PPSSPPQt", "PPSSPPSDL", "ppsspp"), "flatpak": "org.ppsspp.PPSSPP", "arch": "ppsspp"}],
    "psvita":     [{"name": "Vita3K", "binaries": ("Vita3K", "vita3k"), "flatpak": "", "arch": ""}],
    "ps4":        [{"name": "shadPS4", "binaries": ("shadps4", "shadPS4"), "flatpak": "", "arch": ""}],
    "gc":         [{"name": "Dolphin", "binaries": ("dolphin-emu", "dolphin-emu-nogui"), "flatpak": "org.DolphinEmu.dolphin-emu", "arch": "dolphin-emu"}],
    "wii":        [{"name": "Dolphin", "binaries": ("dolphin-emu", "dolphin-emu-nogui"), "flatpak": "org.DolphinEmu.dolphin-emu", "arch": "dolphin-emu"}],
    "wiiu":       [{"name": "Cemu", "binaries": ("Cemu", "cemu"), "flatpak": "info.cemu.Cemu", "arch": ""}],
    "switch":     [{"name": "Ryujinx", "binaries": ("Ryujinx", "ryujinx"), "flatpak": "", "arch": ""}],
    "3ds":        [{"name": "Azahar", "binaries": ("azahar", "citra-qt", "citra"), "flatpak": "org.azahar_emu.Azahar", "arch": ""}],
    "nds":        [{"name": "melonDS", "binaries": ("melonDS", "melonds"), "flatpak": "net.kuribo64.melonDS", "arch": "melonds"}],
    "gba":        [{"name": "mGBA", "binaries": ("mgba-qt", "mgba"), "flatpak": "io.mgba.mGBA", "arch": "mgba-qt"}],
    "gbc":        [{"name": "mGBA", "binaries": ("mgba-qt", "mgba"), "flatpak": "io.mgba.mGBA", "arch": "mgba-qt"}],
    "gb":         [{"name": "mGBA", "binaries": ("mgba-qt", "mgba"), "flatpak": "io.mgba.mGBA", "arch": "mgba-qt"}],
    "snes":       [{"name": "Snes9x", "binaries": ("snes9x-gtk", "snes9x"), "flatpak": "com.snes9x.Snes9x", "arch": "snes9x-gtk"}],
    "nes":        [{"name": "Mesen", "binaries": ("Mesen", "mesen"), "flatpak": "", "arch": ""}],
    "n64":        [{"name": "simple64", "binaries": ("simple64-gui", "mupen64plus"), "flatpak": "io.github.simple64.simple64", "arch": "mupen64plus"}],
    "dreamcast":  [{"name": "Flycast", "binaries": ("flycast",), "flatpak": "org.flycast.Flycast", "arch": ""}],
    "saturn":     [{"name": "Flycast", "binaries": ("flycast",), "flatpak": "org.flycast.Flycast", "arch": ""}],
    "xbox":       [{"name": "xemu", "binaries": ("xemu",), "flatpak": "app.xemu.xemu", "arch": "xemu"}],
    "xbox360":    [{"name": "Xenia", "binaries": ("xenia",), "flatpak": "", "arch": ""}],
    "megadrive":  [{"name": "BlastEm", "binaries": ("blastem",), "flatpak": "", "arch": "blastem"}],
    "atari2600":  [{"name": "Stella", "binaries": ("stella",), "flatpak": "io.github.stella_emu.Stella", "arch": "stella"}],
    "arcade":     [{"name": "MAME", "binaries": ("mame",), "flatpak": "org.mamedev.MAME", "arch": "mame"}],
    "dos":        [{"name": "DOSBox Staging", "binaries": ("dosbox-staging", "dosbox"), "flatpak": "io.github.dosbox-staging", "arch": "dosbox-staging"}],
    "scummvm":    [{"name": "ScummVM", "binaries": ("scummvm",), "flatpak": "org.scummvm.ScummVM", "arch": "scummvm"}],
}

RETROARCH = {"binaries": ("retroarch",), "flatpak": "org.libretro.RetroArch", "arch": "retroarch"}


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


def _resolve(candidate: dict, system_id: str, preference: int) -> EmulatorOption:
    """Turn one candidate into an option, resolved against this machine."""
    for binary in candidate.get("binaries", ()):
        path = shutil.which(binary)
        if path:
            return EmulatorOption(
                system_id=system_id, name=candidate["name"], kind="native",
                command=(path,), arch_package=candidate.get("arch", ""),
                flatpak_id=candidate.get("flatpak", ""), preference=preference,
            )

    flatpak_id = candidate.get("flatpak", "")
    if flatpak_id and flatpak_id in installed_flatpaks():
        return EmulatorOption(
            system_id=system_id, name=f"{candidate['name']} (Flatpak)", kind="flatpak",
            command=("flatpak", "run", flatpak_id),
            arch_package=candidate.get("arch", ""), flatpak_id=flatpak_id,
            preference=preference,
        )

    # Not installed. Returned anyway, so the interface can say what to get.
    return EmulatorOption(
        system_id=system_id, name=candidate["name"], kind="native", command=(),
        arch_package=candidate.get("arch", ""), flatpak_id=flatpak_id,
        preference=preference,
    )


def retroarch_command() -> tuple[str, ...]:
    """How to run RetroArch here, or an empty tuple if it is not installed."""
    for binary in RETROARCH["binaries"]:
        path = shutil.which(binary)
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
    if system and system.default_core and system_id not in ("pc",):
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
