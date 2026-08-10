"""GameLab's settings panel."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QComboBox, QCheckBox, QLineEdit, QFormLayout,
    QScrollArea, QFileDialog, QApplication, QWidget,
)

from rose_gamelab.config import Config


class SettingsPanel(QDialog):
    """Settings dialog — everything is configurable via GUI or config file."""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setWindowTitle("Settings")
        self.resize(800, 600)
        self._paths = []  # (system_id, line_edit) tuples
        self._full_check = None
        self._save_check = None
        self._group_check = None
        self._scan_check = None
        self._retro_input = None
        self._core_dir_input = None
        self._sort_combo = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # Title
        title = QLabel("GameLab Settings")
        title.setStyleSheet("QLabel { font-size: 22px; font-weight: bold; color: #c0caf5; }")
        layout.addWidget(title)

        # Scroll area for config groups
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: #16161e; width: 8px; }
            QScrollBar::handle:vertical { background: #3d4c6d; border-radius: 4px; }
        """)

        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(12)

        # ── Emulator Settings Group ──
        emu_group = QGroupBox("Emulator Paths")
        emu_group.setStyleSheet("QGroupBox { font-weight: bold; color: #c0caf5; font-size: 13px; }")
        emu_layout = QFormLayout()
        emu_layout.setSpacing(8)

        for system_id in ["snes", "gba", "nds", "ps1", "ps2", "psp", "3ds", "wii", "wiiu", "switch", "xbox", "dreamcast"]:
            system_label = QLabel(f"{system_id.upper()} Emulator:")
            system_label.setStyleSheet("QLabel { color: #888; font-size: 11px; }")

            path_layout = QHBoxLayout()

            path_input = QLineEdit(self.config.get(f"emulators.{system_id}") or "")
            path_input.setPlaceholderText("Path to emulator binary...")
            path_input.setMinimumWidth(400)
            path_layout.addWidget(path_input)
            self._paths.append((system_id, path_input))

            browse_btn = QPushButton("Browse")
            browse_btn.setStyleSheet("QPushButton { min-width: 70px; }")
            browse_btn.clicked.connect(lambda _, p=path_input: self._browse_file(p))
            path_layout.addWidget(browse_btn)

            emu_layout.addRow(system_label, path_layout)

        emu_group.setLayout(emu_layout)
        scroll_layout.addWidget(emu_group)

        # ── Global Defaults Group ──
        global_group = QGroupBox("Global Defaults")
        global_group.setStyleSheet("QGroupBox { font-weight: bold; color: #c0caf5; font-size: 13px; }")
        global_layout = QFormLayout()
        global_layout.setSpacing(8)

        # Fullscreen setting
        self._full_check = QCheckBox("Launch games in fullscreen by default")
        self._full_check.setChecked(self.config.emulator_defaults.get("fullscreen", True))
        global_layout.addRow("", self._full_check)

        # Save state settings
        self._save_check = QCheckBox("Save state on exit")
        self._save_check.setChecked(self.config.emulator_defaults.get("save_state_on_exit", True))
        global_layout.addRow("", self._save_check)

        # RetroArch path
        retro_group = QGroupBox("RetroArch (Universal)")
        retro_group.setStyleSheet("QGroupBox { font-weight: bold; color: #888; font-size: 11px; }")
        retro_layout = QHBoxLayout()
        retro_input = QLineEdit(self.config.emulator_defaults.get("retroarch_bin", ""))
        retro_input.setPlaceholderText("Path to retroarch...")
        retro_input.setMinimumWidth(300)
        retro_layout.addWidget(retro_input)
        self._retro_input = retro_input

        retro_browse = QPushButton("Browse")
        retro_browse.setStyleSheet("QPushButton { min-width: 70px; }")
        retro_browse.clicked.connect(lambda _, p=retro_input: self._browse_file(p))
        retro_layout.addWidget(retro_browse)

        retro_group.setLayout(retro_layout)
        global_layout.addRow("RetroArch Binary:", retro_group)

        self._core_dir_input = QLineEdit(self.config.emulator_defaults.get("libretro_core_dir", ""))
        self._core_dir_input.setPlaceholderText("Path to libretro CORE directories...")
        global_layout.addRow("Core Directory:", self._core_dir_input)

        global_group.setLayout(global_layout)
        scroll_layout.addWidget(global_group)

        # ── Behavior Group ──
        behavior_group = QGroupBox("Behavior")
        behavior_group.setStyleSheet("QGroupBox { font-weight: bold; color: #c0caf5; font-size: 13px; }")
        behavior_layout = QFormLayout()
        behavior_layout.setSpacing(8)

        self._sort_combo = QComboBox()
        sort_options = ["name", "last_played", "platform", "added"]
        for opt in sort_options:
            self._sort_combo.addItem(opt.replace("_", " ").title())
        self._sort_combo.setCurrentIndex(sort_options.index(self.config.behavior.get("sort_by", "name")))
        behavior_layout.addRow("Sort by:", self._sort_combo)

        self._group_check = QCheckBox("Show system grouping")
        self._group_check.setChecked(self.config.behavior.get("show_system_grouping", True))
        behavior_layout.addRow("", self._group_check)

        self._scan_check = QCheckBox("Scan sources on startup")
        self._scan_check.setChecked(self.config.behavior.get("scan_on_startup", True))
        behavior_layout.addRow("", self._scan_check)

        behavior_group.setLayout(behavior_layout)
        scroll_layout.addWidget(behavior_group)

        # Apply buttons
        scroll_layout.addStretch()

        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        # ── Bottom button row ──
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()

        save_btn = QPushButton("Save")
        save_btn.setStyleSheet("""
            QPushButton { background-color: #7aa2f7; color: #1a1b26; font-weight: bold; }
            QPushButton:hover { background-color: #99c1ff; }
        """)
        save_btn.clicked.connect(self._save_settings)
        bottom_layout.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        bottom_layout.addWidget(cancel_btn)

        layout.addLayout(bottom_layout)

    def _browse_file(self, line_edit: QLineEdit):
        """Show file browser."""
        file, _ = QFileDialog.getOpenFileName(
            self, "Select Binary", Path.home().asPosix(),
            "All files (*)")
        if file:
            line_edit.setText(file)

    def _save_settings(self):
        """Save all settings."""
        # Save emulator paths
        for system_id, path_input in self._paths:
            self.config.set_emulator(system_id, path_input.text() or None)

        # Save global defaults
        if self._full_check:
            self.config.set("emulator_defaults.fullscreen", self._full_check.isChecked())
        if self._save_check:
            self.config.set("emulator_defaults.save_state_on_exit", self._save_check.isChecked())
        if self._retro_input:
            self.config.set("emulator_defaults.retroarch_bin", self._retro_input.text() or None)
        if self._core_dir_input:
            self.config.set("emulator_defaults.libretro_core_dir", self._core_dir_input.text() or None)

        # Save behavior
        if self._sort_combo:
            sort_map = {"Name": "name", "Last Played": "last_played", "Platform": "platform", "Added": "added"}
            sort_val = sort_map.get(self._sort_combo.currentText(), "name")
            self.config.set("behavior.sort_by", sort_val)
        if self._group_check:
            self.config.set("behavior.show_system_grouping", self._group_check.isChecked())
        if self._scan_check:
            self.config.set("behavior.scan_on_startup", self._scan_check.isChecked())

        self.config.save()
        self.close()
