"""The right-click menu on a game.

Everything you can do to ONE game lives here. Before this existed the signal was
emitted, travelled all the way up from the card to the window, and nothing
listened — right-clicking a game did nothing at all. Removing a single game was
only possible from Settings, which offered removing a whole system or source at
once and nothing finer.

These run against a real window on Qt's offscreen platform, so they exercise the
actual menu rather than a description of one.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from rose_gamelab.db.database import Database  # noqa: E402
from rose_gamelab.metadata.cache import ArtCache  # noqa: E402
from rose_gamelab.ui.main_window import MainWindow  # noqa: E402
from rose_gamelab.ui.preferences import Preferences  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path):
    database = Database(tmp_path / "library.db")
    win = MainWindow(database, preferences=Preferences())
    # Never touch the user's real artwork cache.
    win.scraper.cache = ArtCache(tmp_path / "art")
    yield win
    win.close()
    database.close()


@pytest.fixture
def game(window):
    game_id = window.library.add_game(title="Demon's Souls", system="ps3")
    window.library.add_launch_option(
        game_id, kind="emulator", target="/games/EBOOT.BIN", label="RPCS3"
    )
    window.refresh()
    return game_id


def entries(window, game_id) -> list[str]:
    menu = window.build_game_menu(game_id)
    found = [action.text() for action in menu.actions() if action.text()]
    menu.deleteLater()
    return found


def submenus(window, game_id) -> dict[str, list[str]]:
    menu = window.build_game_menu(game_id)
    found = {
        action.menu().title(): [i.text() for i in action.menu().actions()]
        for action in menu.actions() if action.menu()
    }
    menu.deleteLater()
    return found


# ── What the menu offers ──────────────────────────────────────────

def test_the_menu_covers_a_whole_game(window, game):
    found = entries(window, game)

    assert "Play" in found
    assert "Add art…" in found
    assert "Remove from library…" in found


def test_a_game_that_vanished_has_no_menu(window, game):
    """A stale card must not raise when it is right-clicked."""
    window.library.remove_game(game)
    assert window.build_game_menu(game) is None


def test_every_way_to_play_is_offered_by_name(window, game):
    """A RetroArch core and a standalone emulator are not interchangeable."""
    window.library.add_launch_option(
        game, kind="emulator", target="/games/EBOOT.BIN", label="RetroArch"
    )

    assert submenus(window, game)["Play with"] == ["RPCS3", "RetroArch"]


def test_a_single_option_needs_no_submenu(window, game):
    """Collections are always offered; a 'Play with' submenu is not."""
    assert "Play with" not in submenus(window, game)


def test_collections_are_offered_and_reflect_membership(window, game):
    shelf = window.library.create_collection("Backlog")

    entries = submenus(window, game)["Collections"]
    assert any("Backlog" in text for text in entries)
    assert "New collection…" in entries

    window.library.add_to_collection(shelf, game)

    menu = window.build_game_menu(game)
    ticked = [
        item.text()
        for action in menu.actions() if action.menu()
        and action.menu().title() == "Collections"
        for item in action.menu().actions() if item.isChecked()
    ]
    menu.deleteLater()

    assert any("Backlog" in text for text in ticked)


def test_play_is_offered_but_disabled_with_nothing_to_run(window):
    """Honest: the entry stays visible so its absence is not a mystery."""
    game_id = window.library.add_game(title="No Launcher", system="ps3")
    window.refresh()

    menu = window.build_game_menu(game_id)
    play = next(a for a in menu.actions() if a.text() == "Play")

    assert not play.isEnabled()
    menu.deleteLater()


def test_right_clicking_selects_what_was_clicked(window, game):
    """The menu and the detail panel must agree about which game this is."""
    other = window.library.add_game(title="Something Else", system="snes")
    window.refresh()
    window.grid.select(other)

    window.build_game_menu(game)

    assert window.grid.selected_id == game


# ── Entries that reflect the game's state ─────────────────────────

def test_favourites_toggle_both_ways(window, game):
    assert "Add to favourites" in entries(window, game)

    window.library.set_favorite(game, True)
    assert "Remove from favourites" in entries(window, game)


def test_hiding_toggles_both_ways(window, game):
    assert "Hide" in entries(window, game)

    window.set_hidden(game, True)
    assert "Unhide" in entries(window, game)


def test_removing_art_is_only_offered_when_there_is_art(window, game, tmp_path):
    assert "Remove art" not in entries(window, game)

    cover = tmp_path / "cover.png"
    cover.write_bytes(PNG)
    window.library.update_game(game, cover_path=str(cover))

    assert "Remove art" in entries(window, game)


# ── Add art ───────────────────────────────────────────────────────

def test_chosen_art_becomes_the_cover(window, game, tmp_path, monkeypatch):
    source = tmp_path / "my-cover.png"
    source.write_bytes(PNG)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(source), ""))
    )

    window.choose_art(game)

    assert window.library.get(game).cover_path


def test_chosen_art_is_copied_into_the_cache(window, game, tmp_path, monkeypatch):
    """So the cover survives the original being moved, renamed or deleted."""
    source = tmp_path / "elsewhere" / "cover.png"
    source.parent.mkdir()
    source.write_bytes(PNG)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(source), ""))
    )

    window.choose_art(game)
    source.unlink()

    from pathlib import Path
    assert Path(window.library.get(game).cover_path).is_file()


def test_chosen_art_is_not_overwritten_by_a_later_scrape(window, game, tmp_path, monkeypatch):
    source = tmp_path / "cover.png"
    source.write_bytes(PNG)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(source), ""))
    )

    window.choose_art(game)

    assert window.library.get(game).cover_locked, (
        "art the user chose by hand must win over a scraper's guess"
    )


def test_chosen_art_does_not_freeze_the_rest_of_the_entry(
    window, game, tmp_path, monkeypatch
):
    """Picking a cover must not stop the game ever getting a description.

    `metadata_locked` gates the WHOLE scrape, so using it to protect artwork
    quietly cost the game its summary and release date forever.
    """
    source = tmp_path / "cover.png"
    source.write_bytes(PNG)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(source), ""))
    )

    window.choose_art(game)

    locked = window.library.db.query_one(
        "SELECT metadata_locked FROM games WHERE id = ?", (game,)
    )["metadata_locked"]
    assert not locked, "the whole entry was locked, not just the artwork"


def test_a_file_that_is_not_an_image_is_refused(window, game, tmp_path, monkeypatch):
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not a picture")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(junk), ""))
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    window.choose_art(game)

    assert not window.library.get(game).cover_path


def test_cancelling_the_file_dialog_changes_nothing(window, game, monkeypatch):
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", ""))
    )

    window.choose_art(game)

    assert not window.library.get(game).cover_path


def test_removing_art_unlocks_it_for_scraping_again(window, game, tmp_path, monkeypatch):
    source = tmp_path / "cover.png"
    source.write_bytes(PNG)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(source), ""))
    )
    window.choose_art(game)

    window.clear_art(game)

    after = window.library.get(game)
    assert not after.cover_path
    assert not after.cover_locked


# ── Remove one game ───────────────────────────────────────────────

def test_removing_one_game_leaves_the_rest(window, game, monkeypatch):
    """The point of the whole thing: Settings could only remove them wholesale."""
    keep = window.library.add_game(title="Keep Me", system="snes")
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )

    window.remove_game(game)

    assert [g.id for g in window.library.list_games()] == [keep]


def test_removing_is_confirmed_first(window, game, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
    )

    window.remove_game(game)

    assert window.library.get(game) is not None
