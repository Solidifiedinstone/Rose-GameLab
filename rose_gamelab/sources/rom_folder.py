"""ROM folder source provider.

Scans local directories for ROM files based on extensions and system type.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from rose_gamelab.core.emulator import GameEntry
from rose_gamelab.sources.base import SourceDef, SourceProvider


@dataclass
class ROMSourceProvider(SourceProvider):
    """Scans a local folder for ROM files."""

    source_def: SourceDef

    def get_def(self) -> SourceDef:
        return self.source_def

    def validate(self) -> bool:
        path = Path(self.source_def.path) if self.source_def.path else Path("")
        return path.exists() and path.is_dir()

    def discover(self) -> list[GameEntry]:
        path = Path(self.source_def.path) if self.source_def.path else Path("")
        if not path.exists() or not path.is_dir():
            return []

        entries: list[GameEntry] = []
        extensions = self.source_def.extensions or []
        recursive = self.source_def.recursive
        system_id = self.source_def.system

        for f in path.rglob("*") if recursive else path.iterdir():
            if f.is_file() and (not extensions or f.suffix.lower() in extensions):
                game_id = hashlib.sha256(f"{self.source_def.id}:{f!s}".encode()).hexdigest()[:16]
                entries.append(GameEntry(
                    id=game_id,
                    name=f.stem,
                    system=system_id or "unknown",
                    path=str(f),
                    source=self.source_def.id,
                ))

        return entries
