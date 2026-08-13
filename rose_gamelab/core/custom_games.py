"""Games the user adds themselves.

Automatic detection covers Steam, GOG, Heroic, Lutris, emulators and ROM
folders, and it will still never cover everything: An Anime Game Launcher, a
game built from source, a DRM-free installer dropped in a folder, a shell
script someone wrote to launch a game with the right environment.

Rather than leaving those out of the library — which defeats the point of a
launcher that shows every game in one place — anything runnable can be added
by hand and behaves like any other entry from then on: art, playtime,
collections, Big Picture.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from rose_gamelab.core.desktop_entries import DesktopApp, command_parts
from rose_gamelab.core.library import Library

logger = logging.getLogger(__name__)

#: Source id every hand-added game belongs to, so they can be listed together
#: and are never removed by a rescan of somewhere else.
CUSTOM_SOURCE = "custom"


def register_source(library: Library) -> None:
    library.register_source(
        CUSTOM_SOURCE, name="Added by you", type="custom", path=None, system=None
    )


def add_custom_game(
    library: Library,
    *,
    title: str,
    command: str,
    system: str = "pc",
    args: Optional[str] = None,
    working_dir: Optional[str] = None,
    cover: Optional[Path] = None,
    label: str = "Play",
) -> int:
    """Add one hand-entered game. Returns its library id.

    `command` is what actually runs. It is stored as given rather than being
    resolved to an absolute path, because entries like
    `env GDK_BACKEND=wayland an-anime-game-launcher` are meaningful as a whole
    and rewriting them breaks them.
    """
    title = title.strip()
    command = command.strip()

    if not title:
        raise ValueError("A game needs a name.")
    if not command:
        raise ValueError("A game needs something to launch.")

    register_source(library)

    game_id = library.add_game(title=title, system=system, source_id=CUSTOM_SOURCE)

    library.add_launch_option(
        game_id,
        kind="custom",
        target=command,
        label=label,
        args=args or None,
        working_dir=working_dir or None,
        is_primary=True,
    )

    if cover is not None:
        set_cover_from_file(library, game_id, cover)

    return game_id


def add_from_desktop_entry(
    library: Library,
    app: DesktopApp,
    *,
    system: str = "pc",
    title: Optional[str] = None,
    use_icon_as_cover: bool = True,
) -> int:
    """Add an installed application as a game, icon and all."""
    cover = app.icon_file() if use_icon_as_cover else None

    return add_custom_game(
        library,
        title=title or app.name,
        command=app.command,
        system=system,
        cover=cover,
    )


def set_cover_from_file(library: Library, game_id: int, source: Path) -> Optional[Path]:
    """Copy an image in as a game's cover.

    Copied rather than referenced: the original may be a theme icon that
    changes with the user's icon theme, or a file they move later, and a
    library full of broken image links is worse than no art.
    """
    source = Path(source)
    if not source.is_file():
        return None

    from rose_gamelab.metadata.cache import ArtCache

    cache = ArtCache()
    stored = cache.store_file(f"game:{game_id}", "cover", source)

    if stored is None:
        # An unrecognised image type, so keep the file but store it plainly
        # rather than pretending it worked.
        logger.debug("could not store %s through the art cache", source)
        return None

    library.update_game(game_id, cover_path=str(stored))
    return stored


def command_is_runnable(command: str) -> bool:
    """Whether the first word of a command exists as a program or a file.

    Used to warn before saving, never to block it: a command may be valid on a
    machine this one cannot see, and the user knows their system.
    """
    parts = command_parts(command)
    if not parts:
        return False

    # `env VAR=value program` — the program is the first part that is not an
    # assignment.
    if parts[0] == "env":
        parts = [p for p in parts[1:] if "=" not in p.split(" ")[0]] or parts[1:]
        if not parts:
            return False

    first = parts[0]
    if "/" in first:
        return Path(first).expanduser().is_file()

    return shutil.which(first) is not None
