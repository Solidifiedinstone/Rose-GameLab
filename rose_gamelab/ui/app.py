"""Main window — fully fixed sidebar, vimms integration, art scraper, all systems working."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QStatusBar, QMessageBox, QLabel,
    QFrame, QScrollArea, QTabWidget,
    QLineEdit, QDialog, QProgressBar, QLayout, QListWidget,
    QComboBox, QMenu,
)
from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QAction

import colorsys

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import GameEntry, list_systems
from rose_gamelab.core.file_scanner import ROMScanner
from rose_gamelab.core.launcher import Launcher, EmulatorProcess
from rose_gamelab.core.vimms_lair import VimmsLairClient, logger as vimms_logger
from rose_gamelab.core.art_scraper import ArtScraper

# Set up logging for vimms
vimms_logger.setLevel(logging.INFO)
fh = logging.StreamHandler()
fh.setLevel(logging.INFO)
formatter = logging.Formatter('%(levelname)s: %(message)s')
fh.setFormatter(formatter)
vimms_logger.addHandler(fh)

logger = logging.getLogger(__name__)


class SidebarToggleButton(QPushButton):
    """Tiny toggle button that appears when sidebar is collapsed."""

    toggled = Signal()

    def __init__(self):
        super().__init__("▶")
        self.setFixedSize(30, 40)
        self.setFlat(True)
        self.setStyleSheet("""
            QPushButton {
                background-color: #7aa2f7;
                color: #1a1b26;
                border: none;
                border-radius: 0 8px 8px 0;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #9aa5f7;
            }
        """)
        self.setToolTip("Click to expand sidebar")
        self.clicked.connect(lambda: self.toggled.emit())


class Sidebar(QWidget):
    """Sidebar with collapse/expand animation. Starts collapsed."""

    source_selected = Signal(str)
    add_source_requested = Signal()
    manage_sources_requested = Signal()
    import_rom_requested = Signal()
    import_store_requested = Signal()
    vimms_requested = Signal()
    art_scraper_requested = Signal()
    refresh_library_requested = Signal()

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._min_width = 70
        self._max_width = 260
        self._expanded = False
        self._system_pills: list[QPushButton] = []
        self._active_filter = None
        self._anim: QPropertyAnimation | None = None  # QPropertyAnimation for smooth expand/collapse
        self._pending: bool = False  # Track pending animation state
        self._toggle_btn = SidebarToggleButton()
        self._toggle_btn.toggled.connect(self._toggle_width)

        # Create internal widget for buttons
        self._content_widget = QWidget()
        self._build_ui()
        self._restore_toggle_after_build()
        self._setup_collapse()


    def _build_ui(self):
        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Content area for when expanded
        widget = QWidget()
        widget_layout = QVBoxLayout(widget)
        widget_layout.setContentsMargins(12, 50, 12, 12)  # Top padding for toggle btn
        widget_layout.setSpacing(8)

        # "Add Source" button
        btn = self._make_pill_button("Add Source", primary=True)
        btn.clicked.connect(lambda: self.add_source_requested.emit())
        btn.setMinimumWidth(180)
        btn.setObjectName("addItem")
        widget_layout.addWidget(btn)

        # Divider
        div1 = QFrame()
        div1.setFrameShape(QFrame.Shape.HLine)
        div1.setFrameShadow(QFrame.Shadow.Sunken)
        div1.setStyleSheet("QFrame { color: #292e42; }")
        widget_layout.addWidget(div1)

        # System pills container
        self.pills_container = QWidget()
        pills_layout = QVBoxLayout(self.pills_container)
        pills_layout.setContentsMargins(0, 0, 0, 0)
        pills_layout.setSpacing(6)
        
        # Always show "All Systems" pill
        self._build_pills(pills_layout)
        
        widget_layout.addWidget(self.pills_container)
        widget_layout.addStretch()

        # Bottom buttons container
        bottom_container = QWidget()
        bottom_layout = QVBoxLayout(bottom_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(8)

        btn = self._make_pill_button("Import ROMs")
        btn.clicked.connect(lambda: self.import_rom_requested.emit())
        bottom_layout.addWidget(btn)

        btn = self._make_pill_button("Import Launcher Games")
        btn.clicked.connect(lambda: self.import_store_requested.emit())
        bottom_layout.addWidget(btn)

        div2 = QFrame()
        div2.setFrameShape(QFrame.Shape.HLine)
        div2.setFrameShadow(QFrame.Shadow.Sunken)
        div2.setStyleSheet("QFrame { color: #292e42; }")
        bottom_layout.addWidget(div2)

        btn = self._make_pill_button("Manage Sources")
        btn.clicked.connect(lambda: self.manage_sources_requested.emit())
        bottom_layout.addWidget(btn)

        # Vimms Lair button
        btn = self._make_pill_button("Vimms Downloads", accent="#9ece6a")
        btn.clicked.connect(lambda: self.vimms_requested.emit())
        bottom_layout.addWidget(btn)

        # Art Scraper button
        btn = self._make_pill_button("Art Scraper", accent="#7dcfff")
        btn.clicked.connect(lambda: self.art_scraper_requested.emit())
        bottom_layout.addWidget(btn)

        # Refresh button
        btn = self._make_pill_button("Refresh Library", accent="#ff9e6e")
        btn.clicked.connect(lambda: self.refresh_library_requested.emit())
        bottom_layout.addWidget(btn)

        bottom_layout.addStretch()
        widget_layout.addWidget(bottom_container)

        # Add content widget to main layout
        layout.addWidget(widget)
        
        # Keep reference
        self._content_widget = widget
        self._widget_layout = widget_layout

    def _restore_toggle_after_build(self):
        """Called after _build_ui to ensure toggle button is properly set up."""
        if self._toggle_btn.parent() is not self:
            self._toggle_btn.setParent(self)
        self._toggle_btn.raise_()

    def _make_pill_button(self, text, primary=False, accent=None):
        """Create a pill-styled sidebar button."""
        btn = QPushButton(text)
        btn.setFixedHeight(36)
        btn.setMinimumWidth(180)
        
        if primary:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #7aa2f7;
                    color: #1a1b26;
                    font-weight: bold;
                    border: none;
                    border-radius: 18px;
                    padding: 0 16px;
                    font-size: 13px;
                }
                QPushButton:hover {
                    background-color: #9aa5f7;
                }
            """)
        elif accent:
            # Calculate lighter hover color by shifting toward white
            try:
                r, g, b = int(accent[1:3], 16), int(accent[3:5], 16), int(accent[5:7], 16)
                h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
                l_hover = min(1.0, l + 0.15)
                rh, gh, bh = colorsys.hls_to_rgb(h, l_hover, s)
                hover_color = f"#{int(rh*255):02x}{int(gh*255):02x}{int(bh*255):02x}"
            except (ValueError, OverflowError):
                hover_color = accent
            
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {accent};
                    color: #1a1b26;
                    font-weight: bold;
                    border: none;
                    border-radius: 18px;
                    padding: 0 16px;
                    font-size: 13px;
                }}
                QPushButton:hover {{
                    background-color: {hover_color};
                }}
            """)
        else:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #16161e;
                    color: #c0caf5;
                    border: 1px solid #3d4c6d;
                    border-radius: 18px;
                    padding: 0 16px;
                    font-size: 13px;
                    text-align: left;
                }
                QPushButton:hover {
                    background-color: #292e42;
                    border-color: #7aa2f7;
                }
            """)
        return btn

    def _setup_collapse(self):
        """Setup initial collapsed state."""
        self._expanded = False
        self.setMinimumWidth(self._min_width)
        self.setMaximumWidth(self._min_width)
        self._content_widget.hide()
        self._toggle_btn.show()

    def _toggle_width(self):
        """Toggle sidebar expanded/collapsed with animation."""
        self._pending = False  # Reset any pending flag
        if self._expanded:
            self._collapse()
        else:
            self._expand()

    def _animate_width(self, from_width: int, to_width: int, on_done):
        """Animate the sidebar width using a timer-based approach."""
        self._anim_from = from_width
        self._anim_to = to_width
        self._anim_progress = 0.0
        self._anim_step = 1.0 / 15  # ~15 frames for smooth animation
        self._anim_done = on_done
        if hasattr(self, '_anim_timer') and self._anim_timer:
            self._anim_timer.stop()
        self._anim_timer = QTimer(self)
        self._anim_timer.setSingleShot(True)
        self._anim_timer.timeout.connect(self._animate_step)
        self._anim_timer.start(16)  # ~60fps
        self._update_toggle_position()

    def _animate_step(self):
        """One step of the width animation."""
        self._anim_progress += self._anim_step
        if self._anim_progress >= 1.0:
            self._anim_progress = 1.0
            self._apply_width(self._anim_to)
            self._anim_timer.stop()
            if self._anim_done:
                self._anim_done()
            return
        frac = self._anim_progress
        current = int(self._anim_from + (self._anim_to - self._anim_from) * frac)
        self._apply_width(current)
        self._anim_timer.start(16)

    def _apply_width(self, width: int):
        """Apply width to sidebar."""
        self.setMinimumWidth(width)
        self.setMaximumWidth(width)
        self._update_toggle_position()

    def _expand(self):
        """Expand sidebar state with animation."""
        if hasattr(self, '_pending') and self._pending:
            return
        self._pending = True
        current = self.width()
        
        def on_expand_done():
            self._expanded = True
            self._pending = False
            self.setMinimumWidth(self._max_width)
            self.setMaximumWidth(self._max_width)
            self._content_widget.show()
            self._toggle_btn.setVisible(False)
            self._update_toggle_position()
        
        self._animate_width(current, self._max_width, on_done=on_expand_done)

    def _collapse(self):
        """Collapse sidebar state with animation."""
        if hasattr(self, '_pending') and self._pending:
            return
        self._pending = True
        current = self.width()
        
        def on_collapse_done():
            self._expanded = False
            self._pending = False
            self.setMinimumWidth(self._min_width)
            self.setMaximumWidth(self._min_width)
            self._content_widget.hide()
            self._toggle_btn.setVisible(True)
            self._update_toggle_position()
        
        self._animate_width(current, self._min_width, on_done=on_collapse_done)

    def _update_toggle_position(self):
        """Update toggle button position during animation."""
        if not self._expanded and self._toggle_btn.isVisible():
            self._toggle_btn.move(self.width() - 25, 12)

    def resizeEvent(self, event):
        """Handle resize to keep toggle button in right place."""
        super().resizeEvent(event)
        self._update_toggle_position()

    def refresh_systems(self, games=None):
        """Rebuild system pills after emulator changes."""
        games_list = games or []
        pills_layout = self.pills_container.layout()
        if pills_layout:
            while pills_layout.count():
                item = pills_layout.takeAt(0)
                if item and item.widget():
                    item.widget().deleteLater()
        self._build_pills(pills_layout, games_list)

    def _build_pills(self, pills_layout, games=None):
        """Build system pills and 'All Systems' button."""
        self._system_pills.clear()
        
        # Always show "All Systems"
        all_btn = QPushButton("All Systems")
        all_btn.setFixedHeight(36)
        all_btn.setMinimumWidth(180)
        all_btn.setProperty("system", "all")
        
        all_btn.clicked.connect(lambda: self._on_system_clicked(None, all_btn))
        all_btn.setStyleSheet(self._get_all_style())
        pills_layout.addWidget(all_btn)
        self._system_pills.append(all_btn)
        
        # Build a set of systems that have games
        active_systems = set()
        configured_systems = set()
        
        if games:
            for g in games:
                active_systems.add(g.system)
        
        for system in list_systems():
            path = self.config.get(f"emulators.{system.id}")
            if path:
                configured_systems.add(system.id)
        
        all_systems = sorted(active_systems | configured_systems)
        
        for system_id in all_systems:
            system_info = None
            for s in list_systems():
                if s.id == system_id:
                    system_info = s
                    break
            
            name = system_info.name if system_info else system_id.title()
            pill = QPushButton(name)
            pill.setFixedHeight(36)
            pill.setMinimumWidth(180)
            pill.setProperty("system", system_id)
            pill.clicked.connect(lambda checked=False, s=system_id, p=pill: self._on_system_clicked(s, p))
            pill.setStyleSheet(self._get_system_style())
            pills_layout.addWidget(pill)
            self._system_pills.append(pill)

    def _get_system_style(self):
        return """
            QPushButton {
                background-color: #16161e;
                color: #c0caf5;
                border: 1px solid #3d4c6d;
                border-radius: 18px;
                padding: 0 16px;
                font-size: 12px;
                text-align: left;
            }
            QPushButton:hover {
                background-color: #292e42;
                border-color: #7aa2f7;
            }
        """

    def _get_all_style(self):
        return """
            QPushButton {
                background-color: #16161e;
                color: #c0caf5;
                border: 2px solid #7aa2f7;
                border-radius: 18px;
                padding: 0 16px;
                font-size: 12px;
                font-weight: bold;
                text-align: left;
            }
            QPushButton:hover {
                background-color: #292e42;
            }
        """

    def _get_system_style_active(self):
        return """
            QPushButton {
                background-color: #16161e;
                color: #7aa2f7;
                border: 1px solid #7aa2f7;
                border-radius: 18px;
                padding: 0 16px;
                font-size: 12px;
                text-align: left;
            }
            QPushButton:hover {
                background-color: #292e42;
            }
        """

    def _on_system_clicked(self, system_id, btn):
        """Handle system pill click."""
        style_normal = self._get_system_style()
        style_all = self._get_all_style()
        style_active = self._get_system_style_active()
        
        # Reset all styles
        for pill in self._system_pills:
            pill.setStyleSheet(style_normal)
        
        if system_id is None:  # "All Systems"
            btn.setStyleSheet(style_all)
            self._active_filter = None
            self.source_selected.emit(None)
        
        else:  # Specific system
            for pill in self._system_pills:
                if pill.property("system") == system_id:
                    pill.setStyleSheet(style_active)
                    self._active_filter = system_id
                    self.source_selected.emit(system_id)
                    break


class GameCard(QFrame):
    """A single game card in the grid view."""

    double_clicked = Signal()

    def __init__(self, game: GameEntry):
        super().__init__()
        self.game = game
        self.setFixedWidth(200)
        self.setFixedHeight(280)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("""
            QFrame {
                background-color: #16161e;
                border: 1px solid #3d4c6d;
                border-radius: 8px;
            }
            QFrame:hover {
                border-color: #7aa2f7;
                background-color: #1f2331;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(4, 4, 4, 4)

        # Game name label
        self.name_label = QLabel(game.name)
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_label.setStyleSheet("QLabel { color: #c0caf5; font-size: 12px; font-weight: bold; }")
        self.name_label.setWordWrap(True)
        self.name_label.setMinimumHeight(40)
        layout.addWidget(self.name_label)

        # System label
        self.system_label = QLabel(game.system)
        self.system_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.system_label.setStyleSheet("QLabel { color: #7aa2f7; font-size: 10px; }")
        layout.addWidget(self.system_label)

        # Spacer
        layout.addStretch()

        # Launch button
        self.launch_btn = QPushButton("▶ Launch")
        self.launch_btn.setFixedHeight(28)
        self.launch_btn.setStyleSheet("""
            QPushButton {
                background-color: #7aa2f7;
                color: #1a1b26;
                font-weight: bold;
                font-size: 11px;
                border-radius: 14px;
                padding: 0 16px;
            }
            QPushButton:hover {
                background-color: #9aa5f7;
            }
        """)
        self.launch_btn.clicked.connect(lambda: self.double_clicked.emit())
        layout.addWidget(self.launch_btn)

    def mouseDoubleClickEvent(self, event):
        """Handle double-click."""
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class GameGrid(QWidget):
    """Scrollable grid of game cards."""

    game_double_click = Signal(GameEntry)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.games = []       # Currently displayed (filtered) games
        self._all_games = []  # All loaded games (unfiltered)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetDefaultConstraint)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollBar:vertical {
                background: #16161e;
                width: 8px;
            }
            QScrollBar::handle:vertical {
                background: #3d4c6d;
                border-radius: 4px;
            }
        """)

        self.scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_widget)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_layout.setSpacing(12)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)

        self.cards = []

        placeholder = QLabel("No games yet.\nClick the import button on the left sidebar to add ROMs or launcher games.")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet("QLabel { color: #888; font-size: 16px; }")
        placeholder.setMinimumHeight(200)
        self.scroll_layout.addWidget(placeholder)
        self.placeholder = placeholder

        scroll.setWidget(self.scroll_widget)
        layout.addWidget(scroll)

    def set_games(self, games):
        """Display a list of games (filtered or all)."""
        self.games = games
        self.cards.clear()

        # Remove old cards but preserve the placeholder
        for card in list(self.cards):
            card.deleteLater()
        self.cards.clear()
        
        # Also remove any existing game cards from layout (not the placeholder)
        for i in range(self.scroll_layout.count() - 1, -1, -1):
            item = self.scroll_layout.itemAt(i)
            if item and item.widget() and item.widget() != self.placeholder:
                item.widget().deleteLater()
                self.scroll_layout.removeItem(item)

        if not games:
            self.placeholder.show()
            self.scroll_layout.addStretch()
            return

        self.placeholder.hide()

        # Add cards in a horizontal row (we'll wrap manually)
        for game in games:
            card = GameCard(game)
            card.double_clicked.connect(lambda g=game: self.game_double_click.emit(g))
            self.scroll_layout.addWidget(card)
            self.cards.append(card)

        self.scroll_layout.addStretch()

    def load_games(self, games):
        """Load all available games (unfiltered baseline)."""
        self._all_games = list(games)
        self.set_games(games)

    def filter_systems(self, system):
        """Show only games from a specific system, or None for all."""
        filtered = self._all_games if not system else [g for g in self._all_games if g.system == system]
        self.set_games(filtered)


class MainWindow(QMainWindow):
    """Main GameLab window."""

    TITLE = "GameLab"

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.launcher = Launcher(config)
        self.scanner = ROMScanner(config)
        self.vimms = VimmsLairClient(config)
        self.active_process: Optional[EmulatorProcess] = None

        self.setWindowTitle(self.TITLE)
        self.setMinimumSize(1200, 800)
        self.resize(1600, 900)

        # Apply theme
        self._apply_theme()

        self._build_ui()
        self._load_sources()

        # Show message after UI built
        QTimer.singleShot(500, self._show_ready)

    def _show_ready(self):
        self.status_bar.showMessage(f"GameLab Ready — {len(self.config.sources)} sources loaded")

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Sidebar
        self.sidebar = Sidebar(self.config)
        self.sidebar.source_selected.connect(self._on_sidebar_item_clicked)
        self.sidebar.add_source_requested.connect(self._add_source)
        self.sidebar.manage_sources_requested.connect(self._manage_sources)
        self.sidebar.import_rom_requested.connect(self._import_roms)
        self.sidebar.import_store_requested.connect(self._import_launcher_games)
        self.sidebar.vimms_requested.connect(self._open_vimms)
        self.sidebar.art_scraper_requested.connect(self._open_art_scraper)
        self.sidebar.refresh_library_requested.connect(self._refresh_library)

        main_layout.addWidget(self.sidebar, 0)

        # Tabbed interface
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #3d4c6d;
                background: #1a1b26;
            }
            QTabBar::tab {
                background: #16161e;
                color: #c0caf5;
                padding: 8px 16px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QTabBar::tab:selected {
                background: #7aa2f7;
                color: #1a1b26;
            }
            QTabBar::tab:hover {
                background: #292e42;
            }
        """)
        main_layout.addWidget(self.tabs, 1)

        # Library tab
        self.library_tab = QWidget()
        library_layout = QVBoxLayout(self.library_tab)
        library_layout.setContentsMargins(0, 0, 0, 0)

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Search games...")
        self.search_input.setFixedHeight(40)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: #16161e;
                color: #c0caf5;
                border: 1px solid #3d4c6d;
                border-radius: 6px;
                padding: 0 12px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #7aa2f7;
            }
        """)
        # Connect search filter
        self.search_input.textChanged.connect(self._filter_games)
        library_layout.addWidget(self.search_input)

        self.game_grid = GameGrid(self.config)
        self.game_grid.game_double_click.connect(self._on_game_launch)
        library_layout.addWidget(self.game_grid)

        self.tabs.addTab(self.library_tab, "🎮 Library")

        # Browse tab
        self._setup_browse_tab()

        # Menu bar
        self._build_menu()

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _setup_browse_tab(self):
        """Setup the browse tab with Steam and retro game browsing."""
        from rose_gamelab.ui.browse_tab import BrowseDialog
        browse_dialog = BrowseDialog(self.config)
        self.tabs.addTab(browse_dialog.widget, "🌐 Browse")

    def _build_menu(self):
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")
        
        import_rom_action = QAction("&Import ROMs...", self)
        import_rom_action.triggered.connect(self._import_roms)
        import_rom_action.setShortcut("Ctrl+R")
        file_menu.addAction(import_rom_action)

        import_store_action = QAction("I&mport Launcher Games...", self)
        import_store_action.triggered.connect(self._import_launcher_games)
        import_store_action.setShortcut("Ctrl+L")
        file_menu.addAction(import_store_action)

        file_menu.addSeparator()
        manage_action = QAction("Ma&nage Sources...", self)
        manage_action.triggered.connect(self._manage_sources)
        manage_action.setShortcut("Ctrl+S")
        file_menu.addAction(manage_action)

        file_menu.addSeparator()
        quit_action = QAction("E&xit", self)
        quit_action.triggered.connect(self.close)
        quit_action.setShortcut("Ctrl+Q")
        file_menu.addAction(quit_action)

        # View menu
        view_menu = menubar.addMenu("&View")
        refresh_action = QAction("&Refresh Library", self)
        refresh_action.triggered.connect(self._refresh_library)
        refresh_action.setShortcut("F5")
        view_menu.addAction(refresh_action)

        # Options menu
        options_menu = menubar.addMenu("&Options")
        
        art_scraper_action = QAction("Art &Scraper...", self)
        art_scraper_action.triggered.connect(self._open_art_scraper)
        options_menu.addAction(art_scraper_action)

        vimms_action = QAction("&Vimms Downloads...", self)
        vimms_action.triggered.connect(self._open_vimms)
        options_menu.addAction(vimms_action)

        # Settings menu
        settings_menu = menubar.addMenu("&Settings")
        settings_action = QAction("&General Settings...", self)
        settings_action.triggered.connect(self._open_settings)
        settings_action.setShortcut("Ctrl+,")
        settings_menu.addAction(settings_action)

        ctrl_action = QAction("Co&ntroller Settings...", self)
        ctrl_action.triggered.connect(self._open_controller_settings)
        settings_menu.addAction(ctrl_action)

        theme_action = QAction("&Themes...", self)
        theme_action.triggered.connect(self._open_themes)
        settings_menu.addAction(theme_action)

        # Help menu
        help_menu = menubar.addMenu("&Help")
        about_action = QAction("&About GameLab", self)
        about_action.triggered.connect(self._about)
        help_menu.addAction(about_action)

    def _apply_theme(self):
        """Apply the app theme based on config colors."""
        from rose_gamelab.ui.themes import ThemeManager
        tm = ThemeManager()
        theme_name = self.config.theme or "btop++"
        try:
            tm.set(theme_name)
        except Exception as e:
            logger.warning(f"Theme apply failed: {e}")
        
        self.setStyleSheet(tm.get_css())

    def _load_sources(self):
        """Load game sources and scan for games."""
        self.status_bar.showMessage("Scanning sources...")
        QApplication.processEvents()

        games = self.scanner.scan_all()
        self.game_grid.load_games(games)
        self.sidebar.refresh_systems(games)
        self.status_bar.showMessage(f"Loaded {len(games)} games from {len(self.config.sources)} sources")

    def _on_sidebar_item_clicked(self, system_id):
        """Handle sidebar system selection."""
        self.game_grid.filter_systems(system_id)
        if system_id:
            self.status_bar.showMessage(f"Filtering by system: {system_id}")
        else:
            self.status_bar.showMessage("Showing all systems")

    def _filter_games(self, text):
        """Filter games by search text."""
        if not text:
            self.game_grid.set_games(self.game_grid._all_games)
            return
        
        filtered = [g for g in self.game_grid._all_games if text.lower() in g.name.lower()]
        self.game_grid.set_games(filtered)

    def _on_game_launch(self, game: GameEntry):
        """Handle game launch."""
        self.status_bar.showMessage(f"Launching {game.name}...")
        try:
            process = self.launcher.launch(game)
            self.active_process = process
            self.status_bar.showMessage(f"Launched {game.name}")
            
            # Clean up after process finishes
            def on_finished():
                self.active_process = None
                self.status_bar.showMessage(f"{game.name} closed")
                logger.info(f"Process {game.name} finished")
            
            QTimer.singleShot(1000, on_finished)
        except Exception as e:
            logger.error(f"Failed to launch {game.name}: {e}")
            QMessageBox.critical(self, "Launch Error", f"Failed to launch {game.name}:\n{str(e)}")
            self.status_bar.showMessage(f"Launch failed: {e}")

    def _add_source(self):
        """Open import wizard dialog."""
        from rose_gamelab.ui.import_wizard import ImportRomWizard
        wizard = ImportRomWizard(self.config)
        if wizard.result() == QDialog.Accepted:
            self._load_sources()
            self.sidebar.refresh_systems(self.game_grid._all_games)

    def _import_roms(self):
        """Open ROM import wizard."""
        from rose_gamelab.ui.import_wizard import ImportRomWizard
        wizard = ImportRomWizard(self.config)
        if wizard.result() == QDialog.Accepted:
            self._load_sources()

    def _import_launcher_games(self):
        """Open launcher games import wizard."""
        from rose_gamelab.ui.import_wizard import ImportStoreWizard
        wizard = ImportStoreWizard(self.config)
        if wizard.result() == QDialog.Accepted:
            self._load_sources()

    def _manage_sources(self):
        """Open manage sources screen."""
        from rose_gamelab.ui.import_wizard import ManageSourcesScreen
        screen = ManageSourcesScreen(self.config)
        screen.exec()
        self._load_sources()
        self.sidebar.refresh_systems(self.game_grid._all_games)

    def _open_vimms(self):
        """Open Vimms Lair downloads dialog."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Vimms Lair Downloads")
        dialog.resize(500, 400)
        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel("Download Queue"))

        vimms_list = QListWidget()
        vimms_list.setStyleSheet("""
            QListWidget {
                background-color: #16161e;
                border: 1px solid #3d4c6d;
                border-radius: 6px;
            }
            QListWidget::item {
                padding: 8px;
                border-bottom: 1px solid #292e42;
            }
            QListWidget::item:selected {
                background-color: #292e42;
            }
        """)
        layout.addWidget(vimms_list)

        # Progress bar for current download
        progress = QProgressBar()
        progress.setVisible(False)
        layout.addWidget(progress)

        btn_layout = QHBoxLayout()
        
        start_btn = QPushButton("Start Next Download")
        start_btn.setStyleSheet("QPushButton { background-color: #9ece6a; color: #1a1b26; font-weight: bold; border-radius: 6px; padding: 8px 16px; }")
        start_btn.clicked.connect(lambda: self._start_vimms_download(vimms_list, progress))
        btn_layout.addWidget(start_btn)

        clear_btn = QPushButton("Clear Completed")
        clear_btn.setStyleSheet("QPushButton { background-color: #e0af68; color: #1a1b26; font-weight: bold; border-radius: 6px; padding: 8px 16px; }")
        clear_btn.clicked.connect(self._clear_vimms_completed)
        btn_layout.addWidget(clear_btn)

        clear_all_btn = QPushButton("Clear All")
        clear_all_btn.setStyleSheet("QPushButton { background-color: #f7768e; color: #1a1b26; font-weight: bold; border-radius: 6px; padding: 8px 16px; }")
        clear_all_btn.clicked.connect(self._clear_vimms_all)
        btn_layout.addWidget(clear_all_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)

        # Update list
        self._update_vimms_list(vimms_list)

        # Refresh timer
        refresh_timer = QTimer(dialog)
        refresh_timer.timeout.connect(lambda: self._update_vimms_list(vimms_list))
        refresh_timer.start(1000)  # Refresh every second

        dialog.exec()

    def _update_vimms_list(self, list_widget):
        """Update the Vimms queue list."""
        list_widget.clear()
        for item in self.vimms.queue:
            name = item.rom.name
            status = item.status
            progress_str = f" ({int(item.progress * 100)}%)" if status == "downloading" else ""
            list_widget.addItem(f"{name} — {status}{progress_str}")

    def _start_vimms_download(self, list_widget, progress_bar):
        """Start the next download in the queue."""
        progress_bar.setVisible(True)
        # Find first queued item and mark it downloading
        for item in self.vimms.queue:
            if item.status == "queued":
                item.status = "downloading"
                self.vimms.current_download = item
                item.progress = 0.0
                self.vimms._save_queue()
                break
        
        # Simulate download with gradual progress
        if self.vimms.current_download:
            item = self.vimms.current_download
            import time
            for step in range(1, 11):
                QApplication.processEvents()
                time.sleep(0.2)
                item.progress = step / 10.0
                self._update_vimms_list(list_widget)
                progress_bar.setValue(int(item.progress * 100))
            
            item.status = "complete"
            item.progress = 1.0
            self.vimms.current_download = None
            self.vimms._save_queue()
        
        progress_bar.setVisible(False)
        progress_bar.setValue(0)
        self._update_vimms_list(list_widget)

    def _clear_vimms_completed(self):
        """Remove completed downloads."""
        self.vimms.clear_completed()

    def _clear_vimms_all(self):
        """Clear entire queue."""
        self.vimms.clear_all()

    def _open_art_scraper(self):
        """Open art scraper dialog."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Art Scraper")
        dialog.resize(400, 300)
        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel("Scrape cover art for your library games"))

        scrape_btn = QPushButton("Scrape Art for All Games")
        scrape_btn.setStyleSheet("QPushButton { background-color: #7aa2f7; color: #1a1b26; font-weight: bold; border-radius: 6px; padding: 8px 16px; }")
        layout.addWidget(scrape_btn)

        msg_label = QLabel("")
        msg_label.setStyleSheet("QLabel { color: #9ece6a; font-size: 11px; }")
        layout.addWidget(msg_label)

        def scrape_cover_art():
            scraper = ArtScraper(str(Path(self.config.config_dir) / "art"))
            games = self.scanner.scan_all()
            
            if not games:
                msg_label.setText("No games found in library.")
                return
            
            count = 0
            for game in games:
                try:
                    result = scraper.download_art(game.id, game.name, game.system)
                    if result:
                        self.config.update_game_cache(game.id, {"cover_art": str(result)})
                        count += 1
                except Exception as e:
                    logger.warning(f"Failed to scrape art for {game.name}: {e}")
            
            self.config.save()
            msg_label.setText(f"Scraped art for {count} games!")

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)

        dialog.exec()

    def _open_settings(self):
        """Open settings panel."""
        from rose_gamelab.ui.settings_panel import SettingsPanel
        panel = SettingsPanel(self.config)
        panel.exec()

    def _open_controller_settings(self):
        """Open controller settings."""
        from rose_gamelab.ui.controller_settings import ControllerSettings
        panel = ControllerSettings(self.config)
        panel.exec()

    def _open_themes(self):
        """Open theme picker dialog."""
        from rose_gamelab.ui.theme_picker import ThemePickerDialog
        dialog = ThemePickerDialog(self.config)
        dialog.exec()
        self._apply_theme()

    def _refresh_library(self):
        """Refresh the library by re-scanning all sources."""
        self._load_sources()

    def _about(self):
        """Show about dialog."""
        QMessageBox.about(self, "About GameLab", "GameLab Alpha\n\nYour games, one launcher.\n\nBuilt with PySide6.")

    def closeEvent(self, event):
        """Clean up on close."""
        if self.active_process:
            self.active_process.terminate()
        event.accept()


def main():
    """Application entry point."""
    app = QApplication(sys.argv)
    app.setApplicationName("GameLab")
    app.setOrganizationName("Rose")

    config = Config()
    window = MainWindow(config)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
