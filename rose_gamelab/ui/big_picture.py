"""Big Picture mode: a full-screen interface meant to be driven from the couch.

Modelled on Steam's Big Picture. Everything is large, high-contrast, and
navigable entirely with a d-pad — no pointer, no small targets, no text the
user has to lean in to read.

The design rule throughout is that exactly one thing is focused and the focused
thing is unmistakable. On a television two metres away, a subtle highlight is
no highlight at all, so the selection scales the cover and draws a thick accent
ring rather than tinting a background.

Navigation is a horizontal shelf per row, which is the layout every console
interface has converged on because it maps directly onto a d-pad: left and
right move within a row, up and down change rows.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtGui import QFont, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core.emulator import get_system
from rose_gamelab.core.launcher import LaunchError
from rose_gamelab.ui import theme as ui_theme
from rose_gamelab.ui.theme import COVER_RATIO, Theme
from rose_gamelab.ui.widgets.controller_indicator import ControllerIndicator
from rose_gamelab.ui.widgets.game_card import load_cover

logger = logging.getLogger(__name__)

# Deliberately large: these are read from across a room.
TILE_WIDTH = 190
TILE_FOCUSED_SCALE = 1.16
SHELF_SPACING = 22
SCROLL_MS = 220


class BigPictureTile(QLabel):
    """One game in a shelf. Grows and gains a ring when focused.

    Clickable as well as d-pad navigable: Big Picture is used on a television,
    but it is also opened on the desktop by people holding a mouse, and a tile
    that cannot be clicked reads as broken.
    """

    #: Emitted with this tile when it is clicked, and again on double click.
    selected = Signal(object)
    activated = Signal(object)

    def __init__(self, game, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.game = game
        self.theme = theme
        self._focused = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.base_width = TILE_WIDTH
        self.base_height = int(TILE_WIDTH * COVER_RATIO)
        # Reserve the grown size so neighbours do not shift when focus moves.
        self.setFixedSize(
            int(self.base_width * TILE_FOCUSED_SCALE),
            int(self.base_height * TILE_FOCUSED_SCALE),
        )
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setScaledContents(False)

        self._render()

    def set_focused(self, focused: bool) -> None:
        if self._focused != focused:
            self._focused = focused
            self._render()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _render(self) -> None:
        width = int(self.base_width * (TILE_FOCUSED_SCALE if self._focused else 1.0))

        pixmap = load_cover(self.game.cover_path or "", width)

        if pixmap is not None:
            self.setPixmap(pixmap)
            self.setText("")
        else:
            # Titled placeholder, same reasoning as the desktop grid: a named
            # box is identifiable from the couch, a blank one is not.
            self.setPixmap(QPixmap())
            self.setText(self.game.title)
            self.setWordWrap(True)

        border = (
            f"border: 4px solid {self.theme.accent};"
            if self._focused
            else "border: 4px solid transparent;"
        )
        self.setStyleSheet(
            f"background-color: {self.theme.placeholder};"
            f"border-radius: {ui_theme.RADIUS_LARGE}px;"
            f"color: {self.theme.text};"
            f"font-size: 15px; font-weight: 600; padding: 10px;"
            f"{border}"
        )


class Shelf(QWidget):
    """A horizontally scrolling row of games with a heading."""

    #: A tile in this shelf was clicked, or double-clicked, with its index.
    tile_selected = Signal(int)
    tile_activated = Signal(int)

    def __init__(self, title: str, games: list, theme: Theme, parent=None) -> None:
        super().__init__(parent)

        self.title = title
        self.games = games
        self.theme = theme
        self.tiles: list[BigPictureTile] = []
        self.index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 8, 48, 8)
        layout.setSpacing(8)

        heading = QLabel(title)
        font = QFont()
        font.setPointSize(19)
        font.setBold(True)
        heading.setFont(font)
        heading.setStyleSheet(f"color: {theme.text};")
        layout.addWidget(heading)

        self.scroll = QScrollArea()
        # A scroll area takes keyboard focus by default and answers the arrow
        # keys itself by scrolling. That silently swallowed every d-pad press
        # before it could reach the window, so the selection never moved and
        # Big Picture could not be navigated at all.
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setWidgetResizable(True)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("background: transparent;")
        self.scroll.setFixedHeight(int(TILE_WIDTH * COVER_RATIO * TILE_FOCUSED_SCALE) + 16)

        strip = QWidget()
        strip.setStyleSheet("background: transparent;")
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(SHELF_SPACING)

        for position, game in enumerate(games):
            tile = BigPictureTile(game, theme)
            tile.selected.connect(
                lambda _tile, index=position: self.tile_selected.emit(index)
            )
            tile.activated.connect(
                lambda _tile, index=position: self.tile_activated.emit(index)
            )
            self.tiles.append(tile)
            row.addWidget(tile)

        row.addStretch(1)
        self.scroll.setWidget(strip)
        layout.addWidget(self.scroll)

        self._scroll_animation = QPropertyAnimation(
            self.scroll.horizontalScrollBar(), b"value", self
        )
        self._scroll_animation.setDuration(SCROLL_MS)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    @property
    def current_game(self):
        return self.games[self.index] if self.games else None

    def set_active(self, active: bool) -> None:
        """Only the active shelf shows a focused tile."""
        for position, tile in enumerate(self.tiles):
            tile.set_focused(active and position == self.index)
        if active:
            self._reveal()

    def move(self, delta: int) -> bool:
        """Move within the shelf. False when already at the end."""
        return self.focus_tile(self.index + delta)

    def focus_tile(self, index: int) -> bool:
        """Focus one tile by position. False if there is no such tile."""
        if not (0 <= index < len(self.tiles)):
            return False

        self.tiles[self.index].set_focused(False)
        self.index = index
        self.tiles[self.index].set_focused(True)
        self._reveal()
        return True

    def _reveal(self) -> None:
        """Scroll so the focused tile sits comfortably inside the viewport."""
        if not self.tiles:
            return

        tile = self.tiles[self.index]
        viewport = self.scroll.viewport().width()
        # Centre the focused tile rather than merely making it visible, so the
        # eye stays in one place while the shelf moves under it.
        target = tile.x() - (viewport - tile.width()) // 2
        bar = self.scroll.horizontalScrollBar()
        target = max(bar.minimum(), min(target, bar.maximum()))

        self._scroll_animation.stop()
        self._scroll_animation.setStartValue(bar.value())
        self._scroll_animation.setEndValue(target)
        self._scroll_animation.start()


class BigPictureWindow(QWidget):
    """The full-screen couch interface."""

    closed = Signal()

    def __init__(self, library, launcher, theme: Theme, parent=None) -> None:
        super().__init__(parent)

        self.library = library
        self.launcher = launcher
        self.theme = theme
        self.shelves: list[Shelf] = []
        self.shelf_index = 0

        self.setWindowTitle("Rose GameLab — Big Picture")
        self.setStyleSheet(f"background-color: {theme.background};")
        # The whole window takes key events; individual tiles never focus, so
        # d-pad navigation cannot get lost inside a child widget.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 30, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(48, 0, 48, 6)

        title = QLabel("Rose GameLab")
        font = QFont()
        font.setPointSize(28)
        font.setBold(True)
        title.setFont(font)
        title.setStyleSheet(f"color: {self.theme.accent};")
        header.addWidget(title)
        header.addStretch(1)

        self.now_showing = QLabel()
        self.now_showing.setStyleSheet(f"color: {self.theme.text_dim}; font-size: 15px;")
        header.addWidget(self.now_showing)

        # Larger than the desktop one: this is read from a sofa, and a dying
        # pad is worth knowing about before the game starts, not during it.
        self.controller_indicator = ControllerIndicator(self.theme, font_size=16)
        header.addSpacing(20)
        header.addWidget(self.controller_indicator)

        layout.addLayout(header)

        shelves_area = QScrollArea()
        # As with each shelf: never take focus, or it eats the arrow keys.
        shelves_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        shelves_area.setWidgetResizable(True)
        shelves_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        shelves_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        shelves_area.setFrameShape(QFrame.Shape.NoFrame)
        shelves_area.setStyleSheet("background: transparent;")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        self.shelf_layout = QVBoxLayout(container)
        self.shelf_layout.setContentsMargins(0, 0, 0, 30)
        self.shelf_layout.setSpacing(10)

        for title_text, games in self._build_shelves():
            if not games:
                continue
            shelf = Shelf(title_text, games, self.theme)
            position = len(self.shelves)
            shelf.tile_selected.connect(
                lambda index, row=position: self._select(row, index)
            )
            shelf.tile_activated.connect(
                lambda index, row=position: self._activate(row, index)
            )
            self.shelves.append(shelf)
            self.shelf_layout.addWidget(shelf)

        self.shelf_layout.addStretch(1)
        shelves_area.setWidget(container)
        self.shelves_area = shelves_area
        layout.addWidget(shelves_area, 1)

        # Only what actually works is listed. The A/B buttons were named here
        # before any gamepad input existed to read them, which is a promise the
        # interface could not keep.
        hint = QLabel(
            "◀ ▶ browse    ▲ ▼ rows    Enter  play    Click / double-click  "
            "select and play    Esc  exit"
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(
            f"color: {self.theme.text_dim}; font-size: 14px; padding: 10px;"
        )
        layout.addWidget(hint)

        if self.shelves:
            self.shelves[0].set_active(True)
            self._update_now_showing()
        else:
            empty = QLabel("Your library is empty.\nAdd a source to get started.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {self.theme.text_dim}; font-size: 20px;")
            self.shelf_layout.insertWidget(0, empty)

    def _build_shelves(self) -> list[tuple[str, list]]:
        """The rows, in the order they are most likely to be wanted.

        Recently played comes first because the most common reason to open a
        launcher is to continue what you were already playing.
        """
        shelves: list[tuple[str, list]] = [
            ("Continue Playing", [
                g for g in self.library.list_games(sort="last_played", descending=True)
                if g.last_played
            ][:20]),
            ("Favourites", self.library.list_games(favorites_only=True)[:20]),
            ("Recently Added", self.library.list_games(sort="added", descending=True)[:20]),
        ]

        for system_id, _count in self.library.systems_in_library():
            system = get_system(system_id)
            shelves.append((
                system.name if system else system_id,
                self.library.list_games(system=system_id)[:40],
            ))

        return shelves

    # ── Navigation ────────────────────────────────────────────────

    @property
    def current_shelf(self) -> Optional[Shelf]:
        if not self.shelves:
            return None
        return self.shelves[self.shelf_index]

    def _select(self, row: int, index: int) -> None:
        """Move the selection to a tile that was clicked."""
        if not (0 <= row < len(self.shelves)):
            return

        if row != self.shelf_index:
            self.shelves[self.shelf_index].set_active(False)
            self.shelf_index = row
            self.shelves[row].set_active(True)

        self.shelves[row].focus_tile(index)
        self._update_now_showing()
        # Clicking a tile must not leave focus somewhere the arrow keys are
        # not read, or the pointer and the d-pad stop agreeing.
        self.setFocus()

    def _activate(self, row: int, index: int) -> None:
        """Select and launch a double-clicked tile."""
        self._select(row, index)
        self._launch_current()

    def set_controllers(self, statuses: list) -> None:
        """Show which pads are connected. Fed by the main window's watcher."""
        self.controller_indicator.set_statuses(statuses)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Without this the first focusable child holds the keyboard, and every
        # arrow key goes to it instead of to the navigation below.
        self.setFocus()

    def _change_shelf(self, delta: int) -> None:
        new_index = self.shelf_index + delta
        if not (0 <= new_index < len(self.shelves)):
            return

        self.shelves[self.shelf_index].set_active(False)
        self.shelf_index = new_index
        self.shelves[self.shelf_index].set_active(True)

        self.shelves_area.ensureWidgetVisible(self.shelves[self.shelf_index], 0, 60)
        self._update_now_showing()

    def _update_now_showing(self) -> None:
        shelf = self.current_shelf
        game = shelf.current_game if shelf else None
        if game is None:
            self.now_showing.clear()
            return

        parts = [game.title]
        if game.play_seconds:
            parts.append(f"{game.playtime_hours} h played")
        self.now_showing.setText("   ·   ".join(parts))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        shelf = self.current_shelf

        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self.close()
        elif key == Qt.Key.Key_Left and shelf:
            shelf.move(-1)
            self._update_now_showing()
        elif key == Qt.Key.Key_Right and shelf:
            shelf.move(1)
            self._update_now_showing()
        elif key == Qt.Key.Key_Up:
            self._change_shelf(-1)
        elif key == Qt.Key.Key_Down:
            self._change_shelf(1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._launch_current()
        else:
            super().keyPressEvent(event)
            return

        event.accept()

    # ── Launching ─────────────────────────────────────────────────

    def _launch_current(self) -> None:
        shelf = self.current_shelf
        game = shelf.current_game if shelf else None
        if game is None:
            return

        try:
            self.launcher.launch(game.id)
        except LaunchError as exc:
            # Errors have to be readable from the couch too.
            box = QMessageBox(self)
            box.setWindowTitle("Could not launch")
            box.setText(str(exc))
            box.setStyleSheet(f"font-size: 16px; background-color: {self.theme.panel};")
            box.exec()
            return

        self.now_showing.setText(f"Launching {game.title}…")

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)
