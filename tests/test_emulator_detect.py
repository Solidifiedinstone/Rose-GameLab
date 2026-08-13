"""Tests for finding installed emulators.

Detection reaches out to the machine it runs on, so these tests fix the parts
that must hold regardless of what happens to be installed.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core import emulator_detect
from rose_gamelab.core.emulator import SYSTEMS
from rose_gamelab.core.emulator_detect import (
    CANDIDATES,
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
