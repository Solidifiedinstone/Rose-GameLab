"""Base class for game source providers (ROM folders, Steam, Epic, GOG, etc.).

Subclasses implement discover() -> list[GameEntry] and import_games() -> list.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rose_gamelab.core.emulator import GameEntry


@dataclass
class SourceDef:
    """Definition of a game source."""
    id: str                               # unique source ID
    name: str                             # display name
    type: str                             # "rom_folder" | "steam" | "epic" | "gog" | "heroic"
    path: Optional[str] = None            # root path to scan
    system: Optional[str] = None          # system ID (for ROM folders)
    extensions: Optional[list[str]] = None  # file extensions (for ROM folders)
    recursive: bool = True               # recurse into subdirectories
    enabled: bool = True


class SourceProvider(ABC):
    """Abstract base for discovering and importing games from various sources."""

    @abstractmethod
    def discover(self) -> list[GameEntry]:
        """Discover games from this source.

        Returns a list of GameEntry objects representing found games.
        """
        ...

    @abstractmethod
    def validate(self) -> bool:
        """Check if the source is valid (paths exist, binaries available, etc.)."""
        ...

    @abstractmethod
    def get_def(self) -> SourceDef:
        """Return the source definition."""
        ...

    def scan_roms(self, root_path: str) -> list[GameEntry]:
        """Generic ROM scanning implementation for subclasses."""
        path = Path(root_path)
        if not path.exists() or not path.is_dir():
            return []

        entries: list[GameEntry] = []
        extensions = self.get_def().extensions or []
        recursive = True

        for f in path.rglob("*") if recursive else path.iterdir():
            if f.is_file() and (not extensions or f.suffix.lower() in extensions):
                game_id = f"{self.get_def().id}:{f!s}"
                entries.append(GameEntry(
                    id=hashlib.sha256(game_id.encode()).hexdigest()[:16],
                    name=f.stem,
                    system=self.get_def().system or "unknown",
                    path=str(f),
                    source=self.get_def().id,
                ))

        return entries
