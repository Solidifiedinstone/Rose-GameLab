"""Games the user adds by hand, and reading installed desktop entries."""

from __future__ import annotations

import pytest

from rose_gamelab.core import custom_games, desktop_entries
from rose_gamelab.core.launcher import build_command
from rose_gamelab.core.library import Library
from rose_gamelab.core.profiles import LaunchProfile
from rose_gamelab.db.database import Database

AAGL = """[Desktop Entry]
Name=An Anime Game Launcher
Comment=Play the game
Exec=env GDK_BACKEND=wayland an-anime-game-launcher %U
Icon=an-anime-game-launcher
Type=Application
Categories=Game;
"""


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "library.db")
    yield Library(db)
    db.close()


def write_entry(directory, name: str, text: str):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text)
    return path


# ── Desktop entries ───────────────────────────────────────────────

def test_reads_name_command_and_icon(tmp_path):
    path = write_entry(tmp_path, "aagl.desktop", AAGL)

    app = desktop_entries.parse_entry(path)

    assert app is not None
    assert app.name == "An Anime Game Launcher"
    assert app.command == "env GDK_BACKEND=wayland an-anime-game-launcher"
    assert app.icon == "an-anime-game-launcher"
    assert app.is_game


def test_field_codes_are_stripped(tmp_path):
    path = write_entry(tmp_path, "a.desktop", AAGL.replace("%U", "%f %i %c"))

    app = desktop_entries.parse_entry(path)

    assert app.command == "env GDK_BACKEND=wayland an-anime-game-launcher"


def test_hidden_and_nodisplay_entries_are_skipped(tmp_path):
    hidden = write_entry(
        tmp_path, "h.desktop", AAGL + "NoDisplay=true\n"
    )
    assert desktop_entries.parse_entry(hidden) is None

    other = write_entry(tmp_path, "i.desktop", AAGL + "Hidden=true\n")
    assert desktop_entries.parse_entry(other) is None


def test_non_applications_are_skipped(tmp_path):
    link = write_entry(
        tmp_path, "l.desktop",
        "[Desktop Entry]\nName=A link\nType=Link\nURL=https://example.com\n",
    )
    assert desktop_entries.parse_entry(link) is None


def test_entries_without_a_command_are_skipped(tmp_path):
    path = write_entry(tmp_path, "n.desktop", "[Desktop Entry]\nName=No exec\nType=Application\n")
    assert desktop_entries.parse_entry(path) is None


def test_only_the_main_section_is_read(tmp_path):
    """Desktop actions repeat Name= and Exec= with different meanings."""
    path = write_entry(
        tmp_path, "a.desktop",
        AAGL + "\n[Desktop Action new]\nName=New Window\nExec=other-program\n",
    )

    app = desktop_entries.parse_entry(path)

    assert app.name == "An Anime Game Launcher"
    assert "other-program" not in app.command


def test_localised_names_do_not_override_the_default(tmp_path):
    path = write_entry(tmp_path, "a.desktop", AAGL + "Name[de]=Ein Anime Spiel\n")

    assert desktop_entries.parse_entry(path).name == "An Anime Game Launcher"


def test_settings_panels_are_marked_as_not_games(tmp_path):
    settings = desktop_entries.parse_entry(write_entry(
        tmp_path, "s.desktop",
        "[Desktop Entry]\nName=Printers\nExec=printers\nType=Application\n"
        "Categories=Settings;System;\n",
    ))

    assert settings.is_obviously_not_a_game
    assert not settings.is_game


def test_an_uncategorised_launcher_is_still_offered_in_the_full_list(tmp_path, monkeypatch):
    """A hand-installed launcher often sets no category at all."""
    write_entry(
        tmp_path / "applications", "u.desktop",
        "[Desktop Entry]\nName=Some Game\nExec=game\nType=Application\n",
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "empty"))

    assert "Some Game" in [a.name for a in desktop_entries.installed_apps()]
    assert "Some Game" not in [
        a.name for a in desktop_entries.installed_apps(games_only=True)
    ]


def test_installed_apps_reads_the_xdg_directories(tmp_path, monkeypatch):
    write_entry(tmp_path / "applications", "aagl.desktop", AAGL)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "empty"))

    names = [a.name for a in desktop_entries.installed_apps()]

    assert "An Anime Game Launcher" in names


# ── Adding games ──────────────────────────────────────────────────

def test_add_custom_game(library):
    game_id = custom_games.add_custom_game(
        library, title="Genshin Impact",
        command="env GDK_BACKEND=wayland an-anime-game-launcher",
    )

    game = library.get(game_id)
    assert game.title == "Genshin Impact"
    assert game.system == "pc"

    (option,) = library.launch_options_for(game_id)
    assert option["kind"] == "custom"
    assert option["target"] == "env GDK_BACKEND=wayland an-anime-game-launcher"


def test_a_custom_game_command_is_split_properly_at_launch():
    """The bug this guards: exec'ing the whole line as one filename."""
    command = build_command(
        kind="custom",
        target="env GDK_BACKEND=wayland an-anime-game-launcher",
        profile=LaunchProfile(id=1, name="Default"),
    )

    assert command == ["env", "GDK_BACKEND=wayland", "an-anime-game-launcher"]


def test_an_executable_path_with_spaces_is_not_split(tmp_path):
    program = tmp_path / "My Game.sh"
    program.write_text("#!/bin/sh\n")

    command = build_command(
        kind="custom", target=str(program),
        profile=LaunchProfile(id=1, name="Default"),
    )

    assert command == [str(program)]


def test_add_from_desktop_entry(library, tmp_path):
    app = desktop_entries.parse_entry(write_entry(tmp_path, "a.desktop", AAGL))

    game_id = custom_games.add_from_desktop_entry(library, app, use_icon_as_cover=False)

    assert library.get(game_id).title == "An Anime Game Launcher"


def test_custom_games_share_one_source(library):
    custom_games.add_custom_game(library, title="A", command="a")
    custom_games.add_custom_game(library, title="B", command="b")

    sources = {row["id"] for row in library.list_sources()}
    assert custom_games.CUSTOM_SOURCE in sources


def test_a_game_needs_a_name_and_a_command(library):
    with pytest.raises(ValueError):
        custom_games.add_custom_game(library, title="  ", command="a")

    with pytest.raises(ValueError):
        custom_games.add_custom_game(library, title="A", command="   ")


def test_runnable_check(tmp_path):
    assert custom_games.command_is_runnable("sh")
    assert custom_games.command_is_runnable("env FOO=bar sh")
    assert not custom_games.command_is_runnable("definitely-not-a-real-program-xyz")
    assert not custom_games.command_is_runnable("")

    program = tmp_path / "game.sh"
    program.write_text("#!/bin/sh\n")
    assert custom_games.command_is_runnable(str(program))
    assert not custom_games.command_is_runnable(str(tmp_path / "missing.sh"))


def test_cover_is_copied_not_referenced(library, tmp_path, monkeypatch):
    """The original may be a theme icon that changes, or a file they move."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    icon = tmp_path / "icon.png"
    # A minimal but real PNG header, so the cache recognises the type.
    icon.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )

    game_id = custom_games.add_custom_game(
        library, title="Genshin", command="sh", cover=icon,
    )

    cover = library.get(game_id).cover_path
    assert cover is not None
    assert str(icon) != cover


def test_a_missing_cover_is_not_fatal(library, tmp_path):
    game_id = custom_games.add_custom_game(
        library, title="Genshin", command="sh", cover=tmp_path / "nope.png",
    )

    assert library.get(game_id).cover_path is None
