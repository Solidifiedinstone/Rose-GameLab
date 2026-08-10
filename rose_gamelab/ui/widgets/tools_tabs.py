"""Settings panels for saves, controllers and Steam export.

Each of these fronts a subsystem that works but would otherwise be reachable
only from the command line. They share a rule: report what actually happened,
including when the answer is "nothing" or "this is not installed".
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.ui.theme import SPACING, Theme


class SavesTab(QWidget):
    """Browse indexed saves, back them up, restore them."""

    def __init__(self, library, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        from rose_gamelab.core.saves import SaveManager

        self.library = library
        self.manager = SaveManager(library)
        self.theme = theme

        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING)

        intro = QLabel(
            "Saves are found where your emulators keep them and are never moved. "
            "Backups are plain folders of ordinary files — you can open them in a "
            "file manager, and they do not need GameLab to read back."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Subtle")
        layout.addWidget(intro)

        self.list = QListWidget()
        layout.addWidget(self.list, 1)

        self.status = QLabel()
        self.status.setObjectName("Subtle")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        for label, slot in (
            ("Find Saves", self.reindex),
            ("Back Up All", self.backup),
            ("Restore…", self.restore),
            ("Open Backups Folder", self.reveal_backups),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.reload()

    def reload(self) -> None:
        self.list.clear()

        rows = self.library.db.query(
            "SELECT s.*, g.title FROM saves s JOIN games g ON g.id = s.game_id"
            " ORDER BY g.sort_title, s.kind, s.slot"
        )

        for row in rows:
            size = (row["size_bytes"] or 0) / 1024
            kind = "state" if row["kind"] == "state" else "save"
            slot = f" slot {row['slot']}" if row["slot"] is not None else ""
            item = QListWidgetItem(
                f"{row['title']}  ·  {kind}{slot}  ·  {size:.0f} KB"
                f"  ·  {(row['modified_at'] or '')[:10]}"
            )
            item.setData(Qt.ItemDataRole.UserRole, row["path"])
            self.list.addItem(item)

        self.status.setText(f"{len(rows)} save files indexed")

    def reindex(self) -> None:
        found = self.manager.index()
        unmatched = self.manager.unmatched_saves()

        self.reload()

        message = f"Indexed {found} save files."
        if unmatched:
            # Surfaced rather than hidden: attaching a save to the wrong game
            # and restoring it later destroys real progress.
            message += (
                f" {len(unmatched)} could not be matched to a game in your "
                "library and were left alone."
            )
        self.status.setText(message)

    def backup(self) -> None:
        result = self.manager.backup()

        if not result.files_copied:
            self.status.setText("Nothing to back up.")
            return

        self.status.setText(
            f"Copied {result.files_copied} files "
            f"({result.bytes_copied / 1024 / 1024:.1f} MB) to {result.destination}"
        )

    def restore(self) -> None:
        item = self.list.currentItem()
        if item is None:
            QMessageBox.information(
                self, "Choose a save",
                "Select the save you want to overwrite, then choose the backup "
                "to restore from.",
            )
            return

        target = Path(item.data(Qt.ItemDataRole.UserRole))
        source, _ = QFileDialog.getOpenFileName(
            self, "Choose a backup file to restore", str(self.manager.backup_root)
        )
        if not source:
            return

        confirm = QMessageBox.question(
            self,
            "Restore this save?",
            f"This will overwrite:\n{target}\n\n"
            "Your current save will be kept alongside it with a .replaced suffix.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            self.manager.restore(Path(source), target)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Could not restore", str(exc))
            return

        self.status.setText(f"Restored {Path(source).name}")

    def reveal_backups(self) -> None:
        import subprocess

        root = self.manager.backup_root
        root.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(root)])


class ControllersTab(QWidget):
    """Detect controllers and export one mapping to every emulator."""

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.theme = theme

        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING)

        intro = QLabel(
            "Configure a controller once and apply it to every emulator, instead "
            "of setting it up separately in each one."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Subtle")
        layout.addWidget(intro)

        self.list = QListWidget()
        layout.addWidget(self.list, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("Subtle")
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        detect = QPushButton("Detect Controllers")
        detect.clicked.connect(self.detect)
        buttons.addWidget(detect)

        export = QPushButton("Write Emulator Configs")
        export.clicked.connect(self.export)
        buttons.addWidget(export)
        layout.addLayout(buttons)

        self.detect()

    def detect(self) -> None:
        from rose_gamelab.core.controller import (
            ControllerDetectionError,
            detect_controllers,
        )

        self.list.clear()

        try:
            controllers = detect_controllers()
        except ControllerDetectionError as exc:
            # A specific reason, not a silent empty list.
            self.status.setText(f"Could not check for controllers: {exc}")
            return

        for controller in controllers:
            self.list.addItem(
                f"{controller.name}  ·  {controller.vendor_id:04x}:{controller.product_id:04x}"
            )

        if controllers:
            self.status.setText(f"{len(controllers)} controller(s) connected.")
        else:
            self.status.setText(
                "No controller connected. Plug one in and press Detect."
            )

    def export(self) -> None:
        from rose_gamelab.core.controller import (
            ControllerDetectionError,
            default_mapping,
            detect_controllers,
        )

        try:
            controllers = detect_controllers()
        except ControllerDetectionError as exc:
            QMessageBox.warning(self, "Could not check for controllers", str(exc))
            return

        if not controllers:
            QMessageBox.information(
                self, "No controller",
                "Connect a controller first — the exported configuration is "
                "specific to the device.",
            )
            return

        mapping = default_mapping(controllers[0])
        configs = mapping.export_all()

        self.status.setText(
            "Generated configuration for: "
            + ", ".join(sorted(configs))
            + ".\nEmulators not listed read gamepads through SDL, which the "
            "SDL mapping already covers."
        )


class SteamExportTab(QWidget):
    """Add library games to Steam as non-Steam shortcuts."""

    def __init__(self, library, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.library = library
        self.theme = theme

        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING)

        intro = QLabel(
            "Add your emulated and non-Steam games to Steam, with cover art and "
            "a library category. They will work with Big Picture, Steam Input "
            "and Remote Play.\n\n"
            "Steam must be completely closed first: it rewrites its shortcuts "
            "file when it exits and would discard anything added while running."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Subtle")
        layout.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Category"))
        self.collection = QComboBox()
        self.collection.setEditable(True)
        self.collection.addItems(["Rose GameLab", "Retro", "Emulated"])
        row.addWidget(self.collection, 1)
        layout.addLayout(row)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("Subtle")
        layout.addWidget(self.status)

        layout.addStretch(1)

        buttons = QHBoxLayout()
        export = QPushButton("Export to Steam")
        export.setObjectName("Primary")
        export.clicked.connect(self.export)
        buttons.addWidget(export)

        remove = QPushButton("Remove Exported Shortcuts")
        remove.clicked.connect(self.remove)
        buttons.addWidget(remove)
        layout.addLayout(buttons)

    def export(self) -> None:
        from rose_gamelab.sources.steam_export import SteamExporter

        exporter = SteamExporter()

        # Exporting Steam games back into Steam would duplicate what it has.
        games = [g for g in self.library.list_games() if g.steam_appid is None]
        if not games:
            self.status.setText("There are no non-Steam games to export.")
            return

        try:
            result = exporter.export(
                games, self.library,
                collection_name=self.collection.currentText().strip() or None,
            )
        except RuntimeError as exc:
            QMessageBox.warning(self, "Close Steam first", str(exc))
            return

        self.status.setText(
            f"Added {result.added}, updated {result.updated}, "
            f"copied {result.artwork_copied} covers."
            + (f"\nPrevious shortcuts backed up to {result.backup}" if result.backup else "")
            + ("\n" + "\n".join(result.errors) if result.errors else "")
        )

    def remove(self) -> None:
        from rose_gamelab.sources.steam_export import SteamExporter

        confirm = QMessageBox.question(
            self, "Remove exported shortcuts?",
            "This removes only the shortcuts GameLab added. Shortcuts you made "
            "yourself are left alone.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        removed = SteamExporter().remove_exported()
        self.status.setText(f"Removed {removed} shortcut(s).")
