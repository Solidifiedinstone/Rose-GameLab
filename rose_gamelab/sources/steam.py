"""Steam library importer.

Reads Steam's `appmanifest_*.acf` files to discover installed games across every
library folder, including secondary drives and Flatpak installs.

Steam games are launched through `steam://run/<appid>` rather than by locating a
binary: Steam handles Proton, cloud saves, the overlay and DRM handshakes, and
running the executable directly bypasses all of it.

Nothing here touches the network.
"""

from __future__ import annotations

import logging
import re

from pathlib import Path
from typing import Iterable, Optional

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import GameEntry
from rose_gamelab.sources.base import SourceDef, SourceProvider

logger = logging.getLogger(__name__)

# Steam's StateFlags is a bitfield. Bit 2 (value 4) is StateFullyInstalled.
# Bit 1 (value 2) means "update required" — checking it, as the previous
# implementation did, matches games that are NOT ready to play.
STATE_FULLY_INSTALLED = 4

# Root directories that may contain a Steam installation. `steamapps` is
# appended per-candidate; secondary drives come from libraryfolders.vdf.
STEAM_ROOTS = (
    "~/.local/share/Steam",
    "~/.steam/steam",
    "~/.steam/root",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",   # Flatpak
    "~/.steam/debian-installation",                            # Debian/Ubuntu
    "/usr/local/share/Steam",
    "/opt/steam",
)

# Matches VDF's `"key"<whitespace>"value"` pairs. Values may contain escaped
# quotes and backslashes (Windows paths in libraryfolders.vdf), so the value
# group consumes escape pairs rather than stopping at the first backslash.
_KV_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"')


def parse_vdf_pairs(content: str) -> list[tuple[str, str]]:
    """Extract every `"key" "value"` pair from VDF text, in file order.

    This is intentionally flat rather than a full VDF tree parse: appmanifest
    files put every field we need at a predictable depth, and a flat scan is
    far harder to break on malformed input than a nesting parser.
    """
    return [
        (key, value.replace('\\\\', '\\').replace('\\"', '"'))
        for key, value in _KV_RE.findall(content)
    ]


def parse_vdf_dict(content: str) -> dict[str, str]:
    """First occurrence of each key wins — appmanifest has no repeated keys
    at the level we care about, and earlier entries are the canonical ones."""
    result: dict[str, str] = {}
    for key, value in parse_vdf_pairs(content):
        result.setdefault(key, value)
    return result


class SteamProvider(SourceProvider):
    """Discovers installed Steam games across all library folders."""

    def __init__(self, config: Optional[Config] = None, steam_root: Optional[str] = None) -> None:
        self.config = config
        self.steam_root: Optional[Path] = (
            Path(steam_root).expanduser() if steam_root else self.find_steam_root()
        )

    # ── Discovery of Steam itself ─────────────────────────────────

    @staticmethod
    def find_steam_root() -> Optional[Path]:
        """Locate the Steam installation root (the directory holding steamapps)."""
        for candidate in STEAM_ROOTS:
            root = Path(candidate).expanduser()
            if (root / "steamapps").is_dir():
                # ~/.steam/root and ~/.steam/steam are usually symlinks into
                # the real install; resolve so libraries dedupe correctly.
                return root.resolve()
        return None

    def library_folders(self) -> list[Path]:
        """Every `steamapps` directory Steam knows about, primary first.

        Secondary libraries (extra drives) are listed in `libraryfolders.vdf`,
        which lives INSIDE steamapps — not beside it.
        """
        if not self.steam_root:
            return []

        primary = self.steam_root / "steamapps"
        folders: list[Path] = []
        seen: set[Path] = set()

        def add(path: Path) -> None:
            try:
                resolved = path.resolve()
            except OSError:
                return
            if resolved not in seen and resolved.is_dir():
                seen.add(resolved)
                folders.append(resolved)

        add(primary)

        vdf = primary / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                content = vdf.read_text(errors="replace")
            except OSError as exc:
                logger.warning("could not read %s: %s", vdf, exc)
                return folders

            # In modern libraryfolders.vdf each library is a numbered block
            # with a "path" key pointing at the library ROOT (not steamapps).
            for key, value in parse_vdf_pairs(content):
                if key == "path":
                    add(Path(value).expanduser() / "steamapps")

        return folders

    # ── Discovery of games ────────────────────────────────────────

    def discover(self) -> list[GameEntry]:
        """Return every fully-installed Steam game, deduplicated by appid."""
        entries: dict[str, GameEntry] = {}

        for library in self.library_folders():
            for manifest in sorted(library.glob("appmanifest_*.acf")):
                entry = self.parse_manifest(manifest)
                if entry and entry.id not in entries:
                    entries[entry.id] = entry

        return list(entries.values())

    def parse_manifest(self, manifest: Path) -> Optional[GameEntry]:
        """Parse one appmanifest file into a GameEntry, or None if unusable.

        Returns None for games that are not fully installed (queued, downloading,
        or update-required) — those cannot be launched yet.
        """
        try:
            content = manifest.read_text(errors="replace")
        except OSError as exc:
            logger.warning("could not read %s: %s", manifest, exc)
            return None

        fields = parse_vdf_dict(content)

        appid = fields.get("appid", "").strip()
        name = fields.get("name", "").strip()
        if not appid.isdigit() or not name:
            logger.debug("skipping %s: missing appid or name", manifest.name)
            return None

        try:
            state_flags = int(fields.get("StateFlags", "0"))
        except ValueError:
            logger.debug("skipping %s: unparseable StateFlags", manifest.name)
            return None

        if not state_flags & STATE_FULLY_INSTALLED:
            return None

        # Steam's own runtime/redistributable entries are installed like games
        # but are not playable; they would otherwise clutter the library.
        if self._is_non_game(appid, name):
            return None

        install_dir = fields.get("installdir", "").strip()
        install_path = manifest.parent / "common" / install_dir if install_dir else None
        if install_path is not None and not install_path.is_dir():
            install_path = None

        size = fields.get("SizeOnDisk", "").strip()

        return GameEntry(
            id=f"steam:{appid}",
            name=name,
            system="pc",
            # Launch target, not a filesystem path — Steam resolves this URL.
            path=f"steam://run/{appid}",
            source="steam",
            is_steam=True,
            metadata={
                "steam_appid": int(appid),
                "install_dir": install_dir,
                "install_path": str(install_path) if install_path else "",
                "size_on_disk": int(size) if size.isdigit() else None,
                "last_updated": fields.get("LastUpdated", ""),
                "manifest": str(manifest),
            },
        )

    @staticmethod
    def _is_non_game(appid: str, name: str) -> bool:
        """Filter out Steam's runtimes, redistributables and Proton builds."""
        KNOWN_NON_GAMES = {
            "228980",   # Steamworks Common Redistributables
            "1070560",  # Steam Linux Runtime 1.0 (scout)
            "1391110",  # Steam Linux Runtime 2.0 (soldier)
            "1628350",  # Steam Linux Runtime 3.0 (sniper)
        }
        if appid in KNOWN_NON_GAMES:
            return True

        lowered = name.lower()
        return (
            lowered.startswith("proton ")
            or lowered.startswith("steam linux runtime")
            or lowered.startswith("steamworks common")
        )

    # ── SourceProvider interface ──────────────────────────────────

    def validate(self) -> bool:
        return self.steam_root is not None and (self.steam_root / "steamapps").is_dir()

    def get_def(self) -> SourceDef:
        return SourceDef(
            id="steam",
            name="Steam",
            type="steam",
            path=str(self.steam_root) if self.steam_root else None,
            system="pc",
            enabled=True,
        )
