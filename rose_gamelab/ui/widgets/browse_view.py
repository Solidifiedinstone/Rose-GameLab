"""The Browse tab: what is popular, and what you already own.

Every list here states what it actually is. A chart derived from the user's own
library carries a visible caveat and is never presented as a global ranking —
the previous implementation showed hardcoded lists as though they were live
data, and this exists to make that mistake impossible rather than merely
unlikely.

Games already in the library are marked, so browsing tells you something you
did not already know.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
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

from rose_gamelab.core.emulator import get_system
from rose_gamelab.ui import theme as ui_theme
from rose_gamelab.ui.theme import Theme


class ChartRow(QFrame):
    """One entry in a chart."""

    def __init__(self, entry, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.entry = entry
        self.theme = theme
        self.setFixedHeight(52)
        self.setStyleSheet(
            f"QFrame {{ background-color: {theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; }}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(14)

        rank = QLabel(f"{entry.rank}")
        rank.setFixedWidth(34)
        rank.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rank.setStyleSheet(
            f"color: {theme.accent}; font-size: 17px; font-weight: 700;"
        )
        layout.addWidget(rank)

        title = QLabel(entry.title)
        title.setStyleSheet("font-size: 14px;")
        layout.addWidget(title, 1)

        # Movement is only meaningful for a real external ranking.
        movement = entry.movement
        if movement:
            arrow = "▲" if movement > 0 else "▼"
            colour = theme.success if movement > 0 else theme.error
            label = QLabel(f"{arrow} {abs(movement)}")
            label.setStyleSheet(f"color: {colour}; font-size: 12px;")
            layout.addWidget(label)

        if entry.peak_players:
            players = QLabel(f"{entry.peak_players:,} playing")
            players.setObjectName("Subtle")
            players.setStyleSheet(f"color: {theme.text_dim}; font-size: 12px;")
            layout.addWidget(players)

        if entry.owned:
            owned = QLabel("In your library")
            owned.setStyleSheet(
                f"color: {theme.success}; font-size: 12px; font-weight: 600;"
            )
            layout.addWidget(owned)


class BrowseView(QWidget):
    """Popular games, per platform."""

    refresh_requested = Signal(str)   # system id

    def __init__(self, library, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.library = library
        self.theme = theme
        self._system = "pc"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING)
        layout.setSpacing(ui_theme.SPACING)

        header = QHBoxLayout()

        heading = QLabel("Browse")
        heading.setObjectName("Heading")
        header.addWidget(heading)
        header.addStretch(1)

        self.system_picker = QComboBox()
        self.system_picker.addItem("PC", "pc")
        for system_id, _count in library.systems_in_library():
            if system_id == "pc":
                continue
            system = get_system(system_id)
            self.system_picker.addItem(system.name if system else system_id, system_id)
        self.system_picker.currentIndexChanged.connect(self._on_system_changed)
        header.addWidget(self.system_picker)

        self.reload = QPushButton("Reload")
        self.reload.clicked.connect(
            lambda: self.refresh_requested.emit(self._system)
        )
        header.addWidget(self.reload)

        layout.addLayout(header)

        # Shown whenever a list is not a genuine external ranking.
        self.caveat = QLabel()
        self.caveat.setWordWrap(True)
        self.caveat.setStyleSheet(
            f"color: {theme.warning}; background-color: {theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; padding: 10px 14px; font-size: 13px;"
        )
        self.caveat.hide()
        layout.addWidget(self.caveat)

        self.chart_title = QLabel()
        self.chart_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(self.chart_title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        self.rows = QVBoxLayout(container)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(6)
        self.rows.addStretch(1)

        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        self.status = QLabel()
        self.status.setObjectName("Subtle")
        layout.addWidget(self.status)

    # ── Content ───────────────────────────────────────────────────

    def restyle(self, theme: Theme) -> None:
        """Adopt a new palette.

        Chart rows are rebuilt whenever a chart loads, so they pick the new
        colours up then; the banner is long-lived and is repainted here.
        """
        self.theme = theme
        self.caveat.setStyleSheet(
            f"color: {theme.warning}; background-color: {theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; padding: 10px 14px; font-size: 13px;"
        )

    def _on_system_changed(self) -> None:
        self._system = self.system_picker.currentData()
        self.refresh_requested.emit(self._system)

    def show_chart(self, chart) -> None:
        """Display a chart, marking which entries are already owned."""
        self._clear()

        self.chart_title.setText(chart.title)

        if chart.caveat:
            self.caveat.setText(chart.caveat)
            self.caveat.show()
        else:
            self.caveat.hide()

        owned_titles = {
            game.sort_title for game in self.library.list_games(include_hidden=True)
        }
        owned_appids = {
            game.steam_appid for game in self.library.list_games(include_hidden=True)
            if game.steam_appid
        }

        from rose_gamelab.core.discs import sort_title

        for entry in chart.entries:
            if not entry.owned:
                entry.owned = (
                    sort_title(entry.title) in owned_titles
                    or (entry.appid is not None and entry.appid in owned_appids)
                )
            self.rows.insertWidget(self.rows.count() - 1, ChartRow(entry, self.theme))

        owned_count = sum(1 for e in chart.entries if e.owned)
        self.status.setText(
            f"{len(chart.entries)} games · you own {owned_count}"
        )

    def show_error(self, message: str) -> None:
        """Say what went wrong instead of showing an empty list."""
        self._clear()
        self.chart_title.setText("Could not load")
        self.caveat.setText(message)
        self.caveat.show()
        self.status.clear()

    def show_loading(self) -> None:
        self.status.setText("Loading…")

    def _clear(self) -> None:
        while self.rows.count() > 1:
            item = self.rows.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()
