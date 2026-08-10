"""Tests for the system registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from rose_gamelab.core.emulator import (
    SYSTEMS,
    all_rom_extensions,
    get_system,
    list_systems,
    systems_for_extension,
)


def test_nes_is_present():
    """Regression: the NES was missing from the registry entirely."""
    nes = get_system("nes")
    assert nes is not None
    assert ".nes" in nes.rom_extensions


def test_snes_does_not_claim_n64_extensions():
    """Regression: SNES listed .z64, which is an N64 extension."""
    assert ".z64" not in SYSTEMS["snes"].rom_extensions
    assert ".z64" in SYSTEMS["n64"].rom_extensions


def test_virtual_boy_and_gba_are_distinct_systems():
    """Regression: as Enum members with equal values these became aliases."""
    assert SYSTEMS["virtualboy"] is not SYSTEMS["gba"]
    assert SYSTEMS["virtualboy"].name == "Virtual Boy"


@pytest.mark.parametrize("system_id", ["ps3", "ps4", "xbox360"])
def test_previously_empty_systems_can_match_files(system_id):
    """Regression: these had empty extension lists and could never match."""
    assert SYSTEMS[system_id].rom_extensions


def test_every_system_except_pc_declares_extensions():
    missing = [
        s.id for s in SYSTEMS.values()
        if s.id != "pc" and not s.rom_extensions
    ]
    assert missing == []


def test_system_ids_match_their_keys():
    assert all(key == system.id for key, system in SYSTEMS.items())


# ── Matching ──────────────────────────────────────────────────────

def test_matches_rom_by_extension():
    assert SYSTEMS["gba"].matches_rom(Path("/roms/pokemon.gba"))


def test_matching_is_case_insensitive():
    assert SYSTEMS["gba"].matches_rom(Path("/roms/POKEMON.GBA"))


def test_does_not_match_foreign_extension():
    assert not SYSTEMS["gba"].matches_rom(Path("/roms/mario.nes"))


def test_system_without_extensions_matches_nothing():
    """A system with no extensions must match nothing, not everything.

    The old implementation returned True for an empty list, so one
    misconfigured entry would swallow every file in the library.
    """
    assert not SYSTEMS["pc"].matches_rom(Path("/games/anything.exe"))


# ── Lookup helpers ────────────────────────────────────────────────

def test_ambiguous_extension_returns_every_candidate():
    """.iso belongs to many systems; the caller disambiguates."""
    ids = {s.id for s in systems_for_extension(".iso")}
    assert {"ps2", "ps3", "psp"} <= ids


def test_extension_lookup_tolerates_a_missing_dot():
    assert systems_for_extension("nes") == systems_for_extension(".nes")


def test_unknown_extension_returns_empty():
    assert systems_for_extension(".notareal") == []


def test_all_extensions_covers_every_system():
    extensions = all_rom_extensions()
    assert ".nes" in extensions
    assert ".gba" in extensions
    assert ".chd" in extensions


def test_list_systems_returns_everything():
    assert len(list_systems()) == len(SYSTEMS)


def test_unknown_system_returns_none():
    assert get_system("nintendo-64-but-wrong") is None


# ── Disc-based flag ───────────────────────────────────────────────

@pytest.mark.parametrize("system_id", ["ps1", "ps2", "saturn", "dreamcast", "segacd"])
def test_disc_systems_are_flagged(system_id):
    assert SYSTEMS[system_id].disc_based


@pytest.mark.parametrize("system_id", ["nes", "snes", "gba", "gb"])
def test_cartridge_systems_are_not_disc_based(system_id):
    assert not SYSTEMS[system_id].disc_based
