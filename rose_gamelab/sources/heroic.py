"""Heroic Games Launcher importer.

Heroic stores game metadata in ~/.config/GOG.com/heroic/ or ~/.local/share/heroic.
We parse the JSON manifests to discover installed games.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from rose_gamelab.core.emulator import GameEntry


class HeroicProvider:
    """Discover and import games from Heroic Games Launcher."""

    HEROIC_CONFIG_PATH = Path.home() / ".config" / "heroic"
    HEROIC_GOG_PATH = Path.home() / ".config" / "GOG.com" / "heroic"

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or self._find_path()

    def _find_path(self) -> Optional[str]:
        for p in (self.HEROIC_CONFIG_PATH, self.HEROIC_GOG_PATH):
            if p.exists():
                return str(p)
        return None

    def discover(self) -> list[GameEntry]:
        if not self.path:
            return []

        entries: list[GameEntry] = []
        p = Path(self.path)

        # Heroic stores game details in config files
        # Structure: config/.../.../.../settings/games/ or similar
        for config_file in p.rglob("*.json"):
            # Look for game info JSON files
            try:
                data = json.loads(config_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            # Check if this looks like a game manifest
            game = self._parse_game_entry(config_file, data)
            if game and game.name and game.path:
                entries.append(game)

        return entries

    def _parse_game_entry(self, config_file: Path, data: dict) -> Optional[GameEntry]:
        """Parse a game entry from a Heroic config JSON."""
        # Try common fields
        name = (data.get("title", "") or data.get("gameTitle", "") or
                data.get("info", {}).get("title", "") or
                data.get("slug", "")).strip()

        if not name:
            return None

        # Try to find the game binary path
        game_path = self._find_game_binary(config_file, data)

        app_id = (data.get("appId", "") or data.get("gameId", "") or "")

        return GameEntry(
            id=f"heroic:{app_id or name}",
            name=name,
            system="pc",
            path=game_path or "",
            source="heroic",
            is_heroic=True,
        )

    def _find_game_binary(self, config_file: Path, data: dict) -> Optional[str]:
        """Locate a game binary from Heroic config."""
        game_folder = (data.get("gameDirectory", "") or
                      data.get("gamePath", "") or
                      data.get("installPath", "") or
                      data.get("path", ""))

        game_folder = Path(game_folder)
        if game_folder.exists():
            # Try common executable patterns
            for ext in ("", ".exe", ".sh", ".bin"):
                exe = game_folder / f"{game_folder.name}{ext}"
                if exe.exists():
                    return str(exe)
                # Try 'Game' subdirectory
                game_sub = game_folder / "Game" / f"{game_folder.name}{ext}"
                if game_sub.exists():
                    return str(game_sub)

        return None
