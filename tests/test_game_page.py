"""The full-page view of one game.

The right-hand panel had a hard ceiling: no room for a hundred achievements, and
nowhere to write down which save slot is the good one. Clicking a game now gives
it the window.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from rose_gamelab.db.database import Database
from rose_gamelab.metadata.cache import ArtCache
from rose_gamelab.metadata.retroachievements import (
    Achievement,
    achievements_for,
    progress_for,
    save_achievements,
)
from rose_gamelab.ui.main_window import MainWindow
from rose_gamelab.ui.preferences import Preferences


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path):
    database = Database(tmp_path / "library.db")
    win = MainWindow(database, preferences=Preferences())
    win.scraper.cache = ArtCache(tmp_path / "art")
    yield win
    win.close()
    database.close()


@pytest.fixture
def game(window):
    game_id = window.library.add_game(title="Super Metroid", system="snes")
    window.library.update_game(
        game_id, summary="A 1994 action-adventure game.",
        developer="Nintendo R&D1", release_date="1994-03-19",
    )
    window.library.add_launch_option(
        game_id, kind="emulator", target="/roms/sm.sfc", label="snes9x"
    )
    window.refresh()
    return game_id


def stock_achievements():
    return [
        Achievement(ra_id=1, title="First Steps", description="Reach Zebes",
                    points=5, badge_url=None, earned_at="2026-01-02T10:00:00Z"),
        Achievement(ra_id=2, title="Power Bomb", description="Find it", points=10,
                    badge_url=None, earned_at="2026-01-03T10:00:00Z", hardcore=True),
        Achievement(ra_id=3, title="Speed Run", description="Under 3 hours",
                    points=50, badge_url=None),
    ]


# ── Opening the page ──────────────────────────────────────────────

def test_clicking_a_game_gives_it_the_window(window, game):
    window.open_game_page(game)

    assert window.pages.currentWidget() is window.game_page
    assert window.game_page.title.text() == "Super Metroid"


def test_the_page_shows_what_the_game_is(window, game):
    window.open_game_page(game)

    facts = window.game_page.subtitle.text()
    assert "Super Nintendo" in facts
    assert "1994" in facts
    assert "Nintendo R&D1" in facts


def test_going_back_returns_to_the_grid(window, game):
    window.open_game_page(game)
    window.close_game_page()

    assert window.pages.currentWidget() is window.grid


def test_a_game_that_vanished_does_not_open(window, game):
    window.library.remove_game(game)
    window.open_game_page(game)

    assert window.pages.currentWidget() is not window.game_page


def test_one_way_to_play_needs_no_picker(window, game):
    window.open_game_page(game)
    assert not window.game_page.option_picker.isVisible()


def test_several_ways_to_play_are_all_offered(window, game):
    window.library.add_launch_option(
        game, kind="emulator", target="/roms/sm.sfc", label="RetroArch"
    )
    window.open_game_page(game)

    picker = window.game_page.option_picker
    assert [picker.itemText(i) for i in range(picker.count())] == ["snes9x", "RetroArch"]


# ── Achievements ──────────────────────────────────────────────────

def test_achievements_are_listed(window, game):
    save_achievements(window.db, game, stock_achievements())
    window.open_game_page(game)

    assert window.game_page.achievement_rows.count() == 3


def test_progress_is_counted_in_both_achievements_and_points(window, game):
    save_achievements(window.db, game, stock_achievements())
    window.open_game_page(game)

    text = window.game_page.achievements_progress.text()
    assert "2 of 3" in text
    assert "15 of 65 points" in text


def test_stored_achievements_show_without_credentials(window, game):
    """Hiding earned achievements because a key is missing loses real progress."""
    save_achievements(window.db, game, stock_achievements())

    window.game_page.show_game(
        window.library.get(game), [], [],
        achievements_for(window.db, game),
        achievements_available=False,
    )

    assert window.game_page.achievement_rows.count() == 3
    assert not window.game_page.refresh_achievements.isEnabled()


def test_a_game_with_no_achievements_says_so(window, game):
    window.open_game_page(game)

    assert window.game_page.achievement_rows.count() == 0
    assert window.game_page.achievements_note.text()


def test_unearned_achievements_are_still_listed(window, game):
    """Seeing what is left is the point of the list."""
    save_achievements(window.db, game, stock_achievements())

    found = achievements_for(window.db, game)

    assert [a.earned for a in found] == [True, True, False]


def test_earned_achievements_come_first(window, game):
    save_achievements(window.db, game, stock_achievements())
    assert achievements_for(window.db, game)[0].earned


def test_progress_helper_agrees_with_the_page(window, game):
    save_achievements(window.db, game, stock_achievements())
    assert progress_for(window.db, game) == (2, 3, 15, 65)


def test_refreshing_needs_credentials(window, game, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    asked = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: asked.append(a))
    )

    window.refresh_achievements(game)

    assert asked, "should say what is missing rather than doing nothing"


# ── Notes ─────────────────────────────────────────────────────────

def test_a_note_is_saved(window, game):
    window.open_game_page(game)
    window.game_page.notes.setPlainText("Save slot 2 is the good one.")
    window.game_page._flush_notes()

    assert window.library.get(game).notes == "Save slot 2 is the good one."


def test_a_note_comes_back_when_the_page_reopens(window, game):
    window.open_game_page(game)
    window.game_page.notes.setPlainText("Needs the original pad.")
    window.game_page._flush_notes()

    window.close_game_page()
    window.open_game_page(game)

    assert window.game_page.notes.toPlainText() == "Needs the original pad."


def test_a_note_survives_a_scrape(window, game):
    """Notes are the user's own writing and are never scraped over."""
    window.open_game_page(game)
    window.game_page.notes.setPlainText("Mine.")
    window.game_page._flush_notes()

    window.library.update_game(game, summary="A scraped summary.")

    assert window.library.get(game).notes == "Mine."


def test_switching_game_does_not_carry_a_note_across(window, game):
    """A pending note belongs to the game being left, not the one arriving."""
    other = window.library.add_game(title="Another Game", system="snes")
    window.refresh()

    window.open_game_page(game)
    window.game_page.notes.setPlainText("Belongs to Super Metroid.")
    # Deliberately not flushed: the timer is still pending.
    window.open_game_page(other)

    assert window.library.get(game).notes == "Belongs to Super Metroid."
    assert window.library.get(other).notes is None


def test_clearing_a_note_removes_it(window, game):
    window.open_game_page(game)
    window.game_page.notes.setPlainText("temporary")
    window.game_page._flush_notes()

    window.game_page.notes.setPlainText("")
    window.game_page._flush_notes()

    assert window.library.get(game).notes is None


# ── Playtime ──────────────────────────────────────────────────────

def played(window, game_id, *sessions):
    """Record real play sessions: (days ago, seconds)."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    total = 0
    for days_ago, seconds in sessions:
        start = now - timedelta(days=days_ago)
        window.db.execute(
            "INSERT INTO play_sessions (game_id, started_at, ended_at, seconds)"
            " VALUES (?, ?, ?, ?)",
            (game_id, start.isoformat(timespec="seconds"),
             (start + timedelta(seconds=seconds)).isoformat(timespec="seconds"), seconds),
        )
        total += seconds

    window.db.execute(
        "UPDATE games SET play_seconds = ?, play_count = ?, last_played = ?"
        " WHERE id = ?",
        (total, len(sessions), now.isoformat(timespec="seconds"), game_id),
    )
    return total


def test_playtime_is_grouped_by_day(window, game):
    played(window, game, (3, 1800), (3, 900), (1, 7200))

    history = window.library.play_history(game)

    assert len(history) == 2, "two sessions on one day are one bar"
    assert history[-1][1] == 7200


def test_the_chart_shows_when_not_just_how_long(window, game):
    played(window, game, (5, 3600), (1, 7200))
    window.open_game_page(game)

    assert window.game_page.playtime_chart.history
    assert "3h" in window.game_page.playtime_summary.text()
    assert "2 sessions" in window.game_page.playtime_summary.text()


def test_a_never_played_game_shows_no_chart(window, game):
    """An empty chart is worse than no chart."""
    window.open_game_page(game)
    assert not window.game_page.playtime_frame.isVisibleTo(window)


def test_only_finished_sessions_count(window, game):
    """A game running right now has no duration yet."""
    window.db.execute(
        "INSERT INTO play_sessions (game_id, started_at) VALUES (?, datetime('now'))",
        (game,),
    )
    assert window.library.play_history(game) == []


# ── Recently played ───────────────────────────────────────────────

def test_recently_played_is_most_recent_first(window, game):
    other = window.library.add_game(title="Older Game", system="snes")
    played(window, other, (9, 600))
    played(window, game, (1, 600))
    # `played` stamps last_played with *now* for both, so on its own this asks
    # for an order the data does not express. Given a real difference, the
    # question has an answer.
    window.db.execute(
        "UPDATE games SET last_played = '2026-01-01T00:00:00+00:00' WHERE id = ?",
        (other,),
    )

    assert [g.id for g in window.library.recently_played()] == [game, other]


def test_games_played_at_the_same_moment_keep_a_stable_order(window, game):
    """Ties were ordered by whatever the query plan happened to do — adding an
    index silently changed it. A shelf that reshuffles equal rows between
    openings looks broken even though nothing changed."""
    other = window.library.add_game(title="Same Second", system="snes")
    played(window, other, (1, 600))
    played(window, game, (1, 600))

    first = [g.id for g in window.library.recently_played()]
    second = [g.id for g in window.library.recently_played()]

    assert first == second


def test_never_played_games_are_not_in_the_shelf(window, game):
    """Its whole purpose is picking up where you left off."""
    window.library.add_game(title="Never Touched", system="snes")
    played(window, game, (1, 600))

    assert [g.id for g in window.library.recently_played()] == [game]


def test_hidden_games_stay_out_of_recently_played(window, game):
    played(window, game, (1, 600))
    window.library.set_hidden(game, True)

    assert window.library.recently_played() == []


# ── Collections ───────────────────────────────────────────────────

def test_a_collection_filters_the_grid(window, game):
    shelf = window.library.create_collection("Backlog")
    window.library.add_to_collection(shelf, game)

    window._on_filter(f"collection:{shelf}")

    assert [g.id for g in window._current_games()] == [game]


def test_an_empty_collection_shows_nothing(window, game):
    shelf = window.library.create_collection("Finished")
    window._on_filter(f"collection:{shelf}")

    assert window._current_games() == []


def test_a_game_can_be_in_several_collections(window, game):
    first = window.library.create_collection("Backlog")
    second = window.library.create_collection("Favourites of 2026")
    window.library.add_to_collection(first, game)
    window.library.add_to_collection(second, game)

    assert set(window.library.collections_for(game)) == {first, second}


def test_removing_from_a_collection_keeps_the_game(window, game):
    shelf = window.library.create_collection("Backlog")
    window.library.add_to_collection(shelf, game)
    window.library.remove_from_collection(shelf, game)

    assert window.library.collections_for(game) == []
    assert window.library.get(game) is not None


def test_deleting_a_collection_keeps_its_games(window, game):
    shelf = window.library.create_collection("Backlog")
    window.library.add_to_collection(shelf, game)

    window.library.delete_collection(shelf)

    assert window.library.get(game) is not None
    assert window.library.list_collections() == []


def test_collections_appear_in_the_sidebar(window, game):
    window.library.create_collection("Backlog", icon="📚")
    window._refresh_sidebar()

    keys = [item.key for item in window.sidebar._items]
    assert any(key.startswith("collection:") for key in keys)


# ── Refreshing achievements on launch ─────────────────────────────

def test_the_launch_refresh_only_touches_linked_games(window, game, monkeypatch):
    """Matching a game to RetroAchievements is the expensive half — a hash or a
    search per game. Doing that for a whole library on every launch would be a
    long rate-limited crawl for games that mostly are not on RA at all."""
    asked = []

    class FakeProvider:
        def available(self):
            return True

        def achievements(self, ra_game_id, user=None):
            asked.append(ra_game_id)
            return []

    unlinked = window.library.add_game(title="Never Matched", system="snes")
    window.db.execute("UPDATE games SET ra_game_id = 4242 WHERE id = ?", (game,))
    monkeypatch.setattr(window, "_achievements_provider", FakeProvider)
    # The refresh also looks up never-checked games. That is a separate pass
    # with its own test; pinning it empty keeps this one about progress alone,
    # and keeps it quick enough not to outlive its own deadline.
    monkeypatch.setattr(
        "rose_gamelab.ui.main_window.games_needing_a_match", lambda *a, **k: []
    )

    window.refresh_all_achievements(quietly=True)

    # Pumped rather than waited on: the worker reports back through queued
    # connections, so blocking this thread on the worker deadlocks both until
    # the timeout expires. Waited to completion rather than to the first
    # result, so the window is not torn down with work still in flight.
    import time
    deadline = time.monotonic() + 5
    while window._thread is not None and time.monotonic() < deadline:
        QApplication.processEvents()

    assert asked == [4242]
    assert unlinked not in asked


def test_the_launch_refresh_says_nothing_without_credentials(window, monkeypatch):
    """It runs unprompted on every launch; a dialog would be an ambush."""
    class Unavailable:
        def available(self):
            return False

    monkeypatch.setattr(window, "_achievements_provider", Unavailable)
    shown = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.information",
        lambda *a, **k: shown.append(a),
    )

    window.refresh_all_achievements(quietly=True)

    assert shown == []


def test_asking_for_it_by_hand_does_explain_the_missing_key(window, monkeypatch):
    class Unavailable:
        def available(self):
            return False

    monkeypatch.setattr(window, "_achievements_provider", Unavailable)
    shown = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.information",
        lambda *a, **k: shown.append(a),
    )

    window.refresh_all_achievements(quietly=False)

    assert shown


def test_never_checked_games_are_looked_up_on_launch(window, monkeypatch):
    """New games get matched without anybody opening them, and a game with no
    achievement set is recorded so it is never looked up again."""
    from rose_gamelab.metadata.retroachievements import games_needing_a_match

    class FakeProvider:
        def available(self):
            return True

        def achievements(self, ra_game_id, user=None):
            return []

    has_a_set = window.library.add_game(title="Known Game", system="snes")
    nothing_there = window.library.add_game(title="Homebrew", system="snes")

    monkeypatch.setattr(window, "_achievements_provider", FakeProvider)
    monkeypatch.setattr(
        window, "_match_retroachievements",
        lambda game: 4242 if game.id == has_a_set else None,
    )

    window.refresh_all_achievements(quietly=True)

    import time
    deadline = time.monotonic() + 10
    while window._thread is not None and time.monotonic() < deadline:
        QApplication.processEvents()

    # Both were checked, so neither is asked about again.
    assert games_needing_a_match(window.db) == []
    row = window.db.query_one(
        "SELECT ra_game_id FROM games WHERE id = ?", (nothing_there,)
    )
    assert row["ra_game_id"] is None
