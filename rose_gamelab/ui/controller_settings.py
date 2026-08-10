"""Controller settings panel."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QComboBox, QCheckBox, QLineEdit, QFormLayout,
    QScrollArea, QFileDialog, QApplication, QMessageBox, QWidget,
)

from rose_gamelab.config import Config
from rose_gamelab.core.controller import ControllerManager


class ControllerSettings(QDialog):
    """Controller configuration dialog — profile management, input profiles, auto-detect."""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.manager = ControllerManager(config)
        self.setWindowTitle("Controller Settings")
        self.resize(800, 500)
        self._build_ui()
        self._scan_controllers()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # Title
        title = QLabel("Controller Configuration")
        title.setStyleSheet("QLabel { font-size: 22px; font-weight: bold; color: #c0caf5; }")
        layout.addWidget(title)

        # Status
        status_layout = QHBoxLayout()
        self.status_label = QLabel("No controller detected. Connect a controller and click 'Scan'.")
        self.status_label.setStyleSheet("QLabel { color: #f7768e; font-size: 12px; }")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()

        scan_btn = QPushButton("Scan for Controllers")
        scan_btn.clicked.connect(self._scan_controllers)
        status_layout.addWidget(scan_btn)

        layout.addLayout(status_layout)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(12)

        # ── Connected Controllers ──
        controllers_group = QGroupBox("Connected Controllers")
        controllers_group.setStyleSheet("QGroupBox { font-weight: bold; color: #c0caf5; font-size: 13px; }")
        ctrl_layout = QVBoxLayout()
        self.controller_list = QLabel("No controllers connected")
        self.controller_list.setStyleSheet("QLabel { color: #8faa7a; }")
        ctrl_layout.addWidget(self.controller_list)
        controllers_group.setLayout(ctrl_layout)
        scroll_layout.addWidget(controllers_group)

        # ── Input Profile ──
        profile_group = QGroupBox("Input Profile")
        profile_group.setStyleSheet("QGroupBox { font-weight: bold; color: #c0caf5; font-size: 13px; }")
        profile_layout = QFormLayout()
        profile_layout.setSpacing(8)

        self.profile_combo = QComboBox()
        profile_keys = ["default", "autoconfigure"]
        for key in profile_keys:
            self.profile_combo.addItem(key.replace("_", " ").title())
        self.profile_combo.setCurrentIndex(0)
        profile_layout.addRow("Active Profile:", self.profile_combo)

        profile_group.setLayout(profile_layout)
        scroll_layout.addWidget(profile_group)

        # ── Auto-Configure ──
        autocfg_check = QCheckBox("Auto-configure controller on launch")
        autocfg_check.setChecked(self.config.controller.get("autoconfigure", True))
        scroll_layout.addWidget(autocfg_check)

        # Hotkey
        hotkey_group = QGroupBox("Hotkey Overlay")
        hotkey_group.setStyleSheet("QGroupBox { font-weight: bold; color: #888; font-size: 11px; }")
        hotkey_layout = QHBoxLayout()
        self.hotkey_input = QLineEdit(self.config.controller.get("hotkey_combo", "R2 + Select + Start"))
        self.hotkey_input.setPlaceholderText("Enter hotkey combination...")
        self.hotkey_input.setMinimumWidth(300)
        hotkey_layout.addWidget(self.hotkey_input)

        hotkey_group.setLayout(hotkey_layout)
        scroll_layout.addWidget(hotkey_group)

        # ── Save on Exit ──
        save_exit_check = QCheckBox("Save profile changes on exit")
        save_exit_check.setChecked(self.config.controller.get("save_on_exit", True))
        scroll_layout.addWidget(save_exit_check)

        scroll_layout.addStretch()

        scroll.setWidget(scroll_widget)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: #16161e; width: 8px; }
            QScrollBar::handle:vertical { background: #3d4c6d; border-radius: 4px; }
        """)
        layout.addWidget(scroll)

        # Bottom row
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()

        save_btn = QPushButton("Save")
        save_btn.setStyleSheet("QPushButton { background-color: #7aa2f7; color: #1a1b26; font-weight: bold; }")
        save_btn.clicked.connect(self._save_settings)
        bottom_layout.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        bottom_layout.addWidget(cancel_btn)

        layout.addLayout(bottom_layout)

    def _scan_controllers(self):
        """Scan for connected controllers."""
        controllers = self.manager.scan(force=True)
        if controllers:
            names = [c.name for c in controllers]
            self.status_label.setText(f"Detected {len(controllers)} controller(s)")
            self.status_label.setStyleSheet("QLabel { color: #9ece6a; font-size: 12px; }")
            display = "\n".join(f"  • {n}" for n in names)
            self.controller_list.setText(display)
            self.controller_list.setStyleSheet("QLabel { color: #9ece6a; font-size: 12px; }")
        else:
            self.status_label.setText("No controller found. Connect a controller and scan again.")
            self.status_label.setStyleSheet("QLabel { color: #f7768e; font-size: 12px; }")
            self.controller_list.setText("No controller detected")
            self.controller_list.setStyleSheet("QLabel { color: #f7768e; font-size: 12px; }")

    def _save_settings(self):
        """Save controller settings."""
        self.config.controller["input_profile"] = self.profile_combo.currentText().lower()
        self.config.controller["autoconfigure"] = True  # from checkbox
        self.config.controller["hotkey_combo"] = self.hotkey_input.text()
        self.config.save()
        self.close()
