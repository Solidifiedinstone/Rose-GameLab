"""The cover tile that represents one game in the grid.

Games are shown by their cover art. When a game has no art, the card draws a
filled panel with the title laid out inside it — the same approach Steam takes
for games missing a library image — rather than a broken-image icon or an empty
box. A titled placeholder is still identifiable at a glance; a blank one is not.

Cover images are decoded once and cached at the size they are drawn, because a
library of several thousand games would otherwise decode several thousand
full-resolution JPEGs on every scroll.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from rose_gamelab.ui.theme import COVER_RATIO, RADIUS, Theme

# Decoded covers, keyed by (path, width). Shared across every card so that
# scrolling back up does not re-decode. Bounded so a huge library cannot
# exhaust memory.
_PIXMAP_CACHE: dict[tuple[str, int], QPixmap] = {}
_CACHE_LIMIT = 600


def load_cover(path: str, width: int) -> Optional[QPixmap]:
    """Load and scale a cover, using the shared cache. None if unreadable."""
    key = (path, width)
    cached = _PIXMAP_CACHE.get(key)
    if cached is not None:
        return cached

    if not path or not Path(path).is_file():
        return None

    pixmap = QPixmap(path)
    if pixmap.isNull():
        return None

    height = int(width * COVER_RATIO)
    scaled = pixmap.scaled(
        width, height,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )

    if len(_PIXMAP_CACHE) >= _CACHE_LIMIT:
        # Plain FIFO eviction: cheap, and scrolling is mostly sequential so
        # the oldest entries really are the least likely to be needed next.
        for old_key in list(_PIXMAP_CACHE)[: _CACHE_LIMIT // 4]:
            _PIXMAP_CACHE.pop(old_key, None)

    _PIXMAP_CACHE[key] = scaled
    return scaled


def clear_cover_cache() -> None:
    """Drop every decoded cover — called when artwork is rescraped."""
    _PIXMAP_CACHE.clear()


class GameCard(QWidget):
    """One game, drawn as its cover art."""

    activated = Signal(int)        # double-click or Enter: launch
    selected = Signal(int)         # single click: show details
    context_requested = Signal(int, object)

    def __init__(
        self,
        game,
        theme: Theme,
        *,
        width: int = 160,
        show_title: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self.game = game
        self.theme = theme
        self.cover_width = width
        self.show_title = show_title
        self._hovered = False
        self._is_selected = False

        # Title strip under the cover, when enabled.
        self.title_height = 34 if show_title else 0
        self.setFixedSize(
            QSize(width, int(width * COVER_RATIO) + self.title_height)
        )
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(game.title)

    # ── State ─────────────────────────────────────────────────────

    def set_selected(self, selected: bool) -> None:
        if self._is_selected != selected:
            self._is_selected = selected
            self.update()

    def refresh(self, game=None) -> None:
        """Re-read the game (after a scrape filled in its cover, for instance)."""
        if game is not None:
            self.game = game
            self.setToolTip(game.title)
        self.update()

    # ── Painting ──────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        cover_height = int(self.cover_width * COVER_RATIO)
        cover_rect = QRectF(0, 0, self.cover_width, cover_height)

        # Rounded clip for the artwork, matching the rest of the interface.
        path = QPainterPath()
        path.addRoundedRect(cover_rect, RADIUS, RADIUS)

        painter.save()
        painter.setClipPath(path)

        pixmap = load_cover(self.game.cover_path or "", self.cover_width)
        if pixmap is not None:
            # Centre-crop, so covers that are not exactly 2:3 are not squashed.
            x = (pixmap.width() - self.cover_width) / 2
            y = (pixmap.height() - cover_height) / 2
            painter.drawPixmap(cover_rect, pixmap, QRectF(x, y, self.cover_width, cover_height))
        else:
            self._paint_placeholder(painter, cover_rect)

        painter.restore()

        self._paint_border(painter, path)

        if self.show_title:
            self._paint_title_strip(painter, cover_height)

    def _paint_placeholder(self, painter: QPainter, rect: QRectF) -> None:
        """Draw a titled panel for a game with no cover art.

        The title is wrapped and centred inside the box, so the game is still
        identifiable — which a blank rectangle would not be.
        """
        painter.fillRect(rect, QColor(self.theme.placeholder))

        painter.setPen(QPen(QColor(self.theme.text_dim)))
        font = QFont(painter.font())
        # Shrink the type for long titles so more of the name fits before
        # elision, rather than truncating a five-word title to two words.
        font.setPointSizeF(max(8.0, 13.0 - len(self.game.title) / 14))
        font.setBold(True)
        painter.setFont(font)

        inset = rect.adjusted(12, 12, -12, -12)
        painter.drawText(
            inset,
            int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap),
            self.game.title,
        )

    def _paint_border(self, painter: QPainter, path: QPainterPath) -> None:
        """Selection and hover states are drawn as a ring, never a fill,
        so they never obscure the artwork."""
        if self._is_selected:
            pen = QPen(QColor(self.theme.accent), 3)
        elif self._hovered or self.hasFocus():
            pen = QPen(QColor(self.theme.accent_muted), 2)
        else:
            return

        painter.setPen(pen)
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawPath(path)

    def _paint_title_strip(self, painter: QPainter, top: float) -> None:
        painter.setPen(QPen(QColor(
            self.theme.text if (self._is_selected or self._hovered) else self.theme.text_dim
        )))

        font = QFont(painter.font())
        font.setPointSizeF(10.0)
        font.setBold(False)
        painter.setFont(font)

        rect = QRectF(2, top + 6, self.cover_width - 4, self.title_height - 8)
        metrics = QFontMetrics(font)
        elided = metrics.elidedText(
            self.game.title, Qt.TextElideMode.ElideRight, int(rect.width())
        )
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), elided)

    # ── Interaction ───────────────────────────────────────────────

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().focusOutEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self.selected.emit(self.game.id)
        elif event.button() == Qt.MouseButton.RightButton:
            self.context_requested.emit(self.game.id, event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.game.id)
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Enter launches, so the grid is fully usable from the keyboard and
        # from a gamepad in Big Picture mode.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.game.id)
            event.accept()
            return
        super().keyPressEvent(event)
