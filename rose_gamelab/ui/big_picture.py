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
    QTimer,
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

# How many games a shelf shows. Applied in the query rather than by slicing.
SHELF_LIMIT = 20
SYSTEM_SHELF_LIMIT = 40

# Covers not yet on screen are decoded between events rather than up front.
# Small batches, so navigation never waits behind a decode.
BACKGROUND_COVER_MS = 30
BACKGROUND_COVER_BATCH = 6


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
        self._cover: Optional[QPixmap] = None
        self._cover_loaded = False
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

    @property
    def cover_loaded(self) -> bool:
        return self._cover_loaded

    def load_cover(self) -> None:
        """Decode this tile's cover, once.

        Decoding is deliberately not done in the constructor. A library of a
        few hundred games builds a couple of thousand tiles, and decoding every
        one of their covers before the window appears meant Big Picture took
        seconds to open — all of it spent on art for shelves nobody had
        scrolled to yet.

        The picture is decoded at the FOCUSED size and scaled down in memory
        for the resting state. Asking the loader for two different widths would
        decode the same file twice and take two slots in a shared cache that a
        large library already overflows, so every focus move would re-read it
        from disk.
        """
        if self._cover_loaded:
            return

        self._cover_loaded = True
        self._cover = load_cover(
            self.game.cover_path or "", int(self.base_width * TILE_FOCUSED_SCALE)
        )
        if self._cover is not None:
            self._render()

    def set_focused(self, focused: bool) -> None:
        if self._focused != focused:
            self._focused = focused
            # A tile being focused is on screen by definition.
            self.load_cover()
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
        pixmap = self._cover
        if pixmap is not None and not self._focused:
            # Scaling an already-decoded pixmap is memory work; re-asking the
            # loader for a smaller width would be a second decode from disk.
            pixmap = pixmap.scaled(
                self.base_width, self.base_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

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

        self._row = row
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

    @property
    def populated(self) -> bool:
        return len(self.tiles) == len(self.games)

    def populate(self) -> None:
        """Build this shelf's tiles.

        Deferred because building widgets is the bulk of what opening this
        window costs — a Qt layout insertion per tile, and hundreds of tiles
        for shelves several screens down that may never be looked at. The
        games are known from the start; only the widgets wait.
        """
        if self.populated:
            return

        for position, game in enumerate(self.games):
            tile = BigPictureTile(game, self.theme)
            tile.selected.connect(
                lambda _tile, index=position: self.tile_selected.emit(index)
            )
            tile.activated.connect(
                lambda _tile, index=position: self.tile_activated.emit(index)
            )
            self.tiles.append(tile)
            # Before the trailing stretch, which must stay last or the tiles
            # bunch up against the far edge.
            self._row.insertWidget(self._row.count() - 1, tile)

    def load_covers_near(self, index: Optional[int] = None, *, span: int = 8) -> int:
        """Decode the covers around a position. Returns how many were loaded.

        A shelf is a horizontal strip: only a handful of tiles either side of
        the selection can be on screen, and the rest are work nobody has asked
        for yet.
        """
        self.populate()
        if not self.tiles:
            return 0

        centre = self.index if index is None else index
        first = max(0, centre - span)
        last = min(len(self.tiles), centre + span + 1)

        loaded = 0
        for tile in self.tiles[first:last]:
            if not tile.cover_loaded:
                tile.load_cover()
                loaded += 1
        return loaded

    def unloaded_tiles(self):
        return [tile for tile in self.tiles if not tile.cover_loaded]

    def fully_loaded(self) -> bool:
        return self.populated and not self.unloaded_tiles()

    def set_active(self, active: bool) -> None:
        """Only the active shelf shows a focused tile."""
        if active:
            self.populate()
        for position, tile in enumerate(self.tiles):
            tile.set_focused(active and position == self.index)
        if active:
            self.load_covers_near()
            self._reveal()

    def move(self, delta: int) -> bool:
        """Move within the shelf. False when already at the end."""
        return self.focus_tile(self.index + delta)

    def focus_tile(self, index: int) -> bool:
        """Focus one tile by position. False if there is no such tile."""
        self.populate()
        if not (0 <= index < len(self.tiles)):
            return False

        self.tiles[self.index].set_focused(False)
        self.index = index
        self.tiles[self.index].set_focused(True)
        # Ahead of the selection, so covers are already there when the user
        # arrives rather than appearing under them.
        self.load_covers_near()
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

        # Created here rather than when work is found, so an empty library —
        # which builds no shelves at all — does not leave this undefined.
        self._background = QTimer(self)
        self._background.setInterval(BACKGROUND_COVER_MS)
        self._background.timeout.connect(self._load_a_few_covers)

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
            # Shelves near the top are the ones about to be looked at.
            for shelf in self.shelves[1:3]:
                shelf.load_covers_near(0)
            self._start_background_covers()
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
        # Limits are pushed into the queries rather than sliced afterwards. A
        # shelf shows at most forty games; fetching a whole system's worth to
        # throw away all but forty built thousands of objects per open.
        shelves: list[tuple[str, list]] = [
            ("Continue Playing", [
                game for game in self.library.list_games(
                    sort="last_played", descending=True, limit=SHELF_LIMIT,
                )
                if game.last_played
            ]),
            ("Favourites", self.library.list_games(
                favorites_only=True, limit=SHELF_LIMIT,
            )),
            ("Recently Added", self.library.list_games(
                sort="added", descending=True, limit=SHELF_LIMIT,
            )),
        ]

        for system_id, _count in self.library.systems_in_library():
            system = get_system(system_id)
            shelves.append((
                system.name if system else system_id,
                self.library.list_games(system=system_id, limit=SYSTEM_SHELF_LIMIT),
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

    @property
    def _work_remaining(self) -> bool:
        return not all(shelf.fully_loaded() for shelf in self.shelves)

    def _start_background_covers(self) -> None:
        """Fill in the remaining covers a few at a time, while idle.

        Decoding every cover up front is what made this window take seconds to
        open. Decoding none of them means art appearing under the user as they
        scroll. So the ones on screen are loaded immediately and the rest
        trickle in between events, in batches small enough that navigation
        never waits on them.
        """
        self._background.start()

    def _load_a_few_covers(self) -> None:
        if not self.shelves:
            self._background.stop()
            return

        remaining = BACKGROUND_COVER_BATCH

        for shelf in self.shelves:
            if not shelf.populated:
                # One shelf per tick: building a shelf is the expensive half,
                # and doing several would be the stall this exists to avoid.
                shelf.populate()
                return

            for tile in shelf.unloaded_tiles():
                tile.load_cover()
                remaining -= 1
                if remaining <= 0:
                    return

        if all(shelf.fully_loaded() for shelf in self.shelves):
            # Everything is built and decoded; stop waking up for nothing.
            self._background.stop()

    def set_controllers(self, statuses: list) -> None:
        """Show which pads are connected. Fed by the main window's watcher."""
        self.controller_indicator.set_statuses(statuses)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Reopening picks the work back up: the window object survives being
        # closed, so there may be covers left from last time.
        if self._work_remaining and not self._background.isActive():
            self._background.start()
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
        # The window object outlives being closed — the main window keeps a
        # reference to it — so without this the timer goes on decoding covers
        # for a window nobody can see. Closing Big Picture usually means a game
        # has just started, which is the worst possible moment to spend the
        # machine on artwork.
        self._background.stop()
        self.closed.emit()
        super().closeEvent(event)
