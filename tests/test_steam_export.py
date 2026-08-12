"""Tests for exporting the library into Steam as non-Steam shortcuts.

The binary VDF format is undocumented, so these tests pin the exact byte
structure. If Valve changes it, these fail loudly rather than silently writing
a file Steam ignores.
"""

from __future__ import annotations

import binascii
from pathlib import Path

import pytest

from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database
from rose_gamelab.sources.steam_export import (
    Shortcut,
    SteamExporter,
    parse_shortcuts,
    serialise_shortcuts,
    shortcut_to_entry,
)


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "library.db")
    yield Library(db)
    db.close()


@pytest.fixture
def steam_root(tmp_path):
    """A fake Steam installation with one user profile."""
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "userdata" / "123456789" / "config").mkdir(parents=True)
    return root


# ── App id derivation ─────────────────────────────────────────────

def test_app_id_matches_steams_algorithm():
    """Steam derives the id from exe+name via CRC32 with the high bit set.

    Artwork filenames use this value; if it is wrong, Steam shows no art.
    """
    shortcut = Shortcut(app_name="Chrono Trigger", exe='"/usr/bin/rose-gamelab"', start_dir="/")

    expected = binascii.crc32(b'"/usr/bin/rose-gamelab"Chrono Trigger') | 0x80000000
    assert shortcut.app_id == expected


def test_app_id_is_stable():
    a = Shortcut(app_name="Game", exe="/x", start_dir="/")
    b = Shortcut(app_name="Game", exe="/x", start_dir="/")
    assert a.app_id == b.app_id


def test_different_games_get_different_ids():
    a = Shortcut(app_name="Game A", exe="/x", start_dir="/")
    b = Shortcut(app_name="Game B", exe="/x", start_dir="/")
    assert a.app_id != b.app_id


# ── Binary VDF round trip ─────────────────────────────────────────

def test_round_trips_a_single_shortcut():
    entry = shortcut_to_entry(
        Shortcut(app_name="Chrono Trigger", exe='"/bin/x"', start_dir='"/bin"')
    )

    parsed = parse_shortcuts(serialise_shortcuts([entry]))

    assert len(parsed) == 1
    assert parsed[0]["AppName"] == "Chrono Trigger"
    assert parsed[0]["Exe"] == '"/bin/x"'


def test_round_trips_several_shortcuts():
    entries = [
        shortcut_to_entry(Shortcut(app_name=f"Game {i}", exe="/x", start_dir="/"))
        for i in range(5)
    ]

    parsed = parse_shortcuts(serialise_shortcuts(entries))
    assert [e["AppName"] for e in parsed] == [f"Game {i}" for i in range(5)]


def test_tags_survive_the_round_trip():
    """Tags become Steam library categories, so they must not be lost."""
    entry = shortcut_to_entry(
        Shortcut(app_name="X", exe="/x", start_dir="/", tags=["Rose GameLab", "SNES"])
    )

    parsed = parse_shortcuts(serialise_shortcuts([entry]))
    assert parsed[0]["tags"] == ["Rose GameLab", "SNES"]


def test_integers_survive_the_round_trip():
    entry = shortcut_to_entry(Shortcut(app_name="X", exe="/x", start_dir="/"))
    parsed = parse_shortcuts(serialise_shortcuts([entry]))

    assert parsed[0]["AllowOverlay"] == 1
    assert parsed[0]["IsHidden"] == 0


def test_unicode_titles_survive():
    entry = shortcut_to_entry(
        Shortcut(app_name="NieR:Automata™ — 二ーア", exe="/x", start_dir="/")
    )
    parsed = parse_shortcuts(serialise_shortcuts([entry]))
    assert parsed[0]["AppName"] == "NieR:Automata™ — 二ーア"


def test_unknown_keys_survive_a_round_trip():
    """A shortcuts file written by another tool must not lose data."""
    entry = shortcut_to_entry(Shortcut(app_name="X", exe="/x", start_dir="/"))
    entry["SomeFutureKey"] = "value"

    parsed = parse_shortcuts(serialise_shortcuts([entry]))
    assert parsed[0]["SomeFutureKey"] == "value"


def test_empty_file_parses_to_nothing():
    assert parse_shortcuts(b"") == []


def test_empty_list_serialises_and_reparses():
    assert parse_shortcuts(serialise_shortcuts([])) == []


# ── Export ────────────────────────────────────────────────────────

def make_game(library, title: str = "Chrono Trigger", system: str = "snes"):
    game_id = library.add_game(title=title, system=system)
    library.add_launch_option(
        game_id, kind="emulator", target="/roms/ct.sfc", emulator="snes9x"
    )
    return library.get(game_id)


def test_export_writes_a_shortcuts_file(library, steam_root, monkeypatch):
    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: False)

    game = make_game(library)
    result = exporter.export([game], library)

    assert result.added == 1
    shortcuts = steam_root / "userdata" / "123456789" / "config" / "shortcuts.vdf"
    assert shortcuts.is_file()

    entries = parse_shortcuts(shortcuts.read_bytes())
    assert entries[0]["AppName"] == "Chrono Trigger"


def test_export_refuses_while_steam_is_running(library, steam_root, monkeypatch):
    """Steam rewrites shortcuts.vdf on exit and would discard our additions."""
    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: True)

    with pytest.raises(RuntimeError) as exc:
        exporter.export([make_game(library)], library)

    assert "Steam is running" in str(exc.value)


def test_force_overrides_the_running_check(library, steam_root, monkeypatch):
    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: True)

    result = exporter.export([make_game(library)], library, force=True)
    assert result.added == 1


def test_re_exporting_updates_rather_than_duplicating(library, steam_root, monkeypatch):
    """Steam shows duplicates as two identical library entries."""
    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: False)

    game = make_game(library)
    exporter.export([game], library)
    second = exporter.export([game], library)

    assert second.added == 0
    assert second.updated == 1

    shortcuts = steam_root / "userdata" / "123456789" / "config" / "shortcuts.vdf"
    assert len(parse_shortcuts(shortcuts.read_bytes())) == 1


def test_existing_shortcuts_are_preserved(library, steam_root, monkeypatch):
    """The user's own non-Steam games must survive our export."""
    config = steam_root / "userdata" / "123456789" / "config"
    existing = shortcut_to_entry(
        Shortcut(app_name="User's Own Game", exe="/usr/bin/thing", start_dir="/")
    )
    (config / "shortcuts.vdf").write_bytes(serialise_shortcuts([existing]))

    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: False)
    exporter.export([make_game(library)], library)

    names = {e["AppName"] for e in parse_shortcuts((config / "shortcuts.vdf").read_bytes())}
    assert "User's Own Game" in names
    assert "Chrono Trigger" in names


def test_export_backs_up_the_existing_file(library, steam_root, monkeypatch):
    config = steam_root / "userdata" / "123456789" / "config"
    (config / "shortcuts.vdf").write_bytes(serialise_shortcuts([]))

    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: False)
    result = exporter.export([make_game(library)], library)

    assert result.backup is not None and result.backup.is_file()


def test_exported_games_are_tagged(library, steam_root, monkeypatch):
    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: False)

    exporter.export([make_game(library)], library, collection_name="Retro")

    config = steam_root / "userdata" / "123456789" / "config"
    tags = parse_shortcuts((config / "shortcuts.vdf").read_bytes())[0]["tags"]

    assert SteamExporter.TAG in tags
    assert "Retro" in tags


def test_artwork_is_copied_with_steams_naming(library, steam_root, monkeypatch, tmp_path):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"\x00" * 32)

    game = make_game(library)
    library.update_game(game.id, cover_path=str(cover))
    game = library.get(game.id)

    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: False)
    result = exporter.export([game], library)

    assert result.artwork_copied == 1
    grid = steam_root / "userdata" / "123456789" / "config" / "grid"
    assert list(grid.glob("*p.jpg"))


def test_game_without_launch_options_is_reported(library, steam_root, monkeypatch):
    game_id = library.add_game(title="Orphan", system="snes")

    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: False)
    result = exporter.export([library.get(game_id)], library)

    assert result.added == 0
    assert result.errors


def test_removal_only_takes_our_own_shortcuts(library, steam_root, monkeypatch):
    config = steam_root / "userdata" / "123456789" / "config"
    theirs = shortcut_to_entry(
        Shortcut(app_name="Theirs", exe="/x", start_dir="/", tags=["Something Else"])
    )
    (config / "shortcuts.vdf").write_bytes(serialise_shortcuts([theirs]))

    exporter = SteamExporter(steam_root=steam_root)
    monkeypatch.setattr(exporter, "steam_is_running", lambda: False)
    exporter.export([make_game(library)], library)

    assert exporter.remove_exported() == 1

    remaining = parse_shortcuts((config / "shortcuts.vdf").read_bytes())
    assert [e["AppName"] for e in remaining] == ["Theirs"]


def test_missing_steam_is_reported_not_crashed(library, tmp_path, monkeypatch):
    exporter = SteamExporter(steam_root=tmp_path / "nowhere")
    # Must be stubbed: otherwise this test passes or fails depending on
    # whether Steam happens to be running on the machine executing it.
    monkeypatch.setattr(exporter, "steam_is_running", lambda: False)
    result = exporter.export([make_game(library)], library)

    assert result.errors
    assert result.added == 0
