"""Tests for finding installed emulators.

Detection reaches out to the machine it runs on, so these tests fix the parts
that must hold regardless of what happens to be installed.
"""

from __future__ import annotations

import os

import pytest

from rose_gamelab.core import emulator_detect
from rose_gamelab.core.emulator import SYSTEMS
from rose_gamelab.core.emulator_detect import (
    CANDIDATES,
    NO_LIBRETRO_CORE,
    EmulatorOption,
    best_for,
    options_for,
    summary,
)


def test_every_emulated_system_has_a_candidate():
    """A system with no candidate can never tell the user what to install."""
    uncovered = [s for s in SYSTEMS if s != "pc" and s not in CANDIDATES]
    assert uncovered == []


def test_popular_systems_offer_an_alternative():
    """The systems people actually own should list more than one option."""
    for system_id in ("nes", "snes", "ps1", "ps2", "nds", "megadrive", "arcade"):
        assert len(CANDIDATES[system_id]) >= 2, system_id


def test_every_candidate_is_well_formed():
    for system_id, candidates in CANDIDATES.items():
        for candidate in candidates:
            assert candidate["name"], system_id
            assert candidate["binaries"], system_id


def test_retroarch_is_offered_as_a_fallback():
    """RetroArch covers most retro systems through cores."""
    names = [o.name for o in options_for("snes")]
    assert any("RetroArch" in n for n in names)


def test_installed_options_sort_first():
    options = options_for("snes")
    installed = [i for i, o in enumerate(options) if o.installed]
    uninstalled = [i for i, o in enumerate(options) if not o.installed]
    if installed and uninstalled:
        assert max(installed) < min(uninstalled)


def test_pc_has_no_emulator():
    assert best_for("pc") is None


def test_unknown_system_is_handled():
    assert options_for("not-a-system") == [] or best_for("not-a-system") is None


def test_summary_covers_every_emulated_system():
    rows = summary()
    assert len(rows) == len(SYSTEMS) - 1  # everything except 'pc'


# ── Install hints ─────────────────────────────────────────────────

def test_flatpak_hint_is_a_runnable_command():
    option = EmulatorOption(
        system_id="ps2", name="PCSX2", kind="native", flatpak_id="net.pcsx2.PCSX2"
    )
    assert option.install_hint == "flatpak install flathub net.pcsx2.PCSX2"


def test_package_hint_is_a_runnable_command():
    option = EmulatorOption(
        system_id="snes", name="Snes9x", kind="native", arch_package="snes9x-gtk"
    )
    assert option.install_hint == "sudo pacman -S snes9x-gtk"


def test_an_option_with_no_command_is_not_installed():
    assert not EmulatorOption(system_id="x", name="X", kind="native").installed


def test_an_option_with_a_command_is_installed():
    option = EmulatorOption(
        system_id="x", name="X", kind="flatpak", command=("flatpak", "run", "x")
    )
    assert option.installed


# ── Flatpak handling ──────────────────────────────────────────────

def test_flatpak_emulators_are_found(monkeypatch):
    """Regression: checking only PATH reported Flatpak emulators as missing,
    on machines where they were installed and working."""
    monkeypatch.setattr(emulator_detect, "installed_flatpaks",
                        lambda: frozenset({"net.pcsx2.PCSX2"}))
    monkeypatch.setattr(emulator_detect.shutil, "which", lambda _: None)

    option = best_for("ps2")

    assert option is not None
    assert option.kind == "flatpak"
    assert option.command == ("flatpak", "run", "net.pcsx2.PCSX2")


def test_native_binaries_win_over_flatpaks(monkeypatch):
    monkeypatch.setattr(emulator_detect, "installed_flatpaks",
                        lambda: frozenset({"net.pcsx2.PCSX2"}))
    monkeypatch.setattr(emulator_detect.shutil, "which",
                        lambda name: "/usr/bin/pcsx2-qt" if name == "pcsx2-qt" else None)

    option = best_for("ps2")
    assert option.kind == "native"


def test_absent_flatpak_is_not_an_error(monkeypatch):
    monkeypatch.setattr(emulator_detect.shutil, "which", lambda _: None)
    emulator_detect.refresh()
    assert isinstance(emulator_detect.installed_flatpaks(), frozenset)
    emulator_detect.refresh()


# ── AppImages ─────────────────────────────────────────────────────

def _appimage(directory, name, *, executable=True):
    path = directory / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755 if executable else 0o644)
    return path


@pytest.fixture
def appimage_dir(tmp_path, monkeypatch):
    """Point AppImage detection at a throwaway folder."""
    folder = tmp_path / "Applications"
    folder.mkdir()
    # Cleared before patching: refresh() clears the real cached functions, and
    # the patches below replace them with plain callables that have no cache.
    emulator_detect.refresh()
    monkeypatch.setattr(emulator_detect, "APPIMAGE_DIRS", (str(folder),))
    monkeypatch.setattr(emulator_detect, "_which", lambda _: None)
    monkeypatch.setattr(emulator_detect, "installed_flatpaks", frozenset)
    return folder


def test_appimage_emulators_are_found(appimage_dir):
    """Xenia Edge ships as an AppImage and nothing else, so PATH never has it."""
    image = _appimage(appimage_dir, "xenia_edge_linux.AppImage")

    option = best_for("xbox360")

    assert option is not None
    assert option.kind == "appimage"
    assert option.command == (str(image),)
    assert "Xenia Edge" in option.name


def test_non_executable_appimages_are_ignored(appimage_dir):
    """It cannot be launched, so reporting it installed only defers the error."""
    _appimage(appimage_dir, "xenia_edge_linux.AppImage", executable=False)

    assert best_for("xbox360") is None


def test_appimage_matching_is_case_insensitive(appimage_dir):
    _appimage(appimage_dir, "Xenia_Edge_Linux.AppImage")
    assert best_for("xbox360") is not None


def test_an_unrelated_appimage_is_not_mistaken_for_an_emulator(appimage_dir):
    _appimage(appimage_dir, "GIMP-2.10.AppImage")
    assert best_for("xbox360") is None


def test_the_newest_appimage_build_is_used(appimage_dir):
    """Xenia Edge builds are named after a commit, so several pile up."""
    old = _appimage(appimage_dir, "xenia_edge_18f514e.AppImage")
    new = _appimage(appimage_dir, "xenia_edge_c6998cc.AppImage")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    assert best_for("xbox360").command == (str(new),)


def test_missing_appimage_directories_are_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        emulator_detect, "APPIMAGE_DIRS", (str(tmp_path / "nope"), "/dev/null")
    )
    emulator_detect.refresh()
    assert emulator_detect.installed_appimages() == ()


def test_a_binary_on_path_wins_over_an_appimage(appimage_dir, monkeypatch):
    """The packaged install is the better one when both are present."""
    _appimage(appimage_dir, "xenia_edge_linux.AppImage")
    monkeypatch.setattr(
        emulator_detect, "_which",
        lambda name: "/usr/bin/xenia_edge" if name == "xenia_edge" else None,
    )

    option = best_for("xbox360")
    assert option.kind == "native"
    assert option.command == ("/usr/bin/xenia_edge",)


# ── Xenia Edge ────────────────────────────────────────────────────

def test_xenia_edge_is_the_preferred_xbox_360_emulator():
    """It is the fork that targets Vulkan and Linux; upstream Xenia is Windows."""
    assert CANDIDATES["xbox360"][0]["name"] == "Xenia Edge"


def test_xenia_edge_is_offered_by_both_of_its_linux_forms():
    candidate = CANDIDATES["xbox360"][0]
    # The AUR package symlinks /usr/bin/xenia_edge; upstream ships an AppImage.
    assert "xenia_edge" in candidate["binaries"]
    assert candidate["appimage"]
    assert candidate["aur"] == "xenia-edge-bin"


def test_aur_hint_uses_a_helper_not_pacman():
    """`pacman -S` cannot install an AUR package, so suggesting it fails."""
    option = EmulatorOption(
        system_id="xbox360", name="Xenia Edge", kind="native",
        aur_package="xenia-edge-bin",
    )
    assert option.install_hint.endswith("-S xenia-edge-bin")
    assert "pacman" not in option.install_hint


def test_download_hint_names_where_to_get_it():
    option = EmulatorOption(
        system_id="xbox360", name="Xenia Edge", kind="native",
        download_url="https://example.invalid/releases",
    )
    assert "https://example.invalid/releases" in option.install_hint


# ── RetroArch is not offered where it cannot help ─────────────────

def test_retroarch_is_not_offered_for_systems_with_no_core():
    """There is no xenia_libretro.so; RetroArch cannot run an Xbox 360 game.

    Offering it counted the system as playable and then failed at launch.
    """
    for system_id in NO_LIBRETRO_CORE:
        names = [o.name for o in options_for(system_id)]
        assert not any("RetroArch" in n for n in names), system_id


def test_retroarch_is_still_offered_for_systems_with_a_core():
    for system_id in ("snes", "megadrive", "psp", "arcade"):
        names = [o.name for o in options_for(system_id)]
        assert any("RetroArch" in n for n in names), system_id
