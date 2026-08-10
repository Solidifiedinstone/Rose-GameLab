"""Walks directories and builds a structured ROM library.

Each source (folder + extensions) gets scanned into GameEntry objects.
Scans are incremental — cached metadata means re-scans are fast.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import GameEntry, SYSTEMS


class ROMScanner:
    """Scans configured source folders and builds GameEntry objects."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def scan_all(self, sources: Optional[list[dict]] = None) -> list[GameEntry]:
        """Scan all configured sources and return a flat list of games."""
        entries: list[GameEntry] = []
        source_list = sources or self.config.sources

        for source in source_list:
            entries.extend(self.scan_source(source))

        # Apply game cache metadata (last_played, play_count, custom art)
        entries = self._apply_cache(entries)

        return entries

    def scan_source(self, source: dict) -> list[GameEntry]:
        """Scan a single source entry and return its games."""
        path_str = source.get("path") or source.get("roms_path", "")
        path = Path(path_str)
        if not path.exists() or not path.is_dir():
            return []

        system_id = source.get("system") or self._detect_system(source)
        extensions = source.get("extensions")
        recursive = source.get("recursive", True)

        entries: list[GameEntry] = []

        file_patterns = extensions or self._get_extensions_for_system(system_id)

        for f in path.rglob("*") if recursive else path.iterdir():
            if f.is_file() and f.suffix.lower() in file_patterns:
                entry = self._make_entry(f, system_id, source, path)
                if entry:
                    entries.append(entry)

        return entries

    def _detect_system(self, source: dict) -> Optional[str]:
        """Try to detect the system from the source's known extensions."""
        extensions = source.get("extensions", [])
        # Match against known system extensions
        for system_id, ext_list in SYSTEMS.items():
            if ext_list.rom_extensions and any(e in ext_list.rom_extensions for e in extensions):
                return system_id
        return None

    def _get_extensions_for_system(self, system_id: str) -> list[str]:
        return SYSTEMS.get(system_id, None).rom_extensions or []

    def _make_entry(self, file: Path, system_id: str, source: dict, base_path: Path) -> Optional[GameEntry]:
        """Create a GameEntry from a ROM file."""
        try:
            rel = file.relative_to(base_path)
        except ValueError:
            rel = file.name

        # Generate stable ID
        id_parts = str(rel).encode()
        game_id = hashlib.sha256(id_parts).hexdigest()[:16]

        name = file.stem  # ROM filename without extension

        return GameEntry(
            id=game_id,
            name=name,
            system=system_id,
            path=str(file),
            source=source.get("id", ""),
        )

    def _apply_cache(self, entries: list[GameEntry]) -> list[GameEntry]:
        """Apply cached metadata from config to game entries."""
        cache = self.config.game_cache
        for entry in entries:
            cached = cache.get(entry.id, {})
            if cached.get("cover_art"):
                entry.cover_art = cached["cover_art"]
            if cached.get("last_played"):
                entry.metadata["last_played"] = cached["last_played"]
            if cached.get("play_count"):
                entry.metadata["play_count"] = cached["play_count"]
        return entries
