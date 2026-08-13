"""Finding installed Proton builds.

The profile field for this used to be a text box, so somebody had to know
their build is called exactly "GE-Proton10-34". Every build is a directory
with its version in the name, so nobody should have to remember it.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core import proton
from rose_gamelab.core.proton import ProtonVersion


def make_proton(root, name, *, real=True):
    directory = root / name
    directory.mkdir(parents=True)
    if real:
        (directory / "proton").write_text("#!/usr/bin/env python\n")
    return directory


@pytest.fixture
def steam(tmp_path, monkeypatch):
    """A Steam installation with nothing in it yet."""
    root = tmp_path / ".steam" / "steam"
    (root / "compatibilitytools.d").mkdir(parents=True)
    (root / "steamapps" / "common").mkdir(parents=True)
    monkeypatch.setattr(proton, "STEAM_ROOTS", (str(root),))
    monkeypatch.setattr(proton, "HEROIC_ROOTS", ())
    monkeypatch.setattr(proton, "_steam_library_folders", list)
    return root


def test_community_builds_are_found(steam):
    make_proton(steam / "compatibilitytools.d", "GE-Proton10-34")

    found = proton.installed()

    assert [v.name for v in found] == ["GE-Proton10-34"]
    assert found[0].source == "custom"


def test_steams_own_builds_are_found(steam):
    make_proton(steam / "steamapps" / "common", "Proton 9.0 (Beta)")

    assert [v.name for v in proton.installed()] == ["Proton 9.0 (Beta)"]


def test_games_are_not_mistaken_for_proton(steam):
    """steamapps/common holds the games as well."""
    make_proton(steam / "steamapps" / "common", "Half-Life 2")
    make_proton(steam / "steamapps" / "common", "Proton 8.0")

    assert [v.name for v in proton.installed()] == ["Proton 8.0"]


def test_an_empty_directory_is_not_a_build(steam):
    make_proton(steam / "compatibilitytools.d", "GE-Proton10-34", real=False)

    assert proton.installed() == []


def test_community_builds_come_first(steam):
    """Somebody who installed Proton-GE did it deliberately."""
    make_proton(steam / "steamapps" / "common", "Proton 8.0")
    make_proton(steam / "compatibilitytools.d", "GE-Proton10-34")

    assert proton.installed()[0].name == "GE-Proton10-34"


def test_version_ten_sorts_above_version_nine(steam):
    """Plain string ordering puts GE-Proton9 above GE-Proton10, which is the
    wrong build and exactly the one somebody is trying to pick."""
    for name in ("GE-Proton9-20", "GE-Proton10-34", "GE-Proton10-9"):
        make_proton(steam / "compatibilitytools.d", name)

    assert [v.name for v in proton.installed()] == [
        "GE-Proton10-34", "GE-Proton10-9", "GE-Proton9-20",
    ]


def test_the_same_build_seen_twice_is_listed_once(tmp_path, monkeypatch):
    """~/.steam/root is usually a symlink to the same installation."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    for root in (first, second):
        make_proton(root / "compatibilitytools.d", "GE-Proton10-34")

    monkeypatch.setattr(proton, "STEAM_ROOTS", (str(first), str(second)))
    monkeypatch.setattr(proton, "HEROIC_ROOTS", ())
    monkeypatch.setattr(proton, "_steam_library_folders", list)

    assert len(proton.installed()) == 1


def test_builds_on_another_drive_are_found(tmp_path, monkeypatch):
    """Steam libraries live on other drives as often as not."""
    drive = tmp_path / "mnt" / "games" / "steamapps"
    make_proton(drive / "common", "Proton - Experimental")

    monkeypatch.setattr(proton, "STEAM_ROOTS", ())
    monkeypatch.setattr(proton, "HEROIC_ROOTS", ())
    monkeypatch.setattr(proton, "_steam_library_folders", lambda: [drive])

    assert [v.name for v in proton.installed()] == ["Proton - Experimental"]


def test_a_missing_steam_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(proton, "STEAM_ROOTS", (str(tmp_path / "nope"),))
    monkeypatch.setattr(proton, "HEROIC_ROOTS", ())
    monkeypatch.setattr(proton, "_steam_library_folders", list)

    assert proton.installed() == []


def test_finding_one_by_name(steam):
    make_proton(steam / "compatibilitytools.d", "GE-Proton10-34")

    assert proton.find("GE-Proton10-34") is not None
    assert proton.find("GE-Proton3-1") is None
    assert proton.find(None) is None


def test_the_source_is_shown_for_steams_own(steam):
    """Two builds can share a name across sources."""
    version = ProtonVersion(name="Proton 8.0", path=steam, source="steam")
    assert "steam" in version.label
