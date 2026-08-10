"""The details panel for the selected game.

Shows the cover, the metadata that was actually found, and the ways the game
can be launched. Fields with no data are hidden rather than shown empty: a row
reading "Developer: —" tells the user nothing and makes a sparse library look
broken.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.ui.theme import COVER_RATIO, RADIUS, SPACING, Theme

PANEL_WIDTH = 320
DETAIL_COVER_WIDTH = 200


class DetailPanel(QFrame):
    """Metadata and launch controls for one game."""

    launch_requested = Signal(int, object)   # game id, launch option id or None
    favorite_toggled = Signal(int, bool)
    scrape_requested = Signal(int)

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.setObjectName("DetailPanel")
        self.theme = theme
        self.game = None
        self._launch_options: list = []

        self.setFixedWidth(PANEL_WIDTH)
        self._build()
        self.show_empty()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACING, SPACING, SPACING, SPACING)
        outer.setSpacing(SPACING)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)

        self.cover = QLabel()
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setFixedHeight(int(DETAIL_COVER_WIDTH * COVER_RATIO))
        self.body.addWidget(self.cover)

        self.title = QLabel()
        self.title.setObjectName("Heading")
        self.title.setWordWrap(True)
        self.body.addWidget(self.title)

        self.subtitle = QLabel()
        self.subtitle.setObjectName("Subtle")
        self.subtitle.setWordWrap(True)
        self.body.addWidget(self.subtitle)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("Subtle")
        self.body.addWidget(self.summary)

        self.facts = QVBoxLayout()
        self.facts.setSpacing(4)
        self.body.addLayout(self.facts)

        self.body.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # ── Launch controls ───────────────────────────────────────
        self.option_picker = QComboBox()
        self.option_picker.hide()   # only shown when a game has several
        outer.addWidget(self.option_picker)

        self.play = QPushButton("▶  Play")
        self.play.setObjectName("Primary")
        self.play.setMinimumHeight(44)
        self.play.clicked.connect(self._on_play)
        outer.addWidget(self.play)

        row = QHBoxLayout()
        row.setSpacing(6)

        self.favorite = QPushButton("★")
        self.favorite.setCheckable(True)
        self.favorite.setToolTip("Favourite")
        self.favorite.setFixedWidth(48)
        self.favorite.clicked.connect(self._on_favorite)
        row.addWidget(self.favorite)

        self.scrape = QPushButton("Find Art & Info")
        self.scrape.clicked.connect(self._on_scrape)
        row.addWidget(self.scrape, 1)

        outer.addLayout(row)

    # ── Content ───────────────────────────────────────────────────

    def show_empty(self) -> None:
        self.game = None
        self.title.setText("No game selected")
        self.subtitle.clear()
        self.summary.clear()
        self.cover.clear()
        self._clear_facts()
        self.play.setEnabled(False)
        self.favorite.setEnabled(False)
        self.scrape.setEnabled(False)
        self.option_picker.hide()

    def show_game(self, game, launch_options: list, tags: list[str]) -> None:
        self.game = game
        self._launch_options = launch_options

        self.play.setEnabled(bool(launch_options))
        self.favorite.setEnabled(True)
        self.scrape.setEnabled(True)

        self.title.setText(game.title)

        # Subtitle carries the facts that fit on one line.
        parts = []
        from rose_gamelab.core.emulator import get_system
        system = get_system(game.system)
        parts.append(system.name if system else game.system)
        if game.release_date:
            parts.append(game.release_date[:4])
        self.subtitle.setText("  ·  ".join(parts))

        self.summary.setText(game.summary or "")
        self.summary.setVisible(bool(game.summary))

        self._set_cover(game)
        self._set_facts(game, tags)

        self.favorite.setChecked(bool(game.favorite))

        # Only offer a picker when there is genuinely a choice to make.
        self.option_picker.clear()
        if len(launch_options) > 1:
            for option in launch_options:
                label = option["label"] or option["kind"].title()
                self.option_picker.addItem(label, option["id"])
            self.option_picker.show()
        else:
            self.option_picker.hide()

    def _set_cover(self, game) -> None:
        if game.cover_path and Path(game.cover_path).is_file():
            pixmap = QPixmap(game.cover_path)
            if not pixmap.isNull():
                self.cover.setPixmap(pixmap.scaledToWidth(
                    DETAIL_COVER_WIDTH, Qt.TransformationMode.SmoothTransformation
                ))
                return

        self.cover.setText("")
        self.cover.setStyleSheet(
            f"background-color:{self.theme.placeholder};border-radius:{RADIUS}px;"
        )

    def _clear_facts(self) -> None:
        while self.facts.count():
            item = self.facts.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()

    def _set_facts(self, game, tags: list[str]) -> None:
        self._clear_facts()

        rows = [
            ("Developer", game.developer),
            ("Publisher", game.publisher),
            ("Released", game.release_date),
            ("Rating", f"{game.rating:.0f}" if game.rating is not None else None),
            ("Playtime", f"{game.playtime_hours} h" if game.play_seconds else None),
            ("Last played", game.last_played[:10] if game.last_played else None),
            ("Tags", ", ".join(tags) if tags else None),
        ]

        for label, value in rows:
            # Empty rows are omitted entirely; a row reading "Developer: —"
            # tells the user nothing and makes a sparse library look broken.
            if not value:
                continue

            row = QLabel(f"<b>{label}</b>&nbsp;&nbsp;{value}")
            row.setWordWrap(True)
            row.setTextFormat(Qt.TextFormat.RichText)
            self.facts.addWidget(row)

    # ── Actions ───────────────────────────────────────────────────

    def _on_play(self) -> None:
        if not self.game:
            return

        option_id = None
        if self.option_picker.isVisible() and self.option_picker.currentIndex() >= 0:
            option_id = self.option_picker.currentData()

        self.launch_requested.emit(self.game.id, option_id)

    def _on_favorite(self) -> None:
        if self.game:
            self.favorite_toggled.emit(self.game.id, self.favorite.isChecked())

    def _on_scrape(self) -> None:
        if self.game:
            self.scrape_requested.emit(self.game.id)
