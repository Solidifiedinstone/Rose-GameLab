"""Steam library importer.

Reads Steam's appmanifest_*.acf files to discover installed games,
then launches them via steam://run/<appid>.
Supports regular Steam and Flatpak Steam installations.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional, Dict, List

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import GameEntry

logger = logging.getLogger(__name__)


class SteamProvider:
    """Discover and import Steam games across all installation types."""

    def __init__(self, config: Optional[Config] = None, steam_path: Optional[str] = None) -> None:
        self.config = config
        self.steam_path = steam_path or self._find_steam_path()
        self.all_library_folders: List[Path] = []

    def _find_steam_path(self) -> Optional[str]:
        """Locate Steam's library folder across regular, Flatpak, etc."""
        candidates = [
            # Regular Linux installations
            "~/.local/share/Steam/steamapps",
            "~/.steam/steam/steamapps",
            "/home/Gavin/.local/share/Steam/steamapps",
            # Flatpak installations (user install)
            "~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps",
            "/home/Gavin/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps",
            # Flatpak installations (common locations)
            "/home/steamus/.steam/steam/steamapps",
            "/run/media/steam/.local/share/Steam/steamapps",
            # Wine/Proton installations
            "~/.steam/debian-installation/steamapps",
            "~/.wine/drive_c/Program Files (x86)/Steam/steamapps",
            # Common other paths
            "/opt/steam/steamapps",
            "/usr/local/share/Steam/steamapps",
            "/mnt/steam/steamapps",
            "/mnt/gamings/steam/steamapps",
            "/mnt/games/Steam/steam/steamapps",
        ]

        for c in candidates:
            p = Path(c).expanduser()
            if p.exists():
                return str(p)

        return None

    def discover(self) -> List[GameEntry]:
        """Find all Steam games across all library folders."""
        entries: List[GameEntry] = []

        # Get all library folders (primary + any additional)
        self.all_library_folders.clear()
        if self.steam_path:
            self.all_library_folders.append(Path(self.steam_path))
            self._load_library_folders()

        # Scan all folders
        for library in self.all_library_folders:
            library = Path(library)
            if not library.exists():
                continue

            for acf in library.glob("appmanifest_*.acf"):
                app = self._parse_app_manifest(acf, library.parent)
                if app:
                    # Avoid duplicates
                    if app.id not in [e.id for e in entries]:
                        entries.append(app)

        return entries

    def _load_library_folders(self) -> None:
        """Parse libraryfolders.vdf for additional library paths."""
        if not self.steam_path:
            return

        libfolders_path = Path(self.steam_path).parent / "libraryfolders.vdf"
        if not libfolders_path.exists():
            return

        try:
            content = libfolders_path.read_text(errors="replace")
            self._parse_vdf_libraryfolders(content)
        except Exception as e:
            logger.warning(f"Failed to parse libraryfolders.vdf: {e}")

    def _parse_vdf_libraryfolders(self, content: str) -> None:
        """Parse a VDF libraryfolders file to extract library paths."""
        # Extract all "path" entries from the VDF file
        paths = re.findall(r'"path"\s+"([^"]+)"', content)

        for path_str in paths:
            path = Path(path_str).expanduser()

            # Try different Steam directory structures
            steamapps_paths = [
                path / "steamapps",
                path / "Steam/steamapps",
                path / "steam/steamapps",
            ]
            steamapps_path = None
            for p in steamapps_paths:
                if p.exists():
                    steamapps_path = p
                    break

            if steamapps_path and steamapps_path not in self.all_library_folders:
                self.all_library_folders.append(steamapps_path)

            # Also check for additional libraryfolders.vdf in subdirs (recursive scanning)
            lib_vdf = steamapps_path / "libraryfolders.vdf" if steamapps_path else None
            if lib_vdf and lib_vdf.exists() and steamapps_path:
                try:
                    sub_content = lib_vdf.read_text(errors="replace")
                    self._parse_vdf_libraryfolders(sub_content)
                except Exception:
                    pass

    def _parse_app_manifest(self, acf: Path, library_base: Optional[Path] = None) -> Optional[GameEntry]:
        """Parse an appmanifest_*.acf file (binary-ish format)."""
        try:
            # Steam appmanifest files are text-based with key-value pairs
            content = acf.read_text(errors="replace")
            lines = content.splitlines()

            # Extract values
            app_id = None
            name = None
            install_dir = None
            state_flags = 0

            for line in lines:
                stripped = line.strip()
                if stripped.startswith('"appid"') and '"' in stripped:
                    try:
                        app_id = stripped.split('"')[1]
                    except (IndexError, ValueError):
                        app_id = None

                if stripped.startswith('"name"') and '"' in stripped:
                    try:
                        name = stripped.split('"')[1]
                    except (IndexError, ValueError):
                        name = None

                if stripped.startswith('"installdir"') and '"' in stripped:
                    try:
                        install_dir = stripped.split('"')[1]
                    except (IndexError, ValueError):
                        install_dir = None

                if stripped.startswith('"StateFlags"'):
                    try:
                        state_flags = int(stripped.split()[-1])
                    except (IndexError, ValueError):
                        state_flags = 0

            if app_id and name:
                # StateFlags & 2 means installed
                if state_flags & 2:
                    # Locate the game binary
                    game_path = None
                    game_dir = None

                    if install_dir:
                        # Try different paths
                        if library_base:
                            candidates = [
                                library_base / "common" / install_dir,
                                Path(self.steam_path).parent / "common" / install_dir
                                    if "Steam" in self.steam_path
                                    else Path(self.steam_path).parent / "steamapps" / "common" / install_dir,
                            ]
                        else:
                            candidates = [
                                Path(self.steam_path) / "common" / install_dir,
                            ]

                        for game_dir in candidates:
                            if game_dir.exists():
                                break

                    if game_dir and game_dir.exists():
                        # Try to find the game executable
                        for ext in ("", ".exe", ".sh", ".bin", ".AppImage"):
                            exe = game_dir / f"{install_dir}{ext}"
                            if exe.exists():
                                game_path = str(exe)
                                break

                        # If still no exe, look for any executable in the dir
                        if not game_path:
                            for exe in game_dir.iterdir():
                                if exe.is_file():
                                    game_path = str(exe)
                                    break

                    return GameEntry(
                        id=f"steam:{app_id}",
                        name=name,
                        system="pc",
                        path=game_path or "",
                        source="steam",
                        is_steam=True,
                        metadata={"steam_app_id": app_id, "install_dir": install_dir or ""},
                    )
        except Exception as e:
            logger.warning(f"Failed to parse {acf}: {e}")
            return None

        return None
