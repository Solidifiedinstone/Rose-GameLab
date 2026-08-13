"""Tests for directory scanning, system inference and the hashing pass."""

from __future__ import annotations

from pathlib import Path

import pytest

from rose_gamelab.core.library import Library
from rose_gamelab.core.scanner import RomScanner, infer_system, walk_roms
from rose_gamelab.db.database import Database


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "library.db")
    yield Library(db)
    db.close()


@pytest.fixture
def scanner(library):
    return RomScanner(library)


def make(root: Path, relative: str, content: bytes = b"rom") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ── Walking ───────────────────────────────────────────────────────

def test_finds_files_recursively(tmp_path):
    make(tmp_path, "a/b/game.gba")
    assert [p.name for p in walk_roms(tmp_path)] == ["game.gba"]


def test_respects_non_recursive(tmp_path):
    make(tmp_path, "top.gba")
    make(tmp_path, "sub/deep.gba")

    found = {p.name for p in walk_roms(tmp_path, recursive=False)}
    assert found == {"top.gba"}


def test_filters_by_extension(tmp_path):
    make(tmp_path, "game.gba")
    make(tmp_path, "notes.txt")

    found = {p.name for p in walk_roms(tmp_path, extensions={".gba"})}
    assert found == {"game.gba"}


def test_skips_emulator_support_directories(tmp_path):
    make(tmp_path, "game.gba")
    make(tmp_path, "saves/old.gba")
    make(tmp_path, "bios/bios.gba")

    assert {p.name for p in walk_roms(tmp_path)} == {"game.gba"}


def test_skips_hidden_directories(tmp_path):
    make(tmp_path, ".hidden/game.gba")
    assert list(walk_roms(tmp_path)) == []


def test_does_not_loop_on_recursive_symlink(tmp_path):
    """ROM collections often symlink back into themselves."""
    make(tmp_path, "roms/game.gba")
    (tmp_path / "roms" / "loop").symlink_to(tmp_path / "roms")

    assert len(list(walk_roms(tmp_path))) == 1


def test_missing_directory_yields_nothing(tmp_path):
    assert list(walk_roms(tmp_path / "nope")) == []


# ── System inference ──────────────────────────────────────────────

def test_hint_always_wins():
    assert infer_system(Path("/roms/game.iso"), hint="ps2") == "ps2"


def test_unambiguous_extension_resolves():
    assert infer_system(Path("/roms/game.gba")) == "gba"


def test_ambiguous_extension_uses_the_folder_name():
    assert infer_system(Path("/roms/ps2/game.iso")) == "ps2"


def test_ambiguous_extension_without_a_clue_is_unknown():
    """Guessing wrong means the wrong emulator and a failed launch."""
    assert infer_system(Path("/downloads/game.iso")) is None


def test_unknown_extension_is_unknown():
    assert infer_system(Path("/roms/readme.txt")) is None


# ── Scanning ──────────────────────────────────────────────────────

def test_scan_imports_games(tmp_path, scanner, library):
    make(tmp_path, "Chrono Trigger.sfc")
    make(tmp_path, "Super Metroid.sfc")

    result = scanner.scan_folder(tmp_path, system="snes")

    assert result.games_found == 2
    assert result.imported.added == 2
    assert library.count() == 2


def test_scan_merges_multi_disc_and_writes_a_playlist(tmp_path, scanner, library):
    for disc in (1, 2, 3):
        make(tmp_path, f"FF7 (Disc {disc}).cue")

    result = scanner.scan_folder(tmp_path, system="ps1")

    assert result.games_found == 1
    assert result.playlists_written == 1
    assert library.count() == 1
    assert (tmp_path / "FF7.m3u").is_file()


def test_multi_disc_game_launches_from_the_playlist(tmp_path, scanner, library):
    for disc in (1, 2):
        make(tmp_path, f"Game (Disc {disc}).cue")

    scanner.scan_folder(tmp_path, system="ps1")

    game = library.list_games()[0]
    assert library.launch_options_for(game.id)[0]["target"].endswith(".m3u")


def test_rescanning_does_not_duplicate(tmp_path, scanner, library):
    make(tmp_path, "Chrono Trigger.sfc")

    scanner.scan_folder(tmp_path, system="snes")
    second = scanner.scan_folder(tmp_path, system="snes")

    assert library.count() == 1
    assert second.imported.added == 0


def test_scan_assigns_the_default_emulator(tmp_path, scanner, library):
    make(tmp_path, "game.sfc")
    scanner.scan_folder(tmp_path, system="snes")

    game = library.list_games()[0]
    assert library.launch_options_for(game.id)[0]["emulator"] == "snes9x"


def test_scan_reports_unmatched_files_rather_than_dropping_them(tmp_path, scanner):
    make(tmp_path, "mystery.iso")
    result = scanner.scan_folder(tmp_path)

    assert result.errors
    assert "could not be matched" in result.errors[0]


def test_scanning_a_file_instead_of_a_folder_is_an_error(tmp_path, scanner):
    path = make(tmp_path, "game.sfc")
    result = scanner.scan_folder(path)

    assert result.errors
    assert result.games_found == 0


def test_scan_marks_the_source_as_scanned(tmp_path, scanner, library):
    library.register_source("roms", name="ROMs", type="rom_folder")
    make(tmp_path, "game.sfc")

    scanner.scan_folder(tmp_path, system="snes", source_id="roms")

    assert library.list_sources()[0]["scanned_at"] is not None


def test_different_systems_do_not_merge_by_title(tmp_path, scanner, library):
    """A PS1 and a Saturn rip sharing a title must stay separate."""
    make(tmp_path, "ps1/Game.cue")
    make(tmp_path, "saturn/Game.chd")

    scanner.scan_folder(tmp_path)

    assert library.count() == 2


# ── Hashing pass ──────────────────────────────────────────────────

def test_hash_pass_fills_in_checksums(tmp_path, scanner, library):
    make(tmp_path, "game.sfc", b"CONTENT")
    scanner.scan_folder(tmp_path, system="snes")

    assert scanner.hash_pending() == 1

    game = library.list_games()[0]
    row = library.files_for(game.id)[0]
    assert row["sha1"] and row["crc32"] and row["md5"]


def test_hash_pass_is_resumable(tmp_path, scanner, library):
    for i in range(3):
        make(tmp_path, f"game{i}.sfc", f"CONTENT{i}".encode())
    scanner.scan_folder(tmp_path, system="snes")

    assert scanner.hash_pending(limit=1) == 1
    assert scanner.hash_pending() == 2
    assert scanner.hash_pending() == 0


def test_hash_pass_flags_files_that_vanished(tmp_path, scanner, library):
    path = make(tmp_path, "game.sfc")
    scanner.scan_folder(tmp_path, system="snes")
    path.unlink()

    scanner.hash_pending()

    game = library.list_games()[0]
    assert library.files_for(game.id)[0]["missing"] == 1


def test_missing_files_are_flagged_not_deleted(tmp_path, scanner, library):
    """An unplugged drive should grey a game out, not erase its playtime."""
    path = make(tmp_path, "game.sfc")
    scanner.scan_folder(tmp_path, system="snes")
    path.unlink()

    assert scanner.mark_missing_files() == 1
    assert library.count() == 1


def test_returning_files_are_unflagged(tmp_path, scanner, library):
    path = make(tmp_path, "game.sfc")
    scanner.scan_folder(tmp_path, system="snes")

    path.unlink()
    scanner.mark_missing_files()
    path.write_bytes(b"rom")

    assert scanner.mark_missing_files() == 0
    game = library.list_games()[0]
    assert library.files_for(game.id)[0]["missing"] == 0


# ── Folder games ──────────────────────────────────────────────────
#
# Regression cover for the PS3 collection that imported forty games as three
# hundred entries.

def ps3_game(root: Path, name: str) -> Path:
    game = root / name
    make(game, "PS3_GAME/USRDIR/EBOOT.BIN")
    make(game, "PS3_DISC.SFB")
    for junk in ("COALESCED_INT.bin", "GLOBALSHADERCACHE-PS3.bin", "audiof.bin"):
        make(game, f"PS3_GAME/USRDIR/{junk}")
    return game


def test_walk_does_not_descend_into_a_folder_game(tmp_path):
    ps3_game(tmp_path, "Demon's Souls (USA)")

    assert list(walk_roms(tmp_path)) == []


def test_walk_library_yields_the_folder_game_itself(tmp_path):
    from rose_gamelab.core.folder_games import FolderGame
    from rose_gamelab.core.scanner import walk_library

    ps3_game(tmp_path, "Demon's Souls (USA)")
    make(tmp_path, "Chrono Trigger.sfc")

    found = list(walk_library(tmp_path))
    games = [f for f in found if isinstance(f, FolderGame)]
    files = [f for f in found if not isinstance(f, FolderGame)]

    assert [g.title for g in games] == ["Demon's Souls (USA)"]
    assert [f.name for f in files] == ["Chrono Trigger.sfc"]


def test_scanning_ps3_folders_imports_one_game_each(scanner, library, tmp_path):
    ps3_game(tmp_path, "Demon's Souls (USA)")
    ps3_game(tmp_path, "Dark Souls (USA) (En,Fr,Es)")

    result = scanner.scan_folder(tmp_path)

    assert result.games_found == 2
    assert result.imported.added == 2
    assert library.count() == 2
    assert {g.system for g in library.list_games()} == {"ps3"}


def test_folder_game_launches_its_eboot(scanner, library, tmp_path):
    game = ps3_game(tmp_path, "Demon's Souls (USA)")

    scanner.scan_folder(tmp_path)

    (entry,) = library.list_games()
    target = library.launch_options_for(entry.id)[0]["target"]
    assert Path(target) == game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"


def test_scan_removes_debris_from_an_earlier_scan(scanner, library, tmp_path):
    """Entries imported before folder layouts were understood are cleaned up."""
    game = ps3_game(tmp_path, "Demon's Souls (USA)")

    # Imported the way the old scanner would have: one game per inner file.
    for junk in ("COALESCED_INT.bin", "GLOBALSHADERCACHE-PS3.bin"):
        game_id = library.add_game(title=junk[:-4], system="ps3")
        library.add_file(game_id, game / "PS3_GAME" / "USRDIR" / junk)
    assert library.count() == 2

    result = scanner.scan_folder(tmp_path)

    assert result.debris_removed == 2
    assert [g.title for g in library.list_games()] == ["Demon's Souls"]


def test_debris_sweep_leaves_ordinary_games_alone(scanner, library, tmp_path):
    make(tmp_path, "Chrono Trigger.sfc")
    scanner.scan_folder(tmp_path)

    assert scanner.remove_folder_game_debris() == 0
    assert library.count() == 1


def test_debris_sweep_leaves_neighbouring_folder_games_alone(
    scanner, library, tmp_path
):
    """Two games on the same shelf must not be mistaken for one another.

    Both sit directly in the collection folder, so anything keyed on a game's
    parent directory answers for the first one twice — and deletes the second
    as debris from it.
    """
    for name in ("Demon's Souls (USA)", "Ni no Kuni (USA)"):
        game = tmp_path / name
        make(game, "PS3_DISC.SFB")
        make(game, "PS3_GAME/PARAM.SFO")

    scanner.scan_folder(tmp_path)
    assert library.count() == 2

    assert scanner.remove_folder_game_debris() == 0
    assert library.count() == 2


def test_a_game_launched_from_a_directory_is_not_reported_missing(
    scanner, library, tmp_path
):
    """Some games are launched from their folder; testing for a FILE lost them."""
    game = tmp_path / "Ni no Kuni (USA)"
    make(game, "PS3_DISC.SFB")
    make(game, "PS3_GAME/PARAM.SFO")

    scanner.scan_folder(tmp_path)
    (entry,) = library.list_games()
    assert Path(library.files_for(entry.id)[0]["path"]) == game

    assert scanner.mark_missing_files() == 0
    assert library.files_for(entry.id)[0]["missing"] == 0


def test_a_folder_game_is_not_hashed(scanner, library, tmp_path):
    """There is no single file to hash, and hashing one inside it means nothing."""
    game = tmp_path / "Ni no Kuni (USA)"
    make(game, "PS3_DISC.SFB")
    make(game, "PS3_GAME/PARAM.SFO")

    scanner.scan_folder(tmp_path)

    assert scanner.hash_pending() == 0
    (entry,) = library.list_games()
    assert library.files_for(entry.id)[0]["missing"] == 0
