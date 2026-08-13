"""Tests for the library repository: import, dedupe, filtering, playtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from rose_gamelab.core.discs import DiscFile, GameGroup
from rose_gamelab.core.emulator import GameEntry
from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "library.db")
    yield Library(db)
    db.close()


def steam_entry(name: str, appid: int) -> GameEntry:
    return GameEntry(
        id=f"steam:{appid}",
        name=name,
        system="pc",
        path=f"steam://run/{appid}",
        source="steam",
        is_steam=True,
        metadata={"steam_appid": appid},
    )


# ── Basic writes ──────────────────────────────────────────────────

def test_add_and_get_game(library):
    game_id = library.add_game(title="Hades", system="pc")
    game = library.get(game_id)

    assert game is not None
    assert game.title == "Hades"
    assert game.system == "pc"


def test_sort_title_is_derived_automatically(library):
    game_id = library.add_game(title="The Legend of Zelda", system="nes")
    assert library.get(game_id).sort_title == "legend of zelda"


def test_updating_title_keeps_sort_title_consistent(library):
    game_id = library.add_game(title="Placeholder", system="nes")
    library.update_game(game_id, title="A Link to the Past")
    assert library.get(game_id).sort_title == "link to the past"


def test_unknown_game_returns_none(library):
    assert library.get(999) is None


def test_update_ignores_unknown_columns(library):
    """A stray field must not raise or inject SQL."""
    game_id = library.add_game(title="Hades", system="pc")
    library.update_game(game_id, nonsense_column="x", title="Hades II")
    assert library.get(game_id).title == "Hades II"


# ── Duplicate detection ───────────────────────────────────────────

def test_same_steam_game_imported_twice_is_one_entry(library):
    library.import_entries([steam_entry("Hades", 1145360)])
    result = library.import_entries([steam_entry("Hades", 1145360)])

    assert library.count() == 1
    assert result.skipped == 1


def test_steam_match_wins_over_a_different_title(library):
    """Steam renamed the game; the appid still identifies it."""
    library.import_entries([steam_entry("NieR:Automata", 524220)])
    library.import_entries([steam_entry("NieR:Automata™", 524220)])

    assert library.count() == 1


def test_same_game_from_two_sources_merges_with_two_launch_options(library):
    """The point of duplicate detection: one entry, two ways to play."""
    library.import_entries([steam_entry("Cyberpunk 2077", 1091500)])

    gog = GameEntry(
        id="gog:cp", name="Cyberpunk 2077", system="pc",
        path="/games/cyberpunk/start.sh", source="gog", is_gog=True,
    )
    result = library.import_entries([gog])

    assert library.count() == 1
    assert result.merged == 1

    game = library.list_games()[0]
    kinds = {row["kind"] for row in library.launch_options_for(game.id)}
    assert kinds == {"steam", "gog"}


def test_same_title_on_different_systems_stays_separate(library):
    """Tomb Raider on PS1 and on PC are genuinely different entries."""
    library.add_game(title="Tomb Raider", system="ps1")
    library.import_entries([steam_entry("Tomb Raider", 203160)])

    assert library.count() == 2


def test_identical_content_hash_is_treated_as_the_same_game(library):
    game_id = library.add_game(title="Whatever It Was Called", system="snes")
    library.add_file(game_id, "/roms/original.sfc")
    library.db.execute(
        "UPDATE game_files SET sha1 = ? WHERE game_id = ?", ("deadbeef", game_id)
    )

    match = library.find_duplicate(
        title="Completely Different Name", system="snes", sha1="deadbeef"
    )
    assert match == game_id


def test_first_launch_option_becomes_primary(library):
    game_id = library.add_game(title="Hades", system="pc")
    library.add_launch_option(game_id, kind="native", target="/games/hades")

    assert library.launch_options_for(game_id)[0]["is_primary"] == 1


def test_only_one_option_stays_primary(library):
    game_id = library.add_game(title="Hades", system="pc")
    library.add_launch_option(game_id, kind="native", target="/a")
    library.add_launch_option(game_id, kind="steam", target="/b", is_primary=True)

    options = library.launch_options_for(game_id)
    assert sum(o["is_primary"] for o in options) == 1
    assert options[0]["target"] == "/b"


# ── Multi-disc import ─────────────────────────────────────────────

def test_multi_disc_group_imports_as_one_game(library, tmp_path):
    files = []
    for disc in (1, 2, 3):
        path = tmp_path / f"FF7 (Disc {disc}).cue"
        path.write_text("")
        files.append(DiscFile(path=path, disc_number=disc))

    group = GameGroup(title="Final Fantasy VII", files=files)
    game_id, outcome = library.import_group(group, system="ps1")

    assert outcome == "added"
    assert library.count() == 1
    assert len(library.files_for(game_id)) == 3


def test_multi_disc_game_launches_from_its_playlist(library, tmp_path):
    files = [DiscFile(path=tmp_path / f"d{n}.cue", disc_number=n) for n in (1, 2)]
    for f in files:
        f.path.write_text("")

    playlist = tmp_path / "FF7.m3u"
    playlist.write_text("")

    game_id, _ = library.import_group(
        GameGroup(title="FF7", files=files), system="ps1", playlist=playlist
    )

    assert library.launch_options_for(game_id)[0]["target"] == str(playlist)


def test_rescanning_the_same_roms_adds_nothing(library, tmp_path):
    path = tmp_path / "Chrono Trigger.sfc"
    path.write_text("")
    group = GameGroup(title="Chrono Trigger", files=[DiscFile(path=path)])

    library.import_group(group, system="snes")
    _, outcome = library.import_group(group, system="snes")

    assert outcome == "skipped"
    assert library.count() == 1


def test_adding_a_missing_disc_later_updates_the_game(library, tmp_path):
    first = tmp_path / "G (Disc 1).cue"
    first.write_text("")
    library.import_group(
        GameGroup(title="G", files=[DiscFile(path=first, disc_number=1)]), system="ps1"
    )

    second = tmp_path / "G (Disc 2).cue"
    second.write_text("")
    game_id, outcome = library.import_group(
        GameGroup(title="G", files=[
            DiscFile(path=first, disc_number=1),
            DiscFile(path=second, disc_number=2),
        ]),
        system="ps1",
    )

    assert outcome == "updated"
    assert len(library.files_for(game_id)) == 2


def test_duplicate_file_path_is_not_added_twice(library, tmp_path):
    game_id = library.add_game(title="X", system="snes")
    assert library.add_file(game_id, "/roms/x.sfc") is not None
    assert library.add_file(game_id, "/roms/x.sfc") is None


# ── Filtering, sorting, search ────────────────────────────────────

def test_filter_by_system(library):
    library.add_game(title="Mario", system="nes")
    library.add_game(title="Hades", system="pc")

    assert [g.title for g in library.list_games(system="nes")] == ["Mario"]


def test_hidden_games_are_excluded_by_default(library):
    game_id = library.add_game(title="Wallpaper Engine", system="pc")
    library.set_hidden(game_id)

    assert library.list_games() == []
    assert len(library.list_games(include_hidden=True)) == 1


def test_favorites_filter(library):
    a = library.add_game(title="A", system="pc")
    library.add_game(title="B", system="pc")
    library.set_favorite(a)

    assert [g.title for g in library.list_games(favorites_only=True)] == ["A"]


def test_sorted_by_title_ignoring_leading_article(library):
    library.add_game(title="Zelda", system="nes")
    library.add_game(title="The Adventure of Link", system="nes")

    assert [g.title for g in library.list_games()] == ["The Adventure of Link", "Zelda"]


def test_search_finds_partial_words(library):
    library.add_game(title="The Legend of Zelda", system="nes")
    library.add_game(title="Super Metroid", system="snes")

    assert [g.title for g in library.list_games(search="zel")] == ["The Legend of Zelda"]


def test_search_with_punctuation_does_not_crash(library):
    """FTS5 treats punctuation as syntax and raises on malformed queries."""
    library.add_game(title="NieR:Automata", system="pc")
    assert library.list_games(search='nier:"') is not None


def test_search_that_is_only_punctuation_falls_back(library):
    library.add_game(title="Hades", system="pc")
    assert library.list_games(search="!!!") == []


def test_never_played_games_sort_after_played_ones(library):
    played = library.add_game(title="Played", system="pc")
    library.add_game(title="Never", system="pc")
    session = library.start_session(played)
    library.end_session(session)

    titles = [g.title for g in library.list_games(sort="last_played", descending=True)]
    assert titles[0] == "Played"


def test_systems_in_library_only_lists_populated_systems(library):
    library.add_game(title="Mario", system="nes")
    library.add_game(title="Luigi", system="nes")
    library.add_game(title="Hades", system="pc")

    assert dict(library.systems_in_library()) == {"nes": 2, "pc": 1}


def test_random_game_returns_something_from_the_filtered_set(library):
    library.add_game(title="Mario", system="nes")
    library.add_game(title="Hades", system="pc")

    assert library.random_game(system="nes").title == "Mario"


def test_random_game_on_empty_library_is_none(library):
    assert library.random_game() is None


# ── Playtime ──────────────────────────────────────────────────────

def test_session_records_playtime_and_count(library):
    game_id = library.add_game(title="Hades", system="pc")
    session = library.start_session(game_id)
    library.end_session(session)

    game = library.get(game_id)
    assert game.play_count == 1
    assert game.last_played is not None


def test_playtime_accumulates_across_sessions(library):
    game_id = library.add_game(title="Hades", system="pc")
    for _ in range(3):
        library.end_session(library.start_session(game_id))

    assert library.get(game_id).play_count == 3


def test_ending_an_unknown_session_is_harmless(library):
    assert library.end_session(4242) == 0


# ── Collections and tags ──────────────────────────────────────────

def test_collection_membership(library):
    game_id = library.add_game(title="Hades", system="pc")
    collection = library.create_collection("Roguelikes")
    library.add_to_collection(collection, game_id)

    assert [g.title for g in library.list_games(collection_id=collection)] == ["Hades"]


def test_removing_from_collection(library):
    game_id = library.add_game(title="Hades", system="pc")
    collection = library.create_collection("Roguelikes")
    library.add_to_collection(collection, game_id)
    library.remove_from_collection(collection, game_id)

    assert library.list_games(collection_id=collection) == []


def test_adding_to_a_collection_twice_is_idempotent(library):
    game_id = library.add_game(title="Hades", system="pc")
    collection = library.create_collection("Roguelikes")
    library.add_to_collection(collection, game_id)
    library.add_to_collection(collection, game_id)

    assert len(library.list_games(collection_id=collection)) == 1


def test_tagging_and_filtering_by_tag(library):
    game_id = library.add_game(title="Hades", system="pc")
    library.tag_game(game_id, "roguelike", kind="genre")
    library.add_game(title="Celeste", system="pc")

    assert [g.title for g in library.list_games(tag="roguelike")] == ["Hades"]
    assert library.tags_for(game_id) == ["roguelike"]


def test_untagging(library):
    game_id = library.add_game(title="Hades", system="pc")
    library.tag_game(game_id, "roguelike")
    library.untag_game(game_id, "roguelike")

    assert library.tags_for(game_id) == []


def test_same_tag_on_two_games_is_one_tag(library):
    a = library.add_game(title="Hades", system="pc")
    b = library.add_game(title="Dead Cells", system="pc")
    library.tag_game(a, "roguelike")
    library.tag_game(b, "roguelike")

    assert len(library.list_games(tag="roguelike")) == 2
    assert len([t for t in library.list_tags() if t["name"] == "roguelike"]) == 1


# ── Sources ───────────────────────────────────────────────────────

def test_registering_a_source_twice_updates_it(library):
    library.register_source("roms", name="ROMs", type="rom_folder", path="/old")
    library.register_source("roms", name="ROMs", type="rom_folder", path="/new")

    sources = library.list_sources()
    assert len(sources) == 1
    assert sources[0]["path"] == "/new"


def test_removing_a_source_keeps_its_games_by_default(library):
    """Reconfiguring a source must not destroy playtime and artwork."""
    library.register_source("roms", name="ROMs", type="rom_folder")
    library.add_game(title="Mario", system="nes", source_id="roms")

    library.remove_source("roms")

    assert library.count() == 1
    assert library.get(library.list_games()[0].id).source_id is None


def test_removing_a_source_can_remove_its_games_explicitly(library):
    library.register_source("roms", name="ROMs", type="rom_folder")
    library.add_game(title="Mario", system="nes", source_id="roms")

    library.remove_source("roms", remove_games=True)

    assert library.count() == 0


def test_source_lists_its_game_count(library):
    library.register_source("roms", name="ROMs", type="rom_folder")
    library.add_game(title="Mario", system="nes", source_id="roms")

    assert library.list_sources()[0]["game_count"] == 1


# ── Import reporting ──────────────────────────────────────────────

def test_import_result_reports_what_happened(library):
    result = library.import_entries([
        steam_entry("Hades", 1145360),
        steam_entry("Celeste", 504230),
    ])

    assert (result.added, result.skipped, result.merged) == (2, 0, 0)
    assert result.total_seen == 2


def test_one_bad_entry_does_not_abort_the_import(library):
    bad = GameEntry(id="x", name="Bad", system="pc", path="/x")
    bad.metadata = {"steam_appid": "not-an-int"}

    result = library.import_entries([bad, steam_entry("Hades", 1145360)])

    assert result.added >= 1
    assert library.count() >= 1


# ── Removing sources ──────────────────────────────────────────────
#
# Removing a source used to keep its games silently, leaving entries that no
# sidebar row matched and nothing could ever select or delete again.

def test_removing_a_source_can_take_its_games(library):
    library.register_source("roms", name="ROMs", type="rom_folder")
    game_id = library.add_game(title="Chrono Trigger", system="snes", source_id="roms")
    library.add_launch_option(game_id, kind="emulator", target="/roms/ct.sfc")

    removed = library.remove_source("roms", remove_games=True)

    assert removed == 1
    assert library.count() == 0
    assert library.get(game_id) is None


def test_removing_a_source_can_keep_its_games(library):
    library.register_source("roms", name="ROMs", type="rom_folder")
    library.add_game(title="Chrono Trigger", system="snes", source_id="roms")

    removed = library.remove_source("roms", remove_games=False)

    assert removed == 0
    assert library.count() == 1


def test_games_kept_after_a_source_is_removed_stay_reachable(library):
    """The bug: no filter matched them, so they could never be found again."""
    from rose_gamelab.core.library import NO_SOURCE

    library.register_source("roms", name="ROMs", type="rom_folder")
    library.add_game(title="Chrono Trigger", system="snes", source_id="roms")
    library.remove_source("roms")

    assert library.count_orphaned_games() == 1
    assert [g.title for g in library.list_games(source_id=NO_SOURCE)] == ["Chrono Trigger"]


def test_orphaned_games_can_be_cleared(library):
    library.register_source("roms", name="ROMs", type="rom_folder")
    library.add_game(title="Chrono Trigger", system="snes", source_id="roms")
    library.add_game(title="Half-Life", system="pc", source_id=None)
    library.remove_source("roms")

    assert library.remove_orphaned_games() == 2
    assert library.count() == 0


def test_counting_games_for_a_source(library):
    library.register_source("roms", name="ROMs", type="rom_folder")
    library.register_source("steam", name="Steam", type="steam")
    library.add_game(title="A", system="snes", source_id="roms")
    library.add_game(title="B", system="snes", source_id="roms")
    library.add_game(title="C", system="pc", source_id="steam")

    assert library.count_games_for_source("roms") == 2
    assert library.count_games_for_source("steam") == 1
    assert library.count_games_for_source("nope") == 0


def test_removing_a_game_takes_its_files_and_options(library, tmp_path):
    """Cascades must fire, or the database fills with unreachable rows."""
    game_id = library.add_game(title="Chrono Trigger", system="snes")
    library.add_file(game_id, tmp_path / "ct.sfc")
    library.add_launch_option(game_id, kind="emulator", target=str(tmp_path / "ct.sfc"))

    library.remove_game(game_id)

    assert library.launch_options_for(game_id) == []
    assert library.all_game_files() == []


# ── Bulk removal ──────────────────────────────────────────────────

def test_remove_games_by_system(library):
    library.add_game(title="Demon's Souls", system="ps3")
    library.add_game(title="COALESCED_INT", system="ps3")
    library.add_game(title="Chrono Trigger", system="snes")

    removed = library.remove_games_where(system="ps3")

    assert removed == 2
    assert [g.title for g in library.list_games()] == ["Chrono Trigger"]


def test_remove_games_by_source(library):
    library.register_source("roms", name="ROMs", type="rom_folder")
    library.add_game(title="A", system="snes", source_id="roms")
    library.add_game(title="B", system="pc")

    assert library.remove_games_where(source_id="roms") == 1
    assert [g.title for g in library.list_games()] == ["B"]


def test_remove_games_by_system_and_source_together(library):
    library.register_source("roms", name="ROMs", type="rom_folder")
    library.add_game(title="A", system="ps3", source_id="roms")
    library.add_game(title="B", system="snes", source_id="roms")

    assert library.remove_games_where(system="ps3", source_id="roms") == 1
    assert [g.title for g in library.list_games()] == ["B"]


def test_remove_games_with_no_source_filter(library):
    from rose_gamelab.core.library import NO_SOURCE

    library.register_source("roms", name="ROMs", type="rom_folder")
    library.add_game(title="Kept", system="snes", source_id="roms")
    library.add_game(title="Orphan", system="snes")

    assert library.remove_games_where(source_id=NO_SOURCE) == 1
    assert [g.title for g in library.list_games()] == ["Kept"]


def test_removing_games_needs_a_filter(library):
    """'Delete everything' must never be what happens by passing nothing."""
    library.add_game(title="A", system="snes")

    with pytest.raises(ValueError):
        library.remove_games_where()

    assert library.count() == 1


def test_sorting_is_stable_when_values_tie(library):
    """Every sort here has ties — two games added in the same second, two never
    played, two unrated. Without a tiebreaker their order is whatever the query
    plan produces, which changed the day an index was added."""
    for index in range(6):
        library.add_game(title=f"Tied {index}", system="snes", path=f"/r/{index}.sfc")

    for sort in ("title", "added", "last_played", "playtime", "release", "rating"):
        first = [game.id for game in library.list_games(sort=sort)]
        second = [game.id for game in library.list_games(sort=sort)]
        assert first == second, sort


def test_never_played_games_sort_after_played_ones(library):
    played = library.add_game(title="Played", system="snes", path="/r/a.sfc")
    library.add_game(title="Never", system="snes", path="/r/b.sfc")
    library.db.execute(
        "UPDATE games SET last_played = '2026-01-01T00:00:00+00:00' WHERE id = ?",
        (played,),
    )

    order = [game.id for game in library.list_games(sort="last_played", descending=True)]

    assert order[0] == played
