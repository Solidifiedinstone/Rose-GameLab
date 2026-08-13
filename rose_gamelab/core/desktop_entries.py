"""Reading the applications already installed on this machine.

GameLab detects Steam, Heroic, Lutris, GOG and emulators. It will never detect
everything: An Anime Game Launcher, an itch.io build, a game someone compiled
themselves. All of those install a `.desktop` entry, which is the desktop
equivalent of "here is a thing you can run, here is its name and icon".

Reading those turns "add a game manually" from typing a path into picking from
a list of what is already installed — including the icon, which becomes the
cover until something better is found.

Only the fields that matter are parsed. This is not a general freedesktop
implementation and does not pretend to be one.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Field codes a launcher is supposed to substitute. Nothing here opens files
# with a game, so they are simply removed.
FIELD_CODES = re.compile(r"%[fFuUdDnNickvm]")

ICON_DIRECTORIES = (
    Path.home() / ".local/share/icons",
    Path("/usr/share/icons"),
    Path("/usr/share/pixmaps"),
    Path.home() / ".icons",
)

ICON_EXTENSIONS = (".png", ".svg", ".jpg", ".xpm")

#: Entries that are plainly not games, so the picker can lead with the rest.
NON_GAME_CATEGORIES = {
    "Settings", "System", "Development", "Office", "Utility",
    "TextEditor", "FileManager", "TerminalEmulator",
}


@dataclass
class DesktopApp:
    """One installed application."""

    name: str
    command: str
    path: Path
    icon: str = ""
    categories: tuple[str, ...] = ()
    comment: str = ""

    @property
    def is_game(self) -> bool:
        return "Game" in self.categories

    @property
    def is_obviously_not_a_game(self) -> bool:
        """Settings panels, terminals, editors — never worth offering first."""
        return bool(set(self.categories) & NON_GAME_CATEGORIES)

    def icon_file(self) -> Optional[Path]:
        return find_icon(self.icon) if self.icon else None


def entry_directories() -> list[Path]:
    """Where .desktop files live, honouring the XDG variables if set."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"

    directories = [Path(data_home) / "applications"]
    directories.extend(Path(part) / "applications" for part in data_dirs.split(":") if part)
    directories.append(Path.home() / ".local/share/flatpak/exports/share/applications")
    directories.append(Path("/var/lib/flatpak/exports/share/applications"))

    seen: list[Path] = []
    for directory in directories:
        if directory.is_dir() and directory not in seen:
            seen.append(directory)
    return seen


def clean_exec(value: str) -> str:
    """Turn a desktop Exec= line into something runnable.

    Field codes are dropped, and the `env VAR=x program` form is kept intact —
    An Anime Game Launcher ships exactly that, and stripping it would break the
    Wayland backend it asks for.
    """
    return " ".join(FIELD_CODES.sub("", value).split()).strip()


def parse_entry(path: str | Path) -> Optional[DesktopApp]:
    """Read one .desktop file. Returns None if it is not a runnable app."""
    path = Path(path)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("could not read %s: %s", path, exc)
        return None

    values: dict[str, str] = {}
    in_entry = False

    for line in text.splitlines():
        line = line.strip()

        if line.startswith("["):
            # Only the main section; actions and other groups repeat these keys
            # with different meanings.
            in_entry = line == "[Desktop Entry]"
            continue

        if not in_entry or "=" not in line or line.startswith("#"):
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        # Localised keys such as Name[de] would otherwise overwrite the default.
        if "[" not in key and key not in values:
            values[key] = value.strip()

    if values.get("Type", "Application") != "Application":
        return None
    if values.get("NoDisplay", "").lower() == "true":
        return None
    if values.get("Hidden", "").lower() == "true":
        return None

    command = clean_exec(values.get("Exec", ""))
    name = values.get("Name", "").strip()
    if not command or not name:
        return None

    categories = tuple(c for c in values.get("Categories", "").split(";") if c)

    return DesktopApp(
        name=name,
        command=command,
        path=path,
        icon=values.get("Icon", "").strip(),
        categories=categories,
        comment=values.get("Comment", "").strip(),
    )


def installed_apps(*, games_only: bool = False) -> list[DesktopApp]:
    """Every installed application, sorted by name.

    `games_only` keeps just the entries the system itself files under Games.
    That misses a launcher that set no category at all, which is why the
    interface offers the unfiltered list too rather than only this one.

    Duplicates are resolved by name: a Flatpak and a native package of the same
    program are one entry in the picker, not two identical-looking rows.
    """
    found: dict[str, DesktopApp] = {}

    for directory in entry_directories():
        for entry in _desktop_files(directory):
            app = parse_entry(entry)
            if app is None:
                continue
            if games_only and not app.is_game:
                continue
            found.setdefault(app.name, app)

    return sorted(found.values(), key=lambda a: a.name.lower())


def _desktop_files(directory: Path) -> Iterator[Path]:
    try:
        yield from sorted(directory.glob("*.desktop"))
    except OSError as exc:
        logger.debug("could not list %s: %s", directory, exc)


def find_icon(name: str, *, preferred_size: int = 512) -> Optional[Path]:
    """Resolve an icon name to a file, preferring the largest available.

    A desktop Icon= is usually a bare theme name rather than a path, and the
    biggest version is the one worth showing as a cover.
    """
    if not name:
        return None

    # Already a path.
    direct = Path(name)
    if direct.is_absolute() and direct.is_file():
        return direct

    best: Optional[tuple[int, Path]] = None

    for base in ICON_DIRECTORIES:
        if not base.is_dir():
            continue
        for extension in ICON_EXTENSIONS:
            for candidate in _icon_candidates(base, name, extension):
                size = _icon_size(candidate, preferred_size)
                if best is None or size > best[0]:
                    best = (size, candidate)

    return best[1] if best else None


def _icon_candidates(base: Path, name: str, extension: str) -> Iterator[Path]:
    flat = base / f"{name}{extension}"
    if flat.is_file():
        yield flat

    try:
        yield from (p for p in base.glob(f"*/*/apps/{name}{extension}") if p.is_file())
        yield from (p for p in base.glob(f"*/apps/*/{name}{extension}") if p.is_file())
    except OSError:
        return


def _icon_size(path: Path, preferred: int) -> int:
    """Rank a candidate icon. Scalable art beats any fixed size."""
    if path.suffix == ".svg":
        return preferred * 2

    for part in path.parts:
        match = re.fullmatch(r"(\d+)x(\d+)", part)
        if match:
            return int(match.group(1))

    return 0


def command_parts(command: str) -> list[str]:
    """Split a command line the way a shell would, without running one."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()
