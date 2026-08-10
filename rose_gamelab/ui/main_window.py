"""The main window.

Layout is three columns: the sidebar rail, the cover grid, and the details
panel for whatever is selected. The grid is the focus; everything else gets out
of its way.

Long-running work — scanning, hashing, scraping — runs on worker threads and
reports real progress. Nothing in this interface shows a progress bar that is
not backed by actual work, which is what the previous implementation did.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core.emulator import get_system
from rose_gamelab.core.launcher import Launcher, LaunchError
from rose_gamelab.core.library import Library
from rose_gamelab.core.profiles import ProfileStore
from rose_gamelab.core.scanner import RomScanner
from rose_gamelab.db.database import Database
from rose_gamelab.metadata.scraper import Scraper
from rose_gamelab.ui.branding import APP_NAME
from rose_gamelab.ui.theme import COVER_WIDTHS, SPACING, Theme, get_theme, stylesheet
from rose_gamelab.ui.widgets.browse_view import BrowseView
from rose_gamelab.ui.widgets.detail_panel import DetailPanel
from rose_gamelab.ui.widgets.game_grid import GameGrid
from rose_gamelab.ui.widgets.sidebar import Sidebar

logger = logging.getLogger(__name__)

SORT_OPTIONS = [
    ("Title", "title"),
    ("Recently played", "last_played"),
    ("Recently added", "added"),
    ("Playtime", "playtime"),
    ("Release date", "release"),
    ("Rating", "rating"),
]


class Worker(QObject):
    """Runs one callable on a worker thread and reports back.

    Qt widgets may only be touched from the interface thread, so workers emit
    signals rather than updating anything themselves.
    """

    progress = Signal(str, int, int)   # message, done, total
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, work) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:
        try:
            result = self._work(self._report)
        except Exception as exc:
            logger.exception("background work failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)

    def _report(self, message: str, done: int = 0, total: int = 0) -> None:
        self.progress.emit(message, done, total)


class MainWindow(QMainWindow):
    """Rose GameLab's main window."""

    def __init__(
        self,
        database: Database,
        *,
        theme_name: str = "rose-dark",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self.db = database
        self.library = Library(database)
        self.profiles = ProfileStore(database)
        self.profiles.ensure_default_exists()
        self.scanner = RomScanner(self.library)
        self.launcher = Launcher(self.library, self.profiles)
        self.scraper = Scraper(self.library)

        self.theme = get_theme(theme_name)
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None
        self._filter = "all"
        self._search = ""
        self._sort = "title"

        self.setWindowTitle(APP_NAME)
        self.resize(1440, 900)
        self.setStyleSheet(stylesheet(self.theme))

        self._build()
        self._build_shortcuts()
        self.refresh()

    # ── Construction ──────────────────────────────────────────────

    def _build(self) -> None:
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar(self.theme)
        self.sidebar.filter_selected.connect(self._on_filter)
        self.sidebar.add_source_requested.connect(self.add_rom_folder)
        self.sidebar.settings_requested.connect(self.open_settings)
        self.sidebar.big_picture_requested.connect(self.open_big_picture)
        layout.addWidget(self.sidebar)

        middle = QVBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        middle.addWidget(self._build_top_bar())

        # The library grid and the browse view share the same space; the
        # sidebar switches between them.
        self.pages = QStackedWidget()

        self.grid = GameGrid(self.theme)
        self.grid.game_selected.connect(self._on_game_selected)
        self.grid.game_activated.connect(lambda gid: self.launch(gid, None))
        self.pages.addWidget(self.grid)

        self.browse = BrowseView(self.library, self.theme)
        self.browse.refresh_requested.connect(self.load_chart)
        self.pages.addWidget(self.browse)

        middle.addWidget(self.pages, 1)

        middle.addWidget(self._build_status_bar())
        layout.addLayout(middle, 1)

        self.details = DetailPanel(self.theme)
        self.details.launch_requested.connect(self.launch)
        self.details.favorite_toggled.connect(self._on_favorite)
        self.details.scrape_requested.connect(self.scrape_one)
        layout.addWidget(self.details)

        self.setCentralWidget(central)

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(58)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(SPACING, 9, SPACING, 9)
        layout.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your library…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        layout.addWidget(self.search, 1)

        self.sort_picker = QComboBox()
        for label, key in SORT_OPTIONS:
            self.sort_picker.addItem(label, key)
        self.sort_picker.currentIndexChanged.connect(self._on_sort)
        layout.addWidget(self.sort_picker)

        self.size_picker = QComboBox()
        for label in COVER_WIDTHS:
            self.size_picker.addItem(label.title(), COVER_WIDTHS[label])
        self.size_picker.setCurrentIndex(1)
        self.size_picker.currentIndexChanged.connect(
            lambda: self.grid.set_card_width(self.size_picker.currentData())
        )
        layout.addWidget(self.size_picker)

        scan = QPushButton("Scan")
        scan.setToolTip("Rescan every source for new games")
        scan.clicked.connect(self.rescan_all)
        layout.addWidget(scan)

        art = QPushButton("Find Art")
        art.setToolTip("Download covers and metadata for games missing them")
        art.clicked.connect(self.scrape_all)
        layout.addWidget(art)

        return bar

    def _build_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setFixedHeight(30)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(SPACING, 0, SPACING, 0)
        layout.setSpacing(10)

        self.status = QLabel()
        self.status.setObjectName("Subtle")
        layout.addWidget(self.status)

        layout.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.hide()
        layout.addWidget(self.progress)

        return bar

    def _build_shortcuts(self) -> None:
        for key, slot in (
            ("Ctrl+F", lambda: self.search.setFocus()),
            ("Ctrl+R", self.rescan_all),
            ("Ctrl+B", self.open_big_picture),
            ("Ctrl+,", self.open_settings),
            ("F5", self.refresh),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(slot)
            self.addAction(action)

    # ── Data ──────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Reload the sidebar and the grid from the database."""
        self._refresh_sidebar()
        self._refresh_grid()

    def _refresh_sidebar(self) -> None:
        systems = []
        for system_id, count in self.library.systems_in_library():
            system = get_system(system_id)
            systems.append((
                system_id,
                system.icon if system else "🎮",
                system.name if system else system_id,
                count,
            ))
        self.sidebar.set_systems(systems)

        sources = [
            (row["id"], "📁", row["name"], row["game_count"])
            for row in self.library.list_sources()
            if row["game_count"]
        ]
        self.sidebar.set_sources(sources)

    def _refresh_grid(self) -> None:
        games = self._current_games()
        self.grid.set_games(games)

        total = self.library.count()
        if len(games) == total:
            self.status.setText(f"{total} games")
        else:
            self.status.setText(f"{len(games)} of {total} games")

        if games:
            self.grid.select(games[0].id)
            self._on_game_selected(games[0].id)
        else:
            self.details.show_empty()

    def _current_games(self) -> list:
        kwargs = {"search": self._search or None, "sort": self._sort}

        if self._sort in ("last_played", "playtime", "rating", "added"):
            kwargs["descending"] = True

        if self._filter == "favorites":
            kwargs["favorites_only"] = True
        elif self._filter == "recent":
            kwargs["sort"] = "last_played"
            kwargs["descending"] = True
        elif self._filter.startswith("system:"):
            kwargs["system"] = self._filter.split(":", 1)[1]
        elif self._filter.startswith("source:"):
            kwargs["source_id"] = self._filter.split(":", 1)[1]

        return self.library.list_games(**kwargs)

    # ── Interaction ───────────────────────────────────────────────

    def _on_filter(self, key: str) -> None:
        if key == "browse":
            self.pages.setCurrentWidget(self.browse)
            self.load_chart(self.browse.system_picker.currentData() or "pc")
            return

        self.pages.setCurrentWidget(self.grid)

        if key == "random":
            game = self.library.random_game()
            if game:
                self.grid.select(game.id)
                self._on_game_selected(game.id)
            return

        self._filter = key
        self._refresh_grid()

    def _on_search(self, text: str) -> None:
        self._search = text.strip()
        self._refresh_grid()

    def _on_sort(self) -> None:
        self._sort = self.sort_picker.currentData()
        self._refresh_grid()

    def _on_game_selected(self, game_id: int) -> None:
        game = self.library.get(game_id)
        if game is None:
            self.details.show_empty()
            return

        self.details.show_game(
            game,
            self.library.launch_options_for(game_id),
            self.library.tags_for(game_id),
        )

    def _on_favorite(self, game_id: int, favorite: bool) -> None:
        self.library.set_favorite(game_id, favorite)
        if self._filter == "favorites":
            self._refresh_grid()

    # ── Launching ─────────────────────────────────────────────────

    def launch(self, game_id: int, option_id: Optional[int]) -> None:
        try:
            self.launcher.launch(
                game_id,
                launch_option_id=option_id,
                on_exit=lambda gid, seconds: self._on_game_exit(gid, seconds),
            )
        except LaunchError as exc:
            # Launch failures are the user's problem to solve, so the message
            # has to say what went wrong rather than "an error occurred".
            QMessageBox.warning(self, "Could not launch", str(exc))
            return

        game = self.library.get(game_id)
        self.status.setText(f"Playing {game.title}…" if game else "Playing…")

    def _on_game_exit(self, game_id: int, seconds: int) -> None:
        game = self.library.get(game_id)
        if game and seconds:
            self.status.setText(
                f"Played {game.title} for {seconds // 60} min"
            )
        self._on_game_selected(game_id)

    # ── Sources and scanning ──────────────────────────────────────

    def add_rom_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a ROM folder")
        if not directory:
            return

        source_id = f"roms:{directory}"
        self.library.register_source(
            source_id,
            name=directory.rstrip("/").rsplit("/", 1)[-1] or directory,
            type="rom_folder",
            path=directory,
        )

        def work(report):
            return self.scanner.scan_folder(
                directory,
                source_id=source_id,
                progress=lambda message: report(message),
            )

        self._run(work, "Scanning…", self._on_scan_done)

    def rescan_all(self) -> None:
        sources = [row for row in self.library.list_sources() if row["type"] == "rom_folder"]

        def work(report):
            from rose_gamelab.core.scanner import ScanResult
            from rose_gamelab.sources.steam import SteamProvider

            combined = ScanResult()

            steam = SteamProvider()
            if steam.validate():
                report("Reading Steam library…")
                result = self.library.import_entries(steam.discover(), source_id="steam")
                combined.imported.added += result.added
                combined.imported.merged += result.merged

            for index, source in enumerate(sources, start=1):
                if not source["path"]:
                    continue
                report(f"Scanning {source['name']}…", index, len(sources))
                result = self.scanner.scan_folder(
                    source["path"],
                    system=source["system"],
                    source_id=source["id"],
                )
                combined.files_seen += result.files_seen
                combined.games_found += result.games_found
                combined.imported.added += result.imported.added
                combined.imported.updated += result.imported.updated
                combined.errors.extend(result.errors)

            return combined

        self._run(work, "Scanning sources…", self._on_scan_done)

    def _on_scan_done(self, result) -> None:
        self.refresh()

        added = result.imported.added
        message = f"Added {added} game{'s' if added != 1 else ''}"
        if result.imported.updated:
            message += f", updated {result.imported.updated}"
        if result.errors:
            message += f" — {len(result.errors)} issue(s)"

        self.status.setText(message)

    # ── Scraping ──────────────────────────────────────────────────

    def scrape_one(self, game_id: int) -> None:
        def work(report):
            report("Looking for art and info…")
            return self.scraper.scrape_game(game_id)

        def done(found):
            self._on_game_selected(game_id)
            game = self.library.get(game_id)
            if game:
                self.grid.refresh_game(game)
            self.status.setText(
                "Found art and info" if found else "Nothing found for that game"
            )

        self._run(work, "Searching…", done)

    def scrape_all(self) -> None:
        def work(report):
            return self.scraper.scrape_library(
                progress=lambda state, title: report(
                    f"{title}", state.processed, state.total
                )
            )

        def done(state):
            self.refresh()
            self.status.setText(
                f"Found art for {state.art_found}, info for {state.metadata_found}"
                f" of {state.total} games"
            )

        self._run(work, "Finding art…", done)

    # ── Browse ────────────────────────────────────────────────────

    def load_chart(self, system_id: str) -> None:
        """Load the chart for a system on a worker thread."""
        from rose_gamelab.metadata.base import ProviderError
        from rose_gamelab.metadata.charts import SteamCharts, chart_for_system

        self.browse.show_loading()

        def work(report):
            report("Loading charts…")
            chart = chart_for_system(system_id, self.library)

            # Steam's chart returns appids only; resolve real titles so the
            # list is readable rather than a column of numbers.
            if chart.source == "steam":
                report("Reading game names…")
                SteamCharts().resolve_titles(chart, self.scraper.steam)

            return chart

        def done(chart):
            self.browse.show_chart(chart)

        try:
            self._run(work, "Loading charts…", done)
        except ProviderError as exc:
            self.browse.show_error(str(exc))

    # ── Windows ───────────────────────────────────────────────────

    def open_settings(self) -> None:
        from rose_gamelab.ui.settings import SettingsDialog

        dialog = SettingsDialog(self.library, self.profiles, self.theme, parent=self)
        dialog.theme_changed.connect(self.apply_theme)
        dialog.exec()

    def open_big_picture(self) -> None:
        from rose_gamelab.ui.big_picture import BigPictureWindow

        self.big_picture = BigPictureWindow(
            self.library, self.launcher, self.theme, parent=None
        )
        self.big_picture.showFullScreen()

    def apply_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.setStyleSheet(stylesheet(theme))
        self.grid.theme = theme
        self.details.theme = theme
        self._refresh_grid()

    # ── Background work ───────────────────────────────────────────

    def _run(self, work, message: str, on_done) -> None:
        """Run `work` on a thread, showing real progress while it runs."""
        if self._thread is not None and self._thread.isRunning():
            QMessageBox.information(
                self, "Busy", "Something is already running. Let it finish first."
            )
            return

        self.status.setText(message)
        self.progress.setRange(0, 0)   # indeterminate until we know the total
        self.progress.show()

        self._thread = QThread()
        self._worker = Worker(work)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(lambda result: self._finish(result, on_done))

        self._thread.start()

    def _on_progress(self, message: str, done: int, total: int) -> None:
        self.status.setText(message)
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    def _on_failed(self, message: str) -> None:
        self.progress.hide()
        self._teardown_thread()

        # A failed chart load belongs in the browse view, not a modal that
        # interrupts whatever the user was doing.
        if self.pages.currentWidget() is self.browse:
            self.browse.show_error(message)
        else:
            QMessageBox.warning(self, "Something went wrong", message)

        self.status.setText("Failed")

    def _finish(self, result, on_done) -> None:
        self.progress.hide()
        self._teardown_thread()
        on_done(result)

    def _teardown_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
            self._worker = None

    def closeEvent(self, event) -> None:
        # Games keep running; GameLab exiting should not kill what is being
        # played. Only our own worker threads are stopped.
        self.scraper.cancel()
        self._teardown_thread()
        super().closeEvent(event)
