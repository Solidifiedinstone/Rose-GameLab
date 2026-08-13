"""Finding screenshots emulators already took.

Every emulator picks a different directory and a different filename, so a shot
taken three months ago may as well not exist. These cover the matching, which is
the whole difficulty: loose enough to catch the timestamp suffixes emulators
append, tight enough not to claim another game's shots.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core.screenshots import find_for_game, match_key

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def shots(tmp_path):
    def make(*names: str):
        for name in names:
            (tmp_path / name).write_bytes(PNG)
        return tmp_path
    return make


def names_found(directory, *names) -> list[str]:
    return [s.name for s in find_for_game(names, directories=[directory])]


# ── Matching ──────────────────────────────────────────────────────

@pytest.mark.parametrize("filename", [
    "Super Metroid.png",
    "Super Metroid (USA)-260102-140233.png",
    "Super_Metroid-001.png",
    "supermetroid.jpg",
    "Super Metroid [!]_2026-01-02.png",
])
def test_the_shapes_emulators_actually_write(shots, filename):
    directory = shots(filename)
    assert names_found(directory, "Super Metroid") == [filename]


def test_another_games_shots_are_not_claimed(shots):
    directory = shots("Super Mario World-01.png", "Metroid Prime-01.png")
    assert names_found(directory, "Super Metroid") == []


def test_a_folder_game_matches_on_its_folder_name(shots):
    """Emulators name the file after whatever they were launched with."""
    directory = shots("BLES00932-001.png")
    assert names_found(directory, "Demon's Souls", "BLES00932") == ["BLES00932-001.png"]


def test_non_images_are_ignored(shots):
    directory = shots("Super Metroid.sav", "Super Metroid.png")
    assert names_found(directory, "Super Metroid") == ["Super Metroid.png"]


def test_a_very_short_title_matches_nothing(shots):
    """Otherwise a two-letter game claims every screenshot on the disk."""
    directory = shots("Go-001.png", "Anything At All.png")
    assert names_found(directory, "Go") == []


def test_newest_first(shots, tmp_path):
    import os
    import time

    directory = shots("Super Metroid-old.png", "Super Metroid-new.png")
    old = time.time() - 10_000
    os.utime(directory / "Super Metroid-old.png", (old, old))

    assert names_found(directory, "Super Metroid")[0] == "Super Metroid-new.png"


def test_nested_directories_are_searched(shots, tmp_path):
    """RetroArch nests one directory per system."""
    nested = tmp_path / "Nintendo - SNES"
    nested.mkdir()
    (nested / "Super Metroid-1.png").write_bytes(PNG)

    assert names_found(tmp_path, "Super Metroid") == ["Super Metroid-1.png"]


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert find_for_game(["Anything"], directories=[tmp_path / "nope"]) == []


def test_match_key_discards_what_does_not_identify_a_game():
    assert match_key("Super Metroid (USA)") == match_key("super_metroid")
    assert match_key("Super Metroid") != match_key("Super Mario World")
