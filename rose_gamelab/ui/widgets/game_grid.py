"""The cover grid.

A real flow layout: cards wrap to fill the available width and reflow when the
window resizes. The previous implementation was a single-column QVBoxLayout
with a comment promising to "wrap manually" that never did.

Cards are created once and repositioned on resize rather than rebuilt, because
rebuilding several hundred widgets on every resize event makes the window
unusable while being dragged.
"""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QLayout,
    QLayoutItem,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.ui.theme import SPACING, Theme
from rose_gamelab.ui.widgets.game_card import GameCard


class FlowLayout(QLayout):
    """Left-to-right layout that wraps to the next row when it runs out of width."""

    def __init__(self, parent: Optional[QWidget] = None, spacing: int = SPACING) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(spacing)
        self.setContentsMargins(spacing, spacing, spacing, spacing)

    # Qt requires these five overrides for a custom layout.
    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    def _layout(self, rect: QRect, *, apply: bool) -> int:
        """Position items; returns the total height needed."""
        margins = self.contentsMargins()
        available = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )

        x = available.x()
        y = available.y()
        row_height = 0
        spacing = self.spacing()

        for item in self._items:
            hint = item.sizeHint()

            if x + hint.width() > available.right() and row_height > 0:
                x = available.x()
                y += row_height + spacing
                row_height = 0

            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))

            x += hint.width() + spacing
            row_height = max(row_height, hint.height())

        return y + row_height - rect.y() + margins.bottom()


class GameGrid(QScrollArea):
    """Scrollable grid of game cards."""

    game_activated = Signal(int)
    game_selected = Signal(int)
    game_context_requested = Signal(int, object)

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.theme = theme
        self.card_width = 160
        self.show_titles = True
        self._cards: dict[int, GameCard] = {}
        self._selected_id: Optional[int] = None

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._container = QWidget()
        self._flow = FlowLayout(self._container)
        self.setWidget(self._container)

        # Shown instead of the grid when a filter matches nothing.
        self._empty = QLabel()
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setObjectName("Subtle")
        self._empty.setWordWrap(True)
        self._empty.hide()

        wrapper = QVBoxLayout()
        wrapper.addWidget(self._empty)

    # ── Content ───────────────────────────────────────────────────

    def set_games(self, games: Iterable) -> None:
        """Replace the grid's contents."""
        self.clear()

        games = list(games)
        for game in games:
            card = GameCard(
                game, self.theme,
                width=self.card_width,
                show_title=self.show_titles,
            )
            card.activated.connect(self.game_activated.emit)
            card.selected.connect(self._on_card_selected)
            card.context_requested.connect(self.game_context_requested.emit)

            self._cards[game.id] = card
            self._flow.addWidget(card)

        self._container.adjustSize()

    def clear(self) -> None:
        while self._flow.count():
            item = self._flow.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()
        self._selected_id = None

    def refresh_game(self, game) -> None:
        """Update one card in place — after a scrape found its cover."""
        card = self._cards.get(game.id)
        if card:
            card.refresh(game)

    @property
    def count(self) -> int:
        return len(self._cards)

    # ── Selection ─────────────────────────────────────────────────

    def _on_card_selected(self, game_id: int) -> None:
        self.select(game_id)
        self.game_selected.emit(game_id)

    def select(self, game_id: int) -> None:
        if self._selected_id is not None:
            previous = self._cards.get(self._selected_id)
            if previous:
                previous.set_selected(False)

        card = self._cards.get(game_id)
        if card:
            card.set_selected(True)
            self._selected_id = game_id
            self.ensureWidgetVisible(card, 40, 40)

    @property
    def selected_id(self) -> Optional[int]:
        return self._selected_id

    def select_first(self) -> None:
        if self._cards:
            first = next(iter(self._cards))
            self.select(first)
            self._cards[first].setFocus()

    # ── Sizing ────────────────────────────────────────────────────

    def set_card_width(self, width: int) -> None:
        """Change cover size. Cards are rebuilt because their geometry is fixed."""
        if width == self.card_width:
            return

        self.card_width = width
        games = [card.game for card in self._cards.values()]
        self.set_games(games)

    def set_show_titles(self, show: bool) -> None:
        if show == self.show_titles:
            return

        self.show_titles = show
        games = [card.game for card in self._cards.values()]
        self.set_games(games)
