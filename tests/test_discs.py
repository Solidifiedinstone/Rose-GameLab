"""Tests for multi-disc detection, grouping and playlist generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from rose_gamelab.core.discs import (
    DiscFile,
    GameGroup,
    filter_redundant_tracks,
    group_discs,
    normalise_title,
    parse_disc_number,
    sort_title,
    write_m3u,
)

# ── Disc marker parsing ───────────────────────────────────────────

@pytest.mark.parametrize("stem,expected_title,expected_disc", [
    ("Final Fantasy VII (USA) (Disc 1)", "Final Fantasy VII (USA)", 1),
    ("Final Fantasy VII (Disc 2 of 3)", "Final Fantasy VII", 2),
    ("Metal Gear Solid [CD1]", "Metal Gear Solid", 1),
    ("Some Game (Disk 2)", "Some Game", 2),
    ("Grandia (USA) (Disc A)", "Grandia (USA)", 1),
    ("Grandia (USA) (Disc B)", "Grandia (USA)", 2),
    ("Resident Evil - disc 3", "Resident Evil", 3),
])
def test_parses_disc_markers(stem, expected_title, expected_disc):
    title, disc, _label = parse_disc_number(stem)
    assert (title, disc) == (expected_title, expected_disc)


def test_single_disc_game_has_no_disc_number():
    title, disc, label = parse_disc_number("Chrono Trigger (USA)")
    assert (title, disc, label) == ("Chrono Trigger (USA)", None, None)


def test_does_not_mistake_a_number_in_the_title_for_a_disc():
    """'Disc' must actually be present — Sonic 3 is not disc 3 of Sonic."""
    _title, disc, _ = parse_disc_number("Sonic the Hedgehog 3 (USA)")
    assert disc is None


# ── Title normalisation ───────────────────────────────────────────

def test_strips_region_and_dump_tags():
    assert normalise_title("Super Metroid (USA) [!]") == "Super Metroid"


def test_sort_title_moves_leading_article():
    assert sort_title("The Legend of Zelda") == "legend of zelda"


def test_sort_title_handles_trailing_article_form():
    """Dat files store 'Legend of Zelda, The' — both must sort together."""
    assert sort_title("Legend of Zelda, The") == sort_title("The Legend of Zelda")


# ── Grouping ──────────────────────────────────────────────────────

def test_multi_disc_game_becomes_one_entry():
    paths = [
        Path("/roms/ps1/Final Fantasy VII (USA) (Disc 1).cue"),
        Path("/roms/ps1/Final Fantasy VII (USA) (Disc 2).cue"),
        Path("/roms/ps1/Final Fantasy VII (USA) (Disc 3).cue"),
    ]
    groups = group_discs(paths)

    assert len(groups) == 1
    assert groups[0].is_multi_disc
    assert [f.disc_number for f in groups[0].sorted_files] == [1, 2, 3]


def test_separate_games_stay_separate():
    groups = group_discs([
        Path("/roms/snes/Chrono Trigger (USA).sfc"),
        Path("/roms/snes/Super Metroid (USA).sfc"),
    ])
    assert len(groups) == 2


def test_primary_file_is_disc_one_regardless_of_input_order():
    groups = group_discs([
        Path("/roms/Game (Disc 3).cue"),
        Path("/roms/Game (Disc 1).cue"),
        Path("/roms/Game (Disc 2).cue"),
    ])
    assert groups[0].primary_file.name == "Game (Disc 1).cue"


def test_same_title_in_different_folders_does_not_merge():
    """Two unrelated rips both called 'Disc 1' must not become one game."""
    groups = group_discs([
        Path("/roms/game-a/Disc 1.cue"),
        Path("/roms/game-b/Disc 1.cue"),
    ])
    assert len(groups) == 2


def test_identical_titles_without_disc_numbers_are_not_merged():
    """Normalising tags away can collide unrelated dumps; don't silently merge."""
    groups = group_discs([
        Path("/roms/Game (USA).sfc"),
        Path("/roms/Game (Europe).sfc"),
    ])
    assert len(groups) == 2


# ── Cuesheet / track handling ─────────────────────────────────────

def test_cue_wins_over_its_bin_tracks():
    kept = filter_redundant_tracks([
        Path("/roms/Game.cue"),
        Path("/roms/Game.bin"),
    ])
    assert kept == [Path("/roms/Game.cue")]


def test_multi_track_bins_are_dropped():
    kept = filter_redundant_tracks([
        Path("/roms/Game.cue"),
        Path("/roms/Game (Track 1).bin"),
        Path("/roms/Game (Track 2).bin"),
    ])
    assert kept == [Path("/roms/Game.cue")]


def test_bin_without_a_cue_is_kept():
    kept = filter_redundant_tracks([Path("/roms/Game.bin")])
    assert kept == [Path("/roms/Game.bin")]


def test_single_disc_rip_is_one_game_not_five():
    groups = group_discs([
        Path("/roms/Tomb Raider (USA).cue"),
        Path("/roms/Tomb Raider (USA) (Track 1).bin"),
        Path("/roms/Tomb Raider (USA) (Track 2).bin"),
        Path("/roms/Tomb Raider (USA) (Track 3).bin"),
    ])
    assert len(groups) == 1


# ── Playlists ─────────────────────────────────────────────────────

def test_writes_m3u_for_multi_disc_game(tmp_path):
    files = []
    for disc in (1, 2, 3):
        path = tmp_path / f"FF7 (Disc {disc}).cue"
        path.write_text("")
        files.append(DiscFile(path=path, disc_number=disc))

    group = GameGroup(title="FF7", files=files)
    playlist = write_m3u(group, tmp_path)

    assert playlist is not None
    assert playlist.read_text().splitlines() == [
        "FF7 (Disc 1).cue",
        "FF7 (Disc 2).cue",
        "FF7 (Disc 3).cue",
    ]


def test_m3u_uses_relative_paths_so_the_library_can_move(tmp_path):
    path = tmp_path / "Game (Disc 1).cue"
    path.write_text("")
    other = tmp_path / "Game (Disc 2).cue"
    other.write_text("")

    group = GameGroup(title="Game", files=[
        DiscFile(path=path, disc_number=1),
        DiscFile(path=other, disc_number=2),
    ])
    playlist = write_m3u(group, tmp_path)

    assert not any(line.startswith("/") for line in playlist.read_text().splitlines())


def test_m3u_ends_with_a_newline(tmp_path):
    """Some emulators silently drop the last entry without a trailing newline."""
    files = [
        DiscFile(path=tmp_path / f"G (Disc {n}).cue", disc_number=n) for n in (1, 2)
    ]
    for f in files:
        f.path.write_text("")

    playlist = write_m3u(GameGroup(title="G", files=files), tmp_path)
    assert playlist.read_text().endswith("\n")


def test_no_playlist_for_single_disc_game(tmp_path):
    group = GameGroup(title="Solo", files=[DiscFile(path=tmp_path / "solo.sfc")])
    assert write_m3u(group, tmp_path) is None


def test_playlist_name_is_filesystem_safe(tmp_path):
    files = [
        DiscFile(path=tmp_path / f"x{n}.cue", disc_number=n) for n in (1, 2)
    ]
    for f in files:
        f.path.write_text("")

    playlist = write_m3u(GameGroup(title="Ratchet: Up/Down", files=files), tmp_path)
    assert "/" not in playlist.name
    assert ":" not in playlist.name
