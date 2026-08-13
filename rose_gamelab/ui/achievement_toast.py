"""The unlock notification: a pill that rises at the bottom of the screen.

An achievement you are not told about is a number that changes in a menu you
are not looking at. The whole point of the thing is the moment — so this is
modelled on the console notifications everyone already has a feeling about: it
arrives from the bottom edge, sits long enough to read, and leaves without
being dismissed.

Bottom-centre rather than a corner. GameLab is used on a desktop where the
corners hold whatever the compositor puts there, and on a television where the
corners are the first thing overscan eats. The bottom middle is the one place
that is reliably visible and reliably nobody else's.

It is a window over the game, not a layer inside it — GameLab hooks no
renderers, the same limit the in-game panel has. It appears above a windowed or
borderless game, and will not appear inside a fullscreen-exclusive capture.

Several unlocks at once are queued rather than stacked. Three overlapping
notifications is a mess; three in a row is a run of good news.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.ui.theme import Theme

logger = logging.getLogger(__name__)

#: How long a notification stays once it has finished arriving.
HOLD_MS = 4200
#: The rise and the fade. Slow enough to notice, brief enough not to be in the
#: way of a game somebody is in the middle of.
SLIDE_MS = 420
FADE_MS = 320

#: How far above the bottom edge it settles, and how far below it starts.
BOTTOM_MARGIN = 64
RISE_DISTANCE = 90

TOAST_WIDTH = 460
TOAST_HEIGHT = 92

SOUND_FILE = Path(__file__).resolve().parent.parent / "data" / "achievement.wav"


@dataclass(frozen=True)
class Unlock:
    """One achievement worth announcing."""

    title: str
    description: str = ""
    points: int = 0
    game: str = ""


class AchievementToast(QWidget):
    """A single notification. Shows itself, then gets out of the way."""

    finished = Signal()

    def __init__(self, unlock: Unlock, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.unlock = unlock
        self.theme = theme

        # Frameless, transparent to input: a notification that can be clicked
        # is a notification that can steal a click from the game underneath.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(TOAST_WIDTH, TOAST_HEIGHT)

        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.pill = QWidget()
        self.pill.setObjectName("Pill")
        self.pill.setStyleSheet(
            f"#Pill {{"
            f"  background-color: {self.theme.panel};"
            f"  border: 2px solid {self.theme.accent};"
            # Fully rounded ends: the shape is the point.
            f"  border-radius: {TOAST_HEIGHT // 2}px;"
            f"}}"
        )

        # Depth, so it reads as sitting above the game rather than painted onto
        # it. Cheap, and the difference between "notification" and "artefact".
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.pill.setGraphicsEffect(shadow)

        row = QHBoxLayout(self.pill)
        row.setContentsMargins(22, 10, 26, 10)
        row.setSpacing(16)

        self.trophy = QLabel("🏆")
        trophy_font = QFont()
        trophy_font.setPointSize(24)
        self.trophy.setFont(trophy_font)
        self.trophy.setStyleSheet("background: transparent;")
        self.trophy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.trophy.setFixedWidth(46)
        row.addWidget(self.trophy)

        text = QVBoxLayout()
        text.setSpacing(1)

        heading = QLabel("Achievement unlocked")
        heading_font = QFont()
        heading_font.setPointSize(9)
        heading_font.setBold(True)
        heading.setFont(heading_font)
        heading.setStyleSheet(
            f"color: {self.theme.accent}; background: transparent;"
            "letter-spacing: 1px;"
        )
        text.addWidget(heading)

        name = QLabel(self.unlock.title)
        name_font = QFont()
        name_font.setPointSize(13)
        name_font.setBold(True)
        name.setFont(name_font)
        name.setStyleSheet(f"color: {self.theme.text}; background: transparent;")
        text.addWidget(name)

        detail = self.unlock.description or self.unlock.game
        if self.unlock.points:
            detail = f"{detail}  ·  {self.unlock.points} points" if detail \
                else f"{self.unlock.points} points"
        if detail:
            subtitle = QLabel(detail)
            subtitle.setStyleSheet(
                f"color: {self.theme.text_dim}; background: transparent;"
                "font-size: 11px;"
            )
            text.addWidget(subtitle)

        row.addLayout(text, 1)
        outer.addWidget(self.pill)

    # ── Showing ───────────────────────────────────────────────────

    def resting_position(self) -> QPoint:
        """Bottom centre of the screen the pointer is on.

        `availableGeometry` rather than the full screen, so on a desktop with a
        panel or a dock along the bottom the pill sits above it instead of
        underneath it.
        """
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return QPoint(0, 0)

        area = screen.availableGeometry()
        return QPoint(
            area.center().x() - self.width() // 2,
            area.bottom() - self.height() - BOTTOM_MARGIN,
        )

    def announce(self, *, silent: bool = False) -> None:
        """Rise into view, hold, and fade out."""
        target = self.resting_position()
        self.move(target.x(), target.y() + RISE_DISTANCE)

        self._opacity = QGraphicsOpacityEffect(self)
        # The shadow effect is already on the pill, so the fade goes on the
        # window: a widget may only have one graphics effect at a time.
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0.0)

        self.show()
        self.raise_()

        self._rise = QPropertyAnimation(self, b"pos", self)
        self._rise.setDuration(SLIDE_MS)
        self._rise.setStartValue(QPoint(target.x(), target.y() + RISE_DISTANCE))
        self._rise.setEndValue(target)
        # Overshoots very slightly and settles, which is what makes it feel
        # like an object arriving rather than a rectangle appearing.
        self._rise.setEasingCurve(QEasingCurve.Type.OutBack)

        self._appear = QPropertyAnimation(self._opacity, b"opacity", self)
        self._appear.setDuration(SLIDE_MS)
        self._appear.setStartValue(0.0)
        self._appear.setEndValue(1.0)
        self._appear.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._rise.start()
        self._appear.start()

        if not silent:
            play_sound()

        QTimer.singleShot(SLIDE_MS + HOLD_MS, self._leave)

    def _leave(self) -> None:
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade.finished.connect(self._done)
        self._fade.start()

    def _done(self) -> None:
        self.hide()
        self.finished.emit()
        self.deleteLater()


class AchievementNotifier:
    """Shows unlocks one after another, rather than on top of one another."""

    def __init__(self, theme: Theme, *, silent: bool = False) -> None:
        self.theme = theme
        self.silent = silent
        self._queue: list[Unlock] = []
        self._current: Optional[AchievementToast] = None

    def announce(self, unlock: Unlock) -> None:
        self._queue.append(unlock)
        if self._current is None:
            self._next()

    def announce_all(self, unlocks) -> None:
        for unlock in unlocks:
            self.announce(unlock)

    @property
    def busy(self) -> bool:
        return self._current is not None

    @property
    def waiting(self) -> int:
        return len(self._queue)

    def _next(self) -> None:
        if not self._queue:
            self._current = None
            return

        unlock = self._queue.pop(0)
        self._current = AchievementToast(unlock, self.theme)
        self._current.finished.connect(self._next)
        self._current.announce(silent=self.silent)


def play_sound(path: Optional[Path] = None) -> bool:
    """Play the unlock chime. Returns whether anything was actually played.

    Qt Multimedia is not a hard dependency of the interface, and a machine with
    no working audio is a normal machine — so this fails quietly. Nobody should
    lose the notification because the sound could not play.
    """
    source = path or SOUND_FILE
    if not source.is_file():
        return False

    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    except ImportError:
        logger.debug("Qt Multimedia is not available; the unlock is silent")
        return False

    try:
        global _player, _audio
        _player = QMediaPlayer()
        _audio = QAudioOutput()
        _audio.setVolume(0.55)
        _player.setAudioOutput(_audio)
        _player.setSource(QUrl.fromLocalFile(str(source)))
        _player.play()
    except Exception:
        logger.debug("could not play the unlock sound", exc_info=True)
        return False

    return True


#: Held at module level because a QMediaPlayer that goes out of scope stops
#: playing immediately — the sound would be cut off a few milliseconds in.
_player = None
_audio = None
