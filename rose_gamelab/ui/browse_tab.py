"""Browse tab — shows popular Steam games and popular retro games per console.

Inspired by SteamROMManager's browsing feature, this tab displays:
- Top Steam games by region (PC titles that work with Wine/Proton)
- Popular retro games per system (NES classic titles for emulation)
- Real installed Steam games (auto-detected)
- Each console only shows if you've configured that emulator
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QScrollArea,
    QLabel, QPushButton, QFrame, QGridLayout, QComboBox,
    QGroupBox, QDialog, QLineEdit,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QMovie, QPixmap

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import GameEntry, list_systems, EmulatorDef
from rose_gamelab.core.art_scraper import ArtScraper

logger = logging.getLogger(__name__)


# ── Popular games data ─────────

POPULAR_STEAM = [
    {"id": "steam_001", "name": "Hades", "system": "pc", "steam_id": "1145360",
     "genres": ["Roguelike", "Action", "Indie"], "rating": "Overwhelmingly Positive"},
    {"id": "steam_002", "name": "Elden Ring", "system": "pc", "steam_id": "1245620",
     "genres": ["RPG", "Souls-like"], "rating": "Very Positive"},
    {"id": "steam_003", "name": "Baldur's Gate 3", "system": "pc", "steam_id": "1086940",
     "genres": ["RPG", "Strategy", "Turn-based"], "rating": "Overwhelmingly Positive"},
    {"id": "steam_004", "name": "Stardew Valley", "system": "pc", "steam_id": "413150",
     "genres": ["Farming Sim", "Indie"], "rating": "Overwhelmingly Positive"},
    {"id": "steam_005", "name": "Hollow Knight", "system": "pc", "steam_id": "367520",
     "genres": ["Metroidvania", "Indie"], "rating": "Overwhelmingly Positive"},
    {"id": "steam_006", "name": "Celeste", "system": "pc", "steam_id": "504230",
     "genres": ["Platformer", "Indie"], "rating": "Overwhelmingly Positive"},
    {"id": "steam_007", "name": "Outer Wilds", "system": "pc", "steam_id": "753640",
     "genres": ["Adventure", "Exploration"], "rating": "Overwhelmingly Positive"},
    {"id": "steam_008", "name": "Disco Elysium", "system": "pc", "steam_id": "632470",
     "genres": ["RPG", "Narrative"], "rating": "Overwhelmingly Positive"},
    {"id": "steam_009", "name": "Dead Cells", "system": "pc", "steam_id": "588650",
     "genres": ["Roguelike", "Action"], "rating": "Overwhelmingly Positive"},
    {"id": "steam_010", "name": "Ori and the Will of the Wisps", "system": "pc", "steam_id": "1151660",
     "genres": ["Platformer", "Metroidvania"], "rating": "Overwhelmingly Positive"},
    {"id": "steam_011", "name": "Cyberpunk 2077", "system": "pc", "steam_id": "1091500",
     "genres": ["RPG", "Open World"], "rating": "Very Positive"},
    {"id": "steam_012", "name": "Red Dead Redemption 2", "system": "pc", "steam_id": "1222490",
     "genres": ["Action", "Open World"], "rating": "Very Positive"},
    {"id": "steam_013", "name": "God of War (PC)", "system": "pc", "steam_id": "1566330",
     "genres": ["Action", "Adventure"], "rating": "Very Positive"},
    {"id": "steam_014", "name": "Spider-Man Remastered", "system": "pc", "steam_id": "1817070",
     "genres": ["Action", "Adventure"], "rating": "Very Positive"},
    {"id": "steam_015", "name": "Final Fantasy VII Remake", "system": "pc", "steam_id": "1236470",
     "genres": ["RPG", "Action"], "rating": "Very Positive"},
]


# Popular retro games per system (emulation classics)
POPULAR_RETRO: dict[str, list[dict]] = {
    "nes": [  # NES is special — handled via GBA/VBA system
        {"id": "retro_nes_001", "name": "Super Mario Bros. 3", "system": "gb", "is_fictional": True,
         "genres": ["Platformer"], "year": 1988, "rating": "S-Tier"},
        {"id": "retro_nes_002", "name": "The Legend of Zelda", "system": "gb", "is_fictional": True,
         "genres": ["Action", "Adventure"], "year": 1986, "rating": "S-Tier"},
        {"id": "retro_nes_003", "name": "Metroid", "system": "gb", "is_fictional": True,
         "genres": ["Action", "Exploration"], "year": 1986, "rating": "S-Tier"},
        {"id": "retro_nes_004", "name": "Mega Man 2", "system": "gb", "is_fictional": True,
         "genres": ["Platformer", "Action"], "year": 1988, "rating": "S-Tier"},
        {"id": "retro_nes_005", "name": "Castlevania", "system": "gb", "is_fictional": True,
         "genres": ["Action", "Platformer"], "year": 1986, "rating": "A-Tier"},
    ],
    "snes": [
        {"id": "retro_snes_001", "name": "Super Metroid", "system": "snes",
         "genres": ["Action", "Exploration"], "year": 1994, "rating": "S-Tier"},
        {"id": "retro_snes_002", "name": "The Legend of Zelda: A Link to the Past", "system": "snes",
         "genres": ["Action", "Adventure"], "year": 1991, "rating": "S-Tier"},
        {"id": "retro_snes_003", "name": "Super Mario World 2: Yoshi's Island", "system": "snes",
         "genres": ["Platformer"], "year": 1995, "rating": "S-Tier"},
        {"id": "retro_snes_004", "name": "Chrono Trigger", "system": "snes",
         "genres": ["RPG"], "year": 1995, "rating": "S-Tier"},
        {"id": "retro_snes_005", "name": "Final Fantasy VI", "system": "snes",
         "genres": ["RPG"], "year": 1994, "rating": "S-Tier"},
        {"id": "retro_snes_006", "name": "Donkey Kong Country", "system": "snes",
         "genres": ["Platformer"], "year": 1994, "rating": "S-Tier"},
        {"id": "retro_snes_007", "name": "Mega Man X", "system": "snes",
         "genres": ["Action", "Platformer"], "year": 1993, "rating": "A-Tier"},
        {"id": "retro_snes_008", "name": "Street Fighter II Turbo", "system": "snes",
         "genres": ["Fighting"], "year": 1993, "rating": "A-Tier"},
    ],
    "gba": [
        {"id": "retro_gba_001", "name": "Pokémon FireRed", "system": "gba",
         "genres": ["RPG"], "year": 2004, "rating": "S-Tier"},
        {"id": "retro_gba_002", "name": "Metroid: Fusion", "system": "gba",
         "genres": ["Action", "Exploration"], "year": 2002, "rating": "S-Tier"},
        {"id": "retro_gba_003", "name": "Castlevania: Aria of Sorrow", "system": "gba",
         "genres": ["Action", "RPG"], "year": 2002, "rating": "S-Tier"},
        {"id": "retro_gba_004", "name": "Fire Emblem: The Binding Blade", "system": "gba",
         "genres": ["Strategy", "RPG"], "year": 2003, "rating": "A-Tier"},
        {"id": "retro_gba_005", "name": "The Legend of Zelda: The Minish Cap", "system": "gba",
         "genres": ["Action", "Adventure"], "year": 2004, "rating": "S-Tier"},
    ],
    "nds": [
        {"id": "retro_nds_001", "name": "Pokémon HeartGold", "system": "nds",
         "genres": ["RPG"], "year": 2009, "rating": "S-Tier"},
        {"id": "retro_nds_002", "name": "The Legend of Zelda: Phantom Hourglass", "system": "nds",
         "genres": ["Action", "Adventure"], "year": 2007, "rating": "A-Tier"},
        {"id": "retro_nds_003", "name": "Brain Training", "system": "nds",
         "genres": ["Puzzle"], "year": 2005, "rating": "B-Tier"},
        {"id": "retro_nds_004", "name": "New Super Mario Bros.", "system": "nds",
         "genres": ["Platformer"], "year": 2006, "rating": "A-Tier"},
    ],
    "ps1": [
        {"id": "retro_ps1_001", "name": "Metal Gear Solid", "system": "ps1",
         "genres": ["Action", "Stealth"], "year": 1998, "rating": "S-Tier"},
        {"id": "retro_ps1_002", "name": "Final Fantasy VII", "system": "ps1",
         "genres": ["RPG"], "year": 1997, "rating": "S-Tier"},
        {"id": "retro_ps1_003", "name": "Crash Bandicoot", "system": "ps1",
         "genres": ["Platformer"], "year": 1996, "rating": "A-Tier"},
        {"id": "retro_ps1_004", "name": "Tomb Raider", "system": "ps1",
         "genres": ["Action", "Adventure"], "year": 1996, "rating": "A-Tier"},
        {"id": "retro_ps1_005", "name": "Resident Evil 2", "system": "ps1",
         "genres": ["Survival Horror"], "year": 1998, "rating": "S-Tier"},
        {"id": "retro_ps1_006", "name": "Castlevania: Symphony of the Night", "system": "ps1",
         "genres": ["Action", "Exploration"], "year": 1997, "rating": "S-Tier"},
    ],
    "ps2": [
        {"id": "retro_ps2_001", "name": "Shadow of the Colossus", "system": "ps2",
         "genres": ["Action", "Adventure"], "year": 2005, "rating": "S-Tier"},
        {"id": "retro_ps2_002", "name": "Grand Theft Auto: San Andreas", "system": "ps2",
         "genres": ["Open World", "Action"], "year": 2004, "rating": "S-Tier"},
        {"id": "retro_ps2_003", "name": "Kingdom Hearts", "system": "ps2",
         "genres": ["Action", "RPG"], "year": 2002, "rating": "A-Tier"},
        {"id": "retro_ps2_004", "name": "Final Fantasy X", "system": "ps2",
         "genres": ["RPG"], "year": 2001, "rating": "S-Tier"},
        {"id": "retro_ps2_005", "name": "Devil May Cry 3", "system": "ps2",
         "genres": ["Action", "Souls-like"], "year": 2005, "rating": "A-Tier"},
    ],
    "n64": [
        {"id": "retro_n64_001", "name": "The Legend of Zelda: Ocarina of Time", "system": "n64",
         "genres": ["Action", "Adventure"], "year": 1998, "rating": "S-Tier"},
        {"id": "retro_n64_002", "name": "Super Mario 64", "system": "n64",
         "genres": ["Platformer", "3D"], "year": 1996, "rating": "S-Tier"},
        {"id": "retro_n64_003", "name": "Super Mario Kart", "system": "n64",
         "genres": ["Racing"], "year": 1996, "rating": "S-Tier"},
        {"id": "retro_n64_004", "name": "GoldenEye 007", "system": "n64",
         "genres": ["TPS", "Multiplayer"], "year": 1997, "rating": "S-Tier"},
        {"id": "retro_n64_005", "name": "Ogre Battle 64", "system": "n64",
         "genres": ["Strategy", "RPG"], "year": 1999, "rating": "A-Tier"},
    ],
    "dreamcast": [
        {"id": "retro_dc_001", "name": "Shenmue", "system": "dreamcast",
         "genres": ["Adventure", "Open World"], "year": 1999, "rating": "A-Tier"},
        {"id": "retro_dc_002", "name": "Crazy Taxi", "system": "dreamcast",
         "genres": ["Racing"], "year": 1999, "rating": "A-Tier"},
        {"id": "retro_dc_003", "name": "Soulcalibur", "system": "dreamcast",
         "genres": ["Fighting"], "year": 1998, "rating": "A-Tier"},
        {"id": "retro_dc_004", "name": "Jet Set Radio", "system": "dreamcast",
         "genres": ["Action", "Platformer"], "year": 2000, "rating": "A-Tier"},
    ],
    "psp": [
        {"id": "retro_psp_001", "name": "God of War: Chains of Olympus", "system": "psp",
         "genres": ["Action", "Adventure"], "year": 2008, "rating": "S-Tier"},
        {"id": "retro_psp_002", "name": "Persona 3 Portable", "system": "psp",
         "genres": ["RPG"], "year": 2009, "rating": "S-Tier"},
        {"id": "retro_psp_003", "name": "Grand Theft Auto: Vice City Stories", "system": "psp",
         "genres": ["Open World", "Action"], "year": 2002, "rating": "A-Tier"},
        {"id": "retro_psp_004", "name": "Monster Hunter Portable 3rd", "system": "psp",
         "genres": ["Action", "RPG"], "year": 2010, "rating": "A-Tier"},
    ],
    "switch": [
        {"id": "retro_switch_001", "name": "Zelda: Breath of the Wild", "system": "switch",
         "genres": ["Action", "Adventure", "Open World"], "year": 2017, "rating": "S-Tier"},
        {"id": "retro_switch_002", "name": "Mario Kart 8 Deluxe", "system": "switch",
         "genres": ["Racing"], "year": 2017, "rating": "S-Tier"},
        {"id": "retro_switch_003", "name": "Animal Crossing: New Horizons", "system": "switch",
         "genres": ["Simulation"], "year": 2020, "rating": "S-Tier"},
    ],
    "arcade": [
        {"id": "retro_arcade_001", "name": "Street Fighter II", "system": "arcade",
         "genres": ["Fighting"], "year": 1991, "rating": "S-Tier"},
        {"id": "retro_arcade_002", "name": "Metal Slug", "system": "arcade",
         "genres": ["Run-and-Gun", "Action"], "year": 1996, "rating": "S-Tier"},
        {"id": "retro_arcade_003", "name": "Touhou Danmaku", "system": "arcade",
         "genres": ["Shoot 'em up"], "year": 1998, "rating": "A-Tier"},
    ],
    "wii": [
        {"id": "retro_wii_001", "name": "Super Mario Galaxy", "system": "wii",
         "genres": ["Platformer", "3D"], "year": 2007, "rating": "S-Tier"},
        {"id": "retro_wii_002", "name": "Super Smash Bros. Brawl", "system": "wii",
         "genres": ["Fighting"], "year": 2008, "rating": "A-Tier"},
        {"id": "retro_wii_003", "name": "The Legend of Zelda: Twilight Princess", "system": "wii",
         "genres": ["Action", "Adventure"], "year": 2006, "rating": "S-Tier"},
    ],
}


# ── Search input widget ────────────────────────────────────────────


class SearchInput(QLineEdit):
    """Styled search input with placeholder and icon."""

    def __init__(self) -> None:
        super().__init__()
        self.setPlaceholderText("Search games...")
        self.setMinimumWidth(200)
        self.setStyleSheet("""
            QLineEdit {
                background-color: #16161e;
                color: #c0caf5;
                border: 1px solid #3d4c6d;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 12px;
            }
            QLineEdit:focus {
                border-color: #7aa2f7;
            }
        """)


# ── Browser widgets ─────────


class BrowseGameCard(QFrame):
    """A card showing game info for the browse tab."""
    
    selected = Signal()

    def __init__(self, game_data: dict):
        super().__init__()
        self.game_data = game_data
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedHeight(80)
        self.setMinimumWidth(280)
        self._build_ui()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Cover placeholder (colored)
        color_map = {
            "pc": "#7aa2f7",
            "snes": "#9ece6a",
            "gba": "#e0af68",
            "nds": "#bb9af7",
            "ps1": "#f7768e",
            "ps2": "#9ece6a",
            "n64": "#ff9e64",
            "dreamcast": "#7dcfff",
            "psp": "#c0caf5",
            "switch": "#f7768e",
            "arcade": "#e0af68",
            "wii": "#9ece6a",
        }
        primary_color = color_map.get(self.game_data.get("system", "pc"), "#7aa2f7")
        
        # Game info container widget
        info_container = QWidget()
        info_layout = QVBoxLayout(info_container)
        info_layout.setContentsMargins(12, 8, 12, 8)
        info_layout.setSpacing(4)
        
        # Game name
        name_label = QLabel(self.game_data["name"])
        name_label.setStyleSheet(f"QLabel {{ color: #c0caf5; font-size: 14px; font-weight: bold; }}")
        info_layout.addWidget(name_label)
        
        # Meta: system, genres, year
        meta_parts = []
        system = self.game_data.get("system", "").upper()
        meta_parts.append(system)
        
        if "genres" in self.game_data:
            meta_parts.append(" • ".join(self.game_data["genres"][:3]))
        
        if "year" in self.game_data:
            meta_parts.append(str(self.game_data["year"]))
        
        meta_label = QLabel(" • ".join(meta_parts))
        meta_label.setStyleSheet("QLabel { color: #565a78; font-size: 11px; }")
        info_layout.addWidget(meta_label)
        
        # Ratings
        rating = self.game_data.get("rating", "") or self.game_data.get("rating", "")
        if rating:
            rating_label = QLabel(rating)
            if "S-Tier" in rating:
                rating_label.setStyleSheet("QLabel { color: #9ece6a; font-size: 11px; font-weight: bold; }")
            elif "A-Tier" in rating:
                rating_label.setStyleSheet("QLabel { color: #e0af68; font-size: 11px; font-weight: bold; }")
            else:
                rating_label.setStyleSheet("QLabel { color: #7aa2f7; font-size: 11px; }")
            info_layout.addWidget(rating_label)
        
        info_layout.addStretch()
        
        layout.addWidget(info_container, 1)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit()
            event.accept()
        else:
            super().mousePressEvent(event)


class PopGamesWidget(QWidget):
    """Shows a scrollable list of popular games."""
    
    game_selected = Signal(dict)

    def __init__(self, name: str, games: list[dict], parent: QWidget | None = None):
        super().__init__(parent)
        self.name = name
        self.games = games
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        
        # Title
        title = QLabel(self.name)
        title.setStyleSheet("""
            QLabel {
                color: #c0caf5;
                font-size: 16px;
                font-weight: bold;
                margin: 4px 0;
            }
        """)
        layout.addWidget(title)
        
        # Games
        games_layout = QVBoxLayout()
        games_layout.setSpacing(6)
        games_layout.setContentsMargins(0, 0, 0, 0)
        
        for game in self.games:
            card = BrowseGameCard(game)
            card.selected.connect(lambda g=game: self.game_selected.emit(g))
            games_layout.addWidget(card)
        
        games_layout.addStretch()
        
        # Scroll
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: #16161e; width: 6px; }
            QScrollBar::handle:vertical { background: #3d4c6d; border-radius: 3px; }
        """)
        scroll_w = QWidget()
        scroll_w.setLayout(games_layout)
        scroll.setWidget(scroll_w)
        layout.addWidget(scroll)


# ── Browse dialog ─────────


class BrowseDialog(QDialog):
    """Browse dialog showing popular Steam and retro games."""

    @property
    def widget(self) -> QWidget:
        """Return self for embedding in QTabWidget."""
        return self

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setWindowTitle("Browse Games")
        self.resize(900, 600)
        self._build_ui()
        self._update_system_tabs()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        
        # Title
        title = QLabel("Browse Games")
        title.setStyleSheet("""
            QLabel {
                color: #c0caf5;
                font-size: 20px;
                font-weight: bold;
            }
        """)
        layout.addWidget(title)
        
        # Search
        search_layout = QHBoxLayout()
        search_layout.setSpacing(8)
        
        self.search_input = SearchInput()
        search_layout.addWidget(self.search_input, stretch=3)
        
        # Category filter
        self.category_combo = QComboBox()
        self.category_combo.setMinimumWidth(150)
        self.category_combo.addItems(["All Systems", "PC/Steam", "Nintendo", "Sony", "Sega", "Atari"])
        search_layout.addWidget(self.category_combo)
        
        # Sort
        self.sort_combo = QComboBox()
        self.sort_combo.setMinimumWidth(120)
        self.sort_combo.addItems(["Top Rated", "Most Popular", "Random"])
        search_layout.addWidget(self.sort_combo)
        
        search_btn = QPushButton("Search")
        search_btn.setStyleSheet("""
            QPushButton {
                background-color: #7aa2f7;
                color: #1a1b26;
                font-weight: bold;
                border-radius: 6px;
                padding: 6px 16px;
            }
            QPushButton:hover { background-color: #9aa5f7; }
        """)
        search_btn.clicked.connect(self._handle_search)
        search_layout.addWidget(search_btn)
        
        layout.addLayout(search_layout)
        
        # Tabs
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(False)
        self.tabs.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #3d4c6d;
                border-radius: 6px;
                background: #16161e;
            }
            QTabBar::tab {
                background: #1a1b26;
                color: #565a78;
                padding: 8px 20px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }
            QTabBar::tab:selected {
                background: #1e2030;
                color: #c0caf5;
                font-weight: bold;
            }
        """)
        layout.addWidget(self.tabs)
        
        # Import button
        import_layout = QHBoxLayout()
        import_layout.addStretch()
        
        self.import_btn = QPushButton("Import Selected to Library")
        self.import_btn.setStyleSheet("""
            QPushButton {
                background-color: #9ece6a;
                color: #1a1b26;
                font-weight: bold;
                border-radius: 6px;
                padding: 8px 20px;
            }
            QPushButton:hover { background-color: #b3cc7e; }
        """)
        self.import_btn.clicked.connect(self._import_selected)
        self.import_btn.setEnabled(False)
        import_layout.addWidget(self.import_btn)
        
        layout.addLayout(import_layout)
        
        self._selected_games: list[dict] = []

    def _update_system_tabs(self):
        """Update system tabs based on configured emulators and installed Steam games."""
        # Clear existing tabs (skip the first 2 - they're added first)
        for i in range(self.tabs.count() - 1, -1, -1):
            widget = self.tabs.widget(i)
            if widget:
                widget.deleteLater()
        
        # Try to detect installed Steam games
        steam_games = []
        try:
            from rose_gamelab.sources.steam import SteamProvider
            steam = SteamProvider()
            steam_games = steam.discover()
            logger.info(f"Detected {len(steam_games)} installed Steam games")
        except Exception as e:
            logger.warning(f"Failed to detect Steam games: {e}")
        
        # Add Steam installed games tab if any found
        if steam_games:
            steam_widget = PopGamesWidget("🎮 Installed Steam Games", [
                {
                    "id": g.id,
                    "name": g.name,
                    "system": "pc",
                    "steam_id": g.metadata.get("steam_app_id", ""),
                    "genres": ["PC"],
                    "rating": "Installed",
                }
                for g in steam_games[:10]
            ], self)
            steam_widget.game_selected.connect(self._on_game_selected)
            self.tabs.addTab(steam_widget, "Installed Steam Games")
        
        # Steam/PC popular games tab (always shown)
        steam_widget = PopGamesWidget("🎮 Steam / PC Games", POPULAR_STEAM[:8], self)
        steam_widget.game_selected.connect(self._on_game_selected)
        self.tabs.addTab(steam_widget, "Steam/PC")
        self._steam_widget = steam_widget
        
        # System tabs (only if emulator configured)
        systems_configured = {}
        for system in list_systems():
            path = self.config.get(f"emulators.{system.id}")
            if path:
                systems_configured[system.id] = system
        
        if systems_configured:
            for sys_id, sys_def in sorted(systems_configured.items()):
                name = sys_def.name
                games = POPULAR_RETRO.get(sys_id, [])
                
                if games:
                    widget = PopGamesWidget(f"🎮 {name}", games[:5], self)
                    widget.game_selected.connect(self._on_game_selected)
                    self.tabs.addTab(widget, name)
        
        self._system_tabs = systems_configured

    def _handle_search(self):
        """Handle search from input."""
        query = self.search_input.text().strip().lower()
        sort_method = self.sort_combo.currentText()
        
        all_games = list(POPULAR_STEAM)
        for games in POPULAR_RETRO.values():
            all_games.extend(games)
        
        # Filter by query
        if query:
            all_games = [g for g in all_games if query in g.get("name", "").lower()]
        
        # Sort
        if sort_method == "Top Rated":
            sorted_games = sorted(all_games, key=lambda g: g.get("year", 0) or 0, reverse=True)
        elif sort_method == "Random":
            random.shuffle(sorted_games)
        else:  # Most popular (default)
            sorted_games = all_games
        
        # Replace content in each PopGamesWidget tab
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if isinstance(widget, PopGamesWidget):
                # Get the games layout within the widget
                scroll = widget.findChild(QScrollArea)
                if scroll:
                    scroll_w = scroll.widget()
                    if scroll_w:
                        games_layout = scroll_w.layout()
                        if games_layout:
                            # Remove existing game cards
                            for j in range(games_layout.count() - 1, 1, -1):  # Keep title and spacer
                                item = games_layout.itemAt(j)
                                if item and item.widget() and isinstance(item.widget(), BrowseGameCard):
                                    item.widget().deleteLater()
                            
                            # Add filtered games
                            for game in sorted_games[:10]:
                                card = BrowseGameCard(game)
                                card.selected.connect(lambda g=game: self._on_game_selected(g))
                                games_layout.insertWidget(j - 1, card)
                
                # Update tab title to show match count on first system tab
                tab_title = self.tabs.tabText(i)
                if not tab_title.startswith("("):
                    self.tabs.setTabText(i, f"{tab_title} ({len(sorted_games)} results)")
        
        # Update the title label
        title = self.findChild(QLabel)
        if title and hasattr(title, 'text'):
            pass  # Keep title as is
        
        # Show message
        msg = QLabel(f"Found {len(sorted_games)} games")
        msg.setStyleSheet("QLabel { color: #888; font-size: 11px; margin-top: 8px; }")
        
        # Remove any previous search result labels
        for w in self.findChildren(QLabel):
            if w.styleSheet() and "Found" in w.styleSheet():
                w.deleteLater() or w.hide()
        
        layout = self.layout()
        layout.insertWidget(len(layout) - 1, msg)  # Before import button

    def _update_tabs(self):
        """Update tabs to show/hide based on configured emulators."""
        self._update_system_tabs()

    def _on_game_selected(self, game: dict):
        """Handle game selection."""
        if game not in self._selected_games:
            self._selected_games.append(game)
            self.import_btn.setEnabled(True)
            # Update button text to show count
            count = len(self._selected_games)
            self.import_btn.setText(f"Import {count} Game{'s' if count != 1 else ''} to Library")

    def _import_selected(self):
        """Import selected games to library."""
        if not self._selected_games:
            return
        
        count = len(self._selected_games)
        # Create GameEntry objects for each selected game
        from rose_gamelab.core.emulator import GameEntry
        import datetime
        
        for game in self._selected_games:
            game_id = f"browse_{game.get('name', 'unknown').replace(' ', '_').lower()}"
            entry = GameEntry(
                id=game_id,
                name=game.get("name", "Unknown"),
                system=game.get("system", "unknown"),
                path="",  # Not a real game
                metadata={
                    "source": "browse",
                    "imported": datetime.datetime.now().isoformat(),
                    "steam_id": game.get("steam_id", ""),
                }
            )
            # Add source
            self.config.add_source(entry.to_dict() if hasattr(entry, 'to_dict') else {"game": game})
        
        self._selected_games.clear()
        self.import_btn.setEnabled(False)
        self.import_btn.setText("Import to Library")
        self.close()


if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication
    
    class MockConfig:
        def get(self, key: str):
            return None
    
    app = QApplication(sys.argv)
    config = MockConfig()
    dialog = BrowseDialog(config)
    dialog.show()
    sys.exit(app.exec())
