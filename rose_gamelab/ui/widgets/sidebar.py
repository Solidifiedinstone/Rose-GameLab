"""The collapsible sidebar.

Modelled on the sidebar in Proton's web apps: collapsed to a narrow rail of
icons by default, sliding open to reveal labels. The animation is a width
change on the whole panel with an eased curve, which keeps the content laid out
correctly at every intermediate width instead of clipping it.

Sources and systems appear here as they are added to the library, so the rail
reflects what the user actually owns rather than a fixed list of every console
that has ever existed.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.ui.branding import rose_widget
from rose_gamelab.ui.theme import Theme

COLLAPSED_WIDTH = 46
EXPANDED_WIDTH = 248
ANIMATION_MS = 220


class SidebarItem(QPushButton):
    """One row in the sidebar: an icon, and a label that appears when expanded."""

    def __init__(
        self,
        icon: str,
        label: str,
        *,
        key: str = "",
        count: Optional[int] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self.setObjectName("SidebarItem")
        self.icon_text = icon
        self.label_text = label
        self.key = key
        self.count = count

        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(38)

        self.set_expanded(False)

    def set_expanded(self, expanded: bool) -> None:
        """Show or hide the label. The icon stays put so nothing jumps."""
        if expanded:
            suffix = f"   {self.count}" if self.count is not None else ""
            self.setText(f" {self.icon_text}   {self.label_text}{suffix}")
            self.setToolTip("")
        else:
            self.setText(f" {self.icon_text}")
            # The tooltip is the only way to read a collapsed item.
            self.setToolTip(
                f"{self.label_text} ({self.count})" if self.count is not None
                else self.label_text
            )


class SidebarSection(QLabel):
    """A small heading above a group of items. Hidden when collapsed."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(text.upper(), parent)
        self.setObjectName("Subtle")
        self.setContentsMargins(14, 10, 0, 4)
        font = self.font()
        font.setPointSizeF(9.0)
        font.setBold(True)
        font.setLetterSpacing(font.SpacingType.PercentageSpacing, 112)
        self.setFont(font)


class Sidebar(QFrame):
    """The navigation rail."""

    #: Emitted with a filter key: 'all', 'favorites', 'system:snes', 'source:steam'...
    filter_selected = Signal(str)
    add_source_requested = Signal()
    settings_requested = Signal()
    big_picture_requested = Signal()

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.setObjectName("Sidebar")
        self.theme = theme
        self._expanded = False
        self._items: list[SidebarItem] = []
        self._sections: list[SidebarSection] = []

        self.setFixedWidth(COLLAPSED_WIDTH)

        # Animate the panel's own width. Animating the contents instead would
        # reflow text mid-transition and look unstable.
        self._animation = QPropertyAnimation(self, b"minimumWidth", self)
        self._animation.setDuration(ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.valueChanged.connect(self._on_animation_step)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self._build()

    # ── Construction ──────────────────────────────────────────────

    def restyle(self, theme: Theme) -> None:
        """Adopt a new palette.

        Nothing to repaint: every colour in the sidebar comes from the window's
        stylesheet by object name, so it follows a theme change on its own. The
        theme is kept only so anything added later can reach it.
        """
        self.theme = theme

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Toggle. Always visible, always in the same place, so the sidebar can
        # be closed again — the previous implementation hid this on expand and
        # became a one-way door.
        self.toggle = QPushButton("☰")
        self.toggle.setObjectName("SidebarToggle")
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setToolTip("Show menu")
        self.toggle.setMinimumHeight(46)
        self.toggle.clicked.connect(self.toggle_expanded)
        outer.addWidget(self.toggle)

        # Scrollable middle: a library with thirty systems must still fit.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        self.content = QVBoxLayout(container)
        self.content.setContentsMargins(6, 4, 6, 4)
        self.content.setSpacing(2)

        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        # Sections are created first because add_item() anchors new rows
        # relative to the systems heading.
        self._library_section = self._add_section("Library")
        self._systems_section = self._add_section("Systems")
        self._collections_section = self._add_section("Collections")
        self._sources_section = self._add_section("Sources")

        for icon, label, key in (
            ("▦", "All Games", "all"),
            ("★", "Favourites", "favorites"),
            ("⏱", "Recently Played", "recent"),
            ("🎲", "Surprise Me", "random"),
            # Without this, hiding a game removes it from every view and the
            # "Unhide" action can never be reached — the game is gone for good.
            ("👁", "Hidden", "hidden"),
            ("🌐", "Browse", "browse"),
        ):
            self.add_item(icon, label, key=key, checked=(key == "all"))

        self.content.addStretch(1)

        # ── Footer ────────────────────────────────────────────────
        footer = QVBoxLayout()
        footer.setContentsMargins(6, 4, 6, 6)
        footer.setSpacing(2)

        for icon, label, signal in (
            ("＋", "Add Source", self.add_source_requested),
            ("🖵", "Big Picture", self.big_picture_requested),
            ("⚙", "Settings", self.settings_requested),
        ):
            button = SidebarItem(icon, label)
            button.setCheckable(False)
            button.setVisible(False)   # collapsed by default
            button.clicked.connect(signal.emit)
            self._items.append(button)
            footer.addWidget(button)

        self.rose = rose_widget()
        self.rose.setContentsMargins(0, 10, 0, 8)
        self.rose.hide()  # only shown when expanded; it does not fit the rail
        footer.addWidget(self.rose)

        outer.addLayout(footer)

    def _add_section(self, title: str) -> SidebarSection:
        section = SidebarSection(title)
        section.hide()  # hidden until it has items
        self._sections.append(section)
        self.content.addWidget(section)
        return section

    def add_item(
        self,
        icon: str,
        label: str,
        *,
        key: str,
        count: Optional[int] = None,
        checked: bool = False,
        before: Optional[QWidget] = None,
    ) -> SidebarItem:
        item = SidebarItem(icon, label, key=key, count=count)
        item.set_expanded(self._expanded)
        item.setVisible(self._expanded)
        item.setChecked(checked)
        item.clicked.connect(lambda _=False, k=key: self.filter_selected.emit(k))

        self._group.addButton(item)
        self._items.append(item)

        if before is not None:
            self.content.insertWidget(self.content.indexOf(before), item)
        else:
            self.content.insertWidget(self.content.indexOf(self._systems_section), item)

        return item

    # ── Dynamic content ───────────────────────────────────────────

    def set_systems(self, systems: list[tuple[str, str, str, int]]) -> None:
        """Populate the systems group: (key, icon, label, count).

        Only systems with games are shown — an empty console is noise.
        """
        self._repopulate(self._systems_section, systems, prefix="system")

    def set_collections(self, collections: list[tuple[str, str, str, int]]) -> None:
        """Populate the collections group: (key, icon, label, count)."""
        self._repopulate(self._collections_section, collections, prefix="collection")

    def set_sources(self, sources: list[tuple[str, str, str, int]]) -> None:
        """Populate the sources group: (key, icon, label, count)."""
        self._repopulate(self._sources_section, sources, prefix="source")

    def _repopulate(
        self, section: SidebarSection, entries: list[tuple[str, str, str, int]], *, prefix: str
    ) -> None:
        # Remove the previous items for this section only.
        for item in list(self._items):
            if item.key.startswith(f"{prefix}:"):
                self._group.removeButton(item)
                self._items.remove(item)
                self.content.removeWidget(item)
                item.deleteLater()

        # Headings only render when expanded — at the collapsed width they
        # clip mid-word ("SYSTE").
        section.setVisible(bool(entries) and self._expanded)
        section.setProperty("has_items", bool(entries))

        anchor_index = self.content.indexOf(section) + 1
        for offset, (key, icon, label, count) in enumerate(entries):
            item = SidebarItem(icon, label, key=f"{prefix}:{key}", count=count)
            item.set_expanded(self._expanded)
            item.setVisible(self._expanded)
            item.clicked.connect(
                lambda _=False, k=f"{prefix}:{key}": self.filter_selected.emit(k)
            )
            self._group.addButton(item)
            self._items.append(item)
            self.content.insertWidget(anchor_index + offset, item)

    # ── Expansion ─────────────────────────────────────────────────

    @property
    def expanded(self) -> bool:
        return self._expanded

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        if expanded == self._expanded:
            return

        self._expanded = expanded

        # Collapsed, the rail shows NOTHING but the toggle. A column of icons
        # the user cannot read is noise, not navigation — so entries are hidden
        # outright rather than squeezed. Content is applied immediately in both
        # directions so nothing pops in late or overflows the shrinking panel.
        for item in self._items:
            item.set_expanded(expanded)
            item.setVisible(expanded)
        for section in self._sections:
            section.setVisible(expanded and self._section_has_items(section))

        self.rose.setVisible(expanded)

        self._animation.stop()
        self._animation.setStartValue(self.width())
        self._animation.setEndValue(EXPANDED_WIDTH if expanded else COLLAPSED_WIDTH)
        self._animation.start()

    def _section_has_items(self, section: SidebarSection) -> bool:
        """Whether a heading has any rows under it. An empty heading is noise."""
        index = self.content.indexOf(section)
        following = self.content.itemAt(index + 1)
        widget = following.widget() if following else None
        return isinstance(widget, SidebarItem)

    def _on_animation_step(self, value) -> None:
        # minimumWidth is animated; the fixed width has to track it or the
        # panel would not actually resize.
        self.setFixedWidth(int(value))

    # ── Selection ─────────────────────────────────────────────────

    def current_filter(self) -> str:
        for item in self._items:
            if item.isCheckable() and item.isChecked():
                return item.key
        return "all"

    def select(self, key: str) -> None:
        for item in self._items:
            if item.key == key:
                item.setChecked(True)
                return
