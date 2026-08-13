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
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core.controller_profiles import ControllerProfileStore
from rose_gamelab.core.emulator import get_system
from rose_gamelab.core.launcher import Launcher, LaunchError
from rose_gamelab.core.library import NO_SOURCE, Library
from rose_gamelab.core.profiles import ProfileStore
from rose_gamelab.core.scanner import RomScanner
from rose_gamelab.core.system_settings import SystemSettingsStore
from rose_gamelab.db.database import Database
from rose_gamelab.metadata.retroachievements import on_retroachievements
from rose_gamelab.metadata.scraper import Scraper
from rose_gamelab.ui.branding import APP_NAME
from rose_gamelab.ui.controller_watch import ControllerWatcher
from rose_gamelab.ui.preferences import Preferences, retroachievements_credentials
from rose_gamelab.ui.theme import (
    COVER_WIDTHS,
    SPACING,
    Appearance,
    Theme,
    set_active_style,
)
from rose_gamelab.ui.widgets.browse_view import BrowseView
from rose_gamelab.ui.widgets.controller_indicator import ControllerIndicator
from rose_gamelab.ui.widgets.detail_panel import DetailPanel
from rose_gamelab.ui.widgets.game_grid import GameGrid
from rose_gamelab.ui.widgets.game_page import GamePage
from rose_gamelab.ui.widgets.sidebar import Sidebar
from rose_gamelab.ui.worker import Worker

if TYPE_CHECKING:                    # imported lazily at runtime
    from rose_gamelab.metadata.retroachievements import RetroAchievementsProvider

logger = logging.getLogger(__name__)

SORT_OPTIONS = [
    ("Title", "title"),
    ("Recently played", "last_played"),
    ("Recently added", "added"),
    ("Playtime", "playtime"),
    ("Release date", "release"),
    ("Rating", "rating"),
]


class MainWindow(QMainWindow):
    """Rose GameLab's main window."""

    def __init__(
        self,
        database: Database,
        *,
        preferences: Optional[Preferences] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self.db = database
        self.library = Library(database)
        self.profiles = ProfileStore(database)
        self.profiles.ensure_default_exists()
        self.scanner = RomScanner(self.library)
        self.controller_store = ControllerProfileStore(database)
        self.system_settings = SystemSettingsStore(database)
        self.launcher = Launcher(
            self.library, self.profiles,
            controller_profiles=self.controller_store,
            system_settings=self.system_settings,
        )
        self.scraper = Scraper(self.library)

        # Loaded rather than defaulted: the theme picker used to change the
        # running window and be forgotten the moment GameLab closed.
        self.preferences = preferences if preferences is not None else Preferences.load()
        self.appearance = self.preferences.appearance()
        self.theme = self.appearance.theme
        set_active_style(self.appearance.style)
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None
        self._on_done = None
        self._filter = "all"
        self._search = ""
        self._sort = "title"

        self.setWindowTitle(APP_NAME)
        self.resize(1440, 900)
        self.setStyleSheet(self.appearance.stylesheet())

        # Dropping a ROM onto the window is the shortest path from "this is in
        # my Downloads" to "this is in my library".
        self.setAcceptDrops(True)

        self._build()
        self._build_shortcuts()
        self._build_controller_watch()
        self.refresh()

        # Steam is found and imported on its own. Making the user press
        # "import Steam" to see games that are plainly installed on the machine
        # is busywork the launcher can just do.
        # Checking every source on every launch is the right default and the
        # wrong behaviour to force: a settled library does not change between
        # openings, and the scan is not invisible.
        if self.preferences.scan_on_start:
            QTimer.singleShot(200, self.auto_import_steam)
        elif self.preferences.art_on_start:
            # Art is a separate promise from finding games.
            QTimer.singleShot(400, self.scrape_missing_art_quietly)

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

        self.grid = GameGrid(self.theme, style=self.appearance.style)
        self.grid.game_selected.connect(self.open_game_page)
        self.grid.game_activated.connect(lambda gid: self.launch(gid, None))
        self.grid.game_context_requested.connect(self.open_game_menu)
        self.pages.addWidget(self.grid)

        self.browse = BrowseView(self.library, self.theme)
        self.browse.refresh_requested.connect(self.load_chart)
        self.pages.addWidget(self.browse)

        # Clicking a game gives it the window. The right-hand panel could show
        # a cover and a few facts and no more; a hundred achievements and a
        # notes box need the room.
        self.game_page = GamePage(self.theme)
        self.game_page.back_requested.connect(self.close_game_page)
        self.game_page.launch_requested.connect(self.launch)
        self.game_page.favorite_toggled.connect(self._on_favorite)
        self.game_page.scrape_requested.connect(self.scrape_one)
        self.game_page.art_requested.connect(self.choose_art)
        self.game_page.remove_requested.connect(self.remove_game)
        self.game_page.achievements_requested.connect(self.refresh_achievements)
        self.game_page.notes_changed.connect(self._save_notes)
        self.pages.addWidget(self.game_page)

        middle.addWidget(self.pages, 1)

        middle.addWidget(self._build_status_bar())
        layout.addLayout(middle, 1)

        self.details = DetailPanel(self.theme)
        self.details.launch_requested.connect(self.launch)
        self.details.favorite_toggled.connect(self._on_favorite)
        self.details.scrape_requested.connect(self.scrape_one)
        self.details.remove_requested.connect(self.remove_game)
        layout.addWidget(self.details)

        self.setCentralWidget(central)

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(66)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(SPACING, 11, SPACING, 11)
        layout.setSpacing(10)

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
        bar.setFixedHeight(34)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(SPACING, 0, SPACING, 0)
        layout.setSpacing(10)

        self.status = QLabel()
        self.status.setObjectName("Subtle")
        layout.addWidget(self.status)

        layout.addStretch(1)

        self.controller_indicator = ControllerIndicator(self.theme)
        layout.addWidget(self.controller_indicator)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.hide()
        layout.addWidget(self.progress)

        return bar

    def _build_controller_watch(self) -> None:
        """Notice pads being plugged in while GameLab is already running.

        Which is the normal order of events: you sit down, then pick up the pad.
        """
        self.controllers = ControllerWatcher(self)
        self.controllers.changed.connect(self._controllers_changed)
        self.controllers.start()

    def _controllers_changed(self, statuses: list) -> None:
        self.controller_indicator.set_statuses(statuses)
        # Big Picture, if it is open, shows the same information.
        big_picture = getattr(self, "big_picture", None)
        if big_picture is not None and big_picture.isVisible():
            big_picture.set_controllers(statuses)

    def _build_shortcuts(self) -> None:
        for key, slot in (
            ("Ctrl+F", lambda: self.search.setFocus()),
            ("Ctrl+R", self.rescan_all),
            ("Ctrl+B", self.open_big_picture),
            # Shift+Tab, as Steam trained everyone to expect.
            ("Shift+Tab", self.toggle_game_overlay),
            # Wrapped: QAction.triggered would otherwise pass its `checked`
            # flag in as the list of files to organise.
            ("Ctrl+O", lambda: self.organise_roms()),
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

        # Games whose source was removed. Listed only when there are some, but
        # listed unconditionally then — without this row they cannot be
        # selected, filtered or deleted by any means in the interface.
        orphaned = self.library.count_orphaned_games()
        if orphaned:
            sources.append((NO_SOURCE, "❓", "No source", orphaned))

        self.sidebar.set_sources(sources)

        self.sidebar.set_collections([
            (str(row["id"]), row["icon"] or "📚", row["name"], row["game_count"])
            for row in self.library.list_collections()
        ])

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
        elif self._filter == "hidden":
            kwargs["hidden_only"] = True
        elif self._filter == "recent":
            # Filtered, not merely sorted. A shelf whose purpose is picking up
            # where you left off should not be padded out with games that have
            # never been started.
            kwargs["played_only"] = True
            kwargs["sort"] = "last_played"
            kwargs["descending"] = True
        elif self._filter.startswith("system:"):
            kwargs["system"] = self._filter.split(":", 1)[1]
        elif self._filter.startswith("source:"):
            kwargs["source_id"] = self._filter.split(":", 1)[1]
        elif self._filter.startswith("collection:"):
            kwargs["collection_id"] = int(self._filter.split(":", 1)[1])

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

    # ── The game page ─────────────────────────────────────────────

    def open_game_page(self, game_id: int) -> None:
        """Give one game the window."""
        game = self.library.get(game_id)
        if game is None:
            return

        # Keeps the right-hand panel in step, so going back shows the same game.
        self._on_game_selected(game_id)

        from rose_gamelab.metadata.retroachievements import (
            achievements_for,
        )

        self.game_page.show_game(
            game,
            self.library.launch_options_for(game_id),
            self.library.tags_for(game_id),
            achievements_for(self.db, game_id),
            achievements_available=self._achievements_provider().available(),
            achievements_supported=on_retroachievements(game.system),
            play_history=self.library.play_history(game_id),
        )
        self.pages.setCurrentWidget(self.game_page)
        self.game_page.setFocus()

        # Screenshots mean walking several directories, so they arrive a moment
        # later rather than holding the page open.
        QTimer.singleShot(0, lambda: self._load_screenshots(game_id))

    def _load_screenshots(self, game_id: int) -> None:
        from rose_gamelab.core import folder_games, screenshots

        game = self.library.get(game_id)
        if game is None or self.game_page.game is None:
            return
        if self.game_page.game.id != game_id:
            return                       # the user has already moved on

        names = [game.title]
        files = self.library.files_for(game_id)
        if files:
            found = folder_games.game_root_for(files[0]["path"])
            names.append(found.root.name if found else Path(files[0]["path"]).stem)

        try:
            shots = screenshots.find_for_game(names)
        except OSError as exc:
            logger.debug("could not look for screenshots: %s", exc)
            return

        if self.game_page.game is not None and self.game_page.game.id == game_id:
            self.game_page._set_screenshots(shots)

    def close_game_page(self) -> None:
        self.pages.setCurrentWidget(self.grid)
        self.grid.setFocus()

    def _save_notes(self, game_id: int, text: str) -> None:
        """Store a note. Never scraped over — it is the user's own writing."""
        self.library.update_game(game_id, notes=text or None)

    def _achievements_provider(self) -> "RetroAchievementsProvider":
        """A provider carrying whatever credentials the user has stored.

        Credentials moved into `credentials.json` when Settings grew a
        RetroAchievements tab, but the three places that build a provider were
        still constructing it with none. So a key entered in Settings was
        written, read back by Settings — which is why it looked saved — and
        ignored by everything else: achievements said "add your API key", and
        Refresh stayed disabled.

        The window holds no config object, so the stored credentials are read
        directly; `credentials_from_config` falls back to the same file for
        anyone still setting them in YAML.
        """
        from rose_gamelab.metadata.retroachievements import RetroAchievementsProvider

        username, key = retroachievements_credentials()
        return RetroAchievementsProvider(username, key)

    def refresh_achievements(self, game_id: int) -> None:
        """Fetch this game's achievements and the user's progress."""
        from rose_gamelab.metadata.retroachievements import (
            link_game,
            save_achievements,
        )

        provider = self._achievements_provider()
        if not provider.available():
            QMessageBox.information(
                self, "RetroAchievements",
                "Add your RetroAchievements username and API key in "
                "Settings → RetroAchievements first — achievements are tied "
                "to your account.",
            )
            return

        game = self.library.get(game_id)
        if game is None:
            return

        row = self.db.query_one(
            "SELECT ra_game_id FROM games WHERE id = ?", (game_id,)
        )
        ra_id = row["ra_game_id"] if row else None

        def work(report):
            report("Looking up achievements…")
            identifier = ra_id or self._match_retroachievements(game)
            if identifier is None:
                return None

            found = provider.achievements(identifier)
            link_game(self.db, game_id, identifier, None)
            save_achievements(self.db, game_id, found)
            return len(found)

        def done(count):
            if count is None:
                self.status.setText(f"{game.title} is not on RetroAchievements")
            else:
                self.status.setText(f"{count} achievements for {game.title}")
            # Reopen so the page shows what was just stored.
            if self.pages.currentWidget() is self.game_page:
                self.open_game_page(game_id)

        self._run(work, "Fetching achievements…", done)

    def _match_retroachievements(self, game) -> Optional[int]:
        """Find this game on RetroAchievements — by hash where we can, by name otherwise.

        The hash is the right answer: it identifies the exact dump. But GameLab
        only implements it for the cartridge systems whose algorithm was
        verified, and RetroAchievements covers plenty it cannot hash — PS2 among
        them, which is most of what is in a modern library. Refusing to look
        those up at all meant achievements were unreachable for whole consoles.

        So a title match is tried second, with a high bar and a refusal on a
        near miss: showing another game's achievements would be worse than
        showing none.
        """
        from rose_gamelab.metadata.retroachievements import (
            UnverifiedHashAlgorithm,
            on_retroachievements,
            ra_hash,
            supports_hashing,
        )

        if not on_retroachievements(game.system):
            return None

        provider = self._achievements_provider()
        console_id = provider.console_id_for(game.system)
        if console_id is None:
            return None

        if supports_hashing(game.system):
            for row in self.library.files_for(game.id):
                try:
                    digest = ra_hash(row["path"], game.system)
                except (UnverifiedHashAlgorithm, OSError) as exc:
                    logger.debug("could not hash %s for RA: %s", row["path"], exc)
                    continue

                found = provider.find_game_by_hash(console_id, digest)
                if found is not None:
                    return found

        # No hash, or the hash matched nothing — fall back to the name.
        return provider.find_game_by_title(console_id, game.title)

        return None

    def remove_game(self, game_id: int) -> None:
        """Remove one game from the library. Never touches the files."""
        game = self.library.get(game_id)
        if game is None:
            return

        confirmed = QMessageBox.question(
            self, "Remove from library",
            f"Remove {game.title} from your library?\n\n"
            "The files stay on your disk — only GameLab's entry, its playtime "
            "and its artwork are removed.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        self.library.remove_game(game_id)
        self.refresh()
        self.status.setText(f"Removed {game.title}")

    # ── Right-click menu ──────────────────────────────────────────

    def open_game_menu(self, game_id: int, position) -> None:
        """Show the right-click menu for one game at `position`."""
        menu = self.build_game_menu(game_id)
        if menu is not None:
            menu.exec(position)

    def build_game_menu(self, game_id: int) -> Optional[QMenu]:
        """Everything you can do to one game, as a menu. None if it is gone.

        Reaching a single game's options used to mean selecting it, finding the
        detail panel, and — for removing it — the Settings window, which only
        offered removing a whole system or source at once. Everything here acts
        on exactly the game under the cursor.

        Built separately from showing it so the contents can be checked without
        a live event loop.
        """
        game = self.library.get(game_id)
        if game is None:
            return None

        # Right-clicking a game you have not selected should act on THAT game,
        # and the selection should follow so the detail panel agrees.
        if self.grid.selected_id != game_id:
            self.grid.select(game_id)

        menu = QMenu(self)
        menu.setStyleSheet(self.appearance.stylesheet())

        options = self.library.launch_options_for(game_id)

        play = menu.addAction("Play")
        play.triggered.connect(lambda: self.launch(game_id, None))
        play.setEnabled(bool(options))

        # More than one way to play — a RetroArch core and a standalone
        # emulator, say, or Steam and a local install — so offer each by name
        # rather than silently picking the default.
        if len(options) > 1:
            submenu = menu.addMenu("Play with")
            for option in options:
                label = option["label"] or option["kind"].title()
                action = submenu.addAction(label)
                action.triggered.connect(
                    lambda _checked=False, oid=option["id"]: self.launch(game_id, oid)
                )

        menu.addSeparator()

        favourite = menu.addAction(
            "Remove from favourites" if game.favorite else "Add to favourites"
        )
        favourite.triggered.connect(
            lambda: self._on_favorite(game_id, not game.favorite)
        )

        collections = menu.addMenu("Collections")
        self._fill_collections_menu(collections, game_id)

        menu.addSeparator()

        art = menu.addAction("Add art…")
        art.triggered.connect(lambda: self.choose_art(game_id))

        find = menu.addAction("Find art and info")
        find.triggered.connect(lambda: self.scrape_one(game_id))

        if game.cover_path:
            clear = menu.addAction("Remove art")
            clear.triggered.connect(lambda: self.clear_art(game_id))

        menu.addSeparator()

        hide = menu.addAction("Unhide" if game.hidden else "Hide")
        hide.triggered.connect(lambda: self.set_hidden(game_id, not game.hidden))

        remove = menu.addAction("Remove from library…")
        remove.triggered.connect(lambda: self.remove_game(game_id))

        return menu

    def _fill_collections_menu(self, menu, game_id: int) -> None:
        """Tick the collections this game is in; clicking one toggles it."""
        member_of = set(self.library.collections_for(game_id))

        for row in self.library.list_collections():
            action = menu.addAction(f"{row['icon'] or '📚'}  {row['name']}")
            action.setCheckable(True)
            action.setChecked(row["id"] in member_of)
            action.triggered.connect(
                lambda checked, cid=row["id"]: self._set_collection(
                    game_id, cid, checked
                )
            )

        menu.addSeparator()
        new = menu.addAction("New collection…")
        new.triggered.connect(lambda: self.new_collection(game_id))

    def _set_collection(self, game_id: int, collection_id: int, member: bool) -> None:
        if member:
            self.library.add_to_collection(collection_id, game_id)
        else:
            self.library.remove_from_collection(collection_id, game_id)
        self._refresh_sidebar()

    def new_collection(self, game_id: Optional[int] = None) -> None:
        """Make a collection, optionally putting a game straight into it."""
        from PySide6.QtWidgets import QInputDialog

        name, chosen = QInputDialog.getText(
            self, "New collection", "What is it called?"
        )
        if not chosen or not name.strip():
            return

        collection_id = self.library.create_collection(name.strip())
        if game_id is not None:
            self.library.add_to_collection(collection_id, game_id)

        self._refresh_sidebar()
        self.status.setText(f"Created {name.strip()}")

    def choose_art(self, game_id: int) -> None:
        """Set a game's cover from a file the user picks.

        Copied into the artwork cache rather than referenced where it sits, so
        the cover survives the original being moved, renamed or deleted.
        """
        game = self.library.get(game_id)
        if game is None:
            return

        path, _ = QFileDialog.getOpenFileName(
            self, f"Choose cover art for {game.title}", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.gif)",
        )
        if not path:
            return

        stored = self.scraper.cache.store_file(
            self.scraper.cache_key(game), "cover", Path(path)
        )
        if stored is None:
            QMessageBox.warning(
                self, "Could not use that image",
                "That file could not be read as an image.",
            )
            return

        # Only the ART is locked. Locking the whole entry would stop the game
        # ever gaining a description or a release date, which is not what
        # "I picked my own cover" should mean.
        self.library.update_game(
            game_id, cover_path=str(stored), cover_locked=1
        )
        self._after_art_change(game_id, "Art updated")

    def clear_art(self, game_id: int) -> None:
        """Drop a game's cover so it can be scraped or chosen again."""
        game = self.library.get(game_id)
        if game is None:
            return

        self.scraper.cache.remove(self.scraper.cache_key(game), "cover")
        self.library.update_game(game_id, cover_path=None, cover_locked=0)
        self._after_art_change(game_id, "Art removed")

    def _after_art_change(self, game_id: int, message: str) -> None:
        from rose_gamelab.ui.widgets.game_card import clear_cover_cache

        # The card cache is keyed on path and size, and a replaced cover can
        # reuse a path, so it is dropped rather than left showing the old image.
        clear_cover_cache()

        game = self.library.get(game_id)
        if game:
            self.grid.refresh_game(game)
        self._on_game_selected(game_id)
        self.status.setText(message)

    def set_hidden(self, game_id: int, hidden: bool) -> None:
        self.library.set_hidden(game_id, hidden)
        self.refresh()
        self.status.setText("Hidden" if hidden else "Shown again")

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
        """Open the guided Add Source dialog."""
        from rose_gamelab.ui.add_source import AddSourceDialog

        dialog = AddSourceDialog(self.library, self.theme, parent=self)
        dialog.source_chosen.connect(self._scan_new_source)
        dialog.exec()

        if dialog.wants_organiser:
            self.organise_roms()
        elif dialog.wants_manual_entry:
            self.add_game_manually()

    def add_game_manually(self) -> None:
        """Add a game GameLab cannot detect: a launcher, a script, anything."""
        from rose_gamelab.ui.add_game import AddGameDialog

        dialog = AddGameDialog(self.library, self.theme, parent=self)
        dialog.game_added.connect(self._game_added)
        dialog.exec()

    def _game_added(self, game_id: int) -> None:
        self.refresh()
        self.grid.select(game_id)
        self._on_game_selected(game_id)

        game = self.library.get(game_id)
        self.status.setText(f"Added {game.title}" if game else "Added")

    # ── Organising loose ROMs ─────────────────────────────────────

    def organise_roms(self, paths: Optional[list] = None) -> None:
        """Open the ROM organiser, optionally pre-loaded with dropped files."""
        from rose_gamelab.ui.rom_import_dialog import RomImportDialog

        dialog = RomImportDialog(self.theme, paths=paths, parent=self)
        dialog.library_changed.connect(self._organised)
        dialog.exec()

    def _organised(self) -> None:
        """Pick up whatever was just filed into the ROM folder.

        The organiser's destination is registered as a source so the games it
        files show up in the library rather than only on disk.
        """
        from rose_gamelab.core.rom_import import default_library_root

        root = str(default_library_root())
        self.library.register_source(
            f"roms:{root}", name="ROM Library", type="rom_folder", path=root, system=None
        )
        self.rescan_all()

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
            self.organise_roms(paths)

    def _scan_new_source(self, kind: str, directory: str, system) -> None:
        source_id = f"roms:{directory}"
        self.library.register_source(
            source_id,
            name=directory.rstrip("/").rsplit("/", 1)[-1] or directory,
            type="rom_folder",
            path=directory,
            system=system,
        )

        def work(report):
            return self.scanner.scan_folder(
                directory,
                system=system,
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

    # ── Steam auto-detect ─────────────────────────────────────────

    def auto_import_steam(self) -> None:
        """Find and import Steam in the background, without being asked.

        Runs on every start, so newly installed games appear by themselves.
        Import is a merge, not a replace, so nothing the user has curated is
        disturbed and re-running costs nothing.
        """
        from rose_gamelab.sources.steam import SteamProvider

        provider = SteamProvider()
        if not provider.validate():
            # No Steam on this machine. Not an error, and not worth a message.
            return

        if self._thread is not None and self._thread.isRunning():
            return

        def work(report):
            report("Checking Steam…")
            games = provider.discover()
            report(f"Found {len(games)} Steam games")
            return self.library.import_entries(games, source_id="steam")

        def done(result):
            if result.added:
                self.refresh()
                self.status.setText(
                    f"Added {result.added} new Steam game"
                    f"{'s' if result.added != 1 else ''}"
                )
            else:
                self.status.setText(f"{self.library.count()} games")

            # Anything still without a cover gets one, unprompted.
            if self.preferences.art_on_start:
                QTimer.singleShot(400, self.scrape_missing_art_quietly)

        self._run(work, "Checking Steam…", done)

    def scrape_missing_art_quietly(self) -> None:
        """Fetch art for games that have none, without the user asking.

        Only runs when something is actually missing, so a fully-scraped
        library starts instantly and silently.
        """
        missing = [g for g in self.library.list_games(include_hidden=True) if not g.cover_path]
        if not missing or (self._thread is not None and self._thread.isRunning()):
            return

        def work(report):
            return self.scraper.scrape_library(
                only_missing=True,
                progress=lambda state, title: report(title, state.processed, state.total),
            )

        def done(state):
            self.refresh()
            self.status.setText(
                f"{self.library.count()} games · found art for {state.art_found}"
                if state.art_found else f"{self.library.count()} games"
            )

        self._run(work, f"Finding art for {len(missing)} games…", done)

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

        dialog = SettingsDialog(
            self.library, self.profiles, self.theme,
            parent=self, preferences=self.preferences,
        )
        # appearance_changed carries the style too, so it supersedes
        # theme_changed; connecting both would just repaint twice.
        dialog.appearance_changed.connect(self.apply_appearance)
        dialog.artwork_key_changed.connect(self._reload_artwork_key)
        # Removing a source changes what the grid should be showing, so it is
        # redrawn immediately rather than staying stale until the next restart.
        dialog.sources_changed.connect(self.refresh)
        dialog.exec()
        self.refresh()

    def _reload_artwork_key(self) -> None:
        """Pick up a key the user just saved, without restarting."""
        from rose_gamelab.metadata.steamgriddb import SteamGridDBProvider

        self.scraper.griddb = SteamGridDBProvider()
        self.status.setText(
            "Artwork key saved" if self.scraper.griddb.available()
            else "Artwork key cleared"
        )

    def toggle_game_overlay(self) -> None:
        """Show the panel for whatever is running, or hide it if it is up.

        Does nothing when no game is running: a panel about a game that is not
        being played has nothing to say.
        """
        overlay = getattr(self, "game_overlay", None)
        if overlay is not None and overlay.isVisible():
            overlay.close()
            return

        running = next(iter(self.launcher.running.values()), None)
        if running is None:
            self.status.setText("Nothing is running to show a panel for")
            return

        game = self.library.get(running.game_id)
        if game is None:
            return

        if overlay is None:
            from rose_gamelab.ui.game_overlay import GameOverlay

            overlay = GameOverlay(self.library, self.theme)
            self.game_overlay = overlay

        overlay.show_for(game, controllers=self.controllers.statuses)

    def open_big_picture(self) -> None:
        from rose_gamelab.ui.big_picture import BigPictureWindow

        self.big_picture = BigPictureWindow(
            self.library, self.launcher, self.theme, parent=None
        )
        # Whatever is already connected, rather than waiting for the next poll.
        self.big_picture.set_controllers(self.controllers.statuses)
        self.big_picture.showFullScreen()
        # Ask the compositor for the keyboard, rather than assuming a new
        # full-screen window is given it: without this the main window can keep
        # focus and Big Picture ignores every key.
        self.big_picture.raise_()
        self.big_picture.activateWindow()
        self.big_picture.setFocus()

    def apply_theme(self, theme: Theme) -> None:
        """Repaint in a new colour scheme, keeping the current style."""
        self.apply_appearance(Appearance(theme=theme, style=self.appearance.style))

    def apply_appearance(self, appearance: Appearance) -> None:
        """Repaint in a new theme AND style.

        Applied live rather than on close: choosing between twenty-five themes
        and ten styles is something you do by looking at them, not by reading
        their names.
        """
        self.appearance = appearance
        self.theme = appearance.theme
        # Dialogs read these when they are constructed, which is always after
        # this point, so they open in the shape the user chose.
        set_active_style(appearance.style)
        self.setStyleSheet(appearance.stylesheet())

        # Repainted in place. Rebuilding every card cost 100ms in a 500-game
        # library and ran on every tick of an appearance slider, which is most
        # of what made them feel broken.
        self.grid.restyle(appearance.theme, appearance.style)
        self.game_page.restyle(appearance.theme, appearance.style)
        self.details.restyle(appearance.theme)
        self.sidebar.restyle(appearance.theme)
        self.browse.restyle(appearance.theme)

        # Cover size is part of the style, so a style change resizes the grid —
        # the one appearance change that genuinely does rebuild the cards.
        width = COVER_WIDTHS.get(appearance.style.cover_size)
        if width and hasattr(self, "size_picker"):
            index = self.size_picker.findData(width)
            if index >= 0 and index != self.size_picker.currentIndex():
                self.size_picker.setCurrentIndex(index)

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
        self._on_done = on_done

        # These MUST be bound methods of this window, connected queued.
        #
        # A lambda has no thread affinity, so Qt cannot tell which thread should
        # receive the signal and falls back to a direct connection — running the
        # handler ON the worker thread. _finish() then tears that thread down
        # from inside itself, which Qt reports as "thread tried to wait on
        # itself" and which kills the process.
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(
            self._on_progress, Qt.ConnectionType.QueuedConnection
        )
        self._worker.failed.connect(
            self._on_failed, Qt.ConnectionType.QueuedConnection
        )
        self._worker.finished.connect(
            self._finish, Qt.ConnectionType.QueuedConnection
        )

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

    def _finish(self, result) -> None:
        self.progress.hide()
        self._teardown_thread()

        on_done, self._on_done = self._on_done, None
        if on_done is not None:
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
