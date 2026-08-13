"""The Add Source dialog.

The thing users get wrong here is what a "source" even is, and specifically
whether they are supposed to add emulators. They are not: GameLab finds
emulators on its own. What it needs is a folder of games.

So the dialog says that up front, then previews what it found BEFORE anything
is added — including which emulators are missing and the exact command to
install them. Adding a folder and discovering afterwards that nothing can be
launched is the failure this is built to prevent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core import emulator_detect
from rose_gamelab.core.discs import group_discs
from rose_gamelab.core.emulator import SYSTEMS, get_system
from rose_gamelab.core.folder_games import FolderGame
from rose_gamelab.core.media import MediaKind, classify, describe
from rose_gamelab.core.scanner import infer_system, walk_library
from rose_gamelab.ui import theme as ui_theme
from rose_gamelab.ui.theme import Theme

logger = logging.getLogger(__name__)

# Scanning every file of a huge collection just to draw a preview is wasteful;
# this is plenty to tell the user what they are about to add.
PREVIEW_FILE_LIMIT = 4000


class SourceTypeCard(QFrame):
    """One big, obvious choice on the first page."""

    clicked = Signal(str)

    def __init__(
        self,
        key: str,
        icon: str,
        title: str,
        description: str,
        status: str,
        theme: Theme,
        *,
        enabled: bool = True,
    ) -> None:
        super().__init__()

        self.key = key
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self._enabled = enabled

        self.setStyleSheet(
            f"QFrame {{ background-color: {theme.panel}; border-radius: {ui_theme.RADIUS}px;"
            f" border: 1px solid {theme.border}; }}"
            f"QFrame:hover {{ border-color: {theme.accent if enabled else theme.border}; }}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(14)

        glyph = QLabel(icon)
        glyph.setStyleSheet(f"font-size: 26px; color: {theme.text};")
        glyph.setFixedWidth(38)
        layout.addWidget(glyph)

        text = QVBoxLayout()
        text.setSpacing(3)

        heading = QLabel(title)
        heading.setStyleSheet(
            f"font-size: 15px; font-weight: 600;"
            f" color: {theme.text if enabled else theme.text_dim};"
        )
        text.addWidget(heading)

        body = QLabel(description)
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {theme.text_dim}; font-size: 13px;")
        text.addWidget(body)

        layout.addLayout(text, 1)

        if status:
            badge = QLabel(status)
            badge.setStyleSheet(
                f"color: {theme.success if enabled else theme.text_dim};"
                f" font-size: 12px; font-weight: 600;"
            )
            layout.addWidget(badge)

    def mousePressEvent(self, event) -> None:
        if self._enabled and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key)
        super().mousePressEvent(event)


class AddSourceDialog(QDialog):
    """Guided flow for adding games to the library."""

    #: (kind, path, system) — the caller performs the actual scan.
    source_chosen = Signal(str, str, object)

    def __init__(self, library, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.library = library
        self.theme = theme
        self.folder: Optional[Path] = None
        self.detected: dict[str, int] = {}
        #: Per system, how many of each media kind — folders vs disc images.
        self.media: dict[str, dict[MediaKind, int]] = {}
        self.chosen_system: Optional[str] = None
        #: Read by the caller after exec(): the user asked for the organiser.
        self.wants_organiser = False
        #: …or to add a game by hand.
        self.wants_manual_entry = False

        self.setWindowTitle("Add Games")
        self.resize(680, 620)

        from rose_gamelab.ui.theme import stylesheet
        self.setStyleSheet(stylesheet(theme))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING)
        layout.setSpacing(ui_theme.SPACING)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._choose_page())
        self.pages.addWidget(self._preview_page())
        layout.addWidget(self.pages, 1)

        self.buttons = QHBoxLayout()
        self.back = QPushButton("Back")
        self.back.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        self.back.hide()
        self.buttons.addWidget(self.back)

        self.buttons.addStretch(1)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.buttons.addWidget(cancel)

        self.confirm = QPushButton("Add to Library")
        self.confirm.setObjectName("Primary")
        self.confirm.clicked.connect(self._accept_folder)
        self.confirm.hide()
        self.buttons.addWidget(self.confirm)

        layout.addLayout(self.buttons)

    # ── Page 1: what kind of source ───────────────────────────────

    def _choose_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        heading = QLabel("Add games to your library")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        # The single most useful sentence in this dialog.
        note = QLabel(
            "You don't add emulators here — GameLab finds those by itself. "
            "Point it at a folder of games and it works out the rest."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {self.theme.text}; background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; padding: 12px 14px; font-size: 13px;"
        )
        layout.addWidget(note)

        from rose_gamelab.sources.steam import SteamProvider

        steam_found = SteamProvider().validate()
        playable = sum(1 for _, _, e in emulator_detect.summary() if e)

        cards = [
            ("rom_folder", "📁", "Folder of games",
             "ROMs, disc images, anything on disk. Sub-folders are fine — "
             "GameLab detects which system each game belongs to.",
             f"{playable} systems ready"),
            ("organise", "🗂", "Loose ROMs, scattered about",
             "Downloads, a USB stick, an old backup. GameLab identifies each "
             "one and files it into a tidy per-system folder — after showing "
             "you what moves where.",
             "organise"),
            ("steam", "🎮", "Steam",
             "Found and imported automatically every time GameLab starts. "
             "Nothing to do.",
             "automatic" if steam_found else "not found"),
            ("manual", "✎", "Something else entirely",
             "A launcher, a script, a game built from source — anything with a "
             "command. Pick it from your installed apps or type it in.",
             "add by hand"),
            ("other", "🧩", "Heroic, Lutris, GOG",
             "Detected automatically if installed. Use Scan in the top bar to "
             "re-check for new games.",
             "automatic"),
        ]

        for key, icon, title, description, status in cards:
            card = SourceTypeCard(
                key, icon, title, description, status, self.theme,
                enabled=(key in ("rom_folder", "organise", "manual")),
            )
            card.clicked.connect(self._on_type_chosen)
            layout.addWidget(card)

        layout.addSpacing(6)
        layout.addWidget(self._emulator_status_panel(), 1)

        return page

    def _emulator_status_panel(self) -> QWidget:
        """What can be played right now, and what is missing.

        Shown before the user picks anything, because "which emulators do I
        need" is the question this dialog exists to answer.
        """
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; }}"
        )

        outer = QVBoxLayout(panel)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        rows = emulator_detect.summary()
        ready = [(i, n, e) for i, n, e in rows if e]

        heading = QLabel(f"Emulators found on this machine — {len(ready)} systems ready")
        heading.setStyleSheet(f"font-weight: 600; color: {self.theme.text};")
        outer.addWidget(heading)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMaximumHeight(150)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        inner = QWidget()
        rows_layout = QVBoxLayout(inner)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(3)

        if ready:
            for _system_id, name, option in ready:
                row = QLabel(f"✓  {name}  —  {option.name}")
                row.setStyleSheet(f"color: {self.theme.success}; font-size: 13px;")
                rows_layout.addWidget(row)
        else:
            empty = QLabel(
                "None yet. Add a folder of games and GameLab will tell you "
                "exactly which emulator to install for what it finds."
            )
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {self.theme.text_dim}; font-size: 13px;")
            rows_layout.addWidget(empty)

        rows_layout.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        return panel

    def _on_type_chosen(self, key: str) -> None:
        if key == "manual":
            self.wants_manual_entry = True
            self.reject()
            return

        if key == "organise":
            # Closes and lets the caller open the organiser afterwards, rather
            # than stacking a second modal on top of this one.
            self.wants_organiser = True
            self.reject()
            return

        if key != "rom_folder":
            return

        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder containing your games", str(Path.home())
        )
        if not folder:
            return

        self.folder = Path(folder)
        self._build_preview()
        self.pages.setCurrentIndex(1)
        self.back.show()
        self.confirm.show()

    # ── Page 2: preview before committing ─────────────────────────

    def _preview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.preview_heading = QLabel()
        self.preview_heading.setObjectName("Heading")
        layout.addWidget(self.preview_heading)

        self.preview_path = QLabel()
        self.preview_path.setObjectName("Subtle")
        self.preview_path.setWordWrap(True)
        layout.addWidget(self.preview_path)

        row = QHBoxLayout()
        row.addWidget(QLabel("System"))
        self.system_picker = QComboBox()
        self.system_picker.addItem("Detect automatically", None)
        for system_id, system in sorted(SYSTEMS.items(), key=lambda kv: kv[1].name):
            if system_id == "pc":
                continue
            self.system_picker.addItem(system.name, system_id)
        self.system_picker.currentIndexChanged.connect(self._build_preview)
        row.addWidget(self.system_picker, 1)
        layout.addLayout(row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.preview_body = QWidget()
        self.preview_layout = QVBoxLayout(self.preview_body)
        self.preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_layout.setSpacing(8)
        scroll.setWidget(self.preview_body)

        layout.addWidget(scroll, 1)
        return page

    def _build_preview(self) -> None:
        """Scan the folder and describe exactly what would be added."""
        if self.folder is None:
            return

        while self.preview_layout.count():
            item = self.preview_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()

        hint = self.system_picker.currentData()
        self.chosen_system = hint

        # Folder games and files are collected separately because they are
        # counted differently: a PS3 folder is one game however many thousand
        # files are inside it, and walking it looking for ROM extensions is the
        # bug this split exists to prevent.
        files: list[Path] = []
        folders: list[FolderGame] = []
        for index, found in enumerate(walk_library(self.folder)):
            if isinstance(found, FolderGame):
                folders.append(found)
            else:
                files.append(found)
            if index >= PREVIEW_FILE_LIMIT:
                break

        by_system: dict[Optional[str], list[Path]] = {}
        for path in files:
            by_system.setdefault(infer_system(path, hint=hint), []).append(path)

        unknown = by_system.pop(None, [])
        total_games = 0
        self.detected = {}
        self.media: dict[str, dict[MediaKind, int]] = {}

        for system_id, paths in sorted(by_system.items(), key=lambda kv: kv[0] or ""):
            groups = group_discs(paths)
            self.detected[system_id] = len(groups)
            total_games += len(groups)

            kinds = self.media.setdefault(system_id, {})
            for group in groups:
                kind = (
                    MediaKind.PLAYLIST if group.is_multi_disc
                    else classify(group.primary_file, system_id=system_id)
                )
                kinds[kind] = kinds.get(kind, 0) + 1

        # A folder game's own marker files say which system it is, so the
        # picker above does not override it — the same rule the scanner uses.
        for game in folders:
            self.detected[game.system_id] = self.detected.get(game.system_id, 0) + 1
            kinds = self.media.setdefault(game.system_id, {})
            kinds[MediaKind.FOLDER] = kinds.get(MediaKind.FOLDER, 0) + 1
            total_games += 1

        self.preview_heading.setText(
            f"{total_games} games found" if total_games else "No games found"
        )
        self.preview_path.setText(str(self.folder))
        self.confirm.setEnabled(total_games > 0)

        if not total_games:
            self._add_preview_note(
                "Nothing here looks like a game.\n\n"
                "If your games are in sub-folders that is fine — GameLab looks "
                "inside them, and it recognises games that ARE a folder, like "
                "PS3 and Wii U titles. If they are in an unusual format, choose "
                "the system above instead of leaving it on automatic.",
                self.theme.warning,
            )
            return

        # Per-system rows, each saying whether it can actually be played.
        for system_id, count in sorted(
            self.detected.items(), key=lambda kv: -kv[1]
        ):
            self.preview_layout.addWidget(self._system_row(system_id, count))

        if unknown:
            self._add_preview_note(
                f"{len(unknown)} file(s) could not be matched to a system and "
                "will be skipped. Choosing the system above usually fixes this.",
                self.theme.text_dim,
            )

    def _system_row(self, system_id: str, count: int) -> QWidget:
        system = get_system(system_id)
        option = emulator_detect.best_for(system_id)

        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; }}"
        )

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(5)

        top = QHBoxLayout()
        name = QLabel(f"{system.icon if system else ''}  "
                      f"{system.name if system else system_id}")
        name.setStyleSheet(f"font-weight: 600; color: {self.theme.text};")
        top.addWidget(name)
        top.addStretch(1)

        badge = QLabel(f"{count} game{'s' if count != 1 else ''}")
        badge.setStyleSheet(f"color: {self.theme.text_dim};")
        top.addWidget(badge)
        layout.addLayout(top)

        # What shape they are on disk. Worth showing because it is the one
        # thing users check by hand: a PS3 collection that reads back as
        # "40 game folders" is proof GameLab understood the dumps.
        shapes = describe(self.media.get(system_id, {}))
        if shapes:
            kinds = QLabel(shapes)
            kinds.setStyleSheet(f"color: {self.theme.text_dim}; font-size: 12px;")
            layout.addWidget(kinds)

        if option is not None:
            status = QLabel(f"✓  Ready to play with {option.name}")
            status.setStyleSheet(f"color: {self.theme.success}; font-size: 13px;")
            layout.addWidget(status)
        else:
            # The whole point: say what to install, and the exact command.
            choices = [o for o in emulator_detect.options_for(system_id) if not o.installed]
            status = QLabel("!  No emulator installed — games will import but not launch")
            status.setStyleSheet(f"color: {self.theme.warning}; font-size: 13px;")
            layout.addWidget(status)

            # Prefer whichever option we can give a REAL command for. Saying
            # "install Mesen" helps nobody; "flatpak install flathub
            # org.libretro.RetroArch" can be pasted into a terminal.
            actionable = [o for o in choices if o.flatpak_id or o.arch_package]
            best = (actionable or choices)[0]

            if choices:
                command = QLabel(best.install_hint)
                command.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                command.setStyleSheet(
                    f"color: {self.theme.text}; background-color: {self.theme.elevated};"
                    f" border-radius: 6px; padding: 6px 10px;"
                    f" font-family: monospace; font-size: 12px;"
                )
                layout.addWidget(command)

        return frame

    def _add_preview_note(self, text: str, colour: str) -> None:
        note = QLabel(text)
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {colour}; background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; padding: 12px 14px; font-size: 13px;"
        )
        self.preview_layout.addWidget(note)

    def _accept_folder(self) -> None:
        if self.folder is None:
            return
        self.source_chosen.emit("rom_folder", str(self.folder), self.chosen_system)
        self.accept()
