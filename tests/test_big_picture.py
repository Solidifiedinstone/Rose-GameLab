"""Big Picture mode: navigating and launching from the couch.

Big Picture shipped unusable — the selection could not be moved at all — and
nothing here caught it, because there were no tests for this window. The reason
it went unnoticed is worth stating: the navigation logic was correct, and a test
that sends keys straight at the window passes happily. What was broken is where
the keys *go*. Every scroll area in the window took keyboard focus by default and
answered the arrow keys itself, so the presses never reached `keyPressEvent`.

So these tests assert on focus policy and route keys through the focused widget,
rather than assuming the window receives them.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

from rose_gamelab.core.launcher import LaunchError
from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database
from rose_gamelab.ui.big_picture import BigPictureWindow
from rose_gamelab.ui.theme import THEMES


class FakeLauncher:
    """Records launches instead of starting anything."""

    def __init__(self, error: str | None = None) -> None:
        self.launched: list[int] = []
        self.error = error

    def launch(self, game_id: int, **kwargs):
        if self.error:
            raise LaunchError(self.error)
        self.launched.append(game_id)
        return None


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def library(tmp_path):
    database = Database(tmp_path / "library.db")
    yield Library(database)
    database.close()


@pytest.fixture
def window(qt_app, library):
    for index in range(5):
        library.add_game(title=f"Game {index}", system="snes", path=f"/roms/{index}.sfc")
    library.add_game(title="Sonic", system="megadrive", path="/roms/sonic.md")

    launcher = FakeLauncher()
    win = BigPictureWindow(library, launcher, next(iter(THEMES.values())))
    win.launcher = launcher
    win.show()
    qt_app.processEvents()
    yield win
    win.close()


def press(window, key):
    """Send a key the way the compositor would — to whatever holds focus."""
    target = QApplication.focusWidget() or window
    QTest.keyClick(target, key)
    QApplication.processEvents()


# ── Focus routing ─────────────────────────────────────────────────

def test_no_scroll_area_can_take_keyboard_focus(window):
    """Regression: a focusable scroll area answers the arrow keys by scrolling,
    so every d-pad press was consumed before the window could read it."""
    for area in window.findChildren(QScrollArea):
        assert area.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_the_window_holds_focus_once_shown(window):
    """The window itself, not a child, is what the keyboard talks to.

    Asserted through `focusWidget()` rather than `hasFocus()`: the latter is
    also false whenever the window is not the *active* one, which it never is
    on the offscreen platform these tests run on. `focusWidget()` answers the
    question that actually matters — which widget inside would receive a key.
    """
    assert window.focusWidget() is window


def test_the_window_is_the_only_focusable_thing_in_it(window):
    """Nothing inside may sit between the keyboard and the navigation."""
    focusable = [
        child for child in window.findChildren(object)
        if hasattr(child, "focusPolicy")
        and callable(getattr(child, "focusPolicy", None))
        and child.focusPolicy() != Qt.FocusPolicy.NoFocus
    ]
    assert focusable == []


# ── Keyboard navigation ───────────────────────────────────────────

def test_right_moves_the_selection(window):
    shelf = window.current_shelf
    assert shelf.index == 0

    press(window, Qt.Key.Key_Right)

    assert window.current_shelf.index == 1


def test_left_moves_back(window):
    press(window, Qt.Key.Key_Right)
    press(window, Qt.Key.Key_Left)
    assert window.current_shelf.index == 0


def test_left_at_the_start_stays_put(window):
    press(window, Qt.Key.Key_Left)
    assert window.current_shelf.index == 0


def test_down_changes_row(window):
    assert len(window.shelves) > 1
    press(window, Qt.Key.Key_Down)
    assert window.shelf_index == 1


def test_up_at_the_top_stays_put(window):
    press(window, Qt.Key.Key_Up)
    assert window.shelf_index == 0


def test_exactly_one_tile_is_focused(window):
    press(window, Qt.Key.Key_Right)

    focused = [
        (row, index)
        for row, shelf in enumerate(window.shelves)
        for index, tile in enumerate(shelf.tiles)
        if tile._focused
    ]
    assert len(focused) == 1
    assert focused[0] == (window.shelf_index, window.current_shelf.index)


def test_the_header_follows_the_selection(window):
    first = window.current_shelf.current_game.title
    press(window, Qt.Key.Key_Right)
    second = window.current_shelf.current_game.title

    assert first != second
    assert second in window.now_showing.text()


# ── Launching ─────────────────────────────────────────────────────

def test_enter_launches_the_selected_game(window):
    press(window, Qt.Key.Key_Right)
    expected = window.current_shelf.current_game.id

    press(window, Qt.Key.Key_Return)

    assert window.launcher.launched == [expected]


def test_launching_after_changing_row_uses_the_right_game(window):
    """Regression risk: the row and the tile index are tracked separately."""
    press(window, Qt.Key.Key_Down)
    press(window, Qt.Key.Key_Right)
    expected = window.current_shelf.current_game.id

    press(window, Qt.Key.Key_Return)

    assert window.launcher.launched == [expected]


def test_a_launch_failure_does_not_close_big_picture(window, monkeypatch):
    """The message box is shown; the couch interface stays up behind it."""
    window.launcher.error = "No emulator installed for Super Nintendo."
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.exec", lambda self: None
    )

    press(window, Qt.Key.Key_Return)

    assert window.isVisible()


# ── Mouse ─────────────────────────────────────────────────────────

def test_clicking_a_tile_selects_it(window):
    tile = window.shelves[0].tiles[2]

    QTest.mouseClick(tile, Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert window.current_shelf.index == 2
    assert window.launcher.launched == []


def test_clicking_a_tile_in_another_row_moves_the_row_too(window):
    tile = window.shelves[1].tiles[0]

    QTest.mouseClick(tile, Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert window.shelf_index == 1
    assert window.shelves[0].tiles[0]._focused is False


def test_double_clicking_a_tile_launches_it(window):
    tile = window.shelves[0].tiles[1]
    expected = window.shelves[0].games[1].id

    QTest.mouseDClick(tile, Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert window.launcher.launched == [expected]


def test_the_keyboard_still_works_after_clicking(window):
    """Clicking must not park focus somewhere the arrow keys are not read."""
    QTest.mouseClick(window.shelves[0].tiles[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    press(window, Qt.Key.Key_Right)

    assert window.current_shelf.index == 2


# ── Empty library ─────────────────────────────────────────────────

def test_an_empty_library_does_not_crash(qt_app, tmp_path):
    database = Database(tmp_path / "empty.db")
    window = BigPictureWindow(Library(database), FakeLauncher(), next(iter(THEMES.values())))
    window.show()
    qt_app.processEvents()

    press(window, Qt.Key.Key_Right)
    press(window, Qt.Key.Key_Return)

    assert window.current_shelf is None
    window.close()
    database.close()


# ── Lazy building and cover loading ───────────────────────────────
#
# Opening this window used to decode every cover and build every tile first:
# 2.1 seconds before anything appeared, for a library of 1500 games, almost
# all of it spent on shelves several screens down. Both are deferred now, and
# laziness is exactly the kind of optimisation that breaks quietly — a shelf
# that never populates looks empty rather than broken.

@pytest.fixture
def big_library(qt_app, library):
    # Enough systems to produce more shelves than are built up front, which is
    # the whole point of the fixture — three systems would all be eager.
    systems = ["snes", "megadrive", "ps1", "n64", "gba", "nes", "psp", "nds"]
    for index in range(120):
        library.add_game(
            title=f"Game {index}",
            system=systems[index % len(systems)],
            path=f"/roms/{index}.rom",
        )
    win = BigPictureWindow(library, FakeLauncher(), next(iter(THEMES.values())))
    win.show()
    qt_app.processEvents()
    yield win
    win.close()


def test_shelves_further_down_are_not_built_up_front(big_library):
    """The expensive half of opening this window is making widgets."""
    assert big_library.shelves[0].populated
    assert not big_library.shelves[-1].populated


def test_a_shelf_builds_itself_when_navigated_to(big_library):
    last = len(big_library.shelves) - 1
    for _ in range(last):
        press(big_library, Qt.Key.Key_Down)

    assert big_library.shelves[last].populated
    assert big_library.shelves[last].tiles


def test_navigation_into_an_unbuilt_shelf_still_selects_a_game(big_library):
    press(big_library, Qt.Key.Key_Down)
    press(big_library, Qt.Key.Key_Right)

    assert big_library.current_shelf.current_game is not None
    assert big_library.now_showing.text()


def test_the_focused_tile_always_has_its_cover(big_library):
    """Whatever else is deferred, the thing being looked at is not."""
    press(big_library, Qt.Key.Key_Right)
    tile = big_library.current_shelf.tiles[big_library.current_shelf.index]

    assert tile.cover_loaded


def test_covers_are_loaded_around_the_selection_not_all_at_once(big_library):
    shelf = big_library.shelves[0]

    assert any(tile.cover_loaded for tile in shelf.tiles)
    assert not all(tile.cover_loaded for tile in shelf.tiles)


def test_moving_along_a_shelf_loads_covers_ahead(big_library):
    shelf = big_library.shelves[0]
    before = sum(1 for tile in shelf.tiles if tile.cover_loaded)

    for _ in range(6):
        press(big_library, Qt.Key.Key_Right)

    assert sum(1 for tile in shelf.tiles if tile.cover_loaded) > before


def test_the_background_pass_eventually_finishes_everything(big_library):
    for _ in range(500):
        if all(shelf.fully_loaded() for shelf in big_library.shelves):
            break
        big_library._load_a_few_covers()

    assert all(shelf.populated for shelf in big_library.shelves)
    assert all(shelf.fully_loaded() for shelf in big_library.shelves)


def test_the_background_timer_stops_when_there_is_nothing_left(big_library):
    """Otherwise it wakes up thirty times a second forever."""
    for _ in range(500):
        big_library._load_a_few_covers()
        if not big_library._background.isActive():
            break

    assert not big_library._background.isActive()


def test_a_cover_is_decoded_only_once(qt_app, library, monkeypatch):
    """It is decoded at the focused size and scaled down in memory; asking the
    loader for a second width would re-read the file on every focus move —
    which is what happened before, on every single press of an arrow key."""
    from rose_gamelab.ui.big_picture import BigPictureTile

    calls = []
    monkeypatch.setattr(
        "rose_gamelab.ui.big_picture.load_cover",
        lambda path, width: calls.append((path, width)) or None,
    )
    game_id = library.add_game(title="G", system="snes", path="/roms/g.sfc")
    tile = BigPictureTile(library.get(game_id), next(iter(THEMES.values())))

    tile.load_cover()
    tile.set_focused(True)
    tile.set_focused(False)
    tile.load_cover()

    assert len(calls) == 1


def test_populating_twice_does_not_duplicate_tiles(big_library):
    shelf = big_library.shelves[-1]
    shelf.populate()
    count = len(shelf.tiles)
    shelf.populate()

    assert len(shelf.tiles) == count


def test_every_game_in_a_shelf_gets_a_tile(big_library):
    for shelf in big_library.shelves:
        shelf.populate()
        assert len(shelf.tiles) == len(shelf.games)


def test_shelf_queries_are_limited_rather_than_sliced(library, qt_app):
    """Fetching a whole system to keep forty built thousands of objects."""
    calls = []
    original = library.list_games

    def record(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    library.list_games = record
    win = BigPictureWindow(library, FakeLauncher(), next(iter(THEMES.values())))

    assert calls, "the window should query the library"
    assert all(call.get("limit") for call in calls)
    win.close()


def test_the_background_pass_stops_when_the_window_closes(big_library):
    """The window object outlives being closed — the main window keeps a
    reference — so an unstopped timer would go on decoding covers for a window
    nobody can see, right when a game has just started."""
    assert big_library._background.isActive()

    big_library.close()

    assert not big_library._background.isActive()


def test_reopening_picks_the_remaining_work_back_up(big_library):
    big_library.close()
    big_library.show()

    assert big_library._background.isActive()


def test_an_empty_library_has_no_background_work_and_does_not_crash(qt_app, library):
    """Regression: the timer was only created when shelves existed."""
    window = BigPictureWindow(library, FakeLauncher(), next(iter(THEMES.values())))
    window.show()

    window._load_a_few_covers()      # must not raise

    assert not window._background.isActive()
    window.close()
