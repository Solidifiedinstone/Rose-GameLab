"""The unlock notification.

An achievement you are not told about is a number that changes in a menu you
are not looking at, so what matters here is that the right unlocks — and only
the right ones — reach the screen.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from rose_gamelab.ui.achievement_toast import (
    SOUND_FILE,
    AchievementNotifier,
    AchievementToast,
    Unlock,
    play_sound,
)
from rose_gamelab.ui.theme import THEMES


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def theme():
    return next(iter(THEMES.values()))


def test_the_unlock_is_named(qt_app, theme):
    toast = AchievementToast(
        Unlock(title="Kraid Down", description="Defeat Kraid", points=25), theme
    )
    toast.announce(silent=True)

    labels = [child.text() for child in toast.findChildren(type(toast.trophy))]

    assert "Kraid Down" in labels
    assert any("Achievement" in text for text in labels)
    toast.close()


def test_the_points_are_shown(qt_app, theme):
    toast = AchievementToast(Unlock(title="A", description="Do a thing", points=25), theme)
    toast.announce(silent=True)

    labels = " ".join(child.text() for child in toast.findChildren(type(toast.trophy)))

    assert "25 points" in labels
    toast.close()


def test_it_cannot_steal_a_click_from_the_game(qt_app, theme):
    """It sits over whatever is being played; a clickable notification would
    swallow a click meant for the game."""
    from PySide6.QtCore import Qt

    toast = AchievementToast(Unlock(title="A"), theme)

    assert toast.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert toast.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    toast.close()


def test_it_sits_above_the_bottom_edge_of_the_screen_in_use(qt_app, theme):
    """Bottom centre: the corners hold whatever the compositor puts there, and
    on a television they are the first thing overscan eats.

    Compared against the screen the pointer is on, not the primary one — this
    machine has several, and centring a notification on the wrong one is the
    bug this is here to catch.
    """
    from PySide6.QtGui import QCursor

    toast = AchievementToast(Unlock(title="A"), theme)
    screen = (
        QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    ).availableGeometry()

    toast.place()

    assert toast.y() < screen.bottom()
    assert abs(toast.x() + toast.width() // 2 - screen.center().x()) <= 2
    toast.close()


def test_several_unlocks_are_queued_not_stacked(qt_app, theme):
    """Three overlapping notifications is a mess; three in a row is a run of
    good news."""
    notifier = AchievementNotifier(theme, silent=True)

    notifier.announce_all([Unlock(title=f"A{index}") for index in range(4)])

    assert notifier.busy
    assert notifier.waiting == 3


def test_the_sound_ships_with_the_package():
    assert SOUND_FILE.is_file()
    assert SOUND_FILE.stat().st_size > 1000


def test_a_missing_sound_file_is_not_an_error(tmp_path):
    """No audio is a normal state for a machine; nobody should lose the
    notification because the chime could not play."""
    assert play_sound(tmp_path / "nope.wav") is False


def test_placement_reports_whether_it_could_actually_be_done(qt_app, theme):
    """A Wayland client is not allowed to position its own window, and saying
    so is what lets the interface point at the compositor rule instead of
    pretending the request worked."""
    from PySide6.QtGui import QGuiApplication

    toast = AchievementToast(Unlock(title="A"), theme)

    placed = toast.place()

    assert placed is (QGuiApplication.platformName() not in ("wayland", "wayland-egl"))
    toast.close()


def test_the_rule_for_wayland_ships_with_the_project():
    """Otherwise every Wayland user gets a notification in the middle of their
    screen and no way to know why."""
    from pathlib import Path

    rule = Path(__file__).resolve().parent.parent / "packaging" / "hyprland-achievement-rule.lua"

    assert rule.is_file()
    text = rule.read_text()
    assert "Rose GameLab Achievement" in text
    assert "window_rule" in text


def test_an_unlock_with_no_description_still_renders(qt_app, theme):
    toast = AchievementToast(Unlock(title="Bare", game="Super Metroid"), theme)
    toast.announce(silent=True)

    assert toast.isVisible()
    toast.close()
