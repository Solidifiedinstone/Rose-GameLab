"""Verifying ROMs against No-Intro and Redump catalogues."""

from __future__ import annotations

import pytest

from rose_gamelab.core.library import Library
from rose_gamelab.core.rom_health import (
    DatEntry,
    DatIndex,
    Health,
    check_library,
    load_dats,
    normalise_name,
    parse_clrmamepro,
    parse_dat,
    parse_logiqx,
    problems,
    summarise,
    verify,
)
from rose_gamelab.db.database import Database

LOGIQX = """<?xml version="1.0"?>
<datafile>
  <header>
    <name>Nintendo - Super Nintendo Entertainment System</name>
    <version>20260101</version>
  </header>
  <game name="Super Mario World (USA)">
    <description>Super Mario World (USA)</description>
    <rom name="Super Mario World (USA).sfc" size="524288"
         crc="B19ED489" md5="cdd3c8c37322978ca8669b34bc89c804"
         sha1="6b47bb75d16514b6a476aa0c73a683a2a4c18765"/>
  </game>
  <game name="Chrono Trigger (USA)">
    <rom name="Chrono Trigger (USA).sfc" size="4194304" crc="2D206BF7"
         sha1="de5b8d4a6f0c9b1e3f2a7c8d9e0f1a2b3c4d5e6f"/>
  </game>
  <game name="Broken Game (USA)">
    <rom name="Broken Game (USA).sfc" size="1024" crc="DEADBEEF" status="baddump"/>
  </game>
</datafile>
"""

CLRMAMEPRO = """
clrmamepro (
	name "Sega - Mega Drive"
)

game (
	name "Sonic The Hedgehog (USA, Europe)"
	description "Sonic The Hedgehog (USA, Europe)"
	rom ( name "Sonic The Hedgehog (USA, Europe).md" size 524288 crc AFCE0E3B md5 1bc674be034e43c96b86487ac69d9293 sha1 ea9b2c1c5f6c4b31a2f7e1d0a9b8c7d6e5f4a3b2 )
)
"""


@pytest.fixture
def index():
    built = DatIndex()
    built.extend(parse_logiqx(LOGIQX, catalogue="snes"))
    return built


# ── Parsing ───────────────────────────────────────────────────────

def test_logiqx_entries_are_read():
    entries = parse_logiqx(LOGIQX)

    assert len(entries) == 3
    assert entries[0].game_name == "Super Mario World (USA)"
    assert entries[0].crc32 == "b19ed489"      # normalised to lower case


def test_the_catalogue_name_comes_from_the_header():
    assert "Super Nintendo" in parse_logiqx(LOGIQX)[0].catalogue


def test_clrmamepro_entries_are_read():
    entries = parse_clrmamepro(CLRMAMEPRO, catalogue="megadrive")

    assert len(entries) == 1
    assert entries[0].game_name == "Sonic The Hedgehog (USA, Europe)"
    assert entries[0].crc32 == "afce0e3b"
    assert entries[0].size == 524288


def test_the_dialect_is_detected_automatically():
    assert len(parse_dat(LOGIQX)) == 3
    assert len(parse_dat(CLRMAMEPRO)) == 1


def test_malformed_xml_does_not_raise():
    assert parse_dat("<datafile><game><rom broken=") == []


def test_an_empty_file_is_not_an_error():
    assert parse_dat("") == []


# ── Name normalising ──────────────────────────────────────────────

def test_region_and_dump_tags_are_ignored_when_comparing_names():
    assert normalise_name("Super Mario World (USA).sfc") == normalise_name(
        "Super Mario World (Europe) [!].smc"
    )


def test_different_games_do_not_normalise_together():
    assert normalise_name("Chrono Trigger.sfc") != normalise_name("Chrono Cross.sfc")


# ── Verifying ─────────────────────────────────────────────────────

def test_a_good_dump_verifies_by_sha1(index):
    result = verify(index, sha1="6B47BB75D16514B6A476AA0C73A683A2A4C18765")

    assert result.health is Health.VERIFIED
    assert result.health.is_good
    assert "Super Mario World" in result.summary


def test_a_good_dump_verifies_by_crc32(index):
    """No-Intro indexes on CRC32, and older sets publish nothing else."""
    assert verify(index, crc32="b19ed489").health is Health.VERIFIED


def test_a_good_dump_verifies_by_md5(index):
    assert verify(index, md5="cdd3c8c37322978ca8669b34bc89c804").health is Health.VERIFIED


def test_hashes_match_regardless_of_case(index):
    assert verify(index, crc32="B19ED489").health is Health.VERIFIED


def test_a_known_bad_dump_is_reported_as_such(index):
    """Matching the catalogue is a match, but not good news."""
    result = verify(index, crc32="deadbeef")

    assert result.health is Health.KNOWN_BAD
    assert not result.health.is_good
    assert "bad dump" in result.summary


def test_a_file_claiming_a_catalogued_name_but_not_matching_is_flagged(index):
    """The usual sign of a hack, a translation, or a bad dump."""
    result = verify(index, sha1="0" * 40, name="Super Mario World (USA).sfc")

    assert result.health is Health.MODIFIED
    assert result.expected.game_name == "Super Mario World (USA)"
    assert "hack" in result.summary


def test_something_nobody_catalogued_is_unknown(index):
    result = verify(index, sha1="1" * 40, name="Homebrew Thing.sfc")

    assert result.health is Health.UNKNOWN


def test_an_empty_index_says_so_rather_than_guessing(index):
    """"We have no catalogue" must not look like "your ROM is bad"."""
    result = verify(DatIndex(), sha1="6b47bb75d16514b6a476aa0c73a683a2a4c18765")

    assert result.health is Health.NOT_CATALOGUED


def test_a_file_with_no_hashes_at_all_is_unknown(index):
    assert verify(index, name="Mystery.sfc").health is Health.UNKNOWN


def test_the_stronger_hash_decides(index):
    """CRC32 is 32 bits and collides; a SHA-1 match outranks it."""
    index.by_crc32["b19ed489"] = DatEntry(
        game_name="Collision", rom_name="Collision.sfc", crc32="b19ed489",
        status="baddump",
    )

    result = verify(
        index,
        crc32="b19ed489",
        sha1="6b47bb75d16514b6a476aa0c73a683a2a4c18765",
    )

    assert result.health is Health.VERIFIED


# ── Loading a folder ──────────────────────────────────────────────

def test_a_folder_of_dats_loads(tmp_path):
    (tmp_path / "snes.dat").write_text(LOGIQX)
    (tmp_path / "megadrive.dat").write_text(CLRMAMEPRO)

    index = load_dats(tmp_path)

    assert not index.empty
    assert len(index.catalogues) == 2
    assert verify(index, crc32="afce0e3b").health is Health.VERIFIED


def test_one_broken_dat_does_not_lose_the_others(tmp_path):
    (tmp_path / "good.dat").write_text(LOGIQX)
    (tmp_path / "truncated.dat").write_text("<datafile><game><rom nam")

    index = load_dats(tmp_path)

    assert verify(index, crc32="b19ed489").health is Health.VERIFIED


def test_a_missing_folder_is_not_an_error(tmp_path):
    assert load_dats(tmp_path / "nope").empty


def test_files_that_are_not_dats_are_ignored(tmp_path):
    (tmp_path / "notes.txt").write_text("not a dat")
    (tmp_path / "snes.dat").write_text(LOGIQX)

    assert len(load_dats(tmp_path).catalogues) == 1


# ── Across a library ──────────────────────────────────────────────

@pytest.fixture
def library(tmp_path):
    database = Database(tmp_path / "library.db")
    yield Library(database)
    database.close()


def add_hashed_game(library, title, *, sha1=None, crc32=None, path="/roms/g.sfc",
                    system="snes"):
    game_id = library.add_game(title=title, system=system, path=path)
    library.add_file(game_id, path)
    library.db.execute(
        "UPDATE game_files SET sha1 = ?, crc32 = ? WHERE game_id = ?",
        (sha1, crc32, game_id),
    )
    return game_id


def test_a_library_is_verified_end_to_end(library, index):
    add_hashed_game(
        library, "Super Mario World",
        sha1="6b47bb75d16514b6a476aa0c73a683a2a4c18765",
        path="/roms/Super Mario World (USA).sfc",
    )
    add_hashed_game(library, "Some Hack", sha1="f" * 40, path="/roms/Some Hack.sfc")

    results = check_library(library, index)
    counts = summarise(results)

    assert counts[Health.VERIFIED] == 1
    assert counts[Health.UNKNOWN] == 1


def test_unhashed_files_are_skipped_not_condemned(library, index):
    """"We have not looked" is not the user's problem to fix."""
    library.add_game(title="Never Hashed", system="snes", path="/roms/x.sfc")

    assert check_library(library, index) == []


def test_only_problems_are_listed_as_problems(library, index):
    add_hashed_game(
        library, "Good", sha1="6b47bb75d16514b6a476aa0c73a683a2a4c18765",
        path="/roms/Super Mario World (USA).sfc",
    )
    add_hashed_game(
        library, "Bad", crc32="deadbeef", path="/roms/Broken Game (USA).sfc",
    )

    assert [item.title for item in problems(check_library(library, index))] == ["Bad"]


def test_verification_can_be_limited_to_one_system(library, index):
    add_hashed_game(library, "A SNES game", sha1="a" * 40)
    add_hashed_game(library, "A PS1 game", sha1="b" * 40, path="/roms/p.bin",
                    system="ps1")

    results = check_library(library, index, system="snes")

    assert [item.title for item in results] == ["A SNES game"]


def test_no_catalogue_means_no_claims_about_the_library(library):
    add_hashed_game(library, "Anything", sha1="a" * 40)

    assert check_library(library, DatIndex()) == []
