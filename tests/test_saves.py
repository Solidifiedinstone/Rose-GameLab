"""Tests for save/state discovery, indexing, backup and restore."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from rose_gamelab.core.library import Library
from rose_gamelab.core.saves import (
    SaveFile,
    SaveLocation,
    SaveManager,
    _slot_from_name,
    find_saves_beside_rom,
    glob_escape,
    match_save_to_game,
    scan_location,
)
from rose_gamelab.db.database import Database


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "library.db")
    yield Library(db)
    db.close()


@pytest.fixture
def manager(library, tmp_path):
    return SaveManager(library, backup_root=tmp_path / "backups")


def write(path: Path, content: bytes = b"savedata") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ── Slot parsing ──────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("game.state", 0),
    ("game.state1", 1),
    ("game.state12", 12),
    ("game.ss0", 0),
    ("game.ss3", 3),
    ("game.srm", None),
    ("game.sav", None),
])
def test_parses_state_slot(name, expected):
    assert _slot_from_name(name) == expected


# ── Glob escaping ─────────────────────────────────────────────────

def test_dump_tags_are_escaped():
    """ROM names contain [!] and (U); brackets are glob syntax."""
    assert glob_escape("Game [!]") == "Game [[]![]]"


def test_escaped_stem_matches_literally(tmp_path):
    write(tmp_path / "Game [!].srm")
    write(tmp_path / "Game X.srm")

    matches = list(tmp_path.glob(f"{glob_escape('Game [!]')}.srm"))
    assert [p.name for p in matches] == ["Game [!].srm"]


# ── Central location scanning ─────────────────────────────────────

def test_scans_a_central_directory(tmp_path):
    write(tmp_path / "saves" / "Chrono Trigger.srm")
    write(tmp_path / "saves" / "notes.txt")

    location = SaveLocation("retroarch", "save", tmp_path / "saves", ("*.srm",))
    found = list(scan_location(location))

    assert [s.path.name for s in found] == ["Chrono Trigger.srm"]
    assert found[0].emulator == "retroarch"
    assert found[0].kind == "save"


def test_state_scan_records_slots(tmp_path):
    write(tmp_path / "states" / "Game.state")
    write(tmp_path / "states" / "Game.state1")

    location = SaveLocation(
        "retroarch", "state", tmp_path / "states", ("*.state", "*.state[0-9]")
    )
    slots = sorted(s.slot for s in scan_location(location))

    assert slots == [0, 1]


def test_missing_directory_yields_nothing(tmp_path):
    location = SaveLocation("retroarch", "save", tmp_path / "nope", ("*.srm",))
    assert list(scan_location(location)) == []


def test_beside_rom_locations_are_skipped_centrally(tmp_path):
    location = SaveLocation("mgba", "save", tmp_path, ("*.sav",), beside_rom=True)
    write(tmp_path / "game.sav")

    assert list(scan_location(location)) == []


# ── Saves beside the ROM ──────────────────────────────────────────

def test_finds_save_next_to_its_rom(tmp_path):
    rom = write(tmp_path / "Pokemon Emerald.gba")
    write(tmp_path / "Pokemon Emerald.sav")

    found = list(find_saves_beside_rom(rom))
    assert [s.path.name for s in found] == ["Pokemon Emerald.sav"]


def test_does_not_attribute_another_games_save(tmp_path):
    """A folder of 200 ROMs must not give every save to every game."""
    rom = write(tmp_path / "Game A.gba")
    write(tmp_path / "Game A.sav")
    write(tmp_path / "Game B.sav")

    found = list(find_saves_beside_rom(rom))
    assert [s.path.name for s in found] == ["Game A.sav"]


def test_handles_rom_names_with_dump_tags(tmp_path):
    rom = write(tmp_path / "Super Metroid (USA) [!].sfc")
    write(tmp_path / "Super Metroid (USA) [!].srm")

    found = list(find_saves_beside_rom(rom))
    assert len(found) == 1


# ── Matching saves to games ───────────────────────────────────────

def test_matches_by_normalised_title():
    save = SaveFile(
        path=Path("/saves/Chrono Trigger (USA).srm"), kind="save",
        emulator="retroarch", size_bytes=8,
        modified_at=datetime.now(timezone.utc),
    )
    assert match_save_to_game(save, {"chrono trigger": 7}) == 7


def test_unmatched_save_returns_none():
    """Guessing wrong here means restoring onto the wrong game."""
    save = SaveFile(
        path=Path("/saves/Mystery.srm"), kind="save", emulator="retroarch",
        size_bytes=8, modified_at=datetime.now(timezone.utc),
    )
    assert match_save_to_game(save, {"chrono trigger": 7}) is None


# ── Indexing ──────────────────────────────────────────────────────

def test_indexes_saves_found_beside_roms(tmp_path, library, manager):
    rom = write(tmp_path / "Pokemon Emerald.gba")
    write(tmp_path / "Pokemon Emerald.sav")

    game_id = library.add_game(title="Pokemon Emerald", system="gba")
    library.add_file(game_id, rom)

    assert manager.index() >= 1
    assert len(manager.saves_for(game_id)) >= 1


def test_reindexing_updates_rather_than_duplicates(tmp_path, library, manager):
    rom = write(tmp_path / "Game.gba")
    save = write(tmp_path / "Game.sav", b"small")

    game_id = library.add_game(title="Game", system="gba")
    library.add_file(game_id, rom)

    manager.index()
    save.write_bytes(b"much longer save data")
    manager.index()

    rows = manager.saves_for(game_id)
    assert len(rows) == 1
    assert rows[0]["size_bytes"] == len(b"much longer save data")


def test_saves_filter_by_kind(tmp_path, library, manager):
    rom = write(tmp_path / "Game.gba")
    write(tmp_path / "Game.sav")
    write(tmp_path / "Game.ss0")

    game_id = library.add_game(title="Game", system="gba")
    library.add_file(game_id, rom)
    manager.index()

    assert len(manager.saves_for(game_id, kind="save")) == 1
    assert len(manager.saves_for(game_id, kind="state")) == 1


def test_attaching_an_unmatched_save_by_hand(tmp_path, library, manager):
    orphan = write(tmp_path / "Mystery.srm")
    game_id = library.add_game(title="Some Game", system="snes")

    manager.attach(orphan, game_id)

    assert [Path(r["path"]).name for r in manager.saves_for(game_id)] == ["Mystery.srm"]


def test_attaching_a_missing_file_is_rejected(tmp_path, library, manager):
    game_id = library.add_game(title="Some Game", system="snes")
    with pytest.raises(ValueError):
        manager.attach(tmp_path / "nope.srm", game_id)


# ── Backup ────────────────────────────────────────────────────────

def test_backup_copies_saves_without_moving_them(tmp_path, library, manager):
    rom = write(tmp_path / "Game.gba")
    save = write(tmp_path / "Game.sav")

    game_id = library.add_game(title="Game", system="gba")
    library.add_file(game_id, rom)
    manager.index()

    result = manager.backup()

    assert result.files_copied == 1
    assert save.is_file(), "the original must not be moved"
    assert result.destination.is_dir()


def test_backup_is_navigable_by_a_human(tmp_path, library, manager):
    """Plain folders named after the game — no archive, no index file."""
    rom = write(tmp_path / "Chrono Trigger.sfc")
    write(tmp_path / "Chrono Trigger.srm")

    game_id = library.add_game(title="Chrono Trigger", system="snes")
    library.add_file(game_id, rom)
    manager.index()

    result = manager.backup()
    copied = list(result.destination.rglob("*.srm"))

    assert copied
    assert "Chrono Trigger" in str(copied[0])


def test_backup_of_a_single_game(tmp_path, library, manager):
    for name in ("A", "B"):
        rom = write(tmp_path / f"{name}.gba")
        write(tmp_path / f"{name}.sav")
        game_id = library.add_game(title=name, system="gba")
        library.add_file(game_id, rom)

    manager.index()
    target = library.list_games()[0]
    result = manager.backup(game_id=target.id)

    assert result.files_copied == 1


def test_backup_with_nothing_to_copy_is_harmless(manager):
    result = manager.backup()
    assert result.files_copied == 0
    assert result.destination is None


def test_backup_reports_missing_files_rather_than_failing(tmp_path, library, manager):
    rom = write(tmp_path / "Game.gba")
    save = write(tmp_path / "Game.sav")

    game_id = library.add_game(title="Game", system="gba")
    library.add_file(game_id, rom)
    manager.index()

    save.unlink()
    result = manager.backup()

    assert result.files_copied == 0
    assert result.errors


def test_backups_are_listed_newest_first(tmp_path, library, manager):
    rom = write(tmp_path / "Game.gba")
    write(tmp_path / "Game.sav")
    game_id = library.add_game(title="Game", system="gba")
    library.add_file(game_id, rom)
    manager.index()

    manager.backup(label="first")
    manager.backup(label="second")

    backups = manager.list_backups()
    assert len(backups) == 2


def test_pruning_keeps_the_newest(tmp_path, library, manager):
    rom = write(tmp_path / "Game.gba")
    write(tmp_path / "Game.sav")
    game_id = library.add_game(title="Game", system="gba")
    library.add_file(game_id, rom)
    manager.index()

    for i in range(4):
        manager.backup(label=f"b{i}")

    assert manager.prune_backups(keep=2) == 2
    assert len(manager.list_backups()) == 2


# ── Restore ───────────────────────────────────────────────────────

def test_restore_overwrites_the_target(tmp_path, manager):
    backup = write(tmp_path / "backup" / "Game.srm", b"old progress")
    target = write(tmp_path / "live" / "Game.srm", b"current")

    manager.restore(backup, target)

    assert target.read_bytes() == b"old progress"


def test_restore_keeps_the_replaced_save(tmp_path, manager):
    """Restoring is the most destructive action here; undo must exist."""
    backup = write(tmp_path / "backup" / "Game.srm", b"old")
    target = write(tmp_path / "live" / "Game.srm", b"current progress")

    manager.restore(backup, target)

    aside = list(target.parent.glob("*.replaced-*"))
    assert aside and aside[0].read_bytes() == b"current progress"


def test_restore_can_skip_keeping_the_old_save(tmp_path, manager):
    backup = write(tmp_path / "backup" / "Game.srm", b"old")
    target = write(tmp_path / "live" / "Game.srm", b"current")

    manager.restore(backup, target, keep_existing=False)

    assert not list(target.parent.glob("*.replaced-*"))


def test_restore_creates_a_missing_target_directory(tmp_path, manager):
    backup = write(tmp_path / "backup" / "Game.srm", b"data")
    target = tmp_path / "brand" / "new" / "Game.srm"

    manager.restore(backup, target)
    assert target.is_file()


def test_restoring_a_missing_backup_is_rejected(tmp_path, manager):
    with pytest.raises(ValueError):
        manager.restore(tmp_path / "nope.srm", tmp_path / "target.srm")
