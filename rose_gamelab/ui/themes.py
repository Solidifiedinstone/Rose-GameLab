"""Heavy theme system with customizable presets."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable


# ── Color Pools (btop-inspired + btop++ extensions) ──


def _btop() -> dict[str, str]:
    """Default btop++ theme."""
    return {
        "background": "#1a1b26",
        "surface": "#16161e",
        "panel": "#1e2030",
        "accent": "#7aa2f7",
        "text": "#c0caf5",
        "text_dim": "#565a78",
        "success": "#9ece6a",
        "warning": "#e0af68",
        "error": "#f7768e",
        "highlight": "#292e42",
        "border": "#292e42",
    }


def _btop_light() -> dict[str, str]:
    """Light variant of btop."""
    return {
        "background": "#24283b",
        "surface": "#1f2335",
        "panel": "#1f2335",
        "accent": "#7dcfff",
        "text": "#cbd6e6",
        "text_dim": "#636a8a",
        "success": "#98c379",
        "warning": "#d19a66",
        "error": "#e06c75",
        "highlight": "#292e42",
        "border": "#292e42",
    }


def _btop_nord() -> dict[str, str]:
    """Nord-inspired theme."""
    return {
        "background": "#2e3440",
        "surface": "#3b4252",
        "panel": "#3b4252",
        "accent": "#81a1c1",
        "text": "#d8dee9",
        "text_dim": "#4c566a",
        "success": "#a3be8c",
        "warning": "#ebcb8b",
        "error": "#bf616a",
        "highlight": "#434e5e",
        "border": "#434e5e",
    }


def _btop_gruvbox() -> dict[str, str]:
    """Gruvbox Dark hard."""
    return {
        "background": "#282828",
        "surface": "#3c3836",
        "panel": "#3c3836",
        "accent": "#fabd2f",
        "text": "#ebdbb2",
        "text_dim": "#928374",
        "success": "#b8bb26",
        "warning": "#d65d0e",
        "error": "#cc2424",
        "highlight": "#504945",
        "border": "#504945",
    }


def _btop_rose_pine() -> dict[str, str]:
    """Rosé Pine theme."""
    return {
        "background": "#191724",
        "surface": "#1f1d2e",
        "panel": "#1f1d2e",
        "accent": "#c4a7e7",
        "text": "#e0def4",
        "text_dim": "#6e6a86",
        "success": "#9ccfd8",
        "warning": "#f6c177",
        "error": "#e46277",
        "highlight": "#26233a",
        "border": "#26233a",
    }


def _btop_moonlight() -> dict[str, str]:
    """Moonlight (vscode) theme."""
    return {
        "background": "#1e1e2e",
        "surface": "#2a2a3a",
        "panel": "#2a2a3a",
        "accent": "#89b4fa",
        "text": "#cdd6f4",
        "text_dim": "#585b70",
        "success": "#a6e3a1",
        "warning": "#f9e2af",
        "error": "#f38ba8",
        "highlight": "#313244",
        "border": "#313244",
    }


def _btop_tokyo_night() -> dict[str, str]:
    """Tokyo Night theme."""
    return {
        "background": "#1a1b26",
        "surface": "#1f2335",
        "panel": "#1f2335",
        "accent": "#7aa2f7",
        "text": "#c0caf5",
        "text_dim": "#565a78",
        "success": "#9ece6a",
        "warning": "#e0af68",
        "error": "#f7768e",
        "highlight": "#292e42",
        "border": "#292e42",
    }


def _btop_solarized_dark() -> dict[str, str]:
    """Solarized Dark theme."""
    return {
        "background": "#002b36",
        "surface": "#073642",
        "panel": "#073642",
        "accent": "#268bd2",
        "text": "#839496",
        "text_dim": "#073642",
        "success": "#859900",
        "warning": "#b58900",
        "error": "#dc322f",
        "highlight": "#073642",
        "border": "#073642",
    }


def _catppuccin_mocha() -> dict[str, str]:
    """Catppuccin Mocha."""
    return {
        "background": "#1e1d2b",
        "surface": "#181825",
        "panel": "#181825",
        "accent": "#cba6f7",
        "text": "#cdd6f4",
        "text_dim": "#585b70",
        "success": "#a6e3a1",
        "warning": "#f9e2af",
        "error": "#f38ba8",
        "highlight": "#312b44",
        "border": "#312b44",
    }


def _one_monochrome() -> dict[str, str]:
    """One Monochrome dark."""
    return {
        "background": "#27293a",
        "surface": "#35374a",
        "panel": "#35374a",
        "accent": "#61afee",
        "text": "#c5c8c6",
        "text_dim": "#6c7086",
        "success": "#6ec385",
        "warning": "#c18437",
        "error": "#c45657",
        "highlight": "#3a3c50",
        "border": "#3a3c50",
    }


THEME_PRESETS = {
    "btop++": _btop,
    "btop++ light": _btop_light,
    "btop++ Nord": _btop_nord,
    "btop++ Gruvbox": _btop_gruvbox,
    "btop++ Rosé Pine": _btop_rose_pine,
    "btop++ Moonlight": _btop_moonlight,
    "btop++ Tokyo Night": _btop_tokyo_night,
    "btop++ Solarized Dark": _btop_solarized_dark,
    "Catppuccin Mocha": _catppuccin_mocha,
    "One Monochrome": _one_monochrome,
    "Deep OLED Dark": lambda: {
        "background": "#000000",
        "surface": "#0a0a0a",
        "panel": "#111111",
        "accent": "#60a5fa",
        "text": "#e2e2e2",
        "text_dim": "#666666",
        "success": "#4ade80",
        "warning": "#fbbf24",
        "error": "#ef4444",
        "highlight": "#1a1a1a",
        "border": "#222222",
    },
    "OLED Midnight": lambda: {
        "background": "#000000",
        "surface": "#0d0d0d",
        "panel": "#1a1a1a",
        "accent": "#8b5cf6",
        "text": "#d4d4d4",
        "text_dim": "#555555",
        "success": "#22c55e",
        "warning": "#eab308",
        "error": "#f43f5e",
        "highlight": "#141414",
        "border": "#1e1e1e",
    },
    "OLED Amber": lambda: {
        "background": "#000000",
        "surface": "#0a0800",
        "panel": "#151105",
        "accent": "#fbbf24",
        "text": "#f0e6d3",
        "text_dim": "#776d55",
        "success": "#84cc16",
        "warning": "#f59e0b",
        "error": "#ef4444",
        "highlight": "#12100a",
        "border": "#1a1a0a",
    },
}


# ── Theme class ──


@dataclass
class Theme:
    """Customizable game theme with preset support."""

    name: str
    colors: dict[str, str]
    is_custom: bool = False

    def __post_init__(self):
        # Validate colors
        required = [
            "background", "surface", "panel", "accent", "text",
            "text_dim", "success", "warning", "error", "highlight", "border",
        ]
        for color in required:
            if color not in self.colors:
                raise ValueError(f"Missing theme color: {color}")

    def get(self, key: str) -> str:
        return self.colors.get(key, "#000000")

    @property
    def css(self) -> str:
        """Generate QSS stylesheet from theme."""
        c = self.colors
        return f"""
            QMainWindow {{
                background-color: {c.get('background', '#1a1b26')};
            }}
            QWidget {{
                background-color: {c.get('background', '#1a1b26')};
                color: {c.get('text', '#c0caf5')};
                font-family: "Inter", "Segoe UI", "Roboto", sans-serif;
                font-size: 13px;
            }}
            QPushButton {{
                background-color: {c.get('panel', '#292e42')};
                color: {c.get('accent', '#7aa2f7')};
                border: 1px solid {c.get('border', '#3d4c6d')};
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {c.get('highlight', '#3d4c6d')};
                border-color: {c.get('accent', '#7aa2f7')} !important;
            }}
            QPushButton:pressed {{
                background-color: {c.get('accent', '#7aa2f7')};
                color: {c.get('background', '#1a1b26')};
            }}
            QMenuBar {{
                background-color: {c.get('surface', '#16161e')};
                color: {c.get('text', '#c0caf5')};
                padding: 4px;
                border-bottom: 1px solid {c.get('border', '#292e42')};
            }}
            QMenuBar::item:selected {{
                background-color: {c.get('highlight', '#292e42')};
            }}
            QMenu {{
                background-color: {c.get('background', '#1a1b26')};
                color: {c.get('text', '#c0caf5')};
                border: 1px solid {c.get('border', '#3d4c6d')};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item:selected {{
                background-color: {c.get('highlight', '#292e42')};
            }}
            QStatusBar {{
                background-color: {c.get('surface', '#16161e')};
                color: {c.get('text_dim', '#565a78')};
                border-top: 1px solid {c.get('border', '#292e42')};
            }}
            QScrollBar:vertical {{
                background: {c.get('surface', '#16161e')};
                width: 8px;
                margin: 0px;
            }}
            QScrollBar::handle:vertical {{
                background: {c.get('border', '#3d4c6d')};
                border-radius: 4px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QMessageBox {{
                background-color: {c.get('background', '#1a1b26')};
                color: {c.get('text', '#c0caf5')};
            }}
            QLabel {{
                color: {c.get('text', '#c0caf5')};
            }}
            QLineEdit, QTextEdit, QComboBox {{
                background-color: {c.get('surface', '#16161e')};
                border: 1px solid {c.get('border', '#3d4c6d')};
                border-radius: 4px;
                padding: 4px;
                color: {c.get('text', '#c0caf5')};
            }}
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{
                border-color: {c.get('accent', '#7aa2f7')};
            }}
            QGroupBox {{
                border: 1px solid {c.get('border', '#3d4c6d')};
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 16px;
                font-weight: bold;
                color: {c.get('text', '#c0caf5')};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px;
                color: {c.get('accent', '#7aa2f7')};
            }}
            QTabBar::tab {{
                background-color: {c.get('surface', '#16161e')};
                color: {c.get('text_dim', '#565a78')};
                padding: 8px 16px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }}
            QTabBar::tab:selected {{
                background-color: {c.get('panel', '#292e42')};
                color: {c.get('text', '#c0caf5')};
            }}
            QTabWidget::pane {{
                border: 1px solid {c.get('border', '#3d4c6d')};
                border-radius: 0 6px 6px 6px;
            }}
            QScrollBar:horizontal {{
                background: {c.get('surface', '#16161e')};
                height: 8px;
                margin: 0px;
            }}
            QScrollBar::handle:horizontal {{
                background: {c.get('border', '#3d4c6d')};
                border-radius: 4px;
            }}
            QSlider::groove:horizontal {{
                border: 1px solid {c.get('border', '#3d4c6d')};
                height: 4px;
                background: {c.get('highlight', '#292e42')};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {c.get('accent', '#7aa2f7')};
                border: 1px solid {c.get('border', '#3d4c6d')};
                width: 12px;
                height: 12px;
                margin: -4px 0;
                border-radius: 6px;
            }}
            QTableView {{
                background-color: {c.get('surface', '#16161e')};
                gridline-color: {c.get('border', '#292e42')};
                border: 1px solid {c.get('border', '#3d4c6d')};
                border-radius: 6px;
                color: {c.get('text', '#c0caf5')};
            }}
            QHeaderView::section {{
                background-color: {c.get('panel', '#292e42')};
                color: {c.get('text', '#c0caf5')};
                padding: 4px;
                border: 1px solid {c.get('border', '#3d4c6d')};
                font-weight: bold;
            }}
            QComboBox {{
                background-color: {c.get('surface', '#16161e')};
                border: 1px solid {c.get('border', '#3d4c6d')};
                border-radius: 4px;
                padding: 4px 8px;
                color: {c.get('text', '#c0caf5')};
            }}
            QComboBox::drop-down {{
                border: none;
                padding-right: 8px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {c.get('background', '#1a1b26')};
                color: {c.get('text', '#c0caf5')};
                selection-background-color: {c.get('accent', '#7aa2f7')};
            }}
            QProgressBar {{
                border: 1px solid {c.get('border', '#3d4c6d')};
                border-radius: 4px;
                text-align: center;
                background-color: {c.get('highlight', '#292e42')};
            }}
            QProgressBar::chunk {{
                background-color: {c.get('accent', '#7aa2f7')};
                border-radius: 3px;
            }}
            QTreeView, QListView, QTableView {{
                background-color: {c.get('surface', '#16161e')};
                color: {c.get('text', '#c0caf5')};
                selection-background-color: {c.get('accent', '#7aa2f7')};
                selection-color: {c.get('background', '#1a1b26')};
                border: 1px solid {c.get('border', '#3d4c6d')};
                border-radius: 6px;
            }}
            QFileDialog {{
                background-color: {c.get('background', '#1a1b26')};
                color: {c.get('text', '#c0caf5')};
            }}
            QSplitter::handle {{
                background-color: {c.get('border', '#292e42')};
                width: 2px;
            }}
        """


# ── Theme Manager ──


class ThemeManager:
    """Manage themes for the app."""

    def __init__(self) -> None:
        self._active: Theme | None = None
        self._presets: dict[str, Theme] = {}
        self._custom: dict[str, dict[str, str]] = {}

        # Load presets
        for name, theme_fn in THEME_PRESETS.items():
            colors = theme_fn()
            self._presets[name] = Theme(name=name, colors=colors)
            self._custom[name] = colors  # also allow customization

        self._active = self._presets.get("btop++")

    @property
    def active(self) -> Theme | None:
        return self._active

    @property
    def presets(self) -> dict[str, Theme]:
        return dict(self._presets)

    @property
    def available_names(self) -> list[str]:
        return list(self._presets.keys())

    def set(self, name: str) -> None:
        if name in self._presets:
            self._active = self._presets[name]
        else:
            raise ValueError(f"Unknown theme: {name}")

    def create_custom(self, name: str, colors: dict[str, str]) -> Theme:
        theme = Theme(name=name, colors=colors, is_custom=True)
        self._presets[name] = theme
        self._custom[name] = colors
        self._active = theme
        return theme

    def update_colors(self, name: str, color_updates: dict[str, str]) -> None:
        """Update colors of a theme by name."""
        if name not in self._custom:
            raise ValueError(f"Unknown theme to update: {name}")

        self._custom[name].update(color_updates)
        # Rebuild the theme
        self._presets[name] = Theme(name=name, colors=dict(self._custom[name]))
        if self._active and self._active.name == name:
            self._active = self._presets[name]

    def get_css(self) -> str:
        """Get CSS for the current active theme."""
        if self._active is None:
            return ""
        return self._active.css
