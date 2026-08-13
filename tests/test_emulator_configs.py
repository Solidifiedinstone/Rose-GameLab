"""Backing up emulator configuration.

The interesting part is what is *not* copied. An emulator's configuration
directory also holds firmware, installed games, caches and save data, and a
naive copy of one produced a 207 MB "configuration backup" on a real machine —
189 MB of which was PS3 firmware, and 17 MB save data that `core/saves.py`
already looks after.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core import emulator_configs
from rose_gamelab.core.emulator_configs import ConfigLocation, back_up, list_backups


@pytest.fixture
def emulator(tmp_path):
    """A fake emulator configuration directory with junk mixed in."""
    directory = tmp_path / "config" / "pretendstation"
    (directory / "inis").mkdir(parents=True)
    (directory / "inis" / "settings.ini").write_text("[Graphics]\nupscale=3\n")
    (directory / "controllers.ini").write_text("[Pad1]\nA=Cross\n")

    (directory / "cache").mkdir()
    (directory / "cache" / "shaders.bin").write_bytes(b"\x00" * 4096)
    (directory / "memcards").mkdir()
    (directory / "memcards" / "card1.mcd").write_bytes(b"\x00" * 8192)

    return ConfigLocation(
        "PretendStation", directory, skip=("cache", "memcards")
    )


def test_configuration_is_copied(tmp_path, emulator):
    result = back_up(root=tmp_path / "backups", locations=[emulator])

    assert result.emulators == ["PretendStation"]
    assert result.files == 2
    assert (result.directory / "PretendStation" / "controllers.ini").is_file()


def test_the_directory_layout_is_preserved(tmp_path, emulator):
    """A backup nobody can restore by hand is a hostage, not a backup."""
    result = back_up(root=tmp_path / "backups", locations=[emulator])

    copied = result.directory / "PretendStation" / "inis" / "settings.ini"
    assert copied.is_file()
    assert "upscale=3" in copied.read_text()


def test_caches_are_not_copied(tmp_path, emulator):
    result = back_up(root=tmp_path / "backups", locations=[emulator])

    assert not (result.directory / "PretendStation" / "cache").exists()


def test_save_data_is_not_copied(tmp_path, emulator):
    """Saves have their own backup; duplicating them here wastes the space and
    blurs which feature owns them."""
    result = back_up(root=tmp_path / "backups", locations=[emulator])

    assert not (result.directory / "PretendStation" / "memcards").exists()


def test_nothing_installed_is_not_an_error(tmp_path):
    result = back_up(root=tmp_path / "backups", locations=[])

    assert result.files == 0
    assert "No emulator configuration" in result.summary


def test_a_directory_that_does_not_exist_is_skipped(tmp_path):
    missing = ConfigLocation("Ghost", tmp_path / "nope")

    assert not missing.exists()
    assert back_up(root=tmp_path / "backups", locations=[missing]).files == 0


def test_each_backup_is_its_own_folder(tmp_path, emulator):
    first = back_up(root=tmp_path / "backups", locations=[emulator], label="one")
    second = back_up(root=tmp_path / "backups", locations=[emulator], label="two")

    assert first.directory != second.directory
    assert len(list_backups(tmp_path / "backups")) == 2


def test_backups_are_listed_newest_first(tmp_path, emulator):
    back_up(root=tmp_path / "backups", locations=[emulator], label="older")
    back_up(root=tmp_path / "backups", locations=[emulator], label="newer")

    names = [path.name for path in list_backups(tmp_path / "backups")]

    assert len(names) == 2
    assert names == sorted(names, reverse=True)


def test_listing_with_no_backups_is_empty(tmp_path):
    assert list_backups(tmp_path / "never-used") == []


# ── The real locations ────────────────────────────────────────────

def test_every_known_location_names_an_emulator():
    for location in emulator_configs.known_locations():
        assert location.emulator


def test_flatpak_and_ordinary_paths_are_both_considered(monkeypatch, tmp_path):
    """Which one exists is a property of the machine; checking beats guessing."""
    monkeypatch.setattr(emulator_configs, "_config_home", lambda: tmp_path / "config")
    monkeypatch.setattr(emulator_configs, "_data_home", lambda: tmp_path / "data")
    monkeypatch.setattr(emulator_configs.Path, "home", staticmethod(lambda: tmp_path))

    (tmp_path / ".var/app/net.pcsx2.PCSX2/config/PCSX2").mkdir(parents=True)

    found = emulator_configs.known_locations()

    assert any(location.emulator == "PCSX2" for location in found)


def test_firmware_and_installed_games_are_always_skipped():
    """189 MB of PS3 firmware and a 15 GB game directory live under RPCS3's
    configuration folder."""
    for location in emulator_configs.known_locations():
        if location.emulator != "RPCS3":
            continue
        assert "dev_flash" in location.skip
        assert "dev_hdd0" in location.skip
