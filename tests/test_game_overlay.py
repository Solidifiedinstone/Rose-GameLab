"""The panel shown over a running game.

It is a window above the game rather than a layer drawn inside it — GameLab
hooks no renderers — so what is tested here is the composition and the actions,
not anything about injection.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database
from rose_gamelab.ui import game_overlay
from rose_gamelab.ui.game_overlay import GameOverlay, take_screenshot
from rose_gamelab.ui.theme import THEMES


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def library(tmp_path):
    database = Database(tmp_path / "library.db")
    yield Library(database)
    database.close()


@pytest.fixture
def game(library):
    game_id = library.add_game(title="Chrono Trigger", system="snes", path="/roms/ct.sfc")
    return library.get(game_id)


@pytest.fixture
def overlay(qt_app, library):
    made = GameOverlay(library, next(iter(THEMES.values())))
    yield made
    made.close()


# ── Contents ──────────────────────────────────────────────────────

def test_the_panel_names_the_running_game(overlay, game):
    overlay.show_for(game)
    assert "Chrono Trigger" in overlay.title.text()


def test_playtime_is_shown(overlay, game):
    overlay.show_for(game, elapsed_seconds=3 * 3600 + 25 * 60)

    assert "3 h" in overlay.elapsed.text()
    assert "25" in overlay.elapsed.text()


def test_short_sessions_read_in_minutes(overlay, game):
    overlay.show_for(game, elapsed_seconds=90)
    assert "1 m" in overlay.elapsed.text()


def test_a_game_with_no_achievements_says_so_plainly(overlay, game):
    overlay.show_for(game)
    assert "No achievement set" in overlay.achievements.text()


def test_a_game_with_no_saves_says_so_plainly(overlay, game):
    overlay.show_for(game)
    assert overlay.save_list.count() == 1
    assert "No saves found" in overlay.save_list.item(0).text()


def test_connected_controllers_are_listed(overlay, game):
    class FakeStatus:
        label = "DualSense  ·  72%"

    overlay.show_for(game, controllers=[FakeStatus()])

    assert "Player 1" in overlay.controllers.text()
    assert "72%" in overlay.controllers.text()


def test_no_controller_is_stated_rather_than_left_blank(overlay, game):
    overlay.show_for(game, controllers=[])
    assert "No controller" in overlay.controllers.text()


def test_broken_achievements_do_not_take_the_panel_down(overlay, game, monkeypatch):
    monkeypatch.setattr(
        "rose_gamelab.metadata.retroachievements.progress_for",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")),
    )
    overlay.show_for(game)

    assert "unavailable" in overlay.achievements.text()


def test_broken_saves_do_not_take_the_panel_down(overlay, game, monkeypatch):
    monkeypatch.setattr(
        overlay.saves, "saves_for",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")),
    )
    overlay.show_for(game)

    assert "unavailable" in overlay.save_list.item(0).text()


# ── Screenshots ───────────────────────────────────────────────────

def test_no_screenshot_tool_is_reported_before_the_button_is_pressed(
    qt_app, library, monkeypatch
):
    """Better than letting someone press it and watch nothing happen."""
    monkeypatch.setattr(game_overlay.shutil, "which", lambda _name: None)

    panel = GameOverlay(library, next(iter(THEMES.values())))

    assert not panel.screenshot_button.isEnabled()
    assert "No screenshot tool" in panel.capture_status.text()
    panel.close()


def test_a_screenshot_is_taken_with_the_first_available_tool(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(
        game_overlay.shutil, "which",
        lambda name: "/usr/bin/grim" if name == "grim" else None,
    )

    def fake_run(command, **kwargs):
        calls.append(command)
        # Stand in for the tool writing the file.
        Path = type(tmp_path)
        Path(command[-1]).write_bytes(b"png")
        return None

    monkeypatch.setattr(game_overlay.subprocess, "run", fake_run)

    result = take_screenshot("Chrono Trigger", directory=tmp_path)

    assert result is not None
    assert result.exists()
    assert calls[0][0] == "/usr/bin/grim"


def test_the_screenshot_name_carries_the_game_title(tmp_path, monkeypatch):
    monkeypatch.setattr(
        game_overlay.shutil, "which",
        lambda name: "/usr/bin/grim" if name == "grim" else None,
    )
    monkeypatch.setattr(
        game_overlay.subprocess, "run",
        lambda command, **kwargs: type(tmp_path)(command[-1]).write_bytes(b"png"),
    )

    result = take_screenshot("Chrono Trigger", directory=tmp_path)

    assert "Chrono Trigger" in result.name


def test_a_title_that_is_not_a_filename_is_made_into_one(tmp_path, monkeypatch):
    monkeypatch.setattr(
        game_overlay.shutil, "which",
        lambda name: "/usr/bin/grim" if name == "grim" else None,
    )
    monkeypatch.setattr(
        game_overlay.subprocess, "run",
        lambda command, **kwargs: type(tmp_path)(command[-1]).write_bytes(b"png"),
    )

    result = take_screenshot("Sonic 3 & Knuckles / Mania?", directory=tmp_path)

    assert "/" not in result.stem
    assert result.exists()


def test_a_tool_that_writes_nothing_is_reported_as_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        game_overlay.shutil, "which",
        lambda name: "/usr/bin/grim" if name == "grim" else None,
    )
    monkeypatch.setattr(game_overlay.subprocess, "run", lambda *a, **k: None)

    assert take_screenshot("Anything", directory=tmp_path) is None


def test_a_crashing_tool_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(
        game_overlay.shutil, "which",
        lambda name: "/usr/bin/grim" if name == "grim" else None,
    )

    def explode(*args, **kwargs):
        raise OSError("no display")

    monkeypatch.setattr(game_overlay.subprocess, "run", explode)

    assert take_screenshot("Anything", directory=tmp_path) is None


def test_the_panel_hides_itself_before_capturing(overlay, game, monkeypatch):
    """It is a window above the game, so it would be in its own screenshot."""
    overlay.show_for(game)
    assert overlay.isVisible()

    overlay.take_screenshot()

    assert not overlay.isVisible()


# ── Behaviour ─────────────────────────────────────────────────────

def test_escape_closes_the_panel(overlay, game):
    overlay.show_for(game)

    QTest.keyClick(overlay, Qt.Key.Key_Escape)

    assert not overlay.isVisible()


def test_closing_stops_the_clock(overlay, game):
    overlay.show_for(game)
    assert overlay._tick.isActive()

    overlay.close()

    assert not overlay._tick.isActive()


def test_closing_is_announced(overlay, game):
    seen = []
    overlay.closed.connect(lambda: seen.append(True))

    overlay.show_for(game)
    overlay.close()

    assert seen == [True]
