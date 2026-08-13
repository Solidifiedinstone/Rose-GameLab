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

from rose_gamelab.ui.theme import COVER_RATIO, RADIUS, Style, Theme

# Decoded covers, keyed by (path, width). Shared across every card so that
# scrolling back up does not re-decode. Bounded so a huge library cannot
# exhaust memory.
_PIXMAP_CACHE: dict[tuple[str, int], QPixmap] = {}
_CACHE_LIMIT = 600


#: How far a picture's shape may stray from 2:3 before it is fitted rather than
#: cropped. Box art is close enough to crop invisibly; a PS3 dump's ICON0.PNG is
#: 320x176, and cropping that to a portrait tile throws away nearly two thirds of
#: the width — usually including the title.
FIT_TOLERANCE = 0.18


def _blurred(pixmap: QPixmap, width: int, height: int) -> QPixmap:
    """A soft, dark version of a picture, to sit behind one that does not fill.

    Blurred by scaling down hard and back up, which costs one resample and
    needs no graphics-effect machinery.
    """
    small = pixmap.scaled(
        max(1, width // 14), max(1, height // 14),
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    backdrop = small.scaled(
        width, height,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )

    # Darkened so the sharp picture in front stays the thing you look at.
    canvas = QPixmap(width, height)
    canvas.fill(Qt.GlobalColor.black)
    painter = QPainter(canvas)
    x = (backdrop.width() - width) / 2
    y = (backdrop.height() - height) / 2
    painter.drawPixmap(
        QRectF(0, 0, width, height), backdrop, QRectF(x, y, width, height)
    )
    painter.fillRect(0, 0, width, height, QColor(0, 0, 0, 110))
    painter.end()
    return canvas


def load_cover(path: str, width: int) -> Optional[QPixmap]:
    """A cover prepared to exactly fill a card. None if unreadable.

    Art that is roughly 2:3 is cropped to fit, which is invisible. Art that is
    nothing like 2:3 — a dashboard icon, a landscape header — is shown WHOLE on
    a blurred copy of itself, because cropping it to a portrait tile destroys it.
    """
    key = (path, width)
    cached = _PIXMAP_CACHE.get(key)
    if cached is not None:
        return cached

    if not path or not Path(path).is_file():
        return None

    pixmap = QPixmap(path)
    if pixmap.isNull() or pixmap.height() == 0:
        return None

    height = int(width * COVER_RATIO)
    target = width / height
    source = pixmap.width() / pixmap.height()

    if abs(source - target) / target <= FIT_TOLERANCE:
        # Close enough to portrait: fill the tile and crop the overhang.
        expanded = pixmap.scaled(
            width, height,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled = QPixmap(width, height)
        scaled.fill(Qt.GlobalColor.transparent)
        painter = QPainter(scaled)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        x = (expanded.width() - width) / 2
        y = (expanded.height() - height) / 2
        painter.drawPixmap(
            QRectF(0, 0, width, height), expanded, QRectF(x, y, width, height)
        )
        painter.end()
    else:
        # The wrong shape entirely: show all of it, centred, on a blurred
        # backdrop so the tile is still full rather than letterboxed in black.
        fitted = pixmap.scaled(
            width, height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled = _blurred(pixmap, width, height)
        painter = QPainter(scaled)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(
            int((width - fitted.width()) / 2),
            int((height - fitted.height()) / 2),
            fitted,
        )
        painter.end()

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
        style: Optional[Style] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self.game = game
        self.theme = theme
        # Cards are painted by hand rather than styled by QSS, so the chosen
        # style has to reach them explicitly or a card keeps the default
        # corner radius no matter what the user picks.
        self.style_ = style
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

    def restyle(self, theme: Theme, style: Optional[Style] = None) -> None:
        """Repaint in new colours or a new shape, without being rebuilt.

        Cheap on purpose: changing a theme used to destroy and recreate every
        card in the library, which is most of what made the appearance sliders
        unusable.
        """
        self.theme = theme
        self.style_ = style
        self.update()

    @property
    def corner_radius(self) -> int:
        """The cover's corner rounding, clamped to something a cover can have.

        The Pill style asks for a radius larger than any widget; on a rectangle
        the meaningful maximum is half the short side.
        """
        radius = self.style_.radius if self.style_ is not None else RADIUS
        return max(0, min(radius, self.cover_width // 2))

    # ── Painting ──────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        cover_height = int(self.cover_width * COVER_RATIO)
        cover_rect = QRectF(0, 0, self.cover_width, cover_height)

        # Rounded clip for the artwork, matching the rest of the interface.
        path = QPainterPath()
        radius = self.corner_radius
        path.addRoundedRect(cover_rect, radius, radius)

        painter.save()
        painter.setClipPath(path)

        pixmap = load_cover(self.game.cover_path or "", self.cover_width)
        if pixmap is not None:
            # Already prepared at exactly this size, cropped or fitted to suit
            # its shape, so it is drawn straight in.
            painter.drawPixmap(cover_rect, pixmap, QRectF(pixmap.rect()))
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

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.update()
        super().focusOutEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self.selected.emit(self.game.id)
        elif event.button() == Qt.MouseButton.RightButton:
            self.context_requested.emit(self.game.id, event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.game.id)
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        # Enter launches, so the grid is fully usable from the keyboard and
        # from a gamepad in Big Picture mode.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.game.id)
            event.accept()
            return
        super().keyPressEvent(event)
