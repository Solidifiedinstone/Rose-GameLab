"""The RetroArch screen: install it, then tick the systems you want cores for.

RetroArch covers most retro systems, and a fresh install covers none of them —
the emulation lives in cores that have to be fetched one per system. RetroArch
can do that itself, through an online updater several menus deep in an interface
built for a television and a gamepad.

GameLab already knows which core each system needs and how many games you own
for it, so it can ask the only question that matters: which consoles do you
actually want to play? Systems with games in the library are listed first and
ticked by default; everything else is there, unticked, for later.

Downloading happens on a worker thread. It is a few megabytes per core over
somebody else's donated bandwidth, and freezing the window for the duration
would make a working feature look broken.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core import retroarch
from rose_gamelab.ui import theme as ui_theme
from rose_gamelab.ui.theme import Theme

logger = logging.getLogger(__name__)


class RetroArchInstaller(QObject):
    """Installs RetroArch off the interface thread.

    It downloads a few hundred megabytes and may take minutes. Running that on
    the interface thread froze the entire application for the duration — which
    is not distinguishable, from the outside, from having crashed.
    """

    progress = Signal(str)
    finished = Signal(bool, str)

    def run(self) -> None:
        try:
            succeeded, message = retroarch.install_retroarch(
                progress=self.progress.emit
            )
        except Exception as exc:
            logger.exception("installing RetroArch failed")
            succeeded, message = False, str(exc)
        self.finished.emit(succeeded, message)


class CoreInstaller(QObject):
    """Downloads cores off the interface thread."""

    progress = Signal(str, int, int)
    finished = Signal(object)

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self._names = names

    def run(self) -> None:
        try:
            result = retroarch.install_cores(
                self._names,
                progress=lambda name, done, total: self.progress.emit(name, done, total),
            )
        except Exception as exc:
            logger.exception("core installation failed")
            result = retroarch.InstallResult(errors=[str(exc)])
        self.finished.emit(result)


class RetroArchTab(QWidget):
    """Install RetroArch, and choose which cores to fetch."""

    def __init__(self, library, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self.library = library
        self.theme = theme
        self._boxes: dict[str, QCheckBox] = {}
        self._thread: Optional[QThread] = None
        self._worker: Optional[CoreInstaller] = None
        self._install_thread: Optional[QThread] = None
        self._installer: Optional[RetroArchInstaller] = None

        self._build()
        self.refresh()

    # ── Construction ──────────────────────────────────────────────

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING
        )
        layout.setSpacing(ui_theme.SPACING)

        blurb = QLabel(
            "RetroArch plays most retro systems, but a fresh install has no "
            "emulators in it — each system needs its own core. Tick the ones "
            "you want and GameLab will fetch them from the libretro project."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {self.theme.text_dim};")
        layout.addWidget(blurb)

        # ── RetroArch itself ──
        install_row = QFrame()
        install_row.setStyleSheet(
            f"background-color: {self.theme.panel};"
            f"border-radius: {ui_theme.RADIUS_LARGE}px;"
        )
        row = QHBoxLayout(install_row)
        row.setContentsMargins(
            ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING
        )

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {self.theme.text}; background: transparent;")
        row.addWidget(self.status, 1)

        self.install_button = QPushButton("Install RetroArch")
        self.install_button.clicked.connect(self.install_retroarch)
        row.addWidget(self.install_button)

        layout.addWidget(install_row)

        # ── Cores ──
        selection = QHBoxLayout()
        self.core_heading = QLabel("Cores")
        self.core_heading.setStyleSheet(f"color: {self.theme.text}; font-weight: 600;")
        selection.addWidget(self.core_heading)
        selection.addStretch(1)

        for label, slot in (
            ("Select what I own", self.select_owned),
            ("Select none", self.select_none),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            selection.addWidget(button)

        layout.addLayout(selection)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        self._list = QWidget()
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(4)
        area.setWidget(self._list)
        layout.addWidget(area, 1)

        layout.addWidget(self._build_bios())

        self.bar = QProgressBar()
        self.bar.hide()
        layout.addWidget(self.bar)

        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setStyleSheet(f"color: {self.theme.text_dim};")
        layout.addWidget(self.result)

        self.fetch_button = QPushButton("Install selected cores")
        self.fetch_button.clicked.connect(self.install_selected)
        layout.addWidget(self.fetch_button)

    def _build_bios(self) -> QWidget:
        """Firmware: what is wanted, what is missing, and how to add it."""
        box = QFrame()
        box.setStyleSheet(
            f"background-color: {self.theme.panel};"
            f"border-radius: {ui_theme.RADIUS_LARGE}px;"
        )
        layout = QVBoxLayout(box)
        layout.setContentsMargins(
            ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING
        )
        layout.setSpacing(6)

        heading = QLabel("BIOS files")
        heading.setStyleSheet(
            f"color: {self.theme.text}; font-weight: 600; background: transparent;"
        )
        layout.addWidget(heading)

        note = QLabel(
            "Some systems will not start without the console's own firmware. "
            "A core missing it does not say so politely — it fails to start, "
            "which looks exactly like the emulator crashing. Add the files you "
            "dumped from your own hardware; GameLab copies them where RetroArch "
            "looks and never renames them."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {self.theme.text_dim}; font-size: 12px; background: transparent;"
        )
        layout.addWidget(note)

        self.bios_list = QListWidget()
        self.bios_list.setMaximumHeight(150)
        self.bios_list.setStyleSheet(
            f"background-color: {self.theme.surface}; color: {self.theme.text};"
            f"border-radius: {ui_theme.RADIUS_LARGE}px; padding: 6px;"
        )
        layout.addWidget(self.bios_list)

        row = QHBoxLayout()
        self.bios_status = QLabel()
        self.bios_status.setWordWrap(True)
        self.bios_status.setStyleSheet(
            f"color: {self.theme.text_dim}; font-size: 12px; background: transparent;"
        )
        row.addWidget(self.bios_status, 1)

        add = QPushButton("Add BIOS files…")
        add.clicked.connect(self.add_bios)
        row.addWidget(add)

        reveal = QPushButton("Open the folder")
        reveal.clicked.connect(self.open_bios_folder)
        row.addWidget(reveal)

        layout.addLayout(row)
        return box

    def refresh_bios(self) -> None:
        self.bios_list.clear()

        for need in retroarch.bios_needs(self.library):
            mark = "✓" if need.satisfied else "○"
            item = QListWidgetItem(f"{mark}  {need.summary}")
            if need.satisfied:
                item.setForeground(QColor(self.theme.text_dim))
            self.bios_list.addItem(item)

        folder = retroarch.system_directory()
        self.bios_status.setText(
            f"They go in {folder}" if folder
            else "RetroArch has no system directory yet — install it and run it once."
        )

    def add_bios(self) -> None:
        folder = retroarch.system_directory()
        if folder is None:
            self.bios_status.setText(
                "RetroArch has no system directory yet — install it and run it once."
            )
            return

        chosen, _filter = QFileDialog.getOpenFileNames(
            self, "Choose BIOS files", str(Path.home()),
            "Firmware (*.bin *.BIN *.rom *.ROM *.pce *.img);;All files (*)",
        )
        if not chosen:
            return

        result = retroarch.install_bios(chosen)
        self.bios_status.setText(f"{result.summary} — {folder}")
        for error in result.errors[:3]:
            logger.warning("BIOS: %s", error)

        self.refresh_bios()
        self._rebuild_list()          # the warnings on the cores change too

    def open_bios_folder(self) -> None:
        folder = retroarch.system_directory()
        if folder is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ── State ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-read what is installed and rebuild the list."""
        retroarch_present = retroarch.installed()

        if retroarch_present:
            where = retroarch.core_directory()
            system = retroarch.system_directory()
            self.status.setText(
                "RetroArch is installed."
                + (f"\nCores go in {where}" if where else "")
                + (f"\nBIOS files go in {system}" if system else "")
            )
            self.install_button.setEnabled(False)
            self.install_button.setText("Installed")
        else:
            self.status.setText(
                "RetroArch is not installed. "
                + (
                    "GameLab can install it through Flatpak, which needs no password."
                    if retroarch.can_install_without_root()
                    else f"Install it with:  {retroarch.install_command()}"
                )
            )
            self.install_button.setEnabled(retroarch.can_install_without_root())

        self._rebuild_list()
        self.refresh_bios()

    def _rebuild_list(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._boxes.clear()
        cores = retroarch.available_cores(self.library)
        owned = sum(1 for core in cores if core.game_count)

        self.core_heading.setText(
            f"Cores — {len(cores)} available, {owned} for systems you own games for"
        )

        for core in cores:
            box = QCheckBox(self._label_for(core))
            box.setEnabled(not core.installed)
            # Ticked by default only where there are games to play with it.
            box.setChecked(bool(core.game_count) and not core.installed)
            box.setStyleSheet(
                f"color: {self.theme.text_dim if core.installed else self.theme.text};"
            )
            self._boxes[core.name] = box
            self._list_layout.addWidget(box)

        self._list_layout.addStretch(1)

    @staticmethod
    def _label_for(core) -> str:
        parts = [", ".join(core.systems)]
        if core.game_count:
            parts.append(f"{core.game_count} game{'s' if core.game_count != 1 else ''}")
        if core.installed:
            parts.append("installed")

        label = "   ·   ".join(parts) + f"      ({core.name})"

        # Said here rather than left to be discovered. A core that needs a BIOS
        # and has none does not report that politely — it fails to start, which
        # from the outside is indistinguishable from the core crashing.
        missing = retroarch.missing_bios(core.name)
        if core.installed and missing:
            label += f"   ⚠ needs a BIOS: {' or '.join(missing)}"
        return label

    def select_owned(self) -> None:
        for core in retroarch.available_cores(self.library):
            box = self._boxes.get(core.name)
            if box is not None and box.isEnabled():
                box.setChecked(bool(core.game_count))

    def select_none(self) -> None:
        for box in self._boxes.values():
            box.setChecked(False)

    def selected(self) -> list[str]:
        return [name for name, box in self._boxes.items()
                if box.isChecked() and box.isEnabled()]

    # ── Actions ───────────────────────────────────────────────────

    def install_retroarch(self) -> None:
        if self._install_thread is not None:
            return

        self.install_button.setEnabled(False)
        self.status.setText("Installing RetroArch…")
        self.bar.setRange(0, 0)          # indeterminate: flatpak reports no total
        self.bar.show()

        self._install_thread = QThread(self)
        self._installer = RetroArchInstaller()
        self._installer.moveToThread(self._install_thread)

        self._install_thread.started.connect(self._installer.run)
        self._installer.progress.connect(
            self.status.setText, Qt.ConnectionType.QueuedConnection
        )
        self._installer.finished.connect(
            self._on_retroarch_installed, Qt.ConnectionType.QueuedConnection
        )
        self._install_thread.start()

    def _on_retroarch_installed(self, succeeded: bool, message: str) -> None:
        self.bar.hide()
        self.bar.setRange(0, 1)

        if self._install_thread is not None:
            self._install_thread.quit()
            self._install_thread.wait()
            self._install_thread = None
            self._installer = None

        if succeeded:
            self.refresh()
        else:
            self.status.setText(message)
            self.install_button.setEnabled(True)

    def install_selected(self) -> None:
        names = self.selected()
        if not names:
            self.result.setText("Nothing selected.")
            return

        if self._thread is not None:
            return  # already running

        self.fetch_button.setEnabled(False)
        self.bar.setRange(0, len(names))
        self.bar.setValue(0)
        self.bar.show()

        self._thread = QThread(self)
        self._worker = CoreInstaller(names)
        self._worker.moveToThread(self._thread)

        # Bound methods with queued connections, never lambdas: a lambda has no
        # thread affinity and runs on the worker thread, which crashes when the
        # thread it is tearing down is its own.
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(
            self._on_progress, Qt.ConnectionType.QueuedConnection
        )
        self._worker.finished.connect(
            self._on_finished, Qt.ConnectionType.QueuedConnection
        )
        self._thread.start()

    def _on_progress(self, name: str, done: int, total: int) -> None:
        self.bar.setValue(done)
        self.result.setText(f"Downloading {name} ({done} of {total})…")

    def _on_finished(self, result) -> None:
        self.bar.hide()
        self.fetch_button.setEnabled(True)

        message = result.summary
        if result.errors:
            message += "\n" + "\n".join(result.errors[:4])
        self.result.setText(message)

        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
            self._worker = None

        self.refresh()
