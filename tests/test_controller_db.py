"""Recognising pads from the vendored SDL_GameControllerDB.

The database is the only way GameLab can know a SNES pad on a Raphnet adapter
or a PS2 pad on a Mayflash — hand-written vendor tables never cover those. These
tests pin the parsing, SDL's GUID matching rules, and the two things that would
break the feature silently: the file going missing from an installed wheel, and
Linux rows being mixed up with Windows ones.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core.controller import InputDevice, sdl_guid
from rose_gamelab.core.controller_db import (
    DATABASE_PATH,
    DatabaseEntry,
    load_database,
    lookup,
    normalise,
    parse_database,
    resolve,
    resolve_all,
    sdl_environment_for,
)


def device(name="Pad", vendor=0x054C, product=0x09CC, version=0x0100, bustype=0x0003):
    return InputDevice(
        name=name, vendor_id=vendor, product_id=product,
        bustype=bustype, version=version,
    )


# ── The vendored file ─────────────────────────────────────────────

def test_the_database_ships_with_the_package():
    """If this file is missing from an install, every pad silently downgrades
    to its family layout and nothing says so."""
    assert DATABASE_PATH.is_file()
    assert DATABASE_PATH.stat().st_size > 100_000


def test_the_database_licence_ships_beside_it():
    """Redistributing it obliges us to carry its licence."""
    assert (DATABASE_PATH.parent / "gamecontrollerdb.LICENSE").is_file()


def test_the_real_database_parses_to_a_useful_number_of_pads():
    entries = load_database()
    assert len(entries) > 500


def test_known_pads_are_recognised():
    """Spot-check the pads people are most likely to own."""
    for vendor, product, expected in [
        (0x054C, 0x09CC, "PS4"),      # DualShock 4 v2
        (0x054C, 0x0CE6, "PS5"),      # DualSense
        (0x045E, 0x028E, "Xbox"),     # Xbox 360
    ]:
        found = lookup(sdl_guid(0x0003, vendor, product, 0x0100))
        assert found is not None, (vendor, product)
        assert expected.lower() in found.name.lower()


# ── Parsing ───────────────────────────────────────────────────────

LINUX_ROW = (
    "030000004c050000cc09000000010000,PS4 Controller,"
    "a:b1,b:b2,x:b0,y:b3,platform:Linux,"
)
WINDOWS_ROW = (
    "030000004c050000cc09000000000000,PS4 Controller,"
    "a:b1,b:b2,x:b0,y:b3,platform:Windows,"
)


def test_only_linux_rows_are_used():
    """Windows rows describe the same pads through a different driver stack,
    so their button numbers are wrong here — using them looks like success and
    produces a scrambled layout."""
    entries = parse_database(WINDOWS_ROW)
    assert entries == {}

    assert parse_database(LINUX_ROW)


def test_comments_and_blank_lines_are_ignored():
    text = f"# a comment\n\n{LINUX_ROW}\n\n"
    assert len(parse_database(text)) == 1


def test_a_malformed_row_does_not_lose_the_rest():
    """The file is community maintained; one bad line must not cost the lot."""
    text = f"nonsense\n{LINUX_ROW}\nalso,nonsense\n"
    assert len(parse_database(text)) == 1


def test_matching_fields_are_not_treated_as_buttons():
    text = (
        "030000004c050000cc09000000010000,Pad,a:b1,crc:1234,"
        "hint:SDL_GAMECONTROLLER_USE_BUTTON_LABELS:=1,platform:Linux,"
    )
    entry = next(iter(parse_database(text).values()))
    assert "a" in entry.fields
    assert "crc" not in entry.fields
    assert "hint" not in entry.fields


def test_a_row_with_no_buttons_is_skipped():
    assert parse_database("030000004c050000cc09000000010000,Pad,platform:Linux,") == {}


# ── SDL's matching rules ──────────────────────────────────────────

def test_the_name_crc_is_cleared_before_matching():
    """SDL clears this field on both sides; database rows never carry one."""
    with_crc = "03000000" + "abcd" + "4c050000cc09000000010000"[4:]
    assert normalise(with_crc)[4:8] == "0000"


def test_an_exact_guid_matches():
    entries = parse_database(LINUX_ROW)
    assert lookup("030000004c050000cc09000000010000", entries) is not None


def test_a_different_hardware_revision_still_matches():
    """SDL retries with the version zeroed, because revisions of the same pad
    almost always share a layout. Without this a v3 DualShock reads as unknown.
    """
    entries = parse_database(LINUX_ROW)
    other_version = sdl_guid(0x0003, 0x054C, 0x09CC, 0x9999)

    assert lookup(other_version, entries) is not None


def test_a_genuinely_different_pad_does_not_match():
    entries = parse_database(LINUX_ROW)
    assert lookup(sdl_guid(0x0003, 0xDEAD, 0xBEEF, 0x0100), entries) is None


# ── Resolution ────────────────────────────────────────────────────

def test_a_known_pad_resolves_from_the_database():
    resolution = resolve(device(name="Wireless Controller"))

    assert resolution.recognised
    assert resolution.source == "database"
    assert "platform:Linux" in resolution.sdl_mapping


def test_an_unknown_pad_still_gets_a_complete_layout():
    """A miss is not a failure — the family layout is right more often than not."""
    resolution = resolve(device(name="Mystery Pad", vendor=0xDEAD, product=0xBEEF))

    assert not resolution.recognised
    assert resolution.source == "builtin"
    assert "a:" in resolution.sdl_mapping


def test_the_mapping_is_keyed_to_the_plugged_in_device():
    """Re-using the database's GUID would key the export to a revision the user
    does not own, and SDL would not match it."""
    pad = device(version=0x9999)
    expected = sdl_guid(pad.bustype, pad.vendor_id, pad.product_id, pad.version)

    assert resolve(pad).sdl_mapping.startswith(expected)


def test_a_comma_in_a_device_name_cannot_corrupt_the_mapping():
    """Commas terminate fields in this format, so one in a name would shift
    every field after it by a position."""
    mapping = resolve(device(name="Nasty, Pad")).sdl_mapping
    _guid, name, first_field, *_ = mapping.split(",")

    assert name == "Nasty  Pad"
    assert ":" in first_field  # the next field really is a mapping, not "Pad"


def test_entry_render_sorts_fields():
    entry = DatabaseEntry(guid="0" * 32, name="Pad", fields={"b": "b1", "a": "b0"})
    rendered = entry.render(guid="0" * 32, name="Pad")

    assert rendered.index("a:b0") < rendered.index("b:b1")


# ── The environment handed to a game ──────────────────────────────

def test_every_connected_pad_is_configured_at_once():
    """SDL reads the variable as newline-separated mappings, so multiplayer
    works without configuring anything per emulator."""
    env = sdl_environment_for([
        device(name="P1", vendor=0x054C, product=0x09CC),
        device(name="P2", vendor=0x045E, product=0x028E),
    ])

    assert len(env["SDL_GAMECONTROLLERCONFIG"].splitlines()) == 2


def test_hidapi_is_disabled_alongside_the_mapping():
    """HIDAPI re-reports pads under a different GUID and would ignore it."""
    env = sdl_environment_for([device()])
    assert env["SDL_JOYSTICK_HIDAPI"] == "0"


def test_no_controllers_means_no_environment():
    """An empty variable is not the same as an unset one; do not set it."""
    assert sdl_environment_for([]) == {}


def test_resolve_all_keeps_order():
    pads = [device(name="A"), device(name="B", vendor=0x045E, product=0x028E)]
    assert [r.device.name for r in resolve_all(pads)] == ["A", "B"]
