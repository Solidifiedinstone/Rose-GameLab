"""Telling folder games, disc images and plain ROMs apart.

The distinction matters because it decides how a game is MOVED. A disc image is
a file; a PS3 title is a directory that must stay intact, and moving the file an
emulator points at out of it destroys the dump.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core import folder_games
from rose_gamelab.core.media import (
    MediaKind,
    classify,
    describe,
    folder_game_for,
    summarise,
)


def make_file(path, data: bytes = b"\x00" * 16):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def ps3_folder(root, name="Demon's Souls (USA)"):
    game = root / name
    make_file(game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")
    make_file(game / "PS3_DISC.SFB")
    return game


# ── Files ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["game.iso", "game.chd", "game.cue", "game.rvz"])
def test_disc_images_are_recognised(tmp_path, name):
    assert classify(make_file(tmp_path / name)) is MediaKind.DISC_IMAGE


@pytest.mark.parametrize("name", ["game.sfc", "game.gba", "game.nes"])
def test_cartridge_dumps_are_plain_files(tmp_path, name):
    assert classify(make_file(tmp_path / name)) is MediaKind.FILE


def test_an_ambiguous_extension_defers_to_the_system(tmp_path):
    """.bin is a Mega Drive cartridge as often as it is a disc track."""
    rom = make_file(tmp_path / "Sonic.bin")

    assert classify(rom, system_id="megadrive") is MediaKind.FILE
    assert classify(rom, system_id="segacd") is MediaKind.DISC_IMAGE


def test_an_unambiguous_extension_ignores_a_wrong_system(tmp_path):
    """A .iso is a disc image whatever anyone claims it belongs to."""
    assert classify(make_file(tmp_path / "a.iso"), system_id="snes") is (
        MediaKind.DISC_IMAGE
    )


def test_a_playlist_is_not_a_disc(tmp_path):
    assert classify(make_file(tmp_path / "FF7.m3u")) is MediaKind.PLAYLIST


def test_extension_case_does_not_matter(tmp_path):
    assert classify(make_file(tmp_path / "GAME.ISO")) is MediaKind.DISC_IMAGE


# ── Folders ───────────────────────────────────────────────────────

def test_a_ps3_folder_is_a_folder_game(tmp_path):
    assert classify(ps3_folder(tmp_path)) is MediaKind.FOLDER


def test_a_folder_game_is_recognised_from_its_detection(tmp_path):
    """Callers holding scan results should not have to re-detect anything."""
    game = folder_games.detect(ps3_folder(tmp_path))
    assert classify(game) is MediaKind.FOLDER


def test_a_folder_of_isos_is_not_a_folder_game(tmp_path):
    """The dangerous mistake: this must never be moved as a single unit."""
    shelf = tmp_path / "PS2"
    make_file(shelf / "a.iso")
    make_file(shelf / "b.iso")

    assert classify(shelf) is not MediaKind.FOLDER


def test_only_folder_games_move_as_a_unit(tmp_path):
    assert classify(ps3_folder(tmp_path)).moves_as_a_unit
    assert not classify(make_file(tmp_path / "game.iso")).moves_as_a_unit


def test_a_file_inside_a_game_finds_its_folder(tmp_path):
    game = ps3_folder(tmp_path)

    found = folder_game_for(game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")

    assert found is not None and found.root == game


def test_a_loose_rom_belongs_to_no_folder_game(tmp_path):
    assert folder_game_for(make_file(tmp_path / "game.sfc")) is None


# ── Summaries ─────────────────────────────────────────────────────

def test_a_mixed_folder_is_counted_by_kind(tmp_path):
    ps3_folder(tmp_path, "Demon's Souls")
    ps3_folder(tmp_path, "Ni no Kuni")
    make_file(tmp_path / "Jak.iso")
    make_file(tmp_path / "Chrono Trigger.sfc")

    counts = summarise([
        tmp_path / "Demon's Souls", tmp_path / "Ni no Kuni",
        tmp_path / "Jak.iso", tmp_path / "Chrono Trigger.sfc",
    ])

    assert counts == {
        MediaKind.FOLDER: 2, MediaKind.DISC_IMAGE: 1, MediaKind.FILE: 1,
    }


def test_description_reads_as_english():
    assert describe({MediaKind.FOLDER: 2, MediaKind.DISC_IMAGE: 1}) == (
        "2 game folders, 1 disc image"
    )


def test_nothing_describes_as_nothing():
    assert describe({}) == ""
