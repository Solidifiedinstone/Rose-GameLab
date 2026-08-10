"""GOG Galaxy / GOG.com importer.

GOG stores game data in ~/.config/gog/ or ~/.local/share/GOG.com.
We parse the game metadata files to discover installed games.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rose_gamelab.core.emulator import GameEntry


class GOGProvider:
    """Discover and import games from GOG Galaxy / GOG.com."""

    GOG_CONFIG_PATH = Path.home() / ".config" / "gog"
    GOG_DATA_PATH = Path.home() / ".local" / "share" / "GOG.com"

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or self._find_path()

    def _find_path(self) -> Optional[str]:
        for p in (self.GOG_CONFIG_PATH, self.GOG_DATA_PATH):
            if p.exists():
                return str(p)
        return None

    def discover(self) -> list[GameEntry]:
        if not self.path:
            return []

        entries: list[GameEntry] = []
        p = Path(self.path)

        # GOG stores game info in various locations depending on the version
        # Common pattern: GOG Games/<game_name>/<game_binary>
        games_folder = p / "GOG Games"
        if games_folder.exists():
            entries.extend(self._scan_gog_games_folder(games_folder))

        return entries

    def _scan_gog_games_folder(self, games_folder: Path) -> list[GameEntry]:
        """Scan GOG Games folder for installed games."""
        entries: list[GameEntry] = []

        for game_dir in games_folder.iterdir():
            if not game_dir.is_dir():
                continue

            name = game_dir.name
            game_path = self._find_gog_binary(game_dir)

            if game_path:
                entries.append(GameEntry(
                    id=f"gog:{name}",
                    name=name,
                    system="pc",
                    path=game_path,
                    source="gog",
                    is_gog=True,
                ))

        return entries

    def _find_gog_binary(self, game_dir: Path) -> Optional[str]:
        """Find a game executable in a GOG game directory."""
        for ext in ("", ".exe", ".sh"):
            # Try game name as the binary
            exe = game_dir / f"{game_dir.name}{ext}"
            if exe.exists():
                return str(exe)

            # Try subdirectories
            for subdir in game_dir.iterdir():
                if subdir.is_dir():
                    sub_exe = subdir / f"{subdir.name}{ext}"
                    if sub_exe.exists():
                        return str(sub_exe)

        return None
