"""The grid stays fast as a library grows.

Two quadratic behaviours made the interface feel broken, and both are the kind
that pass every functional test while making the app unusable:

  - clearing the grid reparented widgets one at a time, and each reparent made
    Qt search the layout for the matching item. Clearing 3000 cards cost four
    and a half million lookups. Every search, filter and sort paid it.
  - changing a theme rebuilt every card from scratch, so dragging an appearance
    slider destroyed and recreated the whole library per pixel.

These assert the shape of the cost, not wall-clock times, so they do not go
flaky on a loaded machine.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database
from rose_gamelab.ui.theme import STYLES, THEMES
from rose_gamelab.ui.widgets.game_grid import FlowLayout, GameGrid


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def library(tmp_path):
    database = Database(tmp_path / "library.db")
    yield Library(database)
    database.close()


def populate(library, count: int):
    for index in range(count):
        library.add_game(title=f"Game {index:04d}", system="ps3")
    return library.list_games()


# ── Clearing ──────────────────────────────────────────────────────

class CountingFlowLayout(FlowLayout):
    """A layout that records how often Qt asks it to look an item up."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lookups = 0

    def itemAt(self, index):
        self.lookups += 1
        return super().itemAt(index)


def test_clearing_the_grid_does_not_rescan_per_card(qt_app, library):
    """The lookups must not grow with the square of the card count."""
    games = populate(library, 60)
    grid = GameGrid(THEMES["rose-dark"])

    counting = CountingFlowLayout()
    grid._flow = counting
    grid._container.setLayout(counting)

    grid.set_games(games)
    counting.lookups = 0
    grid.clear()

    # Quadratic would be ~1800 here. A small constant per card is fine; the
    # bound catches a return to scanning the whole list each time.
    assert counting.lookups < len(games) * 3, (
        f"{counting.lookups} lookups to clear {len(games)} cards — "
        "clearing is scanning the layout per card again"
    )


def test_the_layout_can_be_emptied_in_one_step(qt_app):
    layout = FlowLayout()
    from PySide6.QtWidgets import QWidget

    widgets = [QWidget() for _ in range(5)]
    for widget in widgets:
        layout.addWidget(widget)

    taken = layout.take_all()

    assert len(taken) == 5
    assert layout.count() == 0


def test_clearing_really_removes_the_cards(qt_app, library):
    grid = GameGrid(THEMES["rose-dark"])
    grid.set_games(populate(library, 10))

    grid.clear()

    assert grid.count == 0
    assert grid._flow.count() == 0
    assert grid.selected_id is None


def test_rebuilding_replaces_rather_than_accumulates(qt_app, library):
    games = populate(library, 10)
    grid = GameGrid(THEMES["rose-dark"])

    grid.set_games(games)
    grid.set_games(games)

    assert grid.count == 10
    assert grid._flow.count() == 10


# ── Restyling ─────────────────────────────────────────────────────

def test_restyling_reuses_the_cards(qt_app, library):
    """A theme change must not destroy and rebuild the whole library."""
    grid = GameGrid(THEMES["rose-dark"])
    grid.set_games(populate(library, 20))
    before = list(grid._cards.values())

    grid.restyle(THEMES["nord"], STYLES["sharp"])

    assert list(grid._cards.values()) == before, "cards were rebuilt, not repainted"


def test_restyling_actually_changes_the_cards(qt_app, library):
    """The other half: reuse is worthless if nothing visibly updates."""
    grid = GameGrid(THEMES["rose-dark"], style=STYLES["soft"])
    grid.set_games(populate(library, 5))
    card = next(iter(grid._cards.values()))

    assert card.corner_radius == STYLES["soft"].radius

    grid.restyle(THEMES["nord"], STYLES["sharp"])

    assert card.theme is THEMES["nord"]
    assert card.corner_radius == 0


def test_card_corners_follow_the_chosen_style(qt_app, library):
    """The complaint that started this: the sliders did not appear to do much."""
    grid = GameGrid(THEMES["rose-dark"], style=STYLES["rounded"])
    grid.set_games(populate(library, 3))
    card = next(iter(grid._cards.values()))

    for radius in (0, 4, 20):
        grid.restyle(THEMES["rose-dark"], STYLES["rounded"].with_overrides(radius=radius))
        assert card.corner_radius == radius


def test_an_absurd_radius_is_clamped_to_the_card(qt_app, library):
    """The Pill style asks for more rounding than any widget can have."""
    grid = GameGrid(THEMES["rose-dark"], style=STYLES["pill"])
    grid.set_games(populate(library, 3))
    card = next(iter(grid._cards.values()))

    assert card.corner_radius == card.cover_width // 2


def test_grid_spacing_follows_the_style(qt_app, library):
    grid = GameGrid(THEMES["rose-dark"], style=STYLES["rounded"])
    grid.set_games(populate(library, 3))

    grid.restyle(THEMES["rose-dark"], STYLES["compact"])

    assert grid._flow.spacing() == STYLES["compact"].spacing
