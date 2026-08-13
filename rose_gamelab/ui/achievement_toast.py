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
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.ui.theme import Theme

logger = logging.getLogger(__name__)

#: How long a notification stays once it has finished arriving.
HOLD_MS = 2400
#: The rise and the fade. Slow enough to notice, brief enough not to be in the
#: way of a game somebody is in the middle of.
FADE_IN_MS = 420
FADE_MS = 620

#: The rise starts first and the sound follows it in. The other way round —
#: sound first, then movement — reads as the picture lagging behind.
SOUND_DELAY_MS = 260

#: How far above the bottom edge the pill sits.
BOTTOM_MARGIN = 72

#: The unroll: it starts this wide and opens out to its full width.
UNROLL_FROM = 84
UNROLL_MS = 620

#: What a compositor rule matches on to place this window.
TOAST_WINDOW_TITLE = "Rose GameLab Achievement"

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

        # A real window, deliberately. ToolTip was tried and does not map at
        # all under Wayland: an unparented tooltip becomes an xdg-popup, which
        # needs a parent surface, so nothing ever appeared on screen.
        #
        # Which leaves the placement problem, and the answer is below: the
        # window covers the whole screen, so where the compositor decides to
        # put it cannot be wrong. Transparent to input throughout, so a
        # notification can never swallow a click meant for the game underneath.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        # A stable, distinctive title. Under Wayland a client cannot place its
        # own window — the compositor decides — so the only way to pin this to
        # the bottom of the screen is a compositor rule, and a rule needs
        # something to match on. See the note in the README.
        self.setWindowTitle(TOAST_WINDOW_TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # The window covers the whole screen and is invisible; the pill is
        # placed at the bottom of it. This is not decoration — under Wayland a
        # client cannot position its own window at all, so asking to sit at the
        # bottom of the screen was a request the compositor was free to ignore,
        # and Hyprland duly centred it. A window that already covers everything
        # has nothing left to be placed wrongly, and where the pill sits inside
        # it is entirely ours to decide.
        self.setFixedSize(TOAST_WIDTH, TOAST_HEIGHT)

        self._build()

    @staticmethod
    def _screen_geometry():
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        return screen.geometry() if screen else QApplication.primaryScreen().geometry()

    def _build(self) -> None:
        # No layout: the pill is positioned by hand, because its width is
        # animated and a layout would fight that every frame.
        self.pill = QWidget(self)
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

        self.pill.resize(TOAST_WIDTH, TOAST_HEIGHT)
        self.pill.move(self._pill_target().topLeft())

    # ── Showing ───────────────────────────────────────────────────

    def _pill_target(self) -> QRect:
        """Where the pill sits inside this window, fully unrolled.

        In window coordinates, so the compositor's opinion about the window
        never enters into it. `availableGeometry` decides the bottom edge, so a
        dock or panel along the bottom is sat above rather than under.
        """
        return QRect(0, 0, TOAST_WIDTH, TOAST_HEIGHT)

    def place(self) -> bool:
        """Put the window at the bottom centre. True if that could be done.

        On X11 an application may position its own windows, so this works and
        needs nothing from the user. On Wayland it cannot — the compositor
        decides, and a client asking is simply ignored — so this returns False
        and the window lands wherever the compositor puts it, usually the
        middle of the screen. `packaging/hyprland-achievement-rule.lua` fixes
        that for Hyprland; other compositors want the equivalent rule.
        """
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return False

        area = screen.availableGeometry()
        self.move(
            area.center().x() - self.width() // 2,
            area.bottom() - self.height() - BOTTOM_MARGIN,
        )
        return QGuiApplication.platformName() not in ("wayland", "wayland-egl")

    def announce(self, *, silent: bool = False) -> None:
        """Unroll into view, hold, and fade away."""
        self.place()
        target = self._pill_target()

        # Starts as a short stub in the middle of where it will end up, and
        # opens outwards from there — a scroll unrolling rather than a
        # rectangle sliding in.
        stub = QRect(
            target.center().x() - UNROLL_FROM // 2,
            target.y(),
            UNROLL_FROM,
            target.height(),
        )
        self.pill.setGeometry(stub)

        # Window opacity, NOT a QGraphicsOpacityEffect. The pill already has a
        # drop shadow, and an effect rendered inside another effect is
        # something Qt cannot do: it produced "a paint device can only be
        # painted by one painter at a time" and left the fade broken.
        self.setWindowOpacity(0.0)

        self.show()
        self.raise_()

        self._unroll = QPropertyAnimation(self.pill, b"geometry", self)
        self._unroll.setDuration(UNROLL_MS)
        self._unroll.setStartValue(stub)
        self._unroll.setEndValue(target)
        # Fast at first and easing to a stop, which is how something with
        # weight unrolls.
        self._unroll.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._appear = QPropertyAnimation(self, b"windowOpacity", self)
        self._appear.setDuration(FADE_IN_MS)
        self._appear.setStartValue(0.0)
        self._appear.setEndValue(1.0)
        self._appear.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._unroll.start()
        self._appear.start()

        if not silent:
            QTimer.singleShot(SOUND_DELAY_MS, play_sound)

        QTimer.singleShot(UNROLL_MS + HOLD_MS, self._leave)

    def _leave(self) -> None:
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade.finished.connect(self._done)
        self._fade.start()

    def _done(self) -> None:
        # close() as well as hide(): hiding leaves the surface around for the
        # compositor to keep drawing decorations for, which is what left a
        # ghost behind after the notification had gone.
        self.hide()
        self.close()
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
        # Deliberately low: it plays over a game already making its own noise.
        _audio.setVolume(0.38)
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
