"""Finding the Proton versions installed on this machine.

A launch profile can name a Proton version, and until now that was a text box:
the user had to know that the thing they installed is called "GE-Proton10-34"
and type it exactly. Every one of them is a directory with a predictable name,
so there is no reason to make anybody remember.

Three places to look, and all three are normal:

  * Steam's own builds, under `steamapps/common`, named "Proton 9.0" and such.
  * Community builds, under `compatibilitytools.d` — Proton-GE, Proton-EM and
    the rest. This is where ProtonPlus and protonup install to.
  * Heroic keeps its own copies, because it manages prefixes itself.

Steam libraries live on other drives as often as not, so the library folders
Steam knows about are consulted rather than assuming everything is in the home
directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Steam installations. `compatibilitytools.d` and `steamapps/common` hang off
#: each of these.
STEAM_ROOTS = (
    "~/.steam/steam",
    "~/.steam/root",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/data/Steam",
)

#: Heroic manages its own prefixes and keeps its own runners.
HEROIC_ROOTS = (
    "~/.config/heroic/tools/proton",
    "~/.var/app/com.heroicgameslauncher.hgl/config/heroic/tools/proton",
)


@dataclass(frozen=True)
class ProtonVersion:
    """One installed Proton build."""

    name: str
    path: Path
    #: 'steam' | 'custom' | 'heroic' — where it came from, which is worth
    #: showing: two builds can share a name across sources.
    source: str

    @property
    def label(self) -> str:
        if self.source == "custom":
            return self.name
        return f"{self.name}  ({self.source})"

    @property
    def runnable(self) -> bool:
        """Whether this looks like a real Proton rather than a stray folder."""
        return (self.path / "proton").is_file() or (self.path / "files").is_dir()


def _steam_library_folders() -> list[Path]:
    """Every `steamapps` Steam knows about, including other drives."""
    try:
        from rose_gamelab.sources.steam import SteamProvider

        return list(SteamProvider().library_folders())
    except Exception:
        logger.debug("could not read Steam's library folders", exc_info=True)
        return []


def installed() -> list[ProtonVersion]:
    """Every Proton build on this machine, best first.

    Community builds are listed before Steam's own, because somebody who has
    installed Proton-GE did it deliberately and is more likely to want it.
    Within a source, newest-looking name first — these sort usefully by name
    because the version is in it.
    """
    found: dict[str, ProtonVersion] = {}

    def consider(directory: Path, source: str) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return

        for entry in entries:
            if not entry.is_dir():
                continue
            version = ProtonVersion(name=entry.name, path=entry, source=source)
            if not version.runnable:
                continue
            # First one wins: the same build is often visible through two
            # paths at once, ~/.steam/root being a symlink to the real one.
            found.setdefault(entry.name, version)

    for root in STEAM_ROOTS:
        base = Path(root).expanduser()
        consider(base / "compatibilitytools.d", "custom")
        consider(base / "steamapps" / "common", "steam")

    for steamapps in _steam_library_folders():
        consider(Path(steamapps) / "common", "steam")

    for root in HEROIC_ROOTS:
        consider(Path(root).expanduser(), "heroic")

    def sort_key(version: ProtonVersion):
        # Custom builds first, then by name descending so the highest version
        # number is at the top.
        return (version.source != "custom", _descending(version.name))

    return sorted(
        (v for v in found.values() if _looks_like_proton(v.name)), key=sort_key
    )


def _looks_like_proton(name: str) -> bool:
    """`steamapps/common` holds games as well as Proton, so it is filtered."""
    lowered = name.lower()
    return "proton" in lowered or lowered.startswith("ge-")


def _descending(name: str) -> tuple:
    """Sort a version-bearing name newest-first, digits compared as numbers.

    Plain string ordering puts GE-Proton9 above GE-Proton10, which is the
    wrong build and exactly the one somebody is trying to pick.
    """
    import re

    parts = re.split(r"(\d+)", name)
    return tuple(
        (-int(part),) if part.isdigit() else (0, part.lower())
        for part in parts if part
    )


def find(name: Optional[str]) -> Optional[ProtonVersion]:
    """The installed build with this name, if it is still there."""
    if not name:
        return None
    return next((version for version in installed() if version.name == name), None)
