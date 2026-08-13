"""Backing up emulator configuration.

Saves are already looked after. Configuration is the other half of the work a
person puts into an emulated setup, and it is worse to lose: a save file is one
game's progress, while a PCSX2 configuration is every graphics tweak, every
per-game patch, every controller binding, arrived at over months of fiddling and
almost impossible to reconstruct from memory.

Nothing here is clever. It copies configuration directories into a timestamped
folder of plain files, the same way `core/saves.py` handles saves, so a backup
can be read, inspected and restored by hand without GameLab existing. Emulator
configuration formats are the emulators' business; this treats them as opaque.

Flatpak installations keep their configuration inside their own sandbox rather
than the ordinary place, so both are listed for every emulator that ships as
one. Only directories that exist are ever visited.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def _config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def _data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base) if base else Path.home() / ".local" / "share"


def _flatpak(app_id: str, *parts: str) -> Path:
    return Path.home() / ".var" / "app" / app_id / "config" / Path(*parts)


@dataclass(frozen=True)
class ConfigLocation:
    """Where one emulator keeps its configuration."""

    emulator: str
    directory: Path
    #: Skipped when copying. Three kinds of thing live under an emulator's
    #: configuration directory that are not configuration: caches and shader
    #: compilations (large and regenerable), firmware and installed games
    #: (enormous, and not ours to copy), and save data — which
    #: `core/saves.py` already backs up, so duplicating it here would waste
    #: the space and blur which feature owns it.
    skip: tuple[str, ...] = ()

    def exists(self) -> bool:
        return self.directory.is_dir()


def known_locations() -> list[ConfigLocation]:
    """Every configuration directory GameLab knows how to back up.

    Both the ordinary and the Flatpak path is listed for anything distributed
    as a Flatpak: which one exists is a property of the machine, and checking
    is cheaper than guessing.
    """
    config = _config_home()
    data = _data_home()

    locations = [
        # cores alone were 103 MB, and every one of them is a download away.
        ConfigLocation("RetroArch", config / "retroarch",
                       skip=("cores", "system", "downloads", "thumbnails",
                             "cache", "assets", "shaders", "saves", "states",
                             "screenshots")),
        ConfigLocation("RetroArch", _flatpak("org.libretro.RetroArch", "retroarch"),
                       skip=("cores", "system", "downloads", "thumbnails",
                             "cache", "assets", "shaders", "saves", "states",
                             "screenshots")),

        # memcards are saves and sstates are save states: both belong to the
        # save backup, not here. On a real machine memcards alone were 17 MB.
        ConfigLocation("PCSX2", config / "PCSX2",
                       skip=("cache", "logs", "covers", "memcards", "sstates",
                             "snaps", "bios")),
        ConfigLocation("PCSX2", _flatpak("net.pcsx2.PCSX2", "PCSX2"),
                       skip=("cache", "logs", "covers", "memcards", "sstates",
                             "snaps", "bios")),

        # dev_flash is the PS3 firmware — 189 MB on a real machine, and not
        # configuration. dev_hdd0 holds installed games and saves and reached
        # 15 GB on the same machine.
        ConfigLocation("RPCS3", config / "rpcs3",
                       skip=("cache", "savestates", "dev_hdd0", "dev_hdd1",
                             "dev_flash", "dev_flash2", "dev_flash3", "dev_bdvd")),
        ConfigLocation("RPCS3", _flatpak("net.rpcs3.RPCS3", "rpcs3"),
                       skip=("cache", "savestates", "dev_hdd0", "dev_hdd1",
                             "dev_flash", "dev_flash2", "dev_flash3", "dev_bdvd")),

        ConfigLocation("Dolphin", config / "dolphin-emu",
                       skip=("Cache", "Logs", "StateSaves", "GC", "Wii")),
        ConfigLocation("Dolphin", _flatpak("org.DolphinEmu.dolphin-emu", "dolphin-emu"),
                       skip=("Cache", "Logs", "StateSaves", "GC", "Wii")),

        ConfigLocation("DuckStation", config / "duckstation",
                       skip=("cache", "covers", "dump", "memcards", "savestates",
                             "screenshots", "bios")),
        ConfigLocation("DuckStation", _flatpak("org.duckstation.DuckStation", "duckstation"),
                       skip=("cache", "covers", "dump", "memcards", "savestates",
                             "screenshots", "bios")),

        ConfigLocation("PPSSPP", config / "ppsspp", skip=("PSP/GAME", "PSP/SAVEDATA")),
        ConfigLocation("PPSSPP", _flatpak("org.ppsspp.PPSSPP", "ppsspp"),
                       skip=("PSP/GAME", "PSP/SAVEDATA")),

        ConfigLocation("melonDS", config / "melonDS"),
        ConfigLocation("mGBA", config / "mgba"),
        ConfigLocation("Flycast", config / "flycast", skip=("cache",)),
        ConfigLocation("Flycast", _flatpak("org.flycast.Flycast", "flycast"), skip=("cache",)),
        ConfigLocation("Cemu", data / "Cemu", skip=("shaderCache", "cache")),
        ConfigLocation("Ryujinx", config / "Ryujinx", skip=("games", "bis", "sdcard")),
        ConfigLocation("Azahar", config / "azahar-emu", skip=("cache",)),
        ConfigLocation("xemu", config / "xemu"),
        ConfigLocation("Xenia", config / "xenia"),
        ConfigLocation("shadPS4", config / "shadps4", skip=("cache",)),
        ConfigLocation("Vita3K", config / "Vita3K", skip=("cache",)),
        ConfigLocation("Mesen", config / "Mesen2"),
        ConfigLocation("Snes9x", config / "snes9x"),
        ConfigLocation("MAME", config / "mame"),
        ConfigLocation("ScummVM", config / "scummvm"),
        ConfigLocation("DOSBox Staging", config / "dosbox"),
    ]

    return [location for location in locations if location.exists()]


@dataclass
class BackupResult:
    """What a configuration backup copied."""

    directory: Optional[Path] = None
    emulators: list[str] = field(default_factory=list)
    files: int = 0
    bytes_copied: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.emulators:
            return "No emulator configuration was found to back up."
        return (
            f"Backed up {len(self.emulators)} emulator(s), {self.files} file(s), "
            f"{self.bytes_copied // 1024} KB"
        )


def default_backup_root() -> Path:
    return _data_home() / "rose-gamelab" / "config-backups"


def _should_skip(relative: Path, skip: Iterable[str]) -> bool:
    text = relative.as_posix()
    return any(text == entry or text.startswith(f"{entry}/") for entry in skip)


def back_up(
    *,
    root: Optional[Path] = None,
    locations: Optional[list[ConfigLocation]] = None,
    label: Optional[str] = None,
) -> BackupResult:
    """Copy emulator configuration into a timestamped folder of plain files.

    Plain files on purpose: a backup nobody can read without the application
    that made it is a hostage, not a backup. Everything here can be restored
    with a file manager.
    """
    found = locations if locations is not None else known_locations()
    result = BackupResult()

    if not found:
        return result

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H-%M-%S")
    name = f"{stamp} {label}" if label else stamp
    destination = (root or default_backup_root()) / name

    for location in found:
        target = destination / location.emulator
        copied_any = False

        for source in location.directory.rglob("*"):
            if not source.is_file():
                continue

            try:
                relative = source.relative_to(location.directory)
            except ValueError:
                continue

            if _should_skip(relative, location.skip):
                continue

            written = target / relative
            try:
                written.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, written)
            except OSError as exc:
                result.errors.append(f"{location.emulator}/{relative}: {exc}")
                continue

            result.files += 1
            try:
                result.bytes_copied += written.stat().st_size
            except OSError:
                pass
            copied_any = True

        if copied_any and location.emulator not in result.emulators:
            result.emulators.append(location.emulator)

    if result.files:
        result.directory = destination
    return result


def list_backups(root: Optional[Path] = None) -> list[Path]:
    """Existing backups, newest first."""
    folder = root or default_backup_root()
    try:
        return sorted(
            (path for path in folder.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return []
