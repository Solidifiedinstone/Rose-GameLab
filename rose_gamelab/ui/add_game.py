"""The Add a Game dialog — for anything GameLab did not find by itself.

Automatic detection will never be complete. An Anime Game Launcher, a game
built from source, a script that sets up an environment before starting
something: all real games, none of them discoverable by scanning for ROMs or
reading Steam's manifests.

Two ways in, because there are two situations:

  - it is already installed, and the system knows its name, command and icon —
    pick it from a list
  - it is not, and the user knows the command — type it

Both end up as an ordinary library entry with art, playtime and Big Picture,
which is the whole point.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core import custom_games, desktop_entries
from rose_gamelab.core.emulator import SYSTEMS
from rose_gamelab.ui import theme as ui_theme
from rose_gamelab.ui.theme import Theme, stylesheet

logger = logging.getLogger(__name__)


class AddGameDialog(QDialog):
    """Add a game by picking an installed application, or by command."""

    #: The id of the game that was added.
    game_added = Signal(int)

    def __init__(self, library, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.library = library
        self.theme = theme
        self.apps: list[desktop_entries.DesktopApp] = []
        self.chosen_cover: Optional[Path] = None

        self.setWindowTitle("Add a Game")
        self.resize(700, 660)
        self.setStyleSheet(stylesheet(theme))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING)
        layout.setSpacing(ui_theme.SPACING)

        heading = QLabel("Add a game GameLab didn't find")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._installed_tab(), "Installed apps")
        self.tabs.addTab(self._manual_tab(), "Enter it myself")
        self.tabs.currentChanged.connect(self._retarget)
        layout.addWidget(self.tabs, 1)

        layout.addWidget(self._shared_fields())

        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)

        layout.addLayout(self._buttons())

        self._load_apps()

    # ── Installed applications ────────────────────────────────────

    def _installed_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)

        note = QLabel(
            "Anything with a desktop entry — An Anime Game Launcher, Prism, "
            "an itch.io build. Its icon becomes the cover until better art "
            "turns up."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {self.theme.text}; background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; padding: 10px 12px; font-size: 13px;"
        )
        layout.addWidget(note)

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search installed applications…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_apps)
        row.addWidget(self.search, 1)

        self.show_all = QCheckBox("Show every application")
        self.show_all.setToolTip(
            "Off: only entries the system files under Games. "
            "On: everything installed, for games that never set a category."
        )
        self.show_all.stateChanged.connect(self._load_apps)
        row.addWidget(self.show_all)
        layout.addLayout(row)

        self.app_list = QListWidget()
        self.app_list.currentItemChanged.connect(self._app_selected)
        self.app_list.itemDoubleClicked.connect(lambda _: self._save())
        layout.addWidget(self.app_list, 1)

        return page

    def _load_apps(self) -> None:
        self.apps = desktop_entries.installed_apps(
            games_only=not self.show_all.isChecked()
        )
        self._filter_apps()

    def _filter_apps(self) -> None:
        needle = self.search.text().strip().lower()

        self.app_list.clear()
        for app in self.apps:
            if needle and needle not in app.name.lower():
                continue
            item = QListWidgetItem(app.name)
            item.setData(Qt.ItemDataRole.UserRole, app)
            item.setToolTip(f"{app.command}\n{app.path}")

            icon = app.icon_file()
            if icon is not None:
                pixmap = QPixmap(str(icon))
                if not pixmap.isNull():
                    item.setIcon(pixmap)

            self.app_list.addItem(item)

    def _app_selected(self, item: Optional[QListWidgetItem]) -> None:
        if item is None:
            return

        app = item.data(Qt.ItemDataRole.UserRole)
        self.title.setText(app.name)
        self.chosen_cover = app.icon_file()
        self._retarget()

    def selected_app(self) -> Optional[desktop_entries.DesktopApp]:
        item = self.app_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # ── Typed in by hand ──────────────────────────────────────────

    def _manual_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)

        note = QLabel(
            "A program, a script, or a full command line. Written as you would "
            "type it in a terminal — `env VAR=value program` works."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {self.theme.text}; background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; padding: 10px 12px; font-size: 13px;"
        )
        layout.addWidget(note)

        row = QHBoxLayout()
        self.command = QLineEdit()
        self.command.setPlaceholderText("/path/to/game, or a command")
        self.command.textChanged.connect(self._retarget)
        row.addWidget(self.command, 1)

        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_command)
        row.addWidget(browse)
        layout.addLayout(row)

        self.arguments = QLineEdit()
        self.arguments.setPlaceholderText("Arguments (optional)")
        layout.addWidget(self.arguments)

        row = QHBoxLayout()
        self.working_dir = QLineEdit()
        self.working_dir.setPlaceholderText("Run it from this folder (optional)")
        row.addWidget(self.working_dir, 1)

        pick_dir = QPushButton("Choose…")
        pick_dir.clicked.connect(self._browse_working_dir)
        row.addWidget(pick_dir)
        layout.addLayout(row)

        layout.addStretch(1)
        return page

    def _browse_command(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the program to run", str(Path.home())
        )
        if path:
            self.command.setText(path)
            if not self.title.text().strip():
                self.title.setText(Path(path).stem.replace("-", " ").replace("_", " ").title())

    def _browse_working_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Run the game from…", str(Path.home())
        )
        if path:
            self.working_dir.setText(path)

    # ── Shared fields ─────────────────────────────────────────────

    def _shared_fields(self) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; }}"
        )

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(QLabel("Name"))
        self.title = QLineEdit()
        self.title.setPlaceholderText("What to call it in your library")
        self.title.textChanged.connect(self._retarget)
        row.addWidget(self.title, 1)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("System"))
        self.system = QComboBox()
        self.system.addItem("PC", "pc")
        for system_id, system in sorted(SYSTEMS.items(), key=lambda kv: kv[1].name):
            if system_id != "pc":
                self.system.addItem(system.name, system_id)
        row.addWidget(self.system, 1)

        self.cover_button = QPushButton("Cover art…")
        self.cover_button.clicked.connect(self._browse_cover)
        row.addWidget(self.cover_button)
        layout.addLayout(row)

        return frame

    def _browse_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose cover art", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.svg)",
        )
        if path:
            self.chosen_cover = Path(path)
            self.cover_button.setText("Cover chosen")

    # ── State ─────────────────────────────────────────────────────

    def current_command(self) -> str:
        if self.tabs.currentIndex() == 0:
            app = self.selected_app()
            return app.command if app else ""
        return self.command.text().strip()

    def _retarget(self) -> None:
        """Enable saving only when there is a name and something to run."""
        command = self.current_command()
        has_name = bool(self.title.text().strip())
        self.save.setEnabled(bool(command) and has_name)

        # A warning, never a block: the command may be valid on a drive that is
        # not mounted right now, and the user knows their own machine.
        if command and not custom_games.command_is_runnable(command):
            self._say(
                "Nothing by that name is installed or on your PATH. You can "
                "still add it — it just won't launch until that changes.",
                self.theme.warning,
            )
        else:
            self.message.hide()

    def _say(self, text: str, colour: str) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(
            f"color: {colour}; background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; padding: 10px 12px; font-size: 13px;"
        )
        self.message.show()

    # ── Saving ────────────────────────────────────────────────────

    def _buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)

        self.save = QPushButton("Add to Library")
        self.save.setObjectName("Primary")
        self.save.setEnabled(False)
        self.save.clicked.connect(self._save)
        row.addWidget(self.save)

        return row

    def _save(self) -> None:
        command = self.current_command()
        if not command or not self.title.text().strip():
            return

        manual = self.tabs.currentIndex() == 1

        try:
            game_id = custom_games.add_custom_game(
                self.library,
                title=self.title.text(),
                command=command,
                system=self.system.currentData(),
                args=self.arguments.text().strip() if manual else None,
                working_dir=self.working_dir.text().strip() if manual else None,
                cover=self.chosen_cover,
            )
        except ValueError as exc:
            self._say(str(exc), self.theme.error)
            return
        except Exception as exc:
            logger.exception("could not add a custom game")
            self._say(f"Could not add it: {exc}", self.theme.error)
            return

        self.game_added.emit(game_id)
        self.accept()
