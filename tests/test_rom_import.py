"""Tests for filing loose ROMs into an organised library folder.

This module moves the user's files, so the tests lean hard on the cases where
getting it wrong loses or misplaces something.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rose_gamelab.core.media import MediaKind
from rose_gamelab.core.rom_import import RomImporter, default_library_root


class NoDatabase:
    """Stands in for OpenVGDB when it has not been downloaded."""

    def available(self) -> bool:
        return False

    def identify(self, **kwargs):
        return None


@pytest.fixture
def importer(tmp_path):
    return RomImporter(root=tmp_path / "ROMs", openvgdb=NoDatabase())


def make(path: Path, content: bytes = b"rom") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ── Folder layout ─────────────────────────────────────────────────

def test_folders_are_named_for_humans(importer):
    """Someone browsing in a file manager has never heard of 'snes'."""
    assert importer.folder_for("snes").name == "Super Nintendo"
    assert importer.folder_for("ps1").name == "PlayStation"


def test_folder_names_are_filesystem_safe(importer):
    name = importer.folder_for("megadrive").name
    assert "/" not in name and ":" not in name


def test_default_root_is_under_home(monkeypatch):
    monkeypatch.delenv("ROSE_ROM_ROOT", raising=False)
    assert default_library_root().is_relative_to(Path.home())


def test_root_can_be_overridden_by_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ROSE_ROM_ROOT", str(tmp_path / "elsewhere"))
    assert default_library_root() == tmp_path / "elsewhere"


# ── Planning ──────────────────────────────────────────────────────

def test_plans_by_unambiguous_extension(importer, tmp_path):
    rom = make(tmp_path / "downloads" / "Chrono Trigger.sfc")

    plan = importer.plan([rom])[0]

    assert plan.system_id == "snes"
    assert plan.identified_by == "extension"
    assert plan.destination.name == "Super Nintendo"
    assert plan.ok


def test_unidentifiable_file_is_not_moved(importer, tmp_path):
    """A ROM filed under the wrong system is worse than one left alone."""
    rom = make(tmp_path / "downloads" / "mystery.iso")

    plan = importer.plan([rom])[0]

    assert plan.system_id is None
    assert not plan.ok
    assert "Could not tell" in plan.problem


def test_user_hint_overrides_detection(importer, tmp_path):
    rom = make(tmp_path / "downloads" / "mystery.iso")

    plan = importer.plan([rom], hint="ps2")[0]

    assert plan.system_id == "ps2"
    assert plan.identified_by == "user"


def test_planning_moves_nothing(importer, tmp_path):
    rom = make(tmp_path / "downloads" / "game.sfc")
    importer.plan([rom])
    assert rom.is_file()


def test_a_directory_is_walked(importer, tmp_path):
    make(tmp_path / "downloads" / "a.sfc")
    make(tmp_path / "downloads" / "nested" / "b.gba")

    plans = importer.plan([tmp_path / "downloads"])
    assert {p.system_id for p in plans} == {"snes", "gba"}


def test_multi_disc_set_is_one_plan(importer, tmp_path):
    for disc in (1, 2, 3):
        make(tmp_path / "downloads" / f"FF7 (Disc {disc}).cue")

    plans = importer.plan([tmp_path / "downloads"])

    assert len(plans) == 1
    assert len(plans[0].group.files) == 3


def test_existing_file_is_reported_not_overwritten(importer, tmp_path):
    make(importer.folder_for("snes") / "game.sfc", b"the original")
    incoming = make(tmp_path / "downloads" / "game.sfc", b"the new one")

    plan = importer.plan([incoming])[0]

    assert not plan.ok
    assert "Already in your library" in plan.problem


def test_already_filed_games_are_recognised(importer, tmp_path):
    rom = make(importer.folder_for("snes") / "game.sfc")
    plan = importer.plan([rom])[0]
    assert plan.problem == "Already filed here"


# ── Applying ──────────────────────────────────────────────────────

def test_moves_a_rom_into_its_system_folder(importer, tmp_path):
    rom = make(tmp_path / "downloads" / "Chrono Trigger.sfc", b"data")

    outcome = importer.apply(importer.plan([rom]))

    target = importer.folder_for("snes") / "Chrono Trigger.sfc"
    assert outcome.files_moved == 1
    assert target.read_bytes() == b"data"
    assert not rom.exists(), "the original should have moved"


def test_copy_leaves_the_original_alone(importer, tmp_path):
    rom = make(tmp_path / "downloads" / "game.sfc", b"data")

    importer.apply(importer.plan([rom]), move=False)

    assert rom.is_file()
    assert (importer.folder_for("snes") / "game.sfc").is_file()


def test_multi_disc_set_moves_together(importer, tmp_path):
    for disc in (1, 2, 3):
        make(tmp_path / "downloads" / f"FF7 (Disc {disc}).cue")

    # .cue is shared by five disc systems, so the user picks one.
    importer.apply(importer.plan([tmp_path / "downloads"], hint="ps1"))

    target = importer.folder_for("ps1")
    assert len(list(target.glob("FF7*.cue"))) == 3


def test_ambiguous_disc_image_is_not_guessed(importer, tmp_path):
    """.cue belongs to PS1, Saturn, Sega CD, PC Engine CD and 3DO alike.

    Picking one at random would file the game where the user will never look
    for it, so it asks instead.
    """
    make(tmp_path / "downloads" / "Some Game.cue")

    plan = importer.plan([tmp_path / "downloads"])[0]

    assert plan.system_id is None
    assert "Could not tell" in plan.problem


def test_a_blocked_disc_stops_the_whole_set(importer, tmp_path):
    """Half a multi-disc game in the library is worse than none of it."""
    for disc in (1, 2):
        make(tmp_path / "downloads" / f"Game (Disc {disc}).cue")
    make(importer.folder_for("ps1") / "Game (Disc 2).cue", b"already here")

    outcome = importer.apply(importer.plan([tmp_path / "downloads"], hint="ps1"))

    assert outcome.files_moved == 0
    assert outcome.skipped
    # Disc 1 must still be where it started, not stranded.
    assert (tmp_path / "downloads" / "Game (Disc 1).cue").is_file()


def test_nothing_is_overwritten(importer, tmp_path):
    existing = make(importer.folder_for("snes") / "game.sfc", b"the original")
    make(tmp_path / "downloads" / "game.sfc", b"the new one")

    importer.apply(importer.plan([tmp_path / "downloads"]))

    assert existing.read_bytes() == b"the original"


def test_unidentified_games_are_skipped_with_a_reason(importer, tmp_path):
    make(tmp_path / "downloads" / "mystery.iso")

    outcome = importer.apply(importer.plan([tmp_path / "downloads"]))

    assert outcome.files_moved == 0
    assert len(outcome.skipped) == 1
    assert outcome.skipped[0][1]


def test_outcome_reports_every_move(importer, tmp_path):
    make(tmp_path / "downloads" / "a.sfc")
    make(tmp_path / "downloads" / "b.gba")

    outcome = importer.apply(importer.plan([tmp_path / "downloads"]))

    assert outcome.files_moved == 2
    assert all(target.is_file() for _source, target in outcome.moved)


# ── Folder games ──────────────────────────────────────────────────
#
# A PS3 title is a directory of thousands of files. Organising it means moving
# the directory; moving the EBOOT.BIN an emulator points at, the way a loose
# ROM would be moved, leaves a broken dump and an entry that no longer launches.

def ps3_game(root, name="Demon's Souls (USA)"):
    game = root / name
    make(game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN", b"eboot")
    make(game / "PS3_DISC.SFB")
    for junk in ("COALESCED_INT.bin", "GLOBALSHADERCACHE-PS3.bin"):
        make(game / "PS3_GAME" / "USRDIR" / junk)
    return game


def test_a_ps3_folder_is_one_plan(importer, tmp_path):
    """Not one per file inside it, which is what walking for ROMs produced."""
    ps3_game(tmp_path / "downloads")

    plans = importer.plan([tmp_path / "downloads"])

    assert len(plans) == 1
    assert plans[0].system_id == "ps3"
    assert plans[0].identified_by == "layout"
    assert plans[0].ok


def test_a_ps3_folder_pointed_at_directly_is_found(importer, tmp_path):
    """The user picks the game's own folder, not the shelf it sits on."""
    game = ps3_game(tmp_path / "downloads")

    plans = importer.plan([game])

    assert len(plans) == 1 and plans[0].system_id == "ps3"


def test_the_whole_ps3_folder_moves(importer, tmp_path):
    game = ps3_game(tmp_path / "downloads")

    outcome = importer.apply(importer.plan([game]))

    target = importer.folder_for("ps3") / "Demon's Souls (USA)"
    assert target.is_dir()
    assert (target / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").read_bytes() == b"eboot"
    assert (target / "PS3_GAME" / "USRDIR" / "COALESCED_INT.bin").is_file()
    assert not game.exists(), "the original folder should have moved"
    assert not outcome.errors


def test_a_file_from_inside_a_ps3_folder_moves_the_game(importer, tmp_path):
    """Dropping the EBOOT on the organiser must not extract it from its game."""
    game = ps3_game(tmp_path / "downloads")
    eboot = game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"

    plans = importer.plan([eboot])

    assert len(plans) == 1
    assert plans[0].folder is not None
    assert plans[0].source_paths == [game]

    importer.apply(plans)

    assert (
        importer.folder_for("ps3") / "Demon's Souls (USA)"
        / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"
    ).is_file()


def test_a_ps3_folder_dropped_twice_is_planned_once(importer, tmp_path):
    game = ps3_game(tmp_path / "downloads")

    plans = importer.plan([game, game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"])

    assert len(plans) == 1


def test_copying_a_ps3_folder_leaves_the_original(importer, tmp_path):
    game = ps3_game(tmp_path / "downloads")

    importer.apply(importer.plan([game]), move=False)

    assert (game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").is_file()
    assert (
        importer.folder_for("ps3") / "Demon's Souls (USA)"
        / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"
    ).is_file()


def test_an_existing_ps3_folder_is_never_merged_into(importer, tmp_path):
    """Half of one release over half of another launches, then fails later."""
    existing = importer.folder_for("ps3") / "Demon's Souls (USA)"
    make(existing / "PS3_GAME" / "USRDIR" / "EBOOT.BIN", b"the original")
    incoming = ps3_game(tmp_path / "downloads")

    plan = importer.plan([incoming])[0]
    assert not plan.ok
    assert "Already in your library" in plan.problem

    outcome = importer.apply([plan])

    assert outcome.skipped
    assert (existing / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").read_bytes() == b"the original"
    assert incoming.is_dir(), "the incoming dump should be untouched"


def test_a_ps3_folder_already_filed_is_left_alone(importer, tmp_path):
    game = ps3_game(importer.folder_for("ps3"))

    plan = importer.plan([game])[0]

    assert plan.problem == "Already filed here"


def test_folder_games_and_loose_roms_plan_together(importer, tmp_path):
    """The everyday mixed drop: it must not be one shape or the other."""
    ps3_game(tmp_path / "downloads")
    make(tmp_path / "downloads" / "Chrono Trigger.sfc")

    plans = importer.plan([tmp_path / "downloads"])

    assert {p.system_id for p in plans} == {"ps3", "snes"}
    assert {p.media_kind for p in plans} == {MediaKind.FOLDER, MediaKind.FILE}


def test_existing_systems_counts_folder_games(importer, tmp_path):
    """Counting only files reported a shelf of PS3 titles as empty."""
    ps3_game(importer.folder_for("ps3"), "Demon's Souls")
    ps3_game(importer.folder_for("ps3"), "Ni no Kuni")

    assert ("PlayStation 3", 2) in importer.existing_systems()


def test_existing_systems_lists_what_is_filed(importer, tmp_path):
    make(tmp_path / "downloads" / "a.sfc")
    make(tmp_path / "downloads" / "b.sfc")
    importer.apply(importer.plan([tmp_path / "downloads"]))

    assert importer.existing_systems() == [("Super Nintendo", 2)]


def test_empty_input_is_harmless(importer):
    outcome = importer.apply(importer.plan([]))
    assert outcome.files_moved == 0
    assert not outcome.errors


# ── Hash identification ───────────────────────────────────────────

class FakeDatabase:
    """OpenVGDB stand-in that identifies anything by hash."""

    def __init__(self):
        self.queried = False

    def available(self) -> bool:
        return True

    def identify(self, **kwargs):
        self.queried = True

        class Found:
            exact = True
            title = "Identified Game"

        return Found()


def test_hash_identification_is_preferred(tmp_path):
    database = FakeDatabase()
    importer = RomImporter(root=tmp_path / "ROMs", openvgdb=database)
    rom = make(tmp_path / "downloads" / "badly-named.sfc", b"content")

    plan = importer.plan([rom])[0]

    assert database.queried
    assert plan.identified_by == "hash"
    assert plan.system_id == "snes"
