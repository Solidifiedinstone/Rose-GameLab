"""Exporting the library back into Steam as non-Steam shortcuts.

This is the Steam ROM Manager side of things: take games GameLab knows about —
emulated ROMs, GOG installs, anything — and add them to Steam so they appear in
the Steam library, work with Big Picture and Steam Input, and can be streamed
or played on a Deck.

Two file formats are involved, both undocumented by Valve:

- `shortcuts.vdf`, a BINARY VDF file listing non-Steam games. Nothing like the
  text VDF used by appmanifest files.
- artwork dropped into `grid/` under filenames derived from the shortcut's app
  id, which is itself a checksum of the target path and name.

Because this WRITES into Steam's own configuration, every operation backs up
the existing file first and Steam must not be running: Steam rewrites
shortcuts.vdf on exit and would silently discard anything added while it ran.
"""

from __future__ import annotations

import binascii
import logging
import shutil
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Optional

logger = logging.getLogger(__name__)

# Binary VDF type markers.
_TYPE_MAP = b"\x00"
_TYPE_STRING = b"\x01"
_TYPE_INT32 = b"\x02"
_END = b"\x08"


@dataclass
class Shortcut:
    """One non-Steam game entry."""

    app_name: str
    exe: str                      # quoted path to the launcher/emulator
    start_dir: str
    icon: str = ""
    launch_options: str = ""
    tags: list[str] = field(default_factory=list)
    is_hidden: bool = False
    allow_overlay: bool = True

    @property
    def app_id(self) -> int:
        """Steam's 32-bit id for this shortcut.

        Derived from exe + app name via CRC32 with the high bit set. Steam
        computes it the same way, so artwork filenames must use exactly this
        value or Steam will not find them.
        """
        key = (self.exe + self.app_name).encode("utf-8")
        return binascii.crc32(key) | 0x80000000

    @property
    def grid_id(self) -> int:
        """The id used for grid artwork filenames (the signed 32-bit form)."""
        return self.app_id - 0x100000000 if self.app_id > 0x7FFFFFFF else self.app_id

    @property
    def shortcut_id(self) -> int:
        """The 64-bit id Steam uses internally for shortcuts."""
        return (self.app_id << 32) | 0x02000000


# ── Binary VDF ────────────────────────────────────────────────────

def _write_string(stream: BinaryIO, key: str, value: str) -> None:
    stream.write(_TYPE_STRING)
    stream.write(key.encode("utf-8") + b"\x00")
    stream.write(value.encode("utf-8") + b"\x00")


def _write_int(stream: BinaryIO, key: str, value: int) -> None:
    stream.write(_TYPE_INT32)
    stream.write(key.encode("utf-8") + b"\x00")
    stream.write(struct.pack("<I", value & 0xFFFFFFFF))


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\x00", offset)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def parse_shortcuts(data: bytes) -> list[dict[str, Any]]:
    """Parse a binary shortcuts.vdf into a list of entry dicts.

    Deliberately tolerant: a shortcuts file written by a different tool may
    contain keys we do not know, and those must survive a read/write round trip
    rather than being dropped.
    """
    entries: list[dict[str, Any]] = []

    if not data:
        return entries

    offset = 0
    # Outer map: "shortcuts" -> { "0" -> {...}, "1" -> {...} }
    if data[offset:offset + 1] == _TYPE_MAP:
        offset += 1
        _root, offset = _read_cstring(data, offset)

    while offset < len(data):
        marker = data[offset:offset + 1]
        offset += 1

        if marker == _END or marker == b"":
            break

        if marker != _TYPE_MAP:
            # Unexpected structure; stop rather than emit nonsense.
            logger.warning("unexpected marker %r in shortcuts.vdf at %d", marker, offset)
            break

        _index, offset = _read_cstring(data, offset)
        entry: dict[str, Any] = {}

        while offset < len(data):
            field_marker = data[offset:offset + 1]
            offset += 1

            if field_marker == _END:
                break

            key, offset = _read_cstring(data, offset)

            if field_marker == _TYPE_STRING:
                value, offset = _read_cstring(data, offset)
                entry[key] = value
            elif field_marker == _TYPE_INT32:
                entry[key] = struct.unpack("<I", data[offset:offset + 4])[0]
                offset += 4
            elif field_marker == _TYPE_MAP:
                # Nested map, e.g. "tags". Read its indexed string values.
                values = []
                while offset < len(data) and data[offset:offset + 1] != _END:
                    sub_marker = data[offset:offset + 1]
                    offset += 1
                    _sub_key, offset = _read_cstring(data, offset)
                    if sub_marker == _TYPE_STRING:
                        value, offset = _read_cstring(data, offset)
                        values.append(value)
                offset += 1  # consume the closing marker
                entry[key] = values
            else:
                logger.warning("unknown field type %r for key %r", field_marker, key)
                break

        entries.append(entry)

    return entries


def serialise_shortcuts(entries: Iterable[dict[str, Any]]) -> bytes:
    """Write entries back to binary VDF."""
    import io

    stream = io.BytesIO()
    stream.write(_TYPE_MAP)
    stream.write(b"shortcuts\x00")

    for index, entry in enumerate(entries):
        stream.write(_TYPE_MAP)
        stream.write(str(index).encode("utf-8") + b"\x00")

        for key, value in entry.items():
            if isinstance(value, bool):
                _write_int(stream, key, int(value))
            elif isinstance(value, int):
                _write_int(stream, key, value)
            elif isinstance(value, list):
                stream.write(_TYPE_MAP)
                stream.write(key.encode("utf-8") + b"\x00")
                for tag_index, tag in enumerate(value):
                    _write_string(stream, str(tag_index), str(tag))
                stream.write(_END)
            else:
                _write_string(stream, key, str(value))

        stream.write(_END)

    stream.write(_END)
    stream.write(_END)
    return stream.getvalue()


def shortcut_to_entry(shortcut: Shortcut) -> dict[str, Any]:
    """Convert a Shortcut into the dict shape shortcuts.vdf expects."""
    return {
        # Stored and compared as unsigned 32-bit, matching how the binary VDF
        # is read back. Mixing the signed and unsigned forms means the
        # duplicate check never matches and every export appends again.
        "appid": shortcut.app_id & 0xFFFFFFFF,
        "AppName": shortcut.app_name,
        "Exe": shortcut.exe,
        "StartDir": shortcut.start_dir,
        "icon": shortcut.icon,
        "ShortcutPath": "",
        "LaunchOptions": shortcut.launch_options,
        "IsHidden": int(shortcut.is_hidden),
        "AllowDesktopConfig": 1,
        "AllowOverlay": int(shortcut.allow_overlay),
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "tags": list(shortcut.tags),
    }


# ── The exporter ──────────────────────────────────────────────────

@dataclass
class ExportResult:
    """What an export actually did."""

    added: int = 0
    updated: int = 0
    artwork_copied: int = 0
    backup: Optional[Path] = None
    errors: list[str] = field(default_factory=list)


class SteamExporter:
    """Adds GameLab games to Steam as non-Steam shortcuts."""

    #: Tag applied to everything we add, so our entries can be found and
    #: removed later without touching shortcuts the user made themselves.
    TAG = "Rose GameLab"

    def __init__(self, steam_root: Optional[Path] = None) -> None:
        from rose_gamelab.sources.steam import SteamProvider

        self.steam_root = steam_root or SteamProvider.find_steam_root()

    # ── Locating Steam's user data ────────────────────────────────

    def user_directories(self) -> list[Path]:
        """Every Steam user profile directory on this machine.

        `userdata/<id>/config/` is where shortcuts and grid art live. There can
        be several profiles; we export to all of them rather than guessing.
        """
        if not self.steam_root:
            return []

        userdata = self.steam_root / "userdata"
        if not userdata.is_dir():
            return []

        return [
            child / "config"
            for child in userdata.iterdir()
            if child.is_dir() and child.name.isdigit() and child.name != "0"
        ]

    def steam_is_running(self) -> bool:
        """Whether Steam is running.

        Writing shortcuts while Steam runs is pointless: it holds the file in
        memory and rewrites it on exit, discarding anything added meanwhile.
        """
        try:
            import psutil
        except ImportError:
            return False

        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").lower()
            if name in ("steam", "steamwebhelper"):
                return True
        return False

    # ── Export ────────────────────────────────────────────────────

    def export(
        self,
        games: Iterable,
        library,
        *,
        collection_name: Optional[str] = None,
        copy_artwork: bool = True,
        force: bool = False,
    ) -> ExportResult:
        """Add games to Steam as shortcuts.

        Raises RuntimeError if Steam is running, unless `force` is set — the
        write would otherwise appear to succeed and then silently vanish.
        """
        result = ExportResult()

        if not self.steam_root:
            result.errors.append("No Steam installation found.")
            return result

        if self.steam_is_running() and not force:
            raise RuntimeError(
                "Steam is running. Close Steam completely before exporting, or "
                "it will discard the new shortcuts when it exits."
            )

        directories = self.user_directories()
        if not directories:
            result.errors.append("No Steam user profile found.")
            return result

        games = list(games)

        for config_dir in directories:
            try:
                self._export_to_profile(
                    games, library, config_dir, result,
                    collection_name=collection_name,
                    copy_artwork=copy_artwork,
                )
            except OSError as exc:
                result.errors.append(f"{config_dir}: {exc}")

        return result

    def _export_to_profile(
        self,
        games: list,
        library,
        config_dir: Path,
        result: ExportResult,
        *,
        collection_name: Optional[str],
        copy_artwork: bool,
    ) -> None:
        shortcuts_path = config_dir / "shortcuts.vdf"

        existing: list[dict[str, Any]] = []
        if shortcuts_path.is_file():
            result.backup = self._backup(shortcuts_path)
            existing = parse_shortcuts(shortcuts_path.read_bytes())

        # Index what is already there so re-exporting updates rather than
        # duplicating. Steam shows duplicates as two identical library entries.
        by_appid = {entry.get("appid"): index for index, entry in enumerate(existing)}

        tags = [self.TAG]
        if collection_name:
            # Steam turns a shortcut's tags into library categories, which is
            # how the "creates folders in your library" behaviour works.
            tags.append(collection_name)

        for game in games:
            options = library.launch_options_for(game.id)
            if not options:
                result.errors.append(f"{game.title}: nothing to launch")
                continue

            shortcut = self._build_shortcut(game, options[0], tags)
            entry = shortcut_to_entry(shortcut)

            position = by_appid.get(entry["appid"])
            if position is not None:
                existing[position] = entry
                result.updated += 1
            else:
                existing.append(entry)
                by_appid[entry["appid"]] = len(existing) - 1
                result.added += 1

            if (
                copy_artwork
                and game.cover_path
                and self._copy_artwork(shortcut, Path(game.cover_path), config_dir)
            ):
                result.artwork_copied += 1

        shortcuts_path.parent.mkdir(parents=True, exist_ok=True)
        shortcuts_path.write_bytes(serialise_shortcuts(existing))

    def _build_shortcut(self, game, option, tags: list[str]) -> Shortcut:
        """Build a shortcut that runs the game the way GameLab would.

        Rather than pointing Steam at an emulator with a pile of arguments,
        the shortcut invokes GameLab's own CLI. That keeps launch profiles,
        playtime tracking and emulator resolution working identically whether
        the game is started from GameLab or from Steam.
        """
        import sys

        launcher = shutil.which("rose-gamelab") or sys.executable
        if launcher == sys.executable:
            options = f'-m rose_gamelab.main play {game.id}'
        else:
            options = f"play {game.id}"

        return Shortcut(
            app_name=game.title,
            exe=f'"{launcher}"',
            start_dir=f'"{Path(launcher).parent}"',
            launch_options=options,
            tags=tags,
        )

    def _copy_artwork(self, shortcut: Shortcut, cover: Path, config_dir: Path) -> bool:
        """Place cover art where Steam looks for it.

        Steam expects specific filenames under `grid/`: `<appid>p.<ext>` for the
        portrait library capsule, which is the one that matters for the grid
        view.
        """
        if not cover.is_file():
            return False

        grid = config_dir / "grid"
        try:
            grid.mkdir(parents=True, exist_ok=True)
            target = grid / f"{shortcut.grid_id}p{cover.suffix}"
            shutil.copy2(cover, target)
            return True
        except OSError as exc:
            logger.warning("could not copy artwork for %s: %s", shortcut.app_name, exc)
            return False

    @staticmethod
    def _backup(path: Path) -> Path:
        """Copy a Steam config file aside before modifying it."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = path.with_suffix(f"{path.suffix}.rosebak-{stamp}")
        shutil.copy2(path, backup)
        return backup

    # ── Removal ───────────────────────────────────────────────────

    def remove_exported(self) -> int:
        """Remove every shortcut GameLab added. Returns how many went.

        Identified by our tag, so shortcuts the user created by hand are left
        alone.
        """
        removed = 0

        for config_dir in self.user_directories():
            path = config_dir / "shortcuts.vdf"
            if not path.is_file():
                continue

            self._backup(path)
            entries = parse_shortcuts(path.read_bytes())
            kept = [e for e in entries if self.TAG not in (e.get("tags") or [])]

            removed += len(entries) - len(kept)
            path.write_bytes(serialise_shortcuts(kept))

        return removed
