"""Save files and save states: finding them, organising them, backing them up.

Emulator saves are scattered across a dozen incompatible conventions. RetroArch
keeps them in its own directory tree, PCSX2 uses memory-card images, Dolphin
splits GameCube and Wii, and several emulators drop saves next to the ROM. The
result is that the thing players care most about losing is the thing hardest
for them to find.

This module gives every save one place to be seen from. It does NOT move the
originals: emulators expect their own paths, and relocating saves behind their
back is how people lose progress. Instead, saves are indexed where they live
and backed up by copying.

Backups are plain timestamped directories of ordinary files. No archive format,
no database, nothing that needs GameLab to read it back. If this project
disappears tomorrow the user still has their saves in a folder.

Nothing here touches the network.
"""

from __future__ import annotations

import logging
import os
import shutil

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from rose_gamelab.core.library import Library
from rose_gamelab.db.database import utc_now

logger = logging.getLogger(__name__)


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


@dataclass(frozen=True)
class SaveLocation:
    """Where one emulator keeps one category of save data."""

    emulator: str
    kind: str                       # 'save' (battery/memory card) or 'state'
    directory: Path
    patterns: tuple[str, ...]       # glob patterns for the files themselves
    #: True when files live beside the ROM rather than in a central directory.
    beside_rom: bool = False

    def exists(self) -> bool:
        return self.directory.is_dir()


def known_locations() -> list[SaveLocation]:
    """Every save location GameLab knows how to look in.

    Paths follow each emulator's Linux defaults. Flatpak variants are included
    because a large share of Linux emulator installs are Flatpaks and their
    saves live somewhere completely different.
    """
    config = _config_home()
    data = _data_home()
    flatpak = Path.home() / ".var" / "app"

    locations: list[SaveLocation] = []

    def add(emulator: str, kind: str, directory: Path, *patterns: str, beside_rom: bool = False) -> None:
        locations.append(
            SaveLocation(emulator, kind, directory, tuple(patterns), beside_rom)
        )

    # ── RetroArch (and every libretro core) ───────────────────────
    for root in (
        config / "retroarch",
        flatpak / "org.libretro.RetroArch" / "config" / "retroarch",
    ):
        add("retroarch", "save", root / "saves", "*.srm", "*.sav", "*.rtc")
        add("retroarch", "state", root / "states", "*.state", "*.state[0-9]", "*.state[0-9][0-9]")

    # ── PlayStation 2 ─────────────────────────────────────────────
    for root in (
        config / "PCSX2",
        flatpak / "net.pcsx2.PCSX2" / "config" / "PCSX2",
    ):
        add("pcsx2", "save", root / "memcards", "*.ps2", "*.mcd", "*.mcr")
        add("pcsx2", "state", root / "sstates", "*.p2s")

    # ── PlayStation 1 ─────────────────────────────────────────────
    for root in (
        data / "duckstation",
        config / "duckstation",
        flatpak / "org.duckstation.DuckStation" / "data" / "duckstation",
    ):
        add("duckstation", "save", root / "memcards", "*.mcd", "*.mcr", "*.srm")
        add("duckstation", "state", root / "savestates", "*.sav")

    # ── GameCube / Wii ────────────────────────────────────────────
    for root in (
        data / "dolphin-emu",
        flatpak / "org.DolphinEmu.dolphin-emu" / "data" / "dolphin-emu",
    ):
        add("dolphin", "save", root / "GC", "*.raw", "*.gci")
        add("dolphin", "save", root / "Wii" / "title", "*")
        add("dolphin", "state", root / "StateSaves", "*.sav", "*.s??")

    # ── PSP ───────────────────────────────────────────────────────
    for root in (
        config / "ppsspp" / "PSP",
        flatpak / "org.ppsspp.PPSSPP" / "config" / "ppsspp" / "PSP",
    ):
        add("ppsspp", "save", root / "SAVEDATA", "*")
        add("ppsspp", "state", root / "PPSSPP_STATE", "*.ppst")

    # ── Nintendo DS / 3DS ─────────────────────────────────────────
    add("melonds", "save", config / "melonDS", "*.sav")
    add("melonds", "state", config / "melonDS", "*.ml?")
    for root in (
        data / "azahar-emu",
        data / "citra-emu",
    ):
        add("azahar", "save", root / "sdmc", "*")
        add("azahar", "state", root / "states", "*")

    # ── Switch ────────────────────────────────────────────────────
    add("ryujinx", "save", config / "Ryujinx" / "bis" / "user" / "save", "*")

    # ── Dreamcast / Saturn ────────────────────────────────────────
    for root in (
        config / "flycast",
        flatpak / "org.flycast.Flycast" / "config" / "flycast",
    ):
        add("flycast", "save", root, "vmu_save_*.bin")
        add("flycast", "state", root, "*.state")

    # ── Emulators that save beside the ROM ────────────────────────
    # mGBA and several standalone emulators default to writing the save next
    # to the ROM file. Those are found relative to each game, not centrally.
    add("mgba", "save", Path(), "*.sav", "*.srm", beside_rom=True)
    add("mgba", "state", Path(), "*.ss[0-9]", beside_rom=True)
    add("snes9x", "save", Path(), "*.srm", beside_rom=True)
    add("mesen", "save", Path(), "*.sav", beside_rom=True)

    return locations


@dataclass
class SaveFile:
    """One save or state file found on disk."""

    path: Path
    kind: str
    emulator: str
    size_bytes: int
    modified_at: datetime
    slot: Optional[int] = None
    game_id: Optional[int] = None

    @property
    def label(self) -> str:
        if self.slot is not None:
            return f"Slot {self.slot}"
        return self.path.stem


@dataclass
class BackupResult:
    """What a backup run actually copied. Reported verbatim."""

    destination: Optional[Path] = None
    files_copied: int = 0
    bytes_copied: int = 0
    errors: list[str] = field(default_factory=list)


# ── Discovery ─────────────────────────────────────────────────────

def _slot_from_name(name: str) -> Optional[int]:
    """Extract a save-state slot number from a filename, if it has one.

    RetroArch uses `game.state`, `game.state1`, `game.state2`; mGBA uses
    `game.ss0`. The unnumbered form is treated as slot 0.
    """
    import re

    match = re.search(r"\.(?:state|ss)(\d+)$", name, re.I)
    if match:
        return int(match.group(1))

    if re.search(r"\.(?:state|ss)$", name, re.I):
        return 0

    return None


def scan_location(location: SaveLocation) -> Iterator[SaveFile]:
    """Yield every save file in one central location."""
    if location.beside_rom or not location.directory.is_dir():
        return

    for pattern in location.patterns:
        for path in location.directory.glob(pattern):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue

            yield SaveFile(
                path=path,
                kind=location.kind,
                emulator=location.emulator,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                slot=_slot_from_name(path.name) if location.kind == "state" else None,
            )


def find_saves_beside_rom(rom: Path) -> Iterator[SaveFile]:
    """Find saves that an emulator wrote next to a ROM file.

    Matched by stem rather than by scanning the whole directory, so a folder of
    two hundred ROMs does not attribute every save to every game.
    """
    directory = rom.parent
    stem = rom.stem

    if not directory.is_dir():
        return

    # Several emulators declare the same extension (mGBA and snes9x both claim
    # .srm), so the same file is reachable through more than one location.
    # Yield each path once, attributed to the first emulator that claims it.
    seen: set[Path] = set()

    for location in known_locations():
        if not location.beside_rom:
            continue

        for pattern in location.patterns:
            # Anchor the glob to this ROM's stem.
            for path in directory.glob(f"{glob_escape(stem)}{pattern.lstrip('*')}"):
                if not path.is_file() or path in seen:
                    continue
                seen.add(path)

                try:
                    stat = path.stat()
                except OSError:
                    continue

                yield SaveFile(
                    path=path,
                    kind=location.kind,
                    emulator=location.emulator,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    slot=_slot_from_name(path.name) if location.kind == "state" else None,
                )


def glob_escape(text: str) -> str:
    """Escape glob metacharacters in a literal filename stem.

    ROM filenames routinely contain `[!]`, `(U)` and similar dump tags, which
    are glob syntax and would otherwise match the wrong files or nothing.
    """
    return text.translate({ord(c): f"[{c}]" for c in "*?[]"})


def match_save_to_game(save: SaveFile, titles: dict[str, int]) -> Optional[int]:
    """Match a save file to a library game id by name.

    `titles` maps a normalised title to a game id. Emulators name saves after
    the ROM, so the stem usually matches the ROM filename closely. Returns None
    when there is no confident match — an unattached save is shown under
    "unmatched" rather than being guessed onto the wrong game, because
    restoring a save onto the wrong game destroys real progress.
    """
    from rose_gamelab.core.discs import normalise_title, sort_title

    stem = save.path.stem
    # Strip a trailing state-slot suffix so 'Game.state2' matches 'Game'.
    for suffix in (".state", ".ss", ".sav", ".srm"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]

    for candidate in (sort_title(stem), sort_title(normalise_title(stem))):
        if candidate in titles:
            return titles[candidate]

    return None


# ── The manager ───────────────────────────────────────────────────

class SaveManager:
    """Indexes saves, and backs them up on request."""

    def __init__(self, library: Library, *, backup_root: Optional[Path] = None) -> None:
        self.library = library
        self.backup_root = backup_root or (_data_home() / "rose-gamelab" / "save-backups")

    # ── Indexing ──────────────────────────────────────────────────

    def discover(self) -> list[SaveFile]:
        """Find every save GameLab can see, from all known locations.

        Central emulator directories are scanned first, then saves sitting
        beside each ROM in the library.
        """
        found: list[SaveFile] = []

        for location in known_locations():
            found.extend(scan_location(location))

        for row in self.library.db.query(
            "SELECT game_id, path FROM game_files WHERE missing = 0"
        ):
            rom = Path(row["path"])
            for save in find_saves_beside_rom(rom):
                save.game_id = row["game_id"]
                found.append(save)

        # The same file can be reached through two configured locations
        # (a native install and a Flatpak sharing a directory, for instance).
        unique: dict[Path, SaveFile] = {}
        for save in found:
            unique.setdefault(save.path.resolve(), save)

        return list(unique.values())

    def index(self) -> int:
        """Discover saves and record them in the database. Returns the count.

        Existing rows are refreshed rather than duplicated, so re-indexing
        after playing updates sizes and timestamps in place.
        """
        titles = {
            game.sort_title: game.id
            for game in self.library.list_games(include_hidden=True)
        }

        saves = self.discover()
        recorded = 0

        for save in saves:
            game_id = save.game_id or match_save_to_game(save, titles)
            if game_id is None:
                # Unmatched saves are deliberately not stored against a game.
                # Attaching them to a guess risks restoring onto the wrong game.
                continue

            existing = self.library.db.query_one(
                "SELECT id FROM saves WHERE path = ?", (str(save.path),)
            )

            if existing:
                self.library.db.execute(
                    "UPDATE saves SET size_bytes = ?, modified_at = ?, slot = ?"
                    " WHERE id = ?",
                    (
                        save.size_bytes,
                        save.modified_at.isoformat(timespec="seconds"),
                        save.slot,
                        existing["id"],
                    ),
                )
            else:
                self.library.db.execute(
                    "INSERT INTO saves"
                    " (game_id, kind, slot, path, size_bytes, modified_at, label)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        game_id, save.kind, save.slot, str(save.path),
                        save.size_bytes,
                        save.modified_at.isoformat(timespec="seconds"),
                        save.label,
                    ),
                )
            recorded += 1

        return recorded

    def saves_for(self, game_id: int, *, kind: Optional[str] = None) -> list:
        sql = "SELECT * FROM saves WHERE game_id = ?"
        params: list = [game_id]

        if kind:
            sql += " AND kind = ?"
            params.append(kind)

        sql += " ORDER BY kind, slot IS NULL, slot, modified_at DESC"
        return self.library.db.query(sql, tuple(params))

    def unmatched_saves(self) -> list[SaveFile]:
        """Saves that could not be attributed to a library game.

        Surfaced rather than hidden, so the user can attach them by hand
        instead of wondering where their progress went.
        """
        titles = {
            game.sort_title: game.id
            for game in self.library.list_games(include_hidden=True)
        }
        return [
            save for save in self.discover()
            if save.game_id is None and match_save_to_game(save, titles) is None
        ]

    def attach(self, save_path: Path, game_id: int) -> None:
        """Attach an unmatched save to a game by hand."""
        try:
            stat = Path(save_path).stat()
        except OSError as exc:
            raise ValueError(f"cannot read {save_path}: {exc}") from exc

        self.library.db.execute(
            "INSERT OR REPLACE INTO saves"
            " (game_id, kind, path, size_bytes, modified_at, label)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                game_id,
                "state" if _slot_from_name(Path(save_path).name) is not None else "save",
                str(save_path),
                stat.st_size,
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                Path(save_path).stem,
            ),
        )

    # ── Backup ────────────────────────────────────────────────────

    def backup(
        self,
        *,
        game_id: Optional[int] = None,
        label: Optional[str] = None,
    ) -> BackupResult:
        """Copy saves into a timestamped backup directory.

        Backups are plain files in plain folders — no archive, no index, no
        dependency on GameLab to read them back. Originals are never moved.
        """
        result = BackupResult()

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        name = f"{stamp}_{label}" if label else stamp
        destination = self.backup_root / name

        if game_id is not None:
            rows = self.saves_for(game_id)
        else:
            rows = self.library.db.query("SELECT * FROM saves ORDER BY game_id")

        if not rows:
            return result

        for row in rows:
            source = Path(row["path"])
            if not source.is_file():
                result.errors.append(f"missing: {source}")
                continue

            game = self.library.get(row["game_id"])
            # One folder per game, named after the game, so the backup is
            # navigable by a human without any tooling.
            folder = destination / _safe_name(game.title if game else "unknown") / row["kind"]

            try:
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / source.name
                shutil.copy2(source, target)
                result.files_copied += 1
                result.bytes_copied += source.stat().st_size
            except OSError as exc:
                result.errors.append(f"{source}: {exc}")

        if result.files_copied:
            result.destination = destination
            self.library.db.execute(
                "UPDATE saves SET backed_up_at = ? WHERE path IN"
                f" ({','.join('?' for _ in rows)})",
                (utc_now(), *[row["path"] for row in rows]),
            )

        return result

    def list_backups(self) -> list[tuple[Path, datetime, int]]:
        """Existing backups, newest first: (path, taken at, file count)."""
        if not self.backup_root.is_dir():
            return []

        backups = []
        for directory in self.backup_root.iterdir():
            if not directory.is_dir():
                continue
            try:
                taken = datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc)
                count = sum(1 for p in directory.rglob("*") if p.is_file())
            except OSError:
                continue
            backups.append((directory, taken, count))

        return sorted(backups, key=lambda entry: entry[1], reverse=True)

    def restore(self, backup_file: Path, target: Path, *, keep_existing: bool = True) -> Path:
        """Restore one backed-up save over its original location.

        The file currently in place is copied aside first unless explicitly
        told not to. Restoring is the single most destructive thing this
        module does, and an undo has to exist.
        """
        backup_file = Path(backup_file)
        target = Path(target)

        if not backup_file.is_file():
            raise ValueError(f"backup file does not exist: {backup_file}")

        if target.is_file() and keep_existing:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            aside = target.with_suffix(target.suffix + f".replaced-{stamp}")
            shutil.copy2(target, aside)
            logger.info("previous save kept at %s", aside)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_file, target)
        return target

    def prune_backups(self, keep: int = 10) -> int:
        """Delete all but the newest `keep` backups. Returns how many went."""
        backups = self.list_backups()
        removed = 0

        for directory, _taken, _count in backups[keep:]:
            try:
                shutil.rmtree(directory)
                removed += 1
            except OSError as exc:
                logger.warning("could not remove backup %s: %s", directory, exc)

        return removed


def _safe_name(text: str) -> str:
    """A directory name safe on every filesystem, still readable by a human."""
    cleaned = "".join("_" if c in '/\\:*?"<>|' else c for c in text).strip()
    return cleaned[:100] or "unknown"
