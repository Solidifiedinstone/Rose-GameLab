"""Heroic Games Launcher importer.

Heroic is an Electron front-end for three command-line store clients: `legendary`
(Epic), `gogdl` (GOG) and `nile` (Amazon). It keeps a cache of each store's
library and a separate record of what is actually installed. Both are needed:
the library cache lists every game the account *owns*, most of which are not on
disk.

Files read (all under the Heroic config root):

    store_cache/legendary_library.json   owned Epic games
    store_cache/gog_library.json         owned GOG games
    legendary/installed.json             Epic games actually installed
    gog_store/installed.json             GOG games actually installed
    config.json                          default install path

Games are launched through `heroic://launch/<runner>/<appName>`. Running the
executable directly skips the Wine/Proton prefix, the per-game environment and
the cloud-save sync that Heroic sets up, which for a Windows game means it
simply does not start.

── What is verified and what is assumed ──────────────────────────────
This module was written without a Heroic installation available to test
against, so the JSON shapes below come from Heroic's published `GameInfo`
type and from legendary's own `installed.json` format, not from a real
install on this machine. Every field access is therefore defensive: a
missing or renamed key drops the game with a logged reason rather than
producing a half-built entry. If Heroic changes a key, the log says which
file and which key, so the fix is a one-line change here.

The library files are read as `{"library": [...]}`, `{"games": [...]}` or a
bare list, because Heroic has used more than one wrapper key across versions
and the correct one is not worth guessing at.

── Launching ─────────────────────────────────────────────────────────
`core/launcher.py` builds the command for kind='heroic' as `[target]`, which
cannot execute a URL. This module still reports the URL as the entry's `path`
because that is the correct launch target; the ready-made argv (with the Heroic
binary or a URL opener resolved) is in `metadata["launch_command"]`. Teaching
the launcher to use it is a change to launcher.py and is deliberately not done
here.

Nothing here touches the network.
"""

from __future__ import annotations

import json
import logging
import shutil

from pathlib import Path
from typing import Any, Optional

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import GameEntry
from rose_gamelab.sources.base import SourceDef, SourceProvider

logger = logging.getLogger(__name__)

# Directories that may hold a Heroic configuration, in priority order.
HEROIC_ROOTS = (
    "~/.config/heroic",
    "~/.var/app/com.heroicgameslauncher.hgl/config/heroic",   # Flatpak
)

# Runner id -> (library cache, installed record). The installed record is the
# authority on what is on disk; the library cache only supplies the title and
# artwork. Paths are relative to the Heroic config root.
#
# legendary's config directory moved from <heroic>/legendary to
# <heroic>/legendaryConfig/legendary in later Heroic releases, so both are
# tried and the first that exists wins.
RUNNERS: dict[str, dict[str, tuple[str, ...]]] = {
    "legendary": {
        "library": ("store_cache/legendary_library.json",),
        "installed": (
            "legendary/installed.json",
            "legendaryConfig/legendary/installed.json",
        ),
    },
    "gog": {
        "library": ("store_cache/gog_library.json",),
        "installed": ("gog_store/installed.json",),
    },
}

# Display name for the store behind each runner, for metadata and log messages.
RUNNER_STORE = {"legendary": "epic", "gog": "gog"}

# Wrapper keys Heroic has used around the list of games in a library cache.
LIBRARY_KEYS = ("library", "games")


def load_json(path: Path) -> Optional[Any]:
    """Read one JSON file, or None with a logged reason.

    Unreadable and malformed files are two different problems and are logged
    as such — "Heroic library missing" and "Heroic library corrupt" need
    different fixes from the user.
    """
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


def extract_library(data: Any, source: Path) -> list[dict]:
    """Pull the list of GameInfo records out of a Heroic library cache."""
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        records = next(
            (data[key] for key in LIBRARY_KEYS if isinstance(data.get(key), list)),
            None,
        )
        if records is None:
            logger.warning(
                "%s has no %s list; Heroic's format may have changed",
                source, " or ".join(LIBRARY_KEYS),
            )
            return []
    else:
        logger.warning("%s is not a JSON object or array", source)
        return []

    return [record for record in records if isinstance(record, dict)]


def extract_installed(data: Any, source: Path) -> dict[str, dict]:
    """Normalise an installed record into {appName: fields}.

    The two runners disagree on shape and both are handled:

      legendary  {"<app_name>": {"app_name": ..., "title": ..., ...}}
      gogdl      {"installed": [{"appName": ..., "install_path": ...}, ...]}
    """
    if isinstance(data, dict) and isinstance(data.get("installed"), list):
        records = [r for r in data["installed"] if isinstance(r, dict)]
    elif isinstance(data, list):
        records = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        # legendary keys the whole file by app name; keep the key as the
        # authoritative id, since the nested app_name has been absent before.
        result: dict[str, dict] = {}
        for app_name, fields in data.items():
            if isinstance(fields, dict) and app_name:
                result[str(app_name)] = fields
        return result
    else:
        logger.warning("%s is not a JSON object or array", source)
        return {}

    result = {}
    for record in records:
        app_name = str(record.get("appName") or record.get("app_name") or "").strip()
        if not app_name:
            logger.debug("skipping entry in %s: no appName", source)
            continue
        result[app_name] = record
    return result


class HeroicProvider(SourceProvider):
    """Discovers games installed through Heroic Games Launcher."""

    def __init__(self, config: Optional[Config] = None, heroic_root: Optional[str] = None) -> None:
        self.config = config
        self.heroic_root: Optional[Path] = (
            Path(heroic_root).expanduser() if heroic_root else self.find_heroic_root()
        )

    # ── Discovery of Heroic itself ────────────────────────────────

    @staticmethod
    def find_heroic_root() -> Optional[Path]:
        """Locate the Heroic config directory, or None if Heroic is not set up.

        A directory that exists but holds no store cache and no installed
        record is not accepted: Heroic creates its config directory on first
        run, and an empty one means the user has never signed in.
        """
        for candidate in HEROIC_ROOTS:
            root = Path(candidate).expanduser()
            if root.is_dir() and any(HeroicProvider._data_files(root)):
                return root.resolve()
        return None

    @staticmethod
    def _data_files(root: Path) -> list[Path]:
        """Every library/installed file that actually exists under `root`."""
        relatives = [
            relative
            for runner in RUNNERS.values()
            for group in runner.values()
            for relative in group
        ]
        return [root / relative for relative in relatives if (root / relative).is_file()]

    def _first_existing(self, relatives: tuple[str, ...]) -> Optional[Path]:
        if not self.heroic_root:
            return None
        for relative in relatives:
            candidate = self.heroic_root / relative
            if candidate.is_file():
                return candidate
        return None

    def default_install_path(self) -> Optional[str]:
        """Heroic's configured default install directory, if config.json says.

        Only used for reporting — install paths come from the per-game
        installed record, which is the only place they are actually correct
        (a game may have been installed elsewhere).
        """
        if not self.heroic_root:
            return None

        data = load_json(self.heroic_root / "config.json")
        if not isinstance(data, dict):
            return None

        settings = data.get("defaultSettings")
        if not isinstance(settings, dict):
            logger.debug("config.json has no defaultSettings block")
            return None

        path = settings.get("defaultInstallPath")
        return str(path) if isinstance(path, str) and path else None

    # ── Discovery of games ────────────────────────────────────────

    def discover(self) -> list[GameEntry]:
        """Return every installed Heroic game, across all runners.

        Deduplicated on (runner, appName): the same appName can legitimately
        appear under two runners, and those are two different games.
        """
        if not self.heroic_root:
            return []

        entries: dict[str, GameEntry] = {}

        for runner in sorted(RUNNERS):
            for entry in self.discover_runner(runner):
                entries.setdefault(entry.id, entry)

        return list(entries.values())

    def discover_runner(self, runner: str) -> list[GameEntry]:
        """Installed games for one runner ('legendary' or 'gog')."""
        paths = RUNNERS.get(runner)
        if paths is None:
            raise ValueError(f"Unknown Heroic runner: {runner!r}")

        installed_file = self._first_existing(paths["installed"])
        library_file = self._first_existing(paths["library"])

        if installed_file is None and library_file is None:
            logger.debug("no %s data under %s", runner, self.heroic_root)
            return []

        installed = (
            extract_installed(load_json(installed_file), installed_file)
            if installed_file else {}
        )
        library = {}
        if library_file is not None:
            for record in extract_library(load_json(library_file), library_file):
                app_name = str(record.get("app_name") or record.get("appName") or "").strip()
                if app_name:
                    library[app_name] = record

        if installed_file is None:
            # No installed record at all. Fall back to the library cache's own
            # is_installed flag, which Heroic keeps up to date but which goes
            # stale if a game is removed outside Heroic. Say so.
            logger.info(
                "no installed.json for runner %r; falling back to is_installed "
                "in the library cache, which can be stale", runner,
            )
            app_names = [
                name for name, record in library.items() if record.get("is_installed") is True
            ]
        else:
            app_names = list(installed)

        entries = []
        for app_name in sorted(app_names):
            entry = self.build_entry(runner, app_name, library.get(app_name), installed.get(app_name))
            if entry is not None:
                entries.append(entry)
        return entries

    def build_entry(
        self,
        runner: str,
        app_name: str,
        library_record: Optional[dict],
        installed_record: Optional[dict],
    ) -> Optional[GameEntry]:
        """Build one GameEntry, or None if the game cannot be identified."""
        library_record = library_record or {}
        installed_record = installed_record or {}

        # Title: the library cache is the nicely-formatted one; legendary's
        # installed.json carries a title too, gogdl's does not.
        title = str(
            library_record.get("title")
            or installed_record.get("title")
            or ""
        ).strip()

        if not title:
            # The game is installed but nothing tells us its name. Import it
            # under its appName rather than dropping it silently — a game the
            # user can launch under an ugly name beats a missing game — but
            # record where the name came from so the interface can say so.
            logger.warning(
                "Heroic %s game %s is installed but has no title in the library "
                "cache; using its appName", runner, app_name,
            )
            title = app_name
            title_source = "app_name"
        else:
            title_source = "library"

        install = library_record.get("install")
        install = install if isinstance(install, dict) else {}

        install_path = str(
            installed_record.get("install_path")
            or install.get("install_path")
            or ""
        )
        executable = str(
            installed_record.get("executable")
            or install.get("executable")
            or ""
        )
        platform = str(
            installed_record.get("platform")
            or install.get("platform")
            or ""
        )

        # DLC is installed into the base game's directory and is not launchable
        # on its own.
        if installed_record.get("is_dlc") is True or install.get("is_dlc") is True:
            logger.debug("skipping DLC %s (%s)", app_name, runner)
            return None

        url = f"heroic://launch/{runner}/{app_name}"

        return GameEntry(
            id=f"heroic:{runner}:{app_name}",
            name=title,
            system="pc",
            # Launch target, not a filesystem path.
            path=url,
            source="heroic",
            is_heroic=True,
            cover_art=str(library_record.get("art_square") or library_record.get("art_cover") or ""),
            metadata={
                "heroic_runner": runner,
                "store": RUNNER_STORE.get(runner, runner),
                "app_name": app_name,
                "title_source": title_source,
                "install_path": install_path,
                "executable": executable,
                "platform": platform,
                "install_size": installed_record.get("install_size") or install.get("install_size"),
                "version": installed_record.get("version") or install.get("version"),
                "developer": str(library_record.get("developer") or ""),
                "in_library_cache": bool(library_record),
                # The launcher cannot exec a URL; this is the argv that works.
                "launch_command": self.launch_command(url),
                "launch_target_is_url": True,
            },
        )

    @staticmethod
    def launch_command(url: str) -> list[str]:
        """The argv that actually starts a Heroic game from its URL.

        Prefers the Heroic binary, because handing it the URL directly works
        whether or not a desktop URL handler is registered. Falls back to
        `xdg-open`, and finally to the Flatpak invocation — which is returned
        even when nothing was found on PATH, so the caller gets a command it
        can show the user rather than an empty list that looks like success.
        """
        binary = shutil.which("heroic")
        if binary:
            return [binary, url]

        opener = shutil.which("xdg-open")
        if opener:
            return [opener, url]

        return ["flatpak", "run", "com.heroicgameslauncher.hgl", url]

    # ── SourceProvider interface ──────────────────────────────────

    def validate(self) -> bool:
        """True when a Heroic config with at least one store file is present."""
        return self.heroic_root is not None and bool(self._data_files(self.heroic_root))

    def get_def(self) -> SourceDef:
        return SourceDef(
            id="heroic",
            name="Heroic Games Launcher",
            type="heroic",
            path=str(self.heroic_root) if self.heroic_root else None,
            system="pc",
            enabled=True,
        )
