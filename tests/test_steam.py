"""Tests for the Steam source provider.

These use synthetic appmanifest files rather than the developer's real Steam
install, so they pass on any machine and in CI.
"""

from __future__ import annotations

import pytest

from rose_gamelab.sources.steam import (
    STATE_FULLY_INSTALLED,
    SteamProvider,
    parse_vdf_dict,
    parse_vdf_pairs,
)

INSTALLED_MANIFEST = """\
"AppState"
{
\t"appid"\t\t"1145360"
\t"Universe"\t\t"1"
\t"name"\t\t"Hades"
\t"StateFlags"\t\t"4"
\t"installdir"\t\t"Hades"
\t"LastUpdated"\t\t"1735689600"
\t"SizeOnDisk"\t\t"15728640000"
}
"""

UPDATE_REQUIRED_MANIFEST = INSTALLED_MANIFEST.replace('"StateFlags"\t\t"4"', '"StateFlags"\t\t"2"')


@pytest.fixture
def library(tmp_path):
    """A fake Steam root with a steamapps directory."""
    steamapps = tmp_path / "steamapps"
    (steamapps / "common").mkdir(parents=True)
    return tmp_path


def write_manifest(library, appid: str, content: str) -> None:
    (library / "steamapps" / f"appmanifest_{appid}.acf").write_text(content)


# ── VDF parsing ───────────────────────────────────────────────────

def test_parses_key_value_pairs():
    fields = parse_vdf_dict(INSTALLED_MANIFEST)
    assert fields["appid"] == "1145360"
    assert fields["name"] == "Hades"
    assert fields["StateFlags"] == "4"


def test_extracts_value_not_key():
    """Regression: splitting on '\"' returns the KEY at index 1, not the value.

    The previous implementation set every game's appid to the literal string
    "appid" because of this.
    """
    fields = parse_vdf_dict('"appid"\t\t"228980"')
    assert fields["appid"] == "228980"
    assert fields["appid"] != "appid"


def test_state_flags_parse_despite_quotes():
    """Regression: int('"4"') raises ValueError; quotes must be stripped."""
    assert int(parse_vdf_dict(INSTALLED_MANIFEST)["StateFlags"]) == 4


def test_handles_escaped_windows_paths():
    pairs = parse_vdf_pairs(r'"path"		"D:\\SteamLibrary"')
    assert pairs == [("path", r"D:\SteamLibrary")]


def test_preserves_pair_order_for_repeated_keys():
    pairs = parse_vdf_pairs('"path" "/a"\n"path" "/b"')
    assert [v for k, v in pairs if k == "path"] == ["/a", "/b"]


# ── Manifest interpretation ───────────────────────────────────────

def test_discovers_installed_game(library):
    write_manifest(library, "1145360", INSTALLED_MANIFEST)
    games = SteamProvider(steam_root=str(library)).discover()

    assert len(games) == 1
    assert games[0].name == "Hades"
    assert games[0].metadata["steam_appid"] == 1145360
    assert games[0].is_steam is True


def test_launches_via_steam_url_not_binary(library):
    """Launching the exe directly bypasses Proton, cloud saves and the overlay."""
    write_manifest(library, "1145360", INSTALLED_MANIFEST)
    game = SteamProvider(steam_root=str(library)).discover()[0]
    assert game.path == "steam://run/1145360"


def test_skips_games_needing_an_update(library):
    """Regression: StateFlags & 2 means 'update required', NOT 'installed'.

    The old code used bit 2, so it matched games that cannot be launched.
    """
    write_manifest(library, "1145360", UPDATE_REQUIRED_MANIFEST)
    assert SteamProvider(steam_root=str(library)).discover() == []


def test_installed_flag_is_bit_four():
    assert STATE_FULLY_INSTALLED == 4


def test_filters_out_runtimes_and_redistributables(library):
    write_manifest(library, "228980", INSTALLED_MANIFEST
                   .replace('"1145360"', '"228980"')
                   .replace('"Hades"', '"Steamworks Common Redistributables"'))
    write_manifest(library, "1628350", INSTALLED_MANIFEST
                   .replace('"1145360"', '"1628350"')
                   .replace('"Hades"', '"Steam Linux Runtime 3.0 (sniper)"'))
    write_manifest(library, "2180100", INSTALLED_MANIFEST
                   .replace('"1145360"', '"2180100"')
                   .replace('"Hades"', '"Proton Hotfix"'))

    assert SteamProvider(steam_root=str(library)).discover() == []


def test_ignores_malformed_manifests(library):
    write_manifest(library, "999", "this is not a vdf file at all")
    write_manifest(library, "1145360", INSTALLED_MANIFEST)

    games = SteamProvider(steam_root=str(library)).discover()
    assert [g.name for g in games] == ["Hades"]


def test_records_install_path_when_directory_exists(library):
    (library / "steamapps" / "common" / "Hades").mkdir()
    write_manifest(library, "1145360", INSTALLED_MANIFEST)

    game = SteamProvider(steam_root=str(library)).discover()[0]
    assert game.metadata["install_path"].endswith("common/Hades")


# ── Library folders ───────────────────────────────────────────────

def test_finds_secondary_libraries(library, tmp_path):
    """Regression: libraryfolders.vdf lives inside steamapps/, not beside it.

    The old code looked one directory too high and so never found games on
    secondary drives.
    """
    second = tmp_path / "other_drive"
    (second / "steamapps").mkdir(parents=True)

    (library / "steamapps" / "libraryfolders.vdf").write_text(f'''
    "libraryfolders"
    {{
        "0" {{ "path" "{library}" }}
        "1" {{ "path" "{second}" }}
    }}
    ''')

    write_manifest(library, "1145360", INSTALLED_MANIFEST)
    (second / "steamapps" / "appmanifest_440.acf").write_text(
        INSTALLED_MANIFEST.replace('"1145360"', '"440"').replace('"Hades"', '"Team Fortress 2"')
    )

    games = SteamProvider(steam_root=str(library)).discover()
    assert {g.name for g in games} == {"Hades", "Team Fortress 2"}


def test_deduplicates_same_game_across_libraries(library, tmp_path):
    second = tmp_path / "other_drive"
    (second / "steamapps").mkdir(parents=True)
    (library / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ "0" {{ "path" "{second}" }} }}'
    )

    write_manifest(library, "1145360", INSTALLED_MANIFEST)
    (second / "steamapps" / "appmanifest_1145360.acf").write_text(INSTALLED_MANIFEST)

    assert len(SteamProvider(steam_root=str(library)).discover()) == 1


def test_missing_steam_is_not_an_error(tmp_path):
    provider = SteamProvider(steam_root=str(tmp_path / "nope"))
    assert provider.validate() is False
    assert provider.discover() == []
