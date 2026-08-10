"""Theme picker dialog — select and customize themes."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QGroupBox, QFormLayout,
    QColorDialog, QWidget, QScrollArea,
    QMessageBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from rose_gamelab.config import Config
from rose_gamelab.ui.themes import ThemeManager, THEME_PRESETS

logger = logging.getLogger(__name__)


class ThemePickerDialog(QDialog):
    """Dialog to pick a theme and customize colors."""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.manager = ThemeManager()
        self.setWindowTitle("Theme Settings")
        self.resize(700, 600)
        self._current_theme = config.theme or "btop++"
        self._custom_colors: dict[str, str] = dict(config.colors or {})
        self._color_buttons: dict[str, QPushButton] = {}
        self._build_ui()
        self._load_current_theme()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # Title
        title = QLabel("Theme Settings")
        title.setStyleSheet("QLabel { font-size: 20px; font-weight: bold; color: #c0caf5; }")
        layout.addWidget(title)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(12)

        # ── Theme List ──
        preset_group = QGroupBox("Preset Themes")
        preset_group.setStyleSheet("QGroupBox { font-weight: bold; color: #c0caf5; font-size: 13px; }")
        preset_layout = QVBoxLayout()

        self.theme_list = QListWidget()
        self.theme_list.setStyleSheet("""
            QListWidget {
                background-color: #16161e;
                border: 1px solid #3d4c6d;
                border-radius: 6px;
                padding: 4px;
            }
            QListWidget::item {
                padding: 8px;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #292e42;
                color: #c0caf5;
            }
            QListWidget::item:hover {
                background-color: #3d4c6d;
            }
        """)

        for theme_name in sorted(THEME_PRESETS.keys()):
            item = QListWidgetItem(theme_name)
            item.setData(Qt.ItemDataRole.UserRole, theme_name)
            if theme_name == self._current_theme:
                item.setSelected(True)
            self.theme_list.addItem(item)

        self.theme_list.currentItemChanged.connect(self._on_theme_changed)
        preset_layout.addWidget(self.theme_list)
        preset_group.setLayout(preset_layout)
        scroll_layout.addWidget(preset_group)

        # ── Preview ──
        preview_group = QGroupBox("Preview")
        preview_group.setStyleSheet("QGroupBox { font-weight: bold; color: #c0caf5; font-size: 13px; }")
        preview_layout = QVBoxLayout()

        self.preview_label = QLabel(
            "This is a preview of the selected theme. "
            "Colors can be customized below."
        )
        self.preview_label.setStyleSheet("QLabel { padding: 12px; }")
        preview_layout.addWidget(self.preview_label)
        preview_group.setLayout(preview_layout)
        scroll_layout.addWidget(preview_group)

        # ── Color Customization ──
        color_group = QGroupBox("Custom Colors")
        color_group.setStyleSheet("QGroupBox { font-weight: bold; color: #c0caf5; font-size: 13px; }")
        color_layout = QFormLayout()
        color_layout.setSpacing(8)

        for color_name in ["background", "surface", "panel", "accent", "text", "text_dim",
                          "success", "warning", "error", "highlight", "border"]:
            btn = QPushButton(f"Change {color_name.replace('_', ' ').title()}")
            btn.setFixedHeight(30)
            btn.clicked.connect(lambda checked=False, name=color_name: self._pick_color(name))
            self._color_buttons[color_name] = btn
            color_layout.addRow(f"{color_name.replace('_', ' ').title()}:", btn)

        color_group.setLayout(color_layout)
        scroll_layout.addWidget(color_group)

        scroll_layout.addStretch()

        scroll.setWidget(scroll_widget)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: #16161e; width: 8px; }
            QScrollBar::handle:vertical { background: #3d4c6d; border-radius: 4px; }
        """)
        layout.addWidget(scroll)

        # ── Buttons ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        apply_btn = QPushButton("Apply Theme")
        apply_btn.setStyleSheet("QPushButton { background-color: #9ece6a; color: #1a1b26; font-weight: bold; }")
        apply_btn.clicked.connect(self._apply_theme)
        btn_layout.addWidget(apply_btn)

        save_btn = QPushButton("Save & Close")
        save_btn.setStyleSheet("QPushButton { background-color: #7aa2f7; color: #1a1b26; font-weight: bold; }")
        save_btn.clicked.connect(self._save_settings)
        btn_layout.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _load_current_theme(self) -> None:
        """Apply current theme selection to UI."""
        theme_name = self._current_theme
        colors = self._get_current_colors()
        
        # Update preview
        if hasattr(self, 'manager'):
            try:
                self.manager.set(theme_name)
                css = self.manager.get_css()
                self.preview_label.setStyleSheet(css)
            except Exception as e:
                logger.warning(f"Failed to preview theme: {e}")
        
        # Set colors on buttons
        for color_name, btn in self._color_buttons.items():
            color_str = colors.get(color_name, "#000000")
            self._color_buttons[color_name].setStyleSheet(
                f"QPushButton {{ background-color: {color_str}; color: #c0caf5; "
                f"border: 1px solid #3d4c6d; border-radius: 4px; }}"
            )

    def _get_current_colors(self) -> dict[str, str]:
        """Get current theme colors."""
        try:
            self.manager.set(self._current_theme)
            theme = self.manager.active
            if theme:
                return dict(theme.colors)
        except Exception:
            pass
        # Fallback to preset
        preset_fn = THEME_PRESETS.get(self._current_theme)
        if preset_fn:
            return preset_fn()
        return {"background": "#1a1b26", "surface": "#16161e", "panel": "#292e42", "accent": "#7aa2f7",
                "text": "#c0caf5", "text_dim": "#565a78", "success": "#9ece6a", "warning": "#e0af68",
                "error": "#f7768e", "highlight": "#292e42", "border": "#3d4c6d"}

    def _on_theme_changed(self, current: Optional[QListWidgetItem],
                         previous: Optional[QListWidgetItem]) -> None:
        """Handle theme selection change."""
        if current:
            theme_name = current.data(Qt.ItemDataRole.UserRole)
            self._current_theme = theme_name
            self._load_current_theme()

    def _pick_color(self, color_name: str) -> None:
        """Open color picker for a color."""
        current_color = self._color_buttons[color_name].palette().color(
            QColor.ColorRole.Window
        )
        
        color, ok = QColorDialog.getColor(current_color, self, 
                                         f"Pick {color_name.replace('_', ' ').title()}")
        
        if ok and color.isValid():
            # Update button color
            self._color_buttons[color_name].setStyleSheet(
                f"QPushButton {{ background-color: {color.name()}; color: #c0caf5; "
                f"border: 1px solid #3d4c6d; border-radius: 4px; }}"
            )
            self._custom_colors[color_name] = color.name()

    def _apply_theme(self) -> None:
        """Apply the selected theme."""
        self.config.theme = self._current_theme
        if self._custom_colors:
            base_colors = self._get_current_colors()
            base_colors.update(self._custom_colors)
        else:
            base_colors = self._get_current_colors()
        self.config.colors.update(self._custom_colors)
        self.manager.set(self._current_theme)
        css = self.manager.get_css()
        self.setStyleSheet(css)
        QMessageBox.information(self, "Theme Applied", f"Applied theme: {self._current_theme}")

    def _save_settings(self) -> None:
        """Save all settings."""
        self.config.theme = self._current_theme
        if self._custom_colors:
            base_colors = self._get_current_colors()
            base_colors.update(self._custom_colors)
            for name, color in self._custom_colors.items():
                self.config.colors[name] = color
        self.config.save()
        QMessageBox.information(self, "Settings Saved", "Theme settings saved successfully.")
        self.close()
