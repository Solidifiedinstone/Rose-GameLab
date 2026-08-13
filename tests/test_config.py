"""The YAML configuration file.

The emulator map here is keyed by system id, and drifted badly once: eight of
its keys were emulator names or typos ("xenia", "sega_sat", "wslan") and
twenty-two real systems were missing. Nothing failed loudly — every lookup is
by system id, so a path saved under one of those keys was simply never read
back. It is generated from the system registry now, and these tests keep it
that way.
"""

from __future__ import annotations

import yaml

from rose_gamelab.config import DEFAULT_CONFIG, Config
from rose_gamelab.core.emulator import SYSTEMS

# ── The emulator map ──────────────────────────────────────────────

def test_every_emulator_key_is_a_real_system_id():
    """A key that is not a system id can never be read back."""
    unknown = [key for key in DEFAULT_CONFIG["emulators"] if key not in SYSTEMS]
    assert unknown == []


def test_every_emulated_system_has_a_slot():
    missing = [
        system_id for system_id in SYSTEMS
        if system_id != "pc" and system_id not in DEFAULT_CONFIG["emulators"]
    ]
    assert missing == []


def test_pc_has_no_emulator_slot():
    assert "pc" not in DEFAULT_CONFIG["emulators"]


def test_emulator_paths_start_unset():
    assert set(DEFAULT_CONFIG["emulators"].values()) == {None}


def test_the_libretro_core_directory_is_spelled_the_way_retroarch_spells_it():
    """It is "cores", plural. The singular path never existed."""
    assert DEFAULT_CONFIG["emulator_defaults"]["libretro_core_dir"].endswith("cores")


# ── Loading and saving ────────────────────────────────────────────

def test_a_fresh_config_uses_the_defaults(tmp_path):
    config = Config(config_dir=str(tmp_path / "conf"))
    assert config.theme == DEFAULT_CONFIG["theme"]


def test_setting_an_emulator_persists(tmp_path):
    folder = str(tmp_path / "conf")
    config = Config(config_dir=folder)
    config.set_emulator("snes", "/usr/bin/snes9x")

    assert Config(config_dir=folder).emulators["snes"] == "/usr/bin/snes9x"


def test_saved_values_do_not_clobber_new_defaults(tmp_path):
    """Someone upgrading keeps their settings and gains the new ones."""
    folder = tmp_path / "conf"
    folder.mkdir()
    (folder / "settings.yaml").write_text(yaml.dump({"theme": "mine"}))

    config = Config(config_dir=str(folder))

    assert config.theme == "mine"
    assert config.behavior == DEFAULT_CONFIG["behavior"]


def test_an_empty_settings_file_is_not_an_error(tmp_path):
    folder = tmp_path / "conf"
    folder.mkdir()
    (folder / "settings.yaml").write_text("")

    assert Config(config_dir=str(folder)).theme == DEFAULT_CONFIG["theme"]


def test_editing_one_config_does_not_affect_another(tmp_path):
    """The defaults are shared module state; a mutable one leaks between them."""
    first = Config(config_dir=str(tmp_path / "one"))
    second = Config(config_dir=str(tmp_path / "two"))

    first.set_emulator("snes", "/usr/bin/snes9x")

    assert second.emulators["snes"] is None
    assert DEFAULT_CONFIG["emulators"]["snes"] is None


def test_dotted_get_and_set(tmp_path):
    config = Config(config_dir=str(tmp_path / "conf"))
    config.set("behavior.sort_by", "last_played")

    assert config.get("behavior.sort_by") == "last_played"
    assert config.get("behavior.nothing.here", "fallback") == "fallback"


def test_sources_round_trip(tmp_path):
    folder = str(tmp_path / "conf")
    config = Config(config_dir=folder)
    config.add_source({"id": "roms", "type": "rom_folder", "path": "/roms"})

    assert Config(config_dir=folder).sources[0]["path"] == "/roms"
