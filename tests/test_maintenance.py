"""Finding what has quietly gone wrong in a library.

Two rules are load-bearing here and both are asserted: inspection changes
nothing, and repair never touches a game or a file the user owns. A maintenance
command that deletes somebody's games is a maintenance command nobody should
run, so the boundary between "reported" and "repaired" is tested rather than
merely documented.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core import maintenance
from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database


@pytest.fixture
def library(tmp_path):
    database = Database(tmp_path / "library.db")
    yield Library(database)
    database.close()


@pytest.fixture
def art(tmp_path):
    folder = tmp_path / "artwork"
    folder.mkdir()
    return folder


def add(library, title, *, system="snes", path=None, missing=False):
    path = path or f"/roms/{title}.sfc"
    game_id = library.add_game(title=title, system=system, path=path)
    library.add_file(game_id, path)
    if missing:
        library.db.execute(
            "UPDATE game_files SET missing = 1 WHERE game_id = ?", (game_id,)
        )
    return game_id


# ── Inspection ────────────────────────────────────────────────────

def test_a_clean_library_reports_nothing(library, art):
    add(library, "Fine")
    report = maintenance.inspect(library, art_directory=art)

    assert [f.kind for f in report.findings if f.kind != "no_emulator"] == []


def test_missing_files_are_found(library, art):
    add(library, "Gone", missing=True)

    finding = maintenance.inspect(library, art_directory=art).of_kind("missing_files")

    assert finding is not None
    assert finding.count == 1


def test_missing_files_are_never_repairable(library, art):
    """An unplugged drive must grey a game out, not erase it."""
    add(library, "On A USB Drive", missing=True)

    finding = maintenance.inspect(library, art_directory=art).of_kind("missing_files")

    assert not finding.repairable
    assert "drive" in finding.summary


def test_duplicates_are_found_but_not_repairable(library, art):
    """Which copy to keep depends on which one launches better."""
    add(library, "Chrono Trigger", path="/roms/ct.sfc")
    add(library, "chrono  trigger!", path="/roms/ct2.sfc")

    finding = maintenance.inspect(library, art_directory=art).of_kind("duplicates")

    assert finding is not None
    assert finding.count == 2
    assert not finding.repairable


def test_the_same_title_on_two_systems_is_not_a_duplicate(library, art):
    """Tomb Raider on PS1 and on PC are genuinely different entries."""
    add(library, "Tomb Raider", system="ps1", path="/roms/tr.bin")
    add(library, "Tomb Raider", system="pc", path="/games/tr.exe")

    assert maintenance.inspect(library, art_directory=art).of_kind("duplicates") is None


def test_orphaned_artwork_is_found(library, art):
    game_id = add(library, "Has Art")
    kept = art / "kept.jpg"
    kept.write_bytes(b"x" * 100)
    (art / "nobody-wants-this.jpg").write_bytes(b"y" * 500)
    library.db.execute(
        "UPDATE games SET cover_path = ? WHERE id = ?", (str(kept), game_id)
    )

    finding = maintenance.inspect(library, art_directory=art).of_kind("orphaned_art")

    assert finding is not None
    assert finding.count == 1
    assert finding.repairable
    assert "nobody-wants-this" in finding.paths[0]


def test_empty_collections_are_found(library, art):
    library.create_collection("Empty")

    finding = maintenance.inspect(
        library, art_directory=art
    ).of_kind("empty_collections")

    assert finding is not None
    assert finding.repairable


def test_inspection_changes_nothing(library, art):
    add(library, "Gone", missing=True)
    (art / "orphan.jpg").write_bytes(b"x")
    library.create_collection("Empty")

    maintenance.inspect(library, art_directory=art)

    assert len(library.list_games()) == 1
    assert (art / "orphan.jpg").exists()
    assert len(library.list_collections()) == 1


# ── Repair ────────────────────────────────────────────────────────

def test_repair_deletes_only_unused_artwork(library, art):
    game_id = add(library, "Has Art")
    kept = art / "kept.jpg"
    kept.write_bytes(b"x" * 100)
    orphan = art / "orphan.jpg"
    orphan.write_bytes(b"y" * 500)
    library.db.execute(
        "UPDATE games SET cover_path = ? WHERE id = ?", (str(kept), game_id)
    )

    report = maintenance.inspect(library, art_directory=art)
    result = maintenance.repair(library, report)

    assert result.removed_art == 1
    assert not orphan.exists()
    assert kept.exists()


def test_repair_never_removes_games(library, art):
    """The whole point: this must be safe to run without thinking."""
    add(library, "Gone", missing=True)
    add(library, "Duplicate", path="/roms/a.sfc")
    add(library, "Duplicate", path="/roms/b.sfc")

    report = maintenance.inspect(library, art_directory=art)
    maintenance.repair(library, report)

    assert len(library.list_games()) == 3


def test_repair_removes_empty_collections(library, art):
    library.create_collection("Empty")
    full = library.create_collection("Full")
    game_id = add(library, "A Game")
    library.add_to_collection(full, game_id)

    report = maintenance.inspect(library, art_directory=art)
    result = maintenance.repair(library, report)

    assert result.removed_collections == 1
    assert [c["name"] for c in library.list_collections()] == ["Full"]


def test_repair_can_be_limited_to_one_kind(library, art):
    library.create_collection("Empty")
    (art / "orphan.jpg").write_bytes(b"y")

    report = maintenance.inspect(library, art_directory=art)
    result = maintenance.repair(library, report, kinds={"orphaned_art"})

    assert result.removed_art == 1
    assert result.removed_collections == 0
    assert len(library.list_collections()) == 1


def test_repairing_a_clean_library_says_so(library, art):
    add(library, "Fine")
    report = maintenance.inspect(library, art_directory=art)

    assert "Nothing needed fixing" in maintenance.repair(library, report).summary


def test_an_unreadable_art_directory_is_not_an_error(library, tmp_path):
    assert maintenance.inspect(library, art_directory=tmp_path / "nope") is not None


def test_a_hand_built_library_is_not_called_broken(library, art):
    """Games added by hand have no source and never had one. Flagging that
    would report an entirely hand-built library as damaged."""
    add(library, "Added By Hand")

    assert maintenance.inspect(library, art_directory=art).of_kind("orphaned_games") is None


def test_games_left_by_a_removed_source_are_reported(library, art):
    library.register_source("steam", name="Steam", type="steam")
    add(library, "Left Behind")

    finding = maintenance.inspect(library, art_directory=art).of_kind("orphaned_games")

    assert finding is not None
