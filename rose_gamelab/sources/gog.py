"""GOG (standalone Linux installs) importer.

There is no GOG Galaxy client for Linux. GOG's Linux builds ship as a
self-extracting `.sh` installer (MojoSetup) which unpacks a directory laid out
like this:

    <install dir>/
        start.sh                    launcher — sets LD_LIBRARY_PATH etc.
        gameinfo                    plain text: name, version, build, lang, ids
        game/                       the actual game
            goggame-<id>.info       JSON: real title and play tasks
        support/
        docs/

A directory is recognised as a GOG game by that structure — `start.sh` plus a
`game/` directory or a `goggame-*.info` file — never by its name. (The previous
implementation invented a `GOG Games/` folder inside `~/.config/gog` and looked
for `<dir>/<dir>.sh` inside it; no GOG installer has ever produced that, so it
found nothing on any machine.)

`start.sh` is the launch target. The play tasks inside `goggame-<id>.info` name
Windows executables — the file is shipped from the Windows build even in Linux
packages — so they are recorded as metadata and are not used to launch
anything.

GOG games installed through Heroic are Windows builds under a Wine prefix and
belong to `sources/heroic.py`; this module does not try to claim them.

Nothing here touches the network.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator, Optional

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import GameEntry
from rose_gamelab.sources.base import SourceDef, SourceProvider

logger = logging.getLogger(__name__)

# The GOG Linux installer's own default install location. This is where the
# installer puts games unless the user picks another directory — it is not a
# convention this module imposes, and any other directory works just as well
# when passed as `gog_root`.
DEFAULT_GOG_ROOTS = ("~/GOG Games",)

# How many directory levels below a scan root to look for game directories.
# 1 covers the installer default (<root>/<Game>/start.sh); 2 covers users who
# group games into subfolders. Deeper than that and a scan of a large drive
# gets expensive for no benefit.
MAX_SCAN_DEPTH = 2

# goggame-1207658930.info — the digits are GOG's product id.
GOGGAME_INFO_RE = re.compile(r"^goggame-(\d+)\.info$")

# `gameinfo` is a bare newline-separated list with no keys. Line order is fixed
# by the installer; only the first line (name) is relied on here, the rest are
# read best-effort and reported as-is.
GAMEINFO_FIELDS = ("name", "version", "build", "language", "game_id", "root_game_id")


def load_json(path: Path) -> Optional[Any]:
    """Read one JSON file, or None with a logged reason."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("%s is not valid JSON: %s", path, exc)
        return None


def parse_gameinfo(path: Path) -> dict[str, str]:
    """Parse the plain-text `gameinfo` file the Linux installer writes."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("could not read %s: %s", path, exc)
        return {}

    return {
        field: line.strip()
        for field, line in zip(GAMEINFO_FIELDS, lines, strict=False)
        if line.strip()
    }


def find_info_files(game_dir: Path) -> list[Path]:
    """Every `goggame-<id>.info` in the install root and its `game/` directory.

    Only those two places are searched. A full recursive walk of a game
    directory can mean tens of thousands of files, and GOG does not put these
    anywhere else.
    """
    found: list[Path] = []
    for directory in (game_dir, game_dir / "game"):
        if not _quietly(Path.is_dir, directory):
            continue
        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            logger.warning("could not list %s: %s", directory, exc)
            continue
        found.extend(
            child for child in children
            if _quietly(Path.is_file, child) and GOGGAME_INFO_RE.match(child.name)
        )
    return found


def _quietly(check, path: Path) -> bool:
    """Run a pathlib predicate, treating an unreadable path as "no".

    `Path.is_file()` and friends swallow permission errors on Python 3.14 and
    **raise** them on 3.12 and 3.13, so a single unreadable directory anywhere
    under a games folder crashed the whole scan on the versions most people are
    running. A directory we cannot look inside is not a GOG game; that is the
    answer, not an exception.
    """
    try:
        return bool(check(path))
    except OSError as exc:
        logger.debug("could not inspect %s: %s", path, exc)
        return False


def parse_info_file(path: Path) -> Optional[dict]:
    """One `goggame-<id>.info`, or None if it is not usable.

    The id in the filename is trusted over `gameId` in the body: DLC info files
    have been seen carrying the base game's `gameId`, and the filename is what
    GOG's own tooling keys on.
    """
    match = GOGGAME_INFO_RE.match(path.name)
    if match is None:
        return None

    data = load_json(path)
    if not isinstance(data, dict):
        if data is not None:
            logger.warning("%s is not a JSON object", path)
        return None

    file_id = match.group(1)
    root_id = str(data.get("rootGameId") or "").strip()

    tasks = data.get("playTasks")
    tasks = [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []

    return {
        "path": str(path),
        "game_id": file_id,
        "root_game_id": root_id,
        # A DLC's info file names the base game as its root; the base game's
        # names itself. When rootGameId is absent we cannot tell, and say so
        # by leaving this None rather than guessing "base game".
        "is_base_game": (file_id == root_id) if root_id else None,
        "name": str(data.get("name") or "").strip(),
        "version": data.get("version"),
        "play_tasks": [
            {
                "name": str(task.get("name") or ""),
                "category": str(task.get("category") or ""),
                "type": str(task.get("type") or ""),
                "path": str(task.get("path") or ""),
                "arguments": str(task.get("arguments") or ""),
                "is_primary": task.get("isPrimary") is True,
            }
            for task in tasks
        ],
    }


class GOGProvider(SourceProvider):
    """Discovers GOG games installed on Linux by the official .sh installer."""

    def __init__(self, config: Optional[Config] = None, gog_root: Optional[str] = None) -> None:
        self.config = config
        # An explicit root is used exactly as given, even if it holds nothing;
        # that is the user saying where their games are, and silently falling
        # back to a default would hide a typo.
        self.roots: list[Path] = (
            [Path(gog_root).expanduser()] if gog_root else self.default_roots()
        )

    @staticmethod
    def default_roots() -> list[Path]:
        """Existing directories from the installer's default location list."""
        return [
            path for path in (Path(root).expanduser() for root in DEFAULT_GOG_ROOTS)
            if path.is_dir()
        ]

    # ── Recognising a GOG install directory ───────────────────────

    @staticmethod
    def is_game_dir(path: Path) -> bool:
        """Whether `path` is the root of a GOG Linux install.

        Structural only: `start.sh` is what makes the directory launchable, and
        a `game/` subdirectory or a `goggame-*.info` is what makes it GOG's
        rather than some other program's start.sh.
        """
        if not _quietly(Path.is_file, path / "start.sh"):
            return False
        return _quietly(Path.is_dir, path / "game") or bool(find_info_files(path))

    def game_dirs(self) -> list[Path]:
        """Every GOG install directory under the configured roots."""
        found: list[Path] = []
        seen: set[Path] = set()

        for root in self.roots:
            for candidate in self._walk(root, MAX_SCAN_DEPTH):
                try:
                    resolved = candidate.resolve()
                except OSError as exc:
                    logger.warning("could not resolve %s: %s", candidate, exc)
                    continue
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)

        return sorted(found)

    def _walk(self, directory: Path, depth: int) -> Iterator[Path]:
        """Yield game directories at or below `directory`, depth-limited.

        Descent stops at a recognised game directory: a game's own `game/`
        subtree must not be searched for more games.
        """
        if not directory.is_dir():
            return

        if self.is_game_dir(directory):
            yield directory
            return

        if depth <= 0:
            return

        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            logger.warning("could not list %s: %s", directory, exc)
            return

        for child in children:
            if _quietly(Path.is_dir, child) and not _quietly(Path.is_symlink, child):
                yield from self._walk(child, depth - 1)

    # ── Discovery of games ────────────────────────────────────────

    def discover(self) -> list[GameEntry]:
        """Return every GOG game installed under the configured roots."""
        entries: dict[str, GameEntry] = {}

        for game_dir in self.game_dirs():
            entry = self.parse_game_dir(game_dir)
            if entry is not None:
                entries.setdefault(entry.id, entry)

        return list(entries.values())

    def parse_game_dir(self, game_dir: Path) -> Optional[GameEntry]:
        """Build a GameEntry for one GOG install directory."""
        start_sh = game_dir / "start.sh"
        if not start_sh.is_file():
            logger.debug("skipping %s: no start.sh", game_dir)
            return None

        infos = [info for info in (parse_info_file(p) for p in find_info_files(game_dir)) if info]
        base = self._base_info(infos)
        gameinfo = parse_gameinfo(game_dir / "gameinfo")

        # Name, best source first: the .info file GOG ships with the real
        # display name, then the plain-text gameinfo, then the directory name.
        name, name_source = next(
            (
                (value, source)
                for value, source in (
                    ((base or {}).get("name", ""), "goggame_info"),
                    (gameinfo.get("name", ""), "gameinfo"),
                    (game_dir.name, "directory"),
                )
                if value
            ),
            (game_dir.name, "directory"),
        )

        game_id = (base or {}).get("game_id") or gameinfo.get("game_id") or ""
        identity = game_id or game_dir.name

        # An unreadable or non-executable start.sh will fail at launch. Flag it
        # here rather than importing a game that cannot start.
        executable = os.access(start_sh, os.X_OK)
        if not executable:
            logger.warning(
                "%s is not executable; %s will not launch until "
                "`chmod +x` is run on it", start_sh, name,
            )

        return GameEntry(
            id=f"gog:{identity}",
            name=name,
            system="pc",
            path=str(start_sh),
            source="gog",
            is_gog=True,
            metadata={
                "gog_game_id": game_id,
                "install_path": str(game_dir),
                "name_source": name_source,
                "start_sh_executable": executable,
                "version": (base or {}).get("version") or gameinfo.get("version") or "",
                "language": gameinfo.get("language", ""),
                # Windows executables even in Linux packages — informational
                # only, never used as a launch target.
                "play_tasks": (base or {}).get("play_tasks", []),
                "info_files": [info["path"] for info in infos],
                "dlc_ids": [
                    info["game_id"] for info in infos
                    if base is not None and info["game_id"] != base["game_id"]
                ],
            },
        )

    @staticmethod
    def _base_info(infos: list[dict]) -> Optional[dict]:
        """Pick the base game's info file out of the ones found.

        A game with DLC has several. The base game is the one whose id equals
        its own rootGameId; if none says so, the first is used and that choice
        is logged, because it may be a DLC's name.
        """
        if not infos:
            return None

        base = next((info for info in infos if info["is_base_game"]), None)
        if base is not None:
            return base

        if len(infos) > 1:
            logger.info(
                "%d goggame-*.info files but none identifies itself as the base "
                "game; using %s", len(infos), infos[0]["path"],
            )
        return infos[0]

    # ── SourceProvider interface ──────────────────────────────────

    def validate(self) -> bool:
        """True when at least one configured root exists on disk.

        Deliberately not "at least one game found": an empty but real GOG
        directory is a valid, correctly configured source.
        """
        return any(root.is_dir() for root in self.roots)

    def get_def(self) -> SourceDef:
        return SourceDef(
            id="gog",
            name="GOG",
            type="gog",
            path=str(self.roots[0]) if self.roots else None,
            system="pc",
            enabled=True,
        )
