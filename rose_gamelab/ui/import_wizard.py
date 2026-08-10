"""Multi-step import wizard for adding games to GameLab."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFileDialog, QGroupBox, QComboBox, QListWidget, QListWidgetItem,
    QWizard, QWizardPage, QLineEdit, QRadioButton, QCheckBox,
    QMessageBox, QWidget, QFormLayout,
)

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import SYSTEMS
from rose_gamelab.sources.base import SourceDef


# ── Page 1: Select import type ─────────────────────────────

class ImportTypePage(QWizardPage):
    """First page — choose ROM folder or launcher import."""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setTitle("Import Type")
        self.setSubTitle("How would you like to add games?")
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ROM folder option
        rom_radio = QRadioButton("&ROM Folder")
        rom_radio.setChecked(True)
        rom_radio.setToolTip("Add a folder containing ROM files")

        # Launcher option
        store_radio = QRadioButton("&Launcher Game Library")
        store_radio.setToolTip("Import games from Steam, Heroic, or GOG")

        layout.addWidget(rom_radio)
        layout.addWidget(store_radio)

        self.rom_radio = rom_radio
        self.store_radio = store_radio

        layout.addStretch()

    def isComplete(self) -> bool:
        return True  # Always can proceed


# ── Page 2: Select folder (ROMs) ───────────────────────────

class FolderSelectPage(QWizardPage):
    """Page 2 — select the folder containing ROMs."""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setTitle("Select Folder")
        self.setSubTitle("Choose the folder containing your ROM files.")
        self._build_ui()
        self.selected_path = ""
        self.selected_system = ""
        self.selected_extensions = []

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # System selector
        system_label = QLabel("System:")
        layout.addWidget(system_label)

        self.system_combo = QComboBox()
        self.system_combo.setMinimumWidth(300)
        self.system_combo.setEditable(False)
        for system in sorted(SYSTEMS.values(), key=lambda s: s.name):
            self.system_combo.addItem(f"{system.icon} {system.name}", system.id)
        layout.addWidget(self.system_combo)

        # Folder selector
        folder_layout = QHBoxLayout()
        folder_label = QLabel("ROM Folder:")
        folder_layout.addWidget(folder_label)

        self.folder_input = QLineEdit()
        self.folder_input.setMinimumWidth(400)
        self.folder_input.setPlaceholderText("Select a folder...")
        folder_layout.addWidget(self.folder_input)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_folder)
        folder_layout.addWidget(browse_btn)

        layout.addLayout(folder_layout)

        # Extensions
        ext_label = QLabel("File Extensions:")
        ext_label.setEnabled(False)
        layout.addWidget(ext_label)

        self.ext_list = QListWidget()
        self.ext_list.setMaximumHeight(150)
        layout.addWidget(self.ext_list)

        layout.addStretch()

        # Connect signals
        self.system_combo.currentIndexChanged.connect(self._on_system_changed)

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select ROM Folder", Path.home().asPosix())
        if folder:
            self.folder_input.setText(folder)

    def _on_system_changed(self, index):
        """Update extensions list when system changes."""
        self.ext_list.clear()
        system_id = self.system_combo.itemData(index)
        system = SYSTEMS.get(system_id)
        if system and system.rom_extensions:
            for ext in system.rom_extensions:
                self.ext_list.addItem(ext)

    def isComplete(self) -> bool:
        path = self.folder_input.text().strip()
        system = self.system_combo.currentData()
        if not path:
            return False
        if not Path(path).is_dir():
            return False
        if not system:
            return False
        return True

    def validate_page(self) -> bool:
        path = self.folder_input.text().strip()
        if not Path(path).is_dir():
            QMessageBox.warning(self, "Validation Error", "Please select a valid folder.")
            return False
        return True


# ── Page 3: Select type (ROM or Launcher) ──────────────────

class LauncherTypePage(QWizardPage):
    """Choose which launcher to import from."""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setTitle("Launcher Type")
        self.setSubTitle("Which launcher contains your games?")
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Radio buttons for launcher types
        self.steam_radio = QRadioButton("Steam")
        self.steam_radio.setToolTip("Import installed Steam games")

        self.heroic_radio = QRadioButton("Heroic Games Launcher")
        self.heroic_radio.setToolTip("Import installed Heroic games")

        self.gog_radio = QRadioButton("GOG Galaxy")
        self.gog_radio.setToolTip("Import installed GOG games")

        layout.addWidget(self.steam_radio)
        layout.addWidget(self.heroic_radio)
        layout.addWidget(self.gog_radio)

        self.steam_radio.setChecked(True)
        self._selected_type = "steam"

        layout.addStretch()

    def isComplete(self) -> bool:
        return True

    def get_selected_type(self) -> str:
        if self.heroic_radio.isChecked():
            return "heroic"
        elif self.gog_radio.isChecked():
            return "gog"
        return "steam"


# ── Page 4: Confirm & Import ────────────────────────────────

class ImportConfirmPage(QWizardPage):
    """Final page — review settings and import."""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setTitle("Import Games")
        self.setSubTitle("Review your settings before importing.")
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("QLabel { font-size: 13px; color: #9ece6a; }")
        layout.addWidget(self.info_label)

        # Auto-scan checkbox
        scan_check = QCheckBox("&Scan source on startup")
        scan_check.setChecked(True)
        self.scan_check = scan_check
        layout.addWidget(scan_check)

        # Import button
        import_btn = QPushButton("Import &All Games")
        import_btn.setStyleSheet("QPushButton { background-color: #7aa2f7; color: #1a1b26; font-weight: bold; }")
        import_btn.clicked.connect(self._on_import)
        layout.addWidget(import_btn)

        layout.addStretch()

    def _on_import(self):
        """Run the import and close the wizard."""
        # Get parent wizard
        wizard = self.wizard()
        
        # Determine import source
        folder_page = wizard.pageByName("folder_select_page") if hasattr(wizard, 'pageByName') else None
        
        if folder_page and folder_page.folder_input.text().strip():
            path = folder_page.folder_input.text().strip()
            system_id = folder_page.system_combo.currentData()
            extensions = [folder_page.ext_list.item(i).text() for i in range(folder_page.ext_list.count())]
            source_type = "rom"
            name = Path(path).name
        elif hasattr(wizard, 'pageByName') and wizard.pageByName("launcher_type_page"):
            launcher_page = wizard.pageByName("launcher_type_page")
            launcher_type = launcher_page.get_selected_type()
            path = None
            system_id = "pc"
            extensions = []
            source_type = "launcher"
            name = f"{launcher_type.title()} Games"
        else:
            QMessageBox.critical(self, "Import Error", "Invalid configuration.")
            return

        # Create source definition
        source = SourceDef(
            id=f"source_{Path(path or name).stem}_{len(self.config.sources)}",
            name=name,
            type=source_type,
            path=path,
            system=system_id,
            extensions=extensions,
            recursive=True,
        )

        # Save source
        self.config.add_source(source)

        # Import games
        if source_type == "rom" and path:
            try:
                count = 0
                path_obj = Path(path)
                for f in path_obj.rglob("*") if source.recursive else path_obj.iterdir():
                    if f.is_file() and (not extensions or f.suffix.lower() in extensions):
                        count += 1
                self.info_label.setText(f"Found {count} files in {name}. Import to your library to add them.")
            except Exception as e:
                self.info_label.setText(f"Scanned source but encountered error: {e}")
        elif source_type == "launcher":
            try:
                from rose_gamelab.sources.steam import SteamProvider
                from rose_gamelab.sources.heroic import HeroicProvider
                from rose_gamelab.sources.gog import GOGProvider
                
                provider = None
                if launcher_type == "steam":
                    provider = SteamProvider()
                elif launcher_type == "heroic":
                    provider = HeroicProvider()
                elif launcher_type == "gog":
                    provider = GOGProvider()
                
                if provider:
                    games = provider.discover()
                    self.info_label.setText(f"Found {len(games)} games from {name}. They are now available in your library.")
                else:
                    self.info_label.setText(f"{name} games sourced successfully.")
            except Exception as e:
                self.info_label.setText(f"{name} sourced. (Could not auto-import: {e})")

        wizard.close()


class ImportRomWizard(QWizard):
    """Full import wizard for ROM folders."""

    PAGE_ROM_TYPE = 1
    PAGE_FOLDER = 2
    PAGE_CONFIRM = 3

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setWindowTitle("Import ROMs")
        self.resize(600, 500)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        # self.setOption(QWizard.WizardOption.NoBackButtonWithoutNextButton)  # Removed - deprecated in Qt6
        self._build_wizard()

    def _build_wizard(self):
        self.addPage(ImportTypePage(self.config))  # Page 0
        folder_page = FolderSelectPage(self.config)
        folder_page.setObjectName("folder_select_page")
        self.addPage(folder_page)  # Page 1
        confirm_page = ImportConfirmPage(self.config)  # Page 2
        self.addPage(confirm_page)  # Page 3


class ImportStoreWizard(QWizard):
    """Full import wizard for launcher games (Steam, Heroic, GOG)."""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setWindowTitle("Import Launcher Games")
        self.resize(600, 400)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        # self.setOption(QWizard.WizardOption.NoBackButtonWithoutNextButton)  # Removed - deprecated in Qt6
        self._build_wizard()

    def _build_wizard(self):
        self.addPage(LauncherTypePage(self.config))  # Page 0
        confirm_page = ImportConfirmPage(self.config)  # Page 1
        self.addPage(confirm_page)  # Page 1


class ManageSourcesScreen(QDialog):
    """Manage sources screen — list, edit, or remove sources."""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.setWindowTitle("Manage Sources")
        self.resize(600, 400)
        self._build_ui()
        self._load_sources()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Sources list
        source_label = QLabel("Configured Sources:")
        source_label.setStyleSheet("QLabel { font-weight: bold; font-size: 13px; }")
        layout.addWidget(source_label)

        self.source_list = QListWidget()
        layout.addWidget(self.source_list)

        # Buttons
        btn_layout = QHBoxLayout()
        remove_btn = QPushButton("Remove Selected")
        remove_btn.setStyleSheet("QPushButton { background-color: #f7768e; color: #1a1b26; }")
        remove_btn.clicked.connect(self._remove_selected)
        btn_layout.addWidget(remove_btn)

        scan_btn = QPushButton("Scan Now")
        scan_btn.clicked.connect(self._scan_sources)
        btn_layout.addWidget(scan_btn)

        layout.addLayout(btn_layout)

        layout.addStretch()

    def _load_sources(self):
        """Load and display all sources."""
        self.source_list.clear()
        for i, source in enumerate(self.config.sources):
            item = QListWidgetItem(f"{i}. {source.get('name', 'Unnamed')} ({source.get('path', 'N/A')})")
            item.setData("index", i)
            item.setToolTip(f"Type: {source.get('type', 'N/A')}\nPath: {source.get('path', 'N/A')}")
            self.source_list.addItem(item)

    def _remove_selected(self):
        """Remove selected source."""
        item = self.source_list.currentItem()
        if item:
            idx = item.data("index")
            name = self.config.sources[idx].get("name", "Unnamed")
            reply = QMessageBox.question(
                self, "Remove Source",
                f"Remove '{name}'? All associated games will be removed.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.config.remove_source(idx)
                self._load_sources()

    def _scan_sources(self):
        """Scan all sources for games."""
        pass  # Could trigger a library refresh
