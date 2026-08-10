"""Tests for the Heroic source provider.

These build a synthetic Heroic config directory in tmp_path rather than reading
the developer's real one, so they pass on any machine and in CI — including
machines with no Heroic installed, which is the normal case.
"""

from __future__ import annotations

import json

import pytest

from rose_gamelab.sources.heroic import (
    HeroicProvider,
    extract_installed,
    extract_library,
    load_json,
)


def write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def heroic(tmp_path):
    """An empty Heroic config root."""
    root = tmp_path / "heroic"
    root.mkdir()
    return root


def epic_library(heroic, *games) -> None:
    write_json(heroic / "store_cache" / "legendary_library.json", {"library": list(games)})


def epic_installed(heroic, mapping) -> None:
    write_json(heroic / "legendary" / "installed.json", mapping)


def gog_library(heroic, *games) -> None:
    write_json(heroic / "store_cache" / "gog_library.json", {"games": list(games)})


def gog_installed(heroic, *games) -> None:
    write_json(heroic / "gog_store" / "installed.json", {"installed": list(games)})


HADES = {
    "app_name": "Min",
    "title": "Hades",
    "runner": "legendary",
    "is_installed": True,
    "art_square": "https://example.invalid/hades.png",
    "developer": "Supergiant Games",
    "install": {"platform": "Windows", "version": "1.0"},
}

WITCHER = {
    "app_name": "1207658924",
    "title": "The Witcher: Enhanced Edition",
    "runner": "gog",
    "is_installed": True,
}


# ── JSON helpers ──────────────────────────────────────────────────

def test_missing_file_is_not_an_error(tmp_path):
    assert load_json(tmp_path / "nope.json") is None


def test_malformed_json_returns_none(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert load_json(broken) is None


@pytest.mark.parametrize("wrapper", ["library", "games"])
def test_library_wrapper_key_variants(tmp_path, wrapper):
    """Heroic has used more than one wrapper key; both must parse."""
    assert extract_library({wrapper: [HADES]}, tmp_path) == [HADES]


def test_bare_list_library(tmp_path):
    assert extract_library([HADES], tmp_path) == [HADES]


def test_unknown_library_shape_yields_nothing(tmp_path):
    assert extract_library({"somethingElse": [HADES]}, tmp_path) == []


def test_legendary_installed_is_keyed_by_app_name(tmp_path):
    parsed = extract_installed({"Min": {"title": "Hades"}}, tmp_path)
    assert parsed == {"Min": {"title": "Hades"}}


def test_gogdl_installed_is_a_list_under_installed(tmp_path):
    parsed = extract_installed(
        {"installed": [{"appName": "1207658924", "install_path": "/games/witcher"}]}, tmp_path
    )
    assert parsed["1207658924"]["install_path"] == "/games/witcher"


# ── Discovery ─────────────────────────────────────────────────────

def test_discovers_installed_epic_game(heroic):
    epic_library(heroic, HADES)
    epic_installed(heroic, {"Min": {"app_name": "Min", "install_path": "/games/Hades"}})

    games = HeroicProvider(heroic_root=str(heroic)).discover()

    assert len(games) == 1
    assert games[0].name == "Hades"
    assert games[0].is_heroic is True
    assert games[0].metadata["store"] == "epic"
    assert games[0].metadata["install_path"] == "/games/Hades"


def test_discovers_installed_gog_game(heroic):
    gog_library(heroic, WITCHER)
    gog_installed(heroic, {"appName": "1207658924", "install_path": "/games/witcher"})

    game = HeroicProvider(heroic_root=str(heroic)).discover()[0]
    assert game.name == "The Witcher: Enhanced Edition"
    assert game.metadata["heroic_runner"] == "gog"


def test_owned_but_not_installed_games_are_excluded(heroic):
    """The library cache lists everything the account owns, not what is on disk."""
    owned = dict(HADES, app_name="NotHere", title="Owned Not Installed", is_installed=False)
    epic_library(heroic, HADES, owned)
    epic_installed(heroic, {"Min": {"app_name": "Min"}})

    assert [g.name for g in HeroicProvider(heroic_root=str(heroic)).discover()] == ["Hades"]


def test_installed_json_overrides_stale_is_installed_flag(heroic):
    """Regression: is_installed in the library cache is not proof of anything.

    A game removed outside Heroic keeps is_installed=True in the cache. The
    installed record is the runner's own bookkeeping and wins.
    """
    stale = dict(HADES, app_name="Removed", title="Removed Game", is_installed=True)
    epic_library(heroic, HADES, stale)
    epic_installed(heroic, {"Min": {"app_name": "Min"}})

    assert [g.name for g in HeroicProvider(heroic_root=str(heroic)).discover()] == ["Hades"]


def test_falls_back_to_is_installed_when_no_installed_record(heroic):
    epic_library(heroic, HADES, dict(HADES, app_name="Other", title="Other", is_installed=False))

    assert [g.name for g in HeroicProvider(heroic_root=str(heroic)).discover()] == ["Hades"]


def test_finds_legendary_config_in_either_location(heroic):
    """Heroic moved legendary's config to legendaryConfig/legendary/."""
    epic_library(heroic, HADES)
    write_json(
        heroic / "legendaryConfig" / "legendary" / "installed.json",
        {"Min": {"app_name": "Min"}},
    )

    assert [g.name for g in HeroicProvider(heroic_root=str(heroic)).discover()] == ["Hades"]


def test_installed_game_missing_from_library_keeps_its_app_name(heroic):
    """A game we cannot name is still importable — under its appName, labelled.

    Dropping it would be worse: the user can see and launch it in Heroic.
    """
    gog_installed(heroic, {"appName": "1207658924", "install_path": "/games/witcher"})

    game = HeroicProvider(heroic_root=str(heroic)).discover()[0]
    assert game.name == "1207658924"
    assert game.metadata["title_source"] == "app_name"
    assert game.metadata["in_library_cache"] is False


def test_dlc_is_not_imported_as_a_game(heroic):
    gog_library(heroic, WITCHER)
    gog_installed(
        heroic,
        {"appName": "1207658924"},
        {"appName": "9999", "is_dlc": True},
    )

    assert [g.metadata["app_name"] for g in HeroicProvider(heroic_root=str(heroic)).discover()] \
        == ["1207658924"]


def test_both_runners_are_scanned(heroic):
    epic_library(heroic, HADES)
    epic_installed(heroic, {"Min": {"app_name": "Min"}})
    gog_library(heroic, WITCHER)
    gog_installed(heroic, {"appName": "1207658924"})

    games = HeroicProvider(heroic_root=str(heroic)).discover()
    assert {g.name for g in games} == {"Hades", "The Witcher: Enhanced Edition"}


def test_same_app_name_under_two_runners_is_two_games(heroic):
    """Regression: keying on appName alone collapses unrelated games.

    Epic and GOG allocate app names independently and can collide.
    """
    epic_library(heroic, dict(HADES, app_name="1207658924", title="Epic Game"))
    epic_installed(heroic, {"1207658924": {"app_name": "1207658924"}})
    gog_library(heroic, WITCHER)
    gog_installed(heroic, {"appName": "1207658924"})

    assert len(HeroicProvider(heroic_root=str(heroic)).discover()) == 2


def test_corrupt_library_does_not_lose_the_other_runner(heroic):
    (heroic / "store_cache").mkdir()
    (heroic / "store_cache" / "legendary_library.json").write_text("{ broken")
    gog_library(heroic, WITCHER)
    gog_installed(heroic, {"appName": "1207658924"})

    games = HeroicProvider(heroic_root=str(heroic)).discover()
    assert [g.name for g in games] == ["The Witcher: Enhanced Edition"]


# ── Launching ─────────────────────────────────────────────────────

def test_launches_via_heroic_url_with_runner(heroic):
    """Regression: heroic://launch/<appName> without the runner is ambiguous.

    Running the game's executable directly skips the Wine prefix and the
    per-game environment Heroic sets up, so it must go through Heroic.
    """
    gog_library(heroic, WITCHER)
    gog_installed(heroic, {"appName": "1207658924"})

    game = HeroicProvider(heroic_root=str(heroic)).discover()[0]
    assert game.path == "heroic://launch/gog/1207658924"


def test_launch_command_is_never_empty(heroic):
    """An empty argv would read as 'nothing to do' rather than 'not found'."""
    command = HeroicProvider.launch_command("heroic://launch/gog/1")
    assert command
    assert command[-1] == "heroic://launch/gog/1"


# ── Provider interface ────────────────────────────────────────────

def test_reads_default_install_path(heroic):
    write_json(heroic / "config.json", {"defaultSettings": {"defaultInstallPath": "/games"}})
    assert HeroicProvider(heroic_root=str(heroic)).default_install_path() == "/games"


def test_missing_config_json_is_not_an_error(heroic):
    assert HeroicProvider(heroic_root=str(heroic)).default_install_path() is None


def test_missing_heroic_is_not_an_error(tmp_path):
    provider = HeroicProvider(heroic_root=str(tmp_path / "nope"))
    assert provider.validate() is False
    assert provider.discover() == []


def test_empty_heroic_directory_is_not_a_valid_source(heroic):
    """Heroic creates its config dir on first run; empty means never signed in."""
    assert HeroicProvider(heroic_root=str(heroic)).validate() is False


def test_validates_with_data_present(heroic):
    gog_installed(heroic, {"appName": "1207658924"})
    assert HeroicProvider(heroic_root=str(heroic)).validate() is True


def test_source_def(heroic):
    gog_installed(heroic, {"appName": "1207658924"})
    definition = HeroicProvider(heroic_root=str(heroic)).get_def()
    assert (definition.id, definition.type, definition.system) == ("heroic", "heroic", "pc")


def test_unknown_runner_is_a_programming_error(heroic):
    """Silently returning [] for a typo'd runner hides the bug."""
    with pytest.raises(ValueError):
        HeroicProvider(heroic_root=str(heroic)).discover_runner("steam")
