"""The Organise ROMs dialog.

A ROM lands in ~/Downloads and filing it by hand means knowing which system it
is for, finding the right folder, and keeping multi-disc sets together. This
dialog does all three: drop files (or a whole folder) on it, and it says
exactly where each game would go before anything moves.

Nothing is moved until the user has seen the plan, because moving someone's
files is the most destructive thing GameLab does. Games it cannot identify are
never guessed at — they are listed with a system picker so the user can decide,
and left alone if they don't.

The actual filing lives in `core.rom_import`; this is only the conversation
about it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core.emulator import SYSTEMS
from rose_gamelab.core.rom_import import ImportPlan, RomImporter
from rose_gamelab.ui import theme as ui_theme
from rose_gamelab.ui.theme import Theme, stylesheet
from rose_gamelab.ui.worker import BackgroundJob

logger = logging.getLogger(__name__)

#: How the system was decided, said in words the user can act on.
EVIDENCE = {
    "hash": "matched by file contents",
    "extension": "matched by file type",
    "folder": "guessed from the folder name",
    "layout": "recognised by its folder layout",
    "user": "chosen by you",
}


class PlanRow(QFrame):
    """One game, where it is going, and why we think so."""

    system_changed = Signal(object, str)   # plan, system id

    def __init__(self, plan: ImportPlan, theme: Theme) -> None:
        super().__init__()

        self.plan = plan
        self.theme = theme

        self.setStyleSheet(
            f"QFrame {{ background-color: {theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(5)

        top = QHBoxLayout()

        title = QLabel(plan.title)
        title.setWordWrap(True)
        title.setStyleSheet(f"font-weight: 600; color: {theme.text};")
        top.addWidget(title, 1)

        discs = len(plan.group.sorted_files)
        if plan.folder is not None:
            # Says the whole directory moves, which is the thing a user with a
            # PS3 collection most wants confirmed before pressing Organise.
            badge = QLabel("game folder")
        elif discs > 1:
            badge = QLabel(f"{discs} discs")
        else:
            badge = None

        if badge is not None:
            badge.setStyleSheet(f"color: {theme.text_dim}; font-size: 12px;")
            top.addWidget(badge)

        layout.addLayout(top)

        # An unidentified game gets a picker rather than a guess. Choosing a
        # system here is the only way one of these ever moves.
        if plan.system_id is None:
            self.picker = QComboBox()
            self.picker.addItem("Choose a system…", None)
            for system_id, system in sorted(SYSTEMS.items(), key=lambda kv: kv[1].name):
                if system_id == "pc":
                    continue
                self.picker.addItem(system.name, system_id)
            self.picker.currentIndexChanged.connect(self._picked)
            layout.addWidget(self.picker)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.detail)

        self.refresh()

    def _picked(self) -> None:
        system_id = self.picker.currentData()
        if system_id:
            self.system_changed.emit(self.plan, system_id)

    def refresh(self) -> None:
        """Redraw the status line from the plan as it now stands."""
        plan = self.plan

        if plan.ok:
            evidence = EVIDENCE.get(plan.identified_by, "")
            text = f"→  {plan.destination}"
            if evidence:
                text += f"    ·  {evidence}"
            colour = self.theme.success
        elif plan.system_id is None:
            text = plan.problem or "Could not tell which system this is for."
            colour = self.theme.warning
        else:
            text = f"{plan.system_name}  ·  {plan.problem}"
            colour = self.theme.text_dim

        self.detail.setText(text)
        self.detail.setStyleSheet(f"color: {colour}; font-size: 13px;")


class RomImportDialog(QDialog):
    """Files loose ROMs into per-system folders, after showing the plan."""

    #: Emitted once files have moved, so the library can rescan the new folders.
    library_changed = Signal()

    def __init__(
        self,
        theme: Theme,
        *,
        paths: Optional[list[Path]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self.theme = theme
        self.importer = RomImporter()
        self.plans: list[ImportPlan] = []
        self.rows: list[PlanRow] = []
        self._job: Optional[BackgroundJob] = None

        self.setWindowTitle("Organise ROMs")
        self.resize(700, 640)
        self.setStyleSheet(stylesheet(theme))
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING, ui_theme.SPACING)
        layout.setSpacing(ui_theme.SPACING)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._drop_page())
        self.pages.addWidget(self._plan_page())
        self.pages.addWidget(self._done_page())
        layout.addWidget(self.pages, 1)

        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)

        layout.addLayout(self._button_row())

        if paths:
            self.plan_for(paths)

    # ── Buttons ───────────────────────────────────────────────────

    def _button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self.copy_instead = QCheckBox("Copy instead of moving")
        self.copy_instead.setToolTip(
            "Leaves the originals where they are. Uses twice the disk space."
        )
        self.copy_instead.hide()
        row.addWidget(self.copy_instead)

        row.addStretch(1)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)

        self.organise = QPushButton("Organise")
        self.organise.setObjectName("Primary")
        self.organise.clicked.connect(self._apply)
        self.organise.hide()
        row.addWidget(self.organise)

        return row

    # ── Page 1: what to file ──────────────────────────────────────

    def _drop_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        heading = QLabel("Organise loose ROMs")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        note = QLabel(
            "Drop ROMs here — from Downloads, a USB stick, anywhere. GameLab "
            "works out which system each one is for and files it into your "
            "ROM folder, keeping multi-disc games together.\n\n"
            "You'll see exactly what moves where before anything happens."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {self.theme.text}; background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; padding: 12px 14px; font-size: 13px;"
        )
        layout.addWidget(note)

        self.drop_zone = QFrame()
        self.drop_zone.setStyleSheet(
            f"QFrame {{ background-color: {self.theme.surface};"
            f" border: 2px dashed {self.theme.border};"
            f" border-radius: {ui_theme.RADIUS}px; }}"
        )
        zone = QVBoxLayout(self.drop_zone)
        zone.setContentsMargins(20, 30, 20, 30)
        zone.setSpacing(14)

        prompt = QLabel("Drop files or folders here")
        prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        prompt.setStyleSheet(f"color: {self.theme.text_dim}; font-size: 15px;")
        zone.addWidget(prompt)

        buttons = QHBoxLayout()
        buttons.addStretch(1)

        choose_files = QPushButton("Choose Files…")
        choose_files.clicked.connect(self._choose_files)
        buttons.addWidget(choose_files)

        choose_folder = QPushButton("Choose Folder…")
        choose_folder.clicked.connect(self._choose_folder)
        buttons.addWidget(choose_folder)

        buttons.addStretch(1)
        zone.addLayout(buttons)

        layout.addWidget(self.drop_zone, 1)
        layout.addWidget(self._destination_row())

        return page

    def _destination_row(self) -> QWidget:
        row = QFrame()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        label = QLabel("Files go to")
        label.setObjectName("Subtle")
        layout.addWidget(label)

        self.root_label = QLabel(str(self.importer.root))
        self.root_label.setStyleSheet(f"color: {self.theme.text};")
        layout.addWidget(self.root_label, 1)

        change = QPushButton("Change…")
        change.clicked.connect(self._change_root)
        layout.addWidget(change)

        return row

    def _change_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Where should organised ROMs live?", str(self.importer.root)
        )
        if not folder:
            return

        self.importer.root = Path(folder)
        self.root_label.setText(folder)

        # Destinations in an existing plan are now wrong; work them out again.
        if self.plans:
            self.plan_for(self._planned_paths())

    def _planned_paths(self) -> list[Path]:
        return [path for plan in self.plans for path in plan.source_paths]

    def _choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose ROMs to organise", str(Path.home())
        )
        if files:
            self.plan_for([Path(f) for f in files])

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder of ROMs", str(Path.home())
        )
        if folder:
            self.plan_for([Path(folder)])

    # ── Drag and drop ─────────────────────────────────────────────

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            event.acceptProposedAction()
            self.plan_for(paths)

    # ── Page 2: the plan ──────────────────────────────────────────

    def _plan_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.plan_heading = QLabel()
        self.plan_heading.setObjectName("Heading")
        layout.addWidget(self.plan_heading)

        self.plan_summary = QLabel()
        self.plan_summary.setObjectName("Subtle")
        self.plan_summary.setWordWrap(True)
        layout.addWidget(self.plan_summary)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QWidget()
        self.plan_layout = QVBoxLayout(body)
        self.plan_layout.setContentsMargins(0, 0, 0, 0)
        self.plan_layout.setSpacing(8)
        scroll.setWidget(body)

        layout.addWidget(scroll, 1)
        return page

    def plan_for(self, paths: list[Path]) -> None:
        """Work out what would happen, on a worker thread."""
        if self._job is not None and self._job.is_running():
            return

        self.progress.setRange(0, 0)
        self.progress.show()
        self.organise.hide()
        self.copy_instead.hide()

        importer = self.importer

        def work(report):
            report("Identifying games…")
            return importer.plan(paths)

        self._job = BackgroundJob(
            work,
            on_done=self._plans_ready,
            on_failed=self._plan_failed,
            parent=self,
        )
        self._job.start()

    def _plan_failed(self, message: str) -> None:
        self.progress.hide()
        self.plan_heading.setText("Could not read those files")
        self.plan_summary.setText(message)
        self.pages.setCurrentIndex(1)

    def _plans_ready(self, plans: list[ImportPlan]) -> None:
        self.progress.hide()
        self.plans = plans
        self.rows = []

        while self.plan_layout.count():
            item = self.plan_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()

        # Ready first, then the ones needing a decision, then the ones already
        # filed — most-actionable at the top.
        def order(plan: ImportPlan) -> tuple[int, str]:
            if plan.ok:
                return (0, plan.title.lower())
            if plan.system_id is None:
                return (1, plan.title.lower())
            return (2, plan.title.lower())

        for plan in sorted(plans, key=order):
            row = PlanRow(plan, self.theme)
            row.system_changed.connect(self._system_chosen)
            self.rows.append(row)
            self.plan_layout.addWidget(row)

        self.plan_layout.addStretch(1)
        self._refresh_plan_header()
        self.pages.setCurrentIndex(1)

    def _system_chosen(self, plan: ImportPlan, system_id: str) -> None:
        """Re-plan one game against the system the user picked."""
        replanned = self.importer.plan(plan.source_paths, hint=system_id)
        if not replanned:
            return

        updated = replanned[0]
        plan.system_id = updated.system_id
        plan.destination = updated.destination
        plan.identified_by = "user"
        plan.problem = updated.problem

        for row in self.rows:
            if row.plan is plan:
                row.refresh()

        self._refresh_plan_header()

    def _refresh_plan_header(self) -> None:
        ready = [p for p in self.plans if p.ok]
        undecided = [p for p in self.plans if not p.ok and p.system_id is None]
        blocked = [p for p in self.plans if not p.ok and p.system_id is not None]

        self.plan_heading.setText(
            f"{len(ready)} game{'s' if len(ready) != 1 else ''} ready to organise"
            if ready else "Nothing to organise"
        )

        parts = []
        if undecided:
            parts.append(
                f"{len(undecided)} could not be identified — pick a system to include them"
            )
        if blocked:
            parts.append(f"{len(blocked)} already in your library or filed here")
        if not parts and ready:
            parts.append(f"Everything goes under {self.importer.root}")
        self.plan_summary.setText(".  ".join(parts))

        self.organise.setVisible(bool(ready))
        self.organise.setEnabled(bool(ready))
        self.copy_instead.setVisible(bool(ready))

    # ── Applying ──────────────────────────────────────────────────

    def _apply(self) -> None:
        if self._job is not None and self._job.is_running():
            return

        plans = [p for p in self.plans if p.ok]
        if not plans:
            return

        self.organise.setEnabled(False)
        self.progress.setRange(0, len(plans))
        self.progress.setValue(0)
        self.progress.show()

        importer = self.importer
        move = not self.copy_instead.isChecked()

        def work(report):
            return importer.apply(
                plans,
                move=move,
                progress=lambda title, done, total: report(title, done, total),
            )

        self._job = BackgroundJob(
            work,
            on_done=self._applied,
            on_progress=self._on_progress,
            on_failed=self._plan_failed,
            parent=self,
        )
        self._job.start()

    def _on_progress(self, message: str, done: int, total: int) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    # ── Page 3: what actually happened ────────────────────────────

    def _done_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.done_heading = QLabel()
        self.done_heading.setObjectName("Heading")
        layout.addWidget(self.done_heading)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QWidget()
        self.done_layout = QVBoxLayout(body)
        self.done_layout.setContentsMargins(0, 0, 0, 0)
        self.done_layout.setSpacing(8)
        scroll.setWidget(body)

        layout.addWidget(scroll, 1)
        return page

    def _applied(self, outcome) -> None:
        self.progress.hide()
        self.organise.hide()
        self.copy_instead.hide()
        self.close_button.setText("Done")

        while self.done_layout.count():
            item = self.done_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()

        moved = outcome.files_moved
        self.done_heading.setText(
            f"Filed {moved} file{'s' if moved != 1 else ''}"
            if moved else "Nothing was moved"
        )

        # Every file is accounted for, including the ones that did not move —
        # a summary that only counts successes hides the ones needing action.
        if moved:
            folders = sorted({str(target.parent) for _source, target in outcome.moved})
            self._note("\n".join(folders), self.theme.success)

        for title, why in outcome.skipped:
            self._note(f"{title} — {why}", self.theme.text_dim)

        for error in outcome.errors:
            self._note(error, self.theme.error)

        self.done_layout.addStretch(1)
        self.pages.setCurrentIndex(2)

        if moved:
            self.library_changed.emit()

    def _note(self, text: str, colour: str) -> None:
        note = QLabel(text)
        note.setWordWrap(True)
        note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        note.setStyleSheet(
            f"color: {colour}; background-color: {self.theme.panel};"
            f" border-radius: {ui_theme.RADIUS}px; padding: 10px 12px; font-size: 13px;"
        )
        self.done_layout.addWidget(note)

    # ── Teardown ──────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if self._job is not None:
            self._job.stop()
        super().closeEvent(event)
