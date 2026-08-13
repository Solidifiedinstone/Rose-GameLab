"""A panel over a running game: screenshots, saves, achievements, buttons.

Modelled on what Steam's Shift+Tab gives you, with one honest difference that
shapes the whole design. Steam draws its overlay *inside* the game by hooking
its renderer. GameLab launches emulators as ordinary processes and hooks
nothing, so this is a real window that sits above the game rather than a layer
drawn within it.

What that costs: it will not appear inside a fullscreen-exclusive capture or a
recording of the game's own surface, and a game that refuses to yield focus can
stay on top of it. What it buys: it works with every emulator, needs no
injection into anything, and cannot crash the game it is sitting over.

Everything it shows is already known — saves from `core/saves.py`, achievements
from the database, controller state from `core/controller_status.py`. The panel
composes, it does not compute. Actions that touch files (backing up a save,
restoring one) go through `SaveManager`, which does them the careful way.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core.saves import SaveManager
from rose_gamelab.ui import theme as ui_theme
from rose_gamelab.ui.theme import Theme

logger = logging.getLogger(__name__)

# Screenshot tools, best first. Every one of these writes a PNG to the path it
# is given; the first one present is used.
SCREENSHOT_TOOLS = (
    ("hyprshot", ("-m", "output", "-z", "-s", "-o", "{directory}", "-f", "{name}")),
    ("grim", ("{path}",)),
    ("spectacle", ("-b", "-n", "-o", "{path}")),
    ("maim", ("{path}",)),
    ("scrot", ("{path}",)),
)


def screenshot_directory() -> Path:
    """Where GameLab puts screenshots it takes itself."""
    return Path.home() / "Pictures" / "Rose GameLab"


def available_screenshot_tool() -> Optional[str]:
    for name, _ in SCREENSHOT_TOOLS:
        if shutil.which(name):
            return name
    return None


def take_screenshot(title: str, *, directory: Optional[Path] = None) -> Optional[Path]:
    """Capture the screen, returning where it landed.

    Returns None when no screenshot tool is installed, rather than raising:
    a missing tool costs a screenshot, not the panel.
    """
    folder = directory or screenshot_directory()
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    safe = "".join(c for c in title if c.isalnum() or c in " -_").strip() or "Game"
    target = folder / f"{safe} {stamp}.png"

    for name, template in SCREENSHOT_TOOLS:
        binary = shutil.which(name)
        if not binary:
            continue

        try:
            folder.mkdir(parents=True, exist_ok=True)
            arguments = [
                part.format(path=str(target), directory=str(folder), name=target.name)
                for part in template
            ]
            subprocess.run([binary, *arguments], timeout=20, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("screenshot with %s failed: %s", name, exc)
            continue

        if target.exists():
            return target

    logger.info("no screenshot tool produced a file")
    return None


def _field(row, name: str, default=None):
    """Read a column that may not be in this query's result.

    `sqlite3.Row` has no `.get`, and `name in row` tests the row's *values*
    rather than its column names — so this asks `keys()` explicitly.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


class Section(QFrame):
    """A titled block in the panel."""

    def __init__(self, title: str, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.setStyleSheet(
            f"background-color: {theme.panel};"
            f"border-radius: {ui_theme.RADIUS_LARGE}px;"
        )

        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(
            ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING
        )
        self.body.setSpacing(8)

        heading = QLabel(title)
        font = QFont()
        font.setPointSize(12)
        font.setBold(True)
        heading.setFont(font)
        heading.setStyleSheet(f"color: {theme.text}; background: transparent;")
        self.body.addWidget(heading)


class GameOverlay(QWidget):
    """The panel shown over a running game."""

    closed = Signal()

    def __init__(
        self,
        library,
        theme: Theme,
        *,
        save_manager: Optional[SaveManager] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Dialog)

        self.library = library
        self.theme = theme
        self.saves = save_manager or SaveManager(library)
        self.game = None
        self._elapsed_seconds = 0
        #: True between hiding for a screenshot and restoring afterwards.
        self._capture_pending = False

        self.setWindowTitle("Rose GameLab")
        # Above the game, and not in the way of it: a panel, not a full screen.
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(f"background-color: {theme.background};")
        self.resize(560, 720)

        self._build()

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_elapsed)

    # ── Construction ──────────────────────────────────────────────

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING
        )
        layout.setSpacing(ui_theme.SPACING)

        self.title = QLabel()
        font = QFont()
        font.setPointSize(20)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setStyleSheet(f"color: {self.theme.accent};")
        layout.addWidget(self.title)

        self.elapsed = QLabel()
        self.elapsed.setStyleSheet(f"color: {self.theme.text_dim}; font-size: 13px;")
        layout.addWidget(self.elapsed)

        layout.addWidget(self._build_actions())
        layout.addWidget(self._build_achievements())
        layout.addWidget(self._build_saves(), 1)
        layout.addWidget(self._build_controls())

        hint = QLabel("Esc  close this panel and go back to the game")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color: {self.theme.text_dim}; font-size: 12px;")
        layout.addWidget(hint)

    def _build_actions(self) -> QWidget:
        section = Section("Capture", self.theme)

        row = QHBoxLayout()
        self.screenshot_button = QPushButton("📷  Take a screenshot")
        self.screenshot_button.clicked.connect(self.take_screenshot)
        row.addWidget(self.screenshot_button)

        self.backup_button = QPushButton("💾  Back up saves")
        self.backup_button.clicked.connect(self.backup_saves)
        row.addWidget(self.backup_button)

        section.body.addLayout(row)

        self.capture_status = QLabel()
        self.capture_status.setStyleSheet(
            f"color: {self.theme.text_dim}; font-size: 12px; background: transparent;"
        )
        self.capture_status.setWordWrap(True)
        section.body.addWidget(self.capture_status)

        if available_screenshot_tool() is None:
            # Say so before the button is pressed, not after it fails.
            self.screenshot_button.setEnabled(False)
            self.capture_status.setText(
                "No screenshot tool found. Install grim, hyprshot, spectacle, "
                "maim or scrot to capture from here."
            )

        return section

    def _build_achievements(self) -> QWidget:
        section = Section("Achievements", self.theme)

        self.achievements = QLabel()
        self.achievements.setStyleSheet(
            f"color: {self.theme.text}; background: transparent;"
        )
        self.achievements.setWordWrap(True)
        section.body.addWidget(self.achievements)

        return section

    def _build_saves(self) -> QWidget:
        section = Section("Saves and states", self.theme)

        self.save_list = QListWidget()
        self.save_list.setStyleSheet(
            f"background-color: {self.theme.surface}; color: {self.theme.text};"
            f"border-radius: {ui_theme.RADIUS_LARGE}px; padding: 6px;"
        )
        section.body.addWidget(self.save_list)

        return section

    def _build_controls(self) -> QWidget:
        section = Section("Controllers", self.theme)

        self.controllers = QLabel()
        self.controllers.setStyleSheet(
            f"color: {self.theme.text}; background: transparent;"
        )
        self.controllers.setWordWrap(True)
        section.body.addWidget(self.controllers)

        return section

    # ── Contents ──────────────────────────────────────────────────

    def show_for(self, game, *, elapsed_seconds: int = 0, controllers=None) -> None:
        """Fill the panel in for a running game and show it."""
        self.game = game
        self._elapsed_seconds = elapsed_seconds

        self.title.setText(game.title if game else "Rose GameLab")
        self._update_elapsed()
        self._load_achievements()
        self._load_saves()
        self.set_controllers(controllers or [])

        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._tick.start()

    def set_controllers(self, statuses) -> None:
        if not statuses:
            self.controllers.setText("No controller connected.")
            return

        lines = []
        for index, status in enumerate(statuses, start=1):
            lines.append(f"Player {index}:  {status.label}")
        self.controllers.setText("\n".join(lines))

    def _update_elapsed(self) -> None:
        if self._tick.isActive():
            self._elapsed_seconds += 1

        minutes, seconds = divmod(self._elapsed_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            self.elapsed.setText(f"Playing for {hours} h {minutes:02d} m")
        else:
            self.elapsed.setText(f"Playing for {minutes} m {seconds:02d} s")

    def _load_achievements(self) -> None:
        if self.game is None:
            self.achievements.setText("")
            return

        try:
            from rose_gamelab.metadata.retroachievements import progress_for

            earned, total, points, _possible = progress_for(self.library.db, self.game.id)
        except Exception:
            logger.exception("could not read achievements")
            self.achievements.setText("Achievements are unavailable.")
            return

        if not total:
            self.achievements.setText(
                "No achievement set for this game, or none downloaded yet."
            )
            return

        self.achievements.setText(
            f"{earned} of {total} unlocked  ·  {points} points"
        )

    def _load_saves(self) -> None:
        self.save_list.clear()
        if self.game is None:
            return

        try:
            saves = self.saves.saves_for(self.game.id)
        except Exception:
            logger.exception("could not read saves")
            self.save_list.addItem(QListWidgetItem("Saves are unavailable."))
            return

        if not saves:
            self.save_list.addItem(QListWidgetItem(
                "No saves found yet — they appear once the game writes one."
            ))
            return

        for save in saves:
            slot = _field(save, "slot")
            kind = _field(save, "kind", "save")
            name = Path(_field(save, "path", "")).name
            label = f"{'State' if kind == 'state' else 'Save'}"
            if slot is not None:
                label += f" {slot}"
            self.save_list.addItem(QListWidgetItem(f"{label}  —  {name}"))

    # ── Actions ───────────────────────────────────────────────────

    def take_screenshot(self) -> None:
        title = self.game.title if self.game else "Rose GameLab"

        # Hide first: the panel is a window above the game, so it would appear
        # in its own screenshot.
        self._capture_pending = True
        self.hide()
        QTimer.singleShot(250, lambda: self._capture(title))

    def _capture(self, title: str) -> None:
        path = take_screenshot(title)

        if not self._capture_pending:
            # Closed while the panel was hidden for the shot. Pressing Escape
            # to get back to the game and having this jump back over it a
            # quarter of a second later — stealing focus — is not what anyone
            # asked for.
            return

        self._capture_pending = False
        self.show()
        self.raise_()
        self.activateWindow()

        if path is None:
            self.capture_status.setText("The screenshot could not be taken.")
        else:
            self.capture_status.setText(f"Saved to {path}")

    def backup_saves(self) -> None:
        if self.game is None:
            return

        try:
            result = self.saves.backup(game_id=self.game.id, label="overlay")
        except Exception as exc:
            logger.exception("save backup failed")
            QMessageBox.warning(self, "Backup failed", str(exc))
            return

        count = getattr(result, "count", None)
        if count is None:
            count = len(getattr(result, "files", []) or [])
        self.capture_status.setText(
            f"Backed up {count} save file{'s' if count != 1 else ''}."
        )
        self._load_saves()

    # ── Behaviour ─────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        # Cancels any screenshot restore still in flight.
        self._capture_pending = False
        self._tick.stop()
        self.closed.emit()
        super().closeEvent(event)
