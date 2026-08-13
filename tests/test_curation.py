"""Merging duplicate entries, and editing games in bulk.

Both are things the cleanup pass deliberately refuses to do on its own, because
each is a judgement about somebody's collection. The rules that matter here:
nothing on disk is ever touched, and a merge must not lose anything the entry
being folded in had.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core.curation import bulk_update, merge_games
from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database


@pytest.fixture
def library(tmp_path):
    database = Database(tmp_path / "library.db")
    yield Library(database)
    database.close()


def add(library, title, *, system="snes", path=None):
    path = path or f"/roms/{title}.sfc"
    game_id = library.add_game(title=title, system=system, path=path)
    library.add_file(game_id, path)
    return game_id


# ── Merging ───────────────────────────────────────────────────────

def test_files_move_onto_the_surviving_entry(library):
    keeper = add(library, "Chrono Trigger", path="/roms/ct.sfc")
    other = add(library, "Chrono Trigger", path="/roms/ct.smc")

    result = merge_games(library, keeper, [other])

    assert result.files == 1
    files = library.db.query("SELECT path FROM game_files WHERE game_id = ?", (keeper,))
    assert {row["path"] for row in files} == {"/roms/ct.sfc", "/roms/ct.smc"}


def test_the_folded_entry_is_gone(library):
    keeper = add(library, "A", path="/roms/a.sfc")
    other = add(library, "A", path="/roms/b.sfc")

    merge_games(library, keeper, [other])

    assert library.get(other) is None
    assert len(library.list_games()) == 1


def test_playtime_is_summed_not_discarded(library):
    """Quietly dropping half of somebody's recorded hours because they tidied
    their library would be the worst possible outcome of a tidy-up."""
    keeper = add(library, "A", path="/roms/a.sfc")
    other = add(library, "A", path="/roms/b.sfc")
    library.db.execute(
        "UPDATE games SET play_seconds = 3600, play_count = 2 WHERE id = ?", (keeper,)
    )
    library.db.execute(
        "UPDATE games SET play_seconds = 1800, play_count = 1 WHERE id = ?", (other,)
    )

    merge_games(library, keeper, [other])

    survivor = library.get(keeper)
    assert survivor.play_seconds == 5400
    assert survivor.play_count == 3


def test_art_the_survivor_lacked_is_inherited(library):
    """A merge must never lose information only the folded entry had."""
    keeper = add(library, "A", path="/roms/a.sfc")
    other = add(library, "A", path="/roms/b.sfc")
    library.db.execute(
        "UPDATE games SET cover_path = '/art/a.jpg', summary = 'A game.'"
        " WHERE id = ?", (other,)
    )

    merge_games(library, keeper, [other])

    survivor = library.get(keeper)
    assert survivor.cover_path == "/art/a.jpg"
    assert survivor.summary == "A game."


def test_the_survivors_own_art_is_not_overwritten(library):
    keeper = add(library, "A", path="/roms/a.sfc")
    other = add(library, "A", path="/roms/b.sfc")
    library.db.execute(
        "UPDATE games SET cover_path = '/art/keep.jpg' WHERE id = ?", (keeper,)
    )
    library.db.execute(
        "UPDATE games SET cover_path = '/art/other.jpg' WHERE id = ?", (other,)
    )

    merge_games(library, keeper, [other])

    assert library.get(keeper).cover_path == "/art/keep.jpg"


def test_the_same_file_is_not_added_twice(library):
    keeper = add(library, "A", path="/roms/same.sfc")
    other = library.add_game(title="A", system="snes", path="/roms/same.sfc")
    library.add_file(other, "/roms/same.sfc")

    merge_games(library, keeper, [other])

    files = library.db.query("SELECT path FROM game_files WHERE game_id = ?", (keeper,))
    assert len(files) == 1


def test_collections_are_carried_across(library):
    keeper = add(library, "A", path="/roms/a.sfc")
    other = add(library, "A", path="/roms/b.sfc")
    collection = library.create_collection("Favourites")
    library.add_to_collection(collection, other)

    merge_games(library, keeper, [other])

    assert collection in library.collections_for(keeper)


def test_several_entries_can_be_folded_at_once(library):
    keeper = add(library, "A", path="/roms/a.sfc")
    others = [add(library, "A", path=f"/roms/{n}.sfc") for n in range(3)]

    result = merge_games(library, keeper, others)

    assert len(result.removed) == 3
    assert len(library.list_games()) == 1


def test_merging_a_game_into_itself_does_nothing(library):
    keeper = add(library, "A", path="/roms/a.sfc")

    result = merge_games(library, keeper, [keeper])

    assert result.errors
    assert library.get(keeper) is not None


def test_merging_into_something_that_does_not_exist_is_reported(library):
    assert merge_games(library, 999, [1]).errors


def test_no_file_on_disk_is_touched(library, tmp_path):
    """The loser's files are re-pointed, never deleted."""
    real = tmp_path / "game.sfc"
    real.write_bytes(b"rom")
    keeper = add(library, "A", path="/roms/a.sfc")
    other = add(library, "A", path=str(real))

    merge_games(library, keeper, [other])

    assert real.exists()


# ── Bulk editing ──────────────────────────────────────────────────

def test_a_field_is_set_on_every_selected_game(library):
    ids = [add(library, f"G{n}", path=f"/roms/{n}.bin") for n in range(4)]

    result = bulk_update(library, ids, system="ps1")

    assert result.changed == 4
    assert {game.system for game in library.list_games()} == {"ps1"}


def test_games_not_selected_are_left_alone(library):
    first = add(library, "First", path="/roms/a.sfc")
    second = add(library, "Second", path="/roms/b.sfc")

    bulk_update(library, [first], system="ps1")

    assert library.get(second).system == "snes"


def test_only_whitelisted_fields_can_be_set(library):
    """A caller must not be able to rewrite an id or a playtime by accident."""
    game_id = add(library, "A", path="/roms/a.sfc")

    result = bulk_update(library, [game_id], play_seconds=99999)

    assert result.errors
    assert library.get(game_id).play_seconds == 0


def test_nothing_selected_is_an_error_not_a_silent_success(library):
    assert bulk_update(library, [], system="ps1").errors


def test_hiding_several_games_at_once(library):
    ids = [add(library, f"G{n}", path=f"/roms/{n}.sfc") for n in range(3)]

    bulk_update(library, ids, hidden=True)

    assert library.list_games() == []
    assert len(library.list_games(include_hidden=True)) == 3


def test_a_retroachievements_link_is_inherited(library):
    """`update_game` will not write this column, so the merge does it itself."""
    keeper = add(library, "A", path="/roms/a.sfc")
    other = add(library, "A", path="/roms/b.sfc")
    library.db.execute("UPDATE games SET ra_game_id = 4242 WHERE id = ?", (other,))

    merge_games(library, keeper, [other])

    row = library.db.query_one("SELECT ra_game_id FROM games WHERE id = ?", (keeper,))
    assert row["ra_game_id"] == 4242


# ── Merging across systems ────────────────────────────────────────

def test_each_way_of_playing_keeps_its_own_system(library):
    """The same game owned on two consoles is one entry with two options, and
    each has to reach its own emulator — the game has only one system field,
    so without this both would be handed to whichever one survived."""
    ps2 = library.add_game(title="San Andreas", system="ps2", path="/roms/sa.iso")
    library.add_launch_option(ps2, kind="emulator", target="/roms/sa.iso")
    ps3 = library.add_game(title="San Andreas", system="ps3", path="/roms/sa/EBOOT.BIN")
    library.add_launch_option(ps3, kind="emulator", target="/roms/sa/EBOOT.BIN")

    merge_games(library, ps2, [ps3])

    systems = {o["system"] for o in library.launch_options_for(ps2)}
    assert systems == {"ps2", "ps3"}


def test_a_moved_option_is_labelled_with_its_console(library):
    """That is the difference somebody is choosing between — not two identical
    game titles."""
    ps2 = library.add_game(title="San Andreas", system="ps2", path="/roms/sa.iso")
    library.add_launch_option(ps2, kind="emulator", target="/roms/sa.iso")
    ps3 = library.add_game(title="San Andreas", system="ps3", path="/roms/sa/EBOOT.BIN")
    library.add_launch_option(ps3, kind="emulator", target="/roms/sa/EBOOT.BIN")

    merge_games(library, ps2, [ps3])

    labels = {o["label"] for o in library.launch_options_for(ps2)}
    assert "PlayStation 3" in labels


def test_an_option_defaults_to_its_games_system(library):
    game_id = library.add_game(title="Solo", system="snes", path="/roms/s.sfc")
    library.add_launch_option(game_id, kind="emulator", target="/roms/s.sfc")

    assert library.launch_options_for(game_id)[0]["system"] == "snes"
