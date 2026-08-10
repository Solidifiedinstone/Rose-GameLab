"""Tests for the GOG source provider.

These build synthetic GOG install directories in tmp_path, laid out the way the
official Linux .sh installer lays them out, so they pass on any machine and in
CI.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from rose_gamelab.sources.gog import GOGProvider, parse_gameinfo, parse_info_file

WITCHER_INFO = {
    "buildId": "52092233255111679",
    "clientId": "50060172864950310",
    "gameId": "1207658924",
    "rootGameId": "1207658924",
    "language": "English",
    "name": "The Witcher: Enhanced Edition",
    "osBitness": ["64"],
    "playTasks": [
        {
            "category": "game",
            "isPrimary": True,
            "languages": ["en-US"],
            "name": "The Witcher",
            "path": "System/witcher.exe",
            "type": "FileTask",
        },
        {
            "category": "tool",
            "name": "Configure",
            "path": "System/config.exe",
            "type": "FileTask",
        },
    ],
    "version": 1,
}

# The plain-text file the Linux installer writes: name, version, build,
# language, gameId, rootGameId — one per line, no keys.
GAMEINFO_TEXT = "The Witcher: Enhanced Edition\n1.5\ngog-3\nenglish\n1207658924\n1207658924\n"


def make_game(root, dir_name, *, info=WITCHER_INFO, gameinfo=None, executable=True):
    """Create a GOG install directory the way the .sh installer would."""
    game_dir = root / dir_name
    (game_dir / "game").mkdir(parents=True)
    (game_dir / "support").mkdir()

    start = game_dir / "start.sh"
    start.write_text("#!/bin/bash\nexec ./game/start\n")
    if executable:
        start.chmod(start.stat().st_mode | stat.S_IXUSR)

    if info is not None:
        (game_dir / "game" / f"goggame-{info['gameId']}.info").write_text(json.dumps(info))
    if gameinfo is not None:
        (game_dir / "gameinfo").write_text(gameinfo)

    return game_dir


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "games"
    root.mkdir()
    return root


# ── Metadata files ────────────────────────────────────────────────

def test_parses_goggame_info(library):
    game_dir = make_game(library, "witcher")
    info = parse_info_file(game_dir / "game" / "goggame-1207658924.info")

    assert info["name"] == "The Witcher: Enhanced Edition"
    assert info["game_id"] == "1207658924"
    assert info["is_base_game"] is True
    assert len(info["play_tasks"]) == 2


def test_info_id_comes_from_the_filename(library):
    """Regression: DLC .info files have carried the base game's gameId body.

    GOG's own tooling keys on the filename, so this does too.
    """
    game_dir = make_game(library, "witcher")
    odd = game_dir / "game" / "goggame-9999.info"
    odd.write_text(json.dumps(dict(WITCHER_INFO, gameId="1207658924")))

    assert parse_info_file(odd)["game_id"] == "9999"


def test_malformed_info_returns_none(library):
    game_dir = make_game(library, "witcher", info=None)
    broken = game_dir / "game" / "goggame-1.info"
    broken.write_text("{ not json")
    assert parse_info_file(broken) is None


def test_parses_plain_text_gameinfo(library):
    game_dir = make_game(library, "witcher", gameinfo=GAMEINFO_TEXT)
    fields = parse_gameinfo(game_dir / "gameinfo")

    assert fields["name"] == "The Witcher: Enhanced Edition"
    assert fields["game_id"] == "1207658924"
    assert fields["version"] == "1.5"


def test_missing_gameinfo_is_not_an_error(tmp_path):
    assert parse_gameinfo(tmp_path / "gameinfo") == {}


# ── Recognising an install directory ──────────────────────────────

def test_recognises_installer_layout(library):
    assert GOGProvider.is_game_dir(make_game(library, "witcher")) is True


def test_directory_without_start_sh_is_not_a_game(library):
    game_dir = make_game(library, "witcher")
    (game_dir / "start.sh").unlink()
    assert GOGProvider.is_game_dir(game_dir) is False


def test_start_sh_alone_is_not_a_gog_game(tmp_path):
    """Plenty of non-GOG software ships a start.sh; the layout is the evidence."""
    other = tmp_path / "some_tool"
    other.mkdir()
    (other / "start.sh").write_text("#!/bin/sh\n")
    assert GOGProvider.is_game_dir(other) is False


def test_folder_name_is_never_the_evidence(tmp_path):
    """Regression: the old importer looked for a 'GOG Games' folder and for
    <dir>/<dir>.sh, neither of which any GOG installer produces."""
    fake = tmp_path / "GOG Games" / "Not A Game"
    fake.mkdir(parents=True)
    (fake / "Not A Game.sh").write_text("#!/bin/sh\n")

    assert GOGProvider(gog_root=str(tmp_path / "GOG Games")).discover() == []


# ── Discovery ─────────────────────────────────────────────────────

def test_discovers_game(library):
    make_game(library, "witcher")
    games = GOGProvider(gog_root=str(library)).discover()

    assert len(games) == 1
    assert games[0].name == "The Witcher: Enhanced Edition"
    assert games[0].is_gog is True
    assert games[0].metadata["gog_game_id"] == "1207658924"


def test_launches_start_sh(library):
    """start.sh sets LD_LIBRARY_PATH and the working directory; the binary
    underneath it does not run correctly on its own."""
    game_dir = make_game(library, "witcher")
    game = GOGProvider(gog_root=str(library)).discover()[0]
    assert game.path == str(game_dir / "start.sh")


def test_play_tasks_are_metadata_not_launch_targets(library):
    """The .info ships Windows exe paths even in Linux packages."""
    make_game(library, "witcher")
    game = GOGProvider(gog_root=str(library)).discover()[0]

    assert not game.path.endswith(".exe")
    assert game.metadata["play_tasks"][0]["path"] == "System/witcher.exe"


def test_name_prefers_info_over_directory(library):
    make_game(library, "the_witcher_enhanced_edition")
    game = GOGProvider(gog_root=str(library)).discover()[0]

    assert game.name == "The Witcher: Enhanced Edition"
    assert game.metadata["name_source"] == "goggame_info"


def test_falls_back_to_gameinfo_then_directory(library):
    make_game(library, "witcher_a", info=None, gameinfo=GAMEINFO_TEXT)
    make_game(library, "Unknown Game", info=None)

    by_name = {g.name: g for g in GOGProvider(gog_root=str(library)).discover()}
    assert by_name["The Witcher: Enhanced Edition"].metadata["name_source"] == "gameinfo"
    assert by_name["Unknown Game"].metadata["name_source"] == "directory"


def test_dlc_info_does_not_become_the_title(library):
    """A game with DLC has several .info files; only one is the base game."""
    game_dir = make_game(library, "witcher")
    (game_dir / "game" / "goggame-1207658925.info").write_text(json.dumps({
        "gameId": "1207658925",
        "rootGameId": "1207658924",
        "name": "The Witcher: Soundtrack",
    }))

    game = GOGProvider(gog_root=str(library)).discover()[0]
    assert game.name == "The Witcher: Enhanced Edition"
    assert game.metadata["dlc_ids"] == ["1207658925"]


def test_does_not_descend_into_a_games_own_subtree(library):
    """Regression: a recursive scan finds a game's bundled tools as games."""
    game_dir = make_game(library, "witcher")
    nested = game_dir / "game" / "extra"
    nested.mkdir()
    (nested / "start.sh").write_text("#!/bin/sh\n")
    (nested / "game").mkdir()

    assert len(GOGProvider(gog_root=str(library)).discover()) == 1


def test_finds_games_nested_one_level_deeper(library):
    make_game(library / "rpgs", "witcher")
    assert len(GOGProvider(gog_root=str(library)).discover()) == 1


def test_root_itself_may_be_the_game(library):
    game_dir = make_game(library, "witcher")
    assert len(GOGProvider(gog_root=str(game_dir)).discover()) == 1


def test_non_executable_start_sh_is_flagged_not_hidden(library):
    """A chmod problem is worth reporting; pretending the game is absent is not."""
    make_game(library, "witcher", executable=False)
    game = GOGProvider(gog_root=str(library)).discover()[0]

    assert game.metadata["start_sh_executable"] is False


def test_executable_start_sh_is_flagged(library):
    make_game(library, "witcher")
    game = GOGProvider(gog_root=str(library)).discover()[0]
    assert game.metadata["start_sh_executable"] is True


def test_ids_are_stable_and_unique(library):
    make_game(library, "witcher")
    make_game(library, "other", info=dict(WITCHER_INFO, gameId="9", rootGameId="9", name="Other"))

    ids = {g.id for g in GOGProvider(gog_root=str(library)).discover()}
    assert ids == {"gog:1207658924", "gog:9"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_directory_is_not_fatal(library):
    make_game(library, "witcher")
    locked = library / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        games = GOGProvider(gog_root=str(library)).discover()
    finally:
        locked.chmod(0o755)

    assert [g.name for g in games] == ["The Witcher: Enhanced Edition"]


# ── Provider interface ────────────────────────────────────────────

def test_missing_root_is_not_an_error(tmp_path):
    provider = GOGProvider(gog_root=str(tmp_path / "nope"))
    assert provider.validate() is False
    assert provider.discover() == []


def test_empty_but_real_root_is_a_valid_source(library):
    provider = GOGProvider(gog_root=str(library))
    assert provider.validate() is True
    assert provider.discover() == []


def test_explicit_root_is_not_replaced_by_a_default(tmp_path):
    """A typo'd path must not silently fall back to ~/GOG Games."""
    provider = GOGProvider(gog_root=str(tmp_path / "typo"))
    assert provider.roots == [tmp_path / "typo"]


def test_source_def(library):
    definition = GOGProvider(gog_root=str(library)).get_def()
    assert (definition.id, definition.type, definition.system) == ("gog", "gog", "pc")
