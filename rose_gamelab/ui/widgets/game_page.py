"""The full-page view of one game.

The narrow panel down the right-hand side could show a cover, a few facts and
a Play button, and that was the ceiling: there is no room in a 320px column for
a hundred achievements, or for somewhere to write down which save slot is the
good one. So clicking a game opens this instead, and the game gets the window.

Three things live here that have nowhere else to be:

  - **achievements**, read from the database rather than the network, so the
    page opens instantly and works with the network off. Refreshing is
    something the user asks for.
  - **notes**, which are the user's own and are never touched by a scraper.
    Saved as they type, because a notes box that loses what you wrote when you
    click away is worse than no notes box.
  - **everything you can do to the game**, in one place rather than split
    between a panel, a right-click menu and the Settings window.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core.emulator import get_system
from rose_gamelab.ui import theme as ui_theme
from rose_gamelab.ui.theme import Theme

logger = logging.getLogger(__name__)

PAGE_COVER_WIDTH = 260

#: How long after the last keystroke a note is written. Long enough not to
#: write on every character, short enough that closing the window keeps it.
NOTE_SAVE_DELAY = 400


class PlaytimeChart(QWidget):
    """Playtime per day, drawn as bars.

    `play_seconds` is a single number, which answers "how long" and nothing
    about "when". The sessions behind it were already being recorded; this is
    the first thing to read them back.
    """

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.history: list[tuple[str, int]] = []
        self.setFixedHeight(90)

    def set_history(self, history: list[tuple[str, int]]) -> None:
        self.history = list(history)
        self.setVisible(bool(self.history))
        self.update()

    def paintEvent(self, event) -> None:
        if not self.history:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        peak = max(seconds for _day, seconds in self.history) or 1
        count = len(self.history)
        width = self.width() / count
        # Bars stay legible when a game has been played on two days and when it
        # has been played on ninety.
        bar = max(2.0, min(width - 2, 18.0))

        for index, (_day, seconds) in enumerate(self.history):
            height = (seconds / peak) * (self.height() - 6)
            left = index * width + (width - bar) / 2
            rect = QRectF(left, self.height() - height, bar, height)
            painter.fillRect(rect, QColor(self.theme.accent))


class ScreenshotStrip(QWidget):
    """Screenshots the emulator already took, newest first."""

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)

    def set_shots(self, shots: list) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()

        for shot in shots[:8]:
            pixmap = QPixmap(str(shot.path))
            if pixmap.isNull():
                continue

            label = QLabel()
            label.setPixmap(pixmap.scaledToHeight(
                110, Qt.TransformationMode.SmoothTransformation
            ))
            label.setToolTip(f"{shot.name}")
            label.setStyleSheet(f"border-radius: {ui_theme.RADIUS_SMALL}px;")
            self._layout.addWidget(label)

        self._layout.addStretch(1)


class AchievementRow(QFrame):
    """One achievement: badge, name, description, points, and whether it's earned."""

    def __init__(self, achievement, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.setStyleSheet(
            f"QFrame {{ background-color: {theme.panel};"
            f" border-radius: {ui_theme.RADIUS_SMALL}px; }}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 12, 8)
        layout.setSpacing(12)

        badge = QLabel()
        badge.setFixedSize(48, 48)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Unearned achievements are dimmed rather than hidden: seeing what is
        # left is the entire point of a list like this.
        badge.setStyleSheet(
            f"background-color: {theme.elevated};"
            f" border-radius: {ui_theme.RADIUS_SMALL}px;"
            f" color: {theme.text_dim};"
        )
        badge.setText("★" if achievement.earned else "☆")
        layout.addWidget(badge)

        text = QVBoxLayout()
        text.setSpacing(2)

        title = QLabel(achievement.title)
        title.setWordWrap(True)
        title.setStyleSheet(
            f"font-weight: 600; color: "
            f"{theme.text if achievement.earned else theme.text_dim};"
        )
        text.addWidget(title)

        if achievement.description:
            description = QLabel(achievement.description)
            description.setWordWrap(True)
            description.setStyleSheet(f"color: {theme.text_dim}; font-size: 12px;")
            text.addWidget(description)

        layout.addLayout(text, 1)

        right = QVBoxLayout()
        right.setSpacing(2)

        points = QLabel(f"{achievement.points}")
        points.setAlignment(Qt.AlignmentFlag.AlignRight)
        points.setStyleSheet(
            f"font-weight: 600; color: "
            f"{theme.accent if achievement.earned else theme.text_dim};"
        )
        right.addWidget(points)

        if achievement.earned:
            # Hardcore is a stricter award, not a cosmetic one, so it is said.
            mark = QLabel("hardcore" if achievement.hardcore else "earned")
            mark.setAlignment(Qt.AlignmentFlag.AlignRight)
            mark.setStyleSheet(f"color: {theme.success}; font-size: 11px;")
            right.addWidget(mark)

        layout.addLayout(right)


class GamePage(QWidget):
    """One game, given the whole window."""

    back_requested = Signal()
    launch_requested = Signal(int, object)      # game id, launch option id
    favorite_toggled = Signal(int, bool)
    scrape_requested = Signal(int)
    art_requested = Signal(int)
    remove_requested = Signal(int)
    achievements_requested = Signal(int)
    #: game id, note text — emitted as the user types, already debounced.
    notes_changed = Signal(int, str)

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.theme = theme
        self.game = None
        self._options: list = []

        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(NOTE_SAVE_DELAY)
        self._note_timer.timeout.connect(self._save_notes)

        self._build()

    # ── Construction ──────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_top_bar())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(
            ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING
        )
        self.body.setSpacing(ui_theme.SPACING)

        self.body.addWidget(self._build_header())
        self.body.addWidget(self._build_playtime())
        self.body.addWidget(self._build_screenshots())
        self.body.addWidget(self._build_notes())
        self.body.addWidget(self._build_achievements(), 1)
        self.body.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(56)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(ui_theme.SPACING, 8, ui_theme.SPACING, 8)
        layout.setSpacing(10)

        back = QPushButton("←  Library")
        back.setToolTip("Back to the grid  (Esc)")
        back.clicked.connect(self.back_requested.emit)
        layout.addWidget(back)

        layout.addStretch(1)
        return bar

    def _build_header(self) -> QWidget:
        frame = QWidget()
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(ui_theme.SPACING)

        self.cover = QLabel()
        self.cover.setFixedSize(PAGE_COVER_WIDTH, int(PAGE_COVER_WIDTH * 3 / 2))
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(10)

        self.title = QLabel()
        self.title.setObjectName("Heading")
        self.title.setWordWrap(True)
        right.addWidget(self.title)

        self.subtitle = QLabel()
        self.subtitle.setObjectName("Subtle")
        self.subtitle.setWordWrap(True)
        right.addWidget(self.subtitle)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size: 13px;")
        right.addWidget(self.summary)

        right.addLayout(self._build_actions())
        right.addStretch(1)

        layout.addLayout(right, 1)
        return frame

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.play = QPushButton("Play")
        self.play.setObjectName("Primary")
        self.play.clicked.connect(self._on_play)
        row.addWidget(self.play)

        # Only shown when there is more than one way to play, so the common
        # case is a single uncluttered button.
        self.option_picker = QComboBox()
        self.option_picker.hide()
        row.addWidget(self.option_picker)

        self.favorite = QPushButton()
        self.favorite.clicked.connect(self._on_favorite)
        row.addWidget(self.favorite)

        art = QPushButton("Art…")
        art.setToolTip("Choose a cover from a file")
        art.clicked.connect(lambda: self.game and self.art_requested.emit(self.game.id))
        row.addWidget(art)

        scrape = QPushButton("Find info")
        scrape.clicked.connect(
            lambda: self.game and self.scrape_requested.emit(self.game.id)
        )
        row.addWidget(scrape)

        remove = QPushButton("Remove…")
        remove.clicked.connect(
            lambda: self.game and self.remove_requested.emit(self.game.id)
        )
        row.addWidget(remove)

        row.addStretch(1)
        return row

    def _build_playtime(self) -> QWidget:
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.playtime_heading = QLabel("Playtime")
        self.playtime_heading.setStyleSheet("font-weight: 600; font-size: 15px;")
        header.addWidget(self.playtime_heading)

        self.playtime_summary = QLabel()
        self.playtime_summary.setObjectName("Subtle")
        header.addWidget(self.playtime_summary)
        header.addStretch(1)
        layout.addLayout(header)

        self.playtime_chart = PlaytimeChart(self.theme)
        layout.addWidget(self.playtime_chart)

        self.playtime_frame = frame
        return frame

    def _build_screenshots(self) -> QWidget:
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        heading = QLabel("Screenshots")
        heading.setStyleSheet("font-weight: 600; font-size: 15px;")
        header.addWidget(heading)

        self.screenshot_count = QLabel()
        self.screenshot_count.setObjectName("Subtle")
        header.addWidget(self.screenshot_count)
        header.addStretch(1)
        layout.addLayout(header)

        strip = QScrollArea()
        strip.setWidgetResizable(True)
        strip.setFrameShape(QFrame.Shape.NoFrame)
        strip.setFixedHeight(130)
        strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.screenshots = ScreenshotStrip(self.theme)
        strip.setWidget(self.screenshots)
        layout.addWidget(strip)

        self.screenshot_frame = frame
        return frame

    def _build_notes(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("NotesPanel")

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        heading = QLabel("Notes")
        heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(heading)

        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText(
            "Anything worth remembering — which save is the good one, the "
            "controller it needs, where you got to…"
        )
        self.notes.setFixedHeight(110)
        # Saved as they type. A notes box that loses what you wrote when you
        # click away is worse than not having one.
        self.notes.textChanged.connect(self._note_timer.start)
        layout.addWidget(self.notes)

        return frame

    def _build_achievements(self) -> QWidget:
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()

        self.achievements_heading = QLabel("Achievements")
        self.achievements_heading.setStyleSheet("font-weight: 600; font-size: 15px;")
        header.addWidget(self.achievements_heading)

        self.achievements_progress = QLabel()
        self.achievements_progress.setObjectName("Subtle")
        header.addWidget(self.achievements_progress)

        header.addStretch(1)

        self.refresh_achievements = QPushButton("Refresh")
        self.refresh_achievements.setToolTip(
            "Fetch this game's achievements and your progress from RetroAchievements"
        )
        self.refresh_achievements.clicked.connect(
            lambda: self.game and self.achievements_requested.emit(self.game.id)
        )
        header.addWidget(self.refresh_achievements)

        layout.addLayout(header)

        self.achievements_note = QLabel()
        self.achievements_note.setWordWrap(True)
        self.achievements_note.setObjectName("Subtle")
        layout.addWidget(self.achievements_note)

        self.achievement_rows = QVBoxLayout()
        self.achievement_rows.setSpacing(6)
        layout.addLayout(self.achievement_rows)

        return frame

    # ── Showing a game ────────────────────────────────────────────

    def show_game(
        self,
        game,
        launch_options: list,
        tags: list[str],
        achievements: list,
        *,
        achievements_available: bool = True,
        achievements_supported: bool = True,
        play_history: Optional[list] = None,
        screenshots: Optional[list] = None,
    ) -> None:
        """Fill the page in. Called every time the page is opened or refreshed."""
        # Any pending note belongs to the game we are leaving, not this one.
        self._flush_notes()

        self.game = game
        self._options = list(launch_options)

        self.title.setText(game.title)
        self.subtitle.setText(self._facts(game, tags))
        self.summary.setText(game.summary or "")
        self.summary.setVisible(bool(game.summary))

        self._set_cover(game)
        self._set_actions(game)

        self.notes.blockSignals(True)
        self.notes.setPlainText(game.notes or "")
        self.notes.blockSignals(False)

        self._set_playtime(game, play_history or [])
        self._set_screenshots(screenshots or [])
        self._set_achievements(achievements, available=achievements_available,
                               supported=achievements_supported)

    def _facts(self, game, tags: list[str]) -> str:
        system = get_system(game.system)
        parts = [system.name if system else game.system]

        if game.release_date:
            parts.append(game.release_date[:4])
        if game.developer:
            parts.append(game.developer)
        if game.play_seconds:
            parts.append(f"{game.play_seconds // 3600}h {game.play_seconds % 3600 // 60}m played")
        if tags:
            parts.append(", ".join(tags[:4]))

        return "  ·  ".join(p for p in parts if p)

    def _set_cover(self, game) -> None:
        if game.cover_path and Path(game.cover_path).is_file():
            pixmap = QPixmap(game.cover_path)
            if not pixmap.isNull():
                self.cover.setPixmap(pixmap.scaledToWidth(
                    PAGE_COVER_WIDTH, Qt.TransformationMode.SmoothTransformation
                ))
                self.cover.setStyleSheet("")
                return

        self.cover.setPixmap(QPixmap())
        self.cover.setText("No art")
        self.cover.setStyleSheet(
            f"background-color: {self.theme.placeholder};"
            f" border-radius: {ui_theme.RADIUS}px; color: {self.theme.text_dim};"
        )

    def _set_actions(self, game) -> None:
        self.play.setEnabled(bool(self._options))
        self.favorite.setText(
            "★ Favourited" if game.favorite else "☆ Favourite"
        )

        self.option_picker.clear()
        if len(self._options) > 1:
            for option in self._options:
                self.option_picker.addItem(
                    option["label"] or option["kind"].title(), option["id"]
                )
            self.option_picker.show()
        else:
            self.option_picker.hide()

    def _set_achievements(self, achievements: list, *, available: bool,
                          supported: bool = True) -> None:
        while self.achievement_rows.count():
            item = self.achievement_rows.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()

        # Credentials are needed to FETCH achievements, not to look at ones
        # already stored. Hiding earned achievements because a key is missing
        # would lose the user progress they have already made.
        # Three different states, and they need three different answers. A
        # console RetroAchievements does not cover will never have achievements
        # however many keys are entered, and offering Refresh for it is a lie.
        self.refresh_achievements.setEnabled(available and supported)
        if not supported:
            self.refresh_achievements.setToolTip(
                "RetroAchievements has no sets for this system"
            )
        elif available:
            self.refresh_achievements.setToolTip(
                "Fetch this game's achievements and your progress from RetroAchievements"
            )
        else:
            self.refresh_achievements.setToolTip(
                "Add your RetroAchievements username and API key in "
                "Settings → RetroAchievements first"
            )

        if not achievements:
            self.achievements_progress.setText("")
            if not supported:
                note = ("RetroAchievements does not cover this system, so there "
                        "are no achievements to fetch.")
            elif available:
                note = ("None stored yet. Refresh to look this game up on "
                        "RetroAchievements — not every game has any.")
            else:
                note = ("Add your RetroAchievements username and API key in "
                        "Settings → RetroAchievements to track achievements.")
            self.achievements_note.setText(note)
            self.achievements_note.show()
            return

        earned = [a for a in achievements if a.earned]
        points = sum(a.points for a in earned)
        total_points = sum(a.points for a in achievements)

        self.achievements_progress.setText(
            f"{len(earned)} of {len(achievements)}  ·  {points} of {total_points} points"
        )
        self.achievements_note.hide()

        for achievement in achievements:
            self.achievement_rows.addWidget(AchievementRow(achievement, self.theme))

    def _set_playtime(self, game, history: list) -> None:
        # A game that has never been launched has nothing to say here, and an
        # empty chart is worse than no chart.
        self.playtime_frame.setVisible(bool(game.play_seconds or history))

        hours, minutes = divmod(game.play_seconds // 60, 60)
        spent = f"{hours}h {minutes}m" if hours else f"{minutes}m"
        sessions = f"{game.play_count} session{'s' if game.play_count != 1 else ''}"
        self.playtime_summary.setText(f"{spent}  ·  {sessions}")

        self.playtime_chart.set_history(history)

    def _set_screenshots(self, shots: list) -> None:
        self.screenshot_frame.setVisible(bool(shots))
        self.screenshot_count.setText(
            f"{len(shots)}" if len(shots) > 8 else ""
        )
        self.screenshots.set_shots(shots)

    # ── Notes ─────────────────────────────────────────────────────

    def _save_notes(self) -> None:
        if self.game is not None:
            self.notes_changed.emit(self.game.id, self.notes.toPlainText())

    def _flush_notes(self) -> None:
        """Write a pending note immediately — before leaving or switching game."""
        if self._note_timer.isActive():
            self._note_timer.stop()
            self._save_notes()

    # ── Events ────────────────────────────────────────────────────

    def _on_play(self) -> None:
        if self.game is None:
            return
        option_id = (
            self.option_picker.currentData()
            if self.option_picker.isVisible() else None
        )
        self.launch_requested.emit(self.game.id, option_id)

    def _on_favorite(self) -> None:
        if self.game is not None:
            self.favorite_toggled.emit(self.game.id, not self.game.favorite)

    def restyle(self, theme: Theme, style=None) -> None:
        self.theme = theme
        self.playtime_chart.theme = theme
        self.playtime_chart.update()
        if self.game is not None:
            self._set_cover(self.game)

    def keyPressEvent(self, event) -> None:
        # Escape goes back, unless the user is typing a note.
        if event.key() == Qt.Key.Key_Escape and not self.notes.hasFocus():
            self._flush_notes()
            self.back_requested.emit()
            return
        super().keyPressEvent(event)
