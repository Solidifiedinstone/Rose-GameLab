"""Saved pad layouts, player order and per-game overrides."""

from __future__ import annotations

import pytest

from rose_gamelab.core.controller import InputDevice
from rose_gamelab.core.controller_profiles import (
    MAX_PLAYERS,
    ControllerProfile,
    ControllerProfileStore,
    guid_for,
)
from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "library.db")
    yield database
    database.close()


@pytest.fixture
def store(db):
    return ControllerProfileStore(db)


@pytest.fixture
def library(db):
    return Library(db)


def pad(name="Wireless Controller", vendor=0x054C, product=0x09CC, version=0x0100):
    return InputDevice(
        name=name, vendor_id=vendor, product_id=product,
        bustype=0x0003, version=version,
    )


XBOX = dict(name="Xbox 360 pad", vendor=0x045E, product=0x028E)


# ── Binding ───────────────────────────────────────────────────────

def test_a_new_pad_is_bound_automatically(store):
    profile = store.bind(pad())

    assert profile.id is not None
    assert profile.mapping
    assert profile.recognised          # the database knows a DualShock 4


def test_an_unknown_pad_still_gets_a_profile(store):
    profile = store.bind(pad(name="Mystery", vendor=0xDEAD, product=0xBEEF))

    assert profile.mapping
    assert profile.source == "builtin"


def test_binding_the_same_pad_twice_reuses_the_profile(store):
    first = store.bind(pad())
    second = store.bind(pad())

    assert first.id == second.id
    assert len(store.list_profiles()) == 1


def test_a_users_own_mapping_is_never_overwritten_by_rebinding(store):
    """Re-binding on every launch must not undo what somebody corrected."""
    profile = store.bind(pad())
    profile.mapping = "custom-mapping"
    profile.source = "user"
    store.save(profile)

    again = store.bind(pad())

    assert again.mapping == "custom-mapping"
    assert again.source == "user"


def test_a_profile_follows_the_pad_not_the_port(store):
    """Keyed by GUID, so replugging into another socket keeps the layout."""
    store.bind(pad())
    same_pad_different_path = pad()

    assert store.for_device(same_pad_different_path) is not None


def test_saving_the_same_guid_updates_rather_than_duplicates(store):
    store.save(ControllerProfile(name="A", guid="g" * 32, mapping="m1"))
    store.save(ControllerProfile(name="B", guid="g" * 32, mapping="m2"))

    profiles = store.list_profiles()
    assert len(profiles) == 1
    assert profiles[0].mapping == "m2"


# ── Player order ──────────────────────────────────────────────────

def test_the_first_pad_becomes_player_one(store):
    assert store.bind(pad()).player == 1


def test_the_second_pad_becomes_player_two(store):
    store.bind(pad())
    assert store.bind(pad(**XBOX)).player == 2


def test_players_are_reported_by_slot(store):
    store.bind(pad())
    store.bind(pad(**XBOX))

    players = store.players()

    assert sorted(players) == [1, 2]


def test_assigning_an_occupied_slot_swaps_the_two(store):
    """Dragging player 2 onto player 1 means "swap", not "fail"."""
    first = store.bind(pad())
    second = store.bind(pad(**XBOX))

    store.assign_player(second.id, 1)

    assert store.get(second.id).player == 1
    assert store.get(first.id).player == 2


def test_a_player_slot_can_be_cleared(store):
    profile = store.bind(pad())
    store.assign_player(profile.id, None)

    assert store.get(profile.id).player is None
    assert store.players() == {}


def test_a_cleared_slot_is_reused_by_the_next_pad(store):
    first = store.bind(pad())
    store.assign_player(first.id, None)

    assert store.bind(pad(**XBOX)).player == 1


def test_an_impossible_player_number_is_rejected(store):
    profile = store.bind(pad())

    with pytest.raises(ValueError):
        store.assign_player(profile.id, 0)
    with pytest.raises(ValueError):
        store.assign_player(profile.id, MAX_PLAYERS + 1)


def test_more_pads_than_slots_do_not_crash(store):
    for index in range(MAX_PLAYERS + 2):
        store.bind(pad(name=f"Pad {index}", product=0x1000 + index))

    assert len(store.players()) == MAX_PLAYERS
    assert len(store.list_profiles()) == MAX_PLAYERS + 2


# ── Per-game overrides ────────────────────────────────────────────

def test_a_game_can_pin_a_specific_pad(store, library):
    game_id = library.add_game(title="Street Fighter", system="arcade", path="/r/sf.zip")
    stick = store.bind(pad(name="Arcade Stick", vendor=0x0F0D, product=0x0092))

    store.set_for_game(game_id, stick.id)

    assert store.for_game(game_id)[1].id == stick.id


def test_pinning_twice_replaces_rather_than_duplicates(store, library):
    game_id = library.add_game(title="G", system="snes", path="/r/g.sfc")
    first = store.bind(pad())
    second = store.bind(pad(**XBOX))

    store.set_for_game(game_id, first.id)
    store.set_for_game(game_id, second.id)

    pinned = store.for_game(game_id)
    assert len(pinned) == 1
    assert pinned[1].id == second.id


def test_an_override_can_be_cleared(store, library):
    game_id = library.add_game(title="G", system="snes", path="/r/g.sfc")
    store.set_for_game(game_id, store.bind(pad()).id)

    store.clear_for_game(game_id)

    assert store.for_game(game_id) == {}


def test_deleting_a_game_takes_its_overrides_with_it(store, library):
    game_id = library.add_game(title="G", system="snes", path="/r/g.sfc")
    store.set_for_game(game_id, store.bind(pad()).id)

    library.remove_game(game_id)

    assert store.for_game(game_id) == {}


def test_deleting_a_profile_takes_its_overrides_with_it(store, library):
    game_id = library.add_game(title="G", system="snes", path="/r/g.sfc")
    profile = store.bind(pad())
    store.set_for_game(game_id, profile.id)

    store.delete(profile.id)

    assert store.for_game(game_id) == {}


# ── What a launch gets ────────────────────────────────────────────

def test_mappings_come_out_in_player_order(store):
    """Player one first, so an emulator reading them in order agrees with the
    order shown in the interface."""
    first = store.bind(pad())
    second = store.bind(pad(**XBOX))
    store.assign_player(second.id, 1)     # swap them

    mappings = store.mappings_for([pad(), pad(**XBOX)])

    assert mappings[0] == store.get(second.id).mapping
    assert mappings[1] == store.get(first.id).mapping


def test_an_unassigned_pad_sorts_after_the_assigned_ones(store):
    store.bind(pad())
    spare = store.bind(pad(**XBOX))
    store.assign_player(spare.id, None)

    mappings = store.mappings_for([pad(**XBOX), pad()])

    assert mappings[0] == store.for_device(pad()).mapping


def test_a_pad_with_no_profile_still_gets_a_mapping(store):
    """Nothing saved yet is not a reason to launch with no configuration."""
    mappings = store.mappings_for([pad()])

    assert len(mappings) == 1
    assert "platform:Linux" in mappings[0]


def test_a_game_override_wins_over_the_saved_profile(store, library):
    game_id = library.add_game(title="G", system="arcade", path="/r/g.zip")
    saved = store.bind(pad())
    saved.mapping = "the-usual-mapping"
    store.save(saved)

    stick = ControllerProfile(
        name="Stick", guid=guid_for(pad()), mapping="the-stick-mapping",
    )
    stick.id = store.save(stick)
    store.set_for_game(game_id, stick.id)

    assert store.mappings_for([pad()], game_id=game_id) == ["the-stick-mapping"]


def test_the_launch_environment_carries_every_pad(store):
    store.bind(pad())
    store.bind(pad(**XBOX))

    env = store.sdl_environment([pad(), pad(**XBOX)])

    assert len(env["SDL_GAMECONTROLLERCONFIG"].splitlines()) == 2
    assert env["SDL_JOYSTICK_HIDAPI"] == "0"


def test_no_pads_means_no_environment(store):
    assert store.sdl_environment([]) == {}


# ── Sharing ───────────────────────────────────────────────────────

def test_a_profile_survives_a_round_trip(store, tmp_path, db):
    """Mapping a pad is tedious and the result is not personal — anyone with
    the same controller wants the same file."""
    from rose_gamelab.core.controller_profiles import ControllerProfileStore
    from rose_gamelab.db.database import Database

    original = store.bind(pad())
    assert store.export_profiles(tmp_path / "pads.json") == 1

    other_db = Database(tmp_path / "other.db")
    other = ControllerProfileStore(other_db)
    summary = other.import_profiles(tmp_path / "pads.json")

    assert summary.imported == 1
    assert other.for_guid(original.guid).mapping == original.mapping
    other_db.close()


def test_importing_does_not_overwrite_your_own_mapping(store, tmp_path):
    """Someone who corrected their own mapping should not lose it to a
    friend's file that happens to cover the same pad."""
    store.bind(pad())
    store.export_profiles(tmp_path / "theirs.json")

    mine = store.for_device(pad())
    mine.mapping = "my-corrected-mapping"
    store.save(mine)

    summary = store.import_profiles(tmp_path / "theirs.json")

    assert summary.kept == 1
    assert store.for_device(pad()).mapping == "my-corrected-mapping"


def test_overwrite_is_available_when_asked_for(store, tmp_path):
    store.bind(pad())
    store.export_profiles(tmp_path / "theirs.json")
    mine = store.for_device(pad())
    mine.mapping = "mine"
    store.save(mine)

    store.import_profiles(tmp_path / "theirs.json", overwrite=True)

    assert store.for_device(pad()).mapping != "mine"


def test_player_assignment_is_not_shared(store, tmp_path):
    """Which pad is player one is about one sofa, not about the pad."""
    import json

    store.bind(pad())
    store.export_profiles(tmp_path / "pads.json")

    payload = json.loads((tmp_path / "pads.json").read_text())

    assert "player" not in payload["profiles"][0]


def test_importing_something_that_is_not_ours_says_so(store, tmp_path):
    (tmp_path / "junk.json").write_text('{"format": "some-other-tool"}')

    summary = store.import_profiles(tmp_path / "junk.json")

    assert summary.imported == 0
    assert "not a Rose GameLab" in summary.summary


def test_importing_broken_json_does_not_raise(store, tmp_path):
    (tmp_path / "broken.json").write_text("{not json at all")

    summary = store.import_profiles(tmp_path / "broken.json")

    assert summary.errors
    assert summary.imported == 0


def test_importing_a_missing_file_does_not_raise(store, tmp_path):
    assert store.import_profiles(tmp_path / "nope.json").errors


def test_entries_with_no_mapping_are_skipped(store, tmp_path):
    import json

    (tmp_path / "empty.json").write_text(json.dumps({
        "format": "rose-gamelab-controller-profiles",
        "version": 1,
        "profiles": [{"name": "Nothing", "guid": "a" * 32, "mapping": ""}],
    }))

    summary = store.import_profiles(tmp_path / "empty.json")

    assert summary.skipped == 1
    assert summary.imported == 0


def test_only_the_asked_for_profiles_are_exported(store, tmp_path):
    first = store.bind(pad())
    store.bind(pad(**XBOX))

    assert store.export_profiles(tmp_path / "one.json", guids=[first.guid]) == 1
