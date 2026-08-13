"""Settings: themes, launch profiles, sources and maintenance.

Everything here writes through to the database or the config file immediately;
there is no separate "apply" step that can silently fail to happen.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core.profiles import (
    LaunchProfile,
    ProfileStore,
    parse_resolution,
    with_resolution,
)
from rose_gamelab.ui.preferences import (
    STYLE_AXES,
    STYLE_RANGES,
    Preferences,
    artwork_key,
    retroachievements_credentials,
    set_artwork_key,
    set_retroachievements_credentials,
)
from rose_gamelab.ui.theme import (
    COVER_WIDTHS,
    RADIUS,
    SPACING,
    Theme,
    list_style_names,
    list_theme_names,
)


class SettingsDialog(QDialog):
    """The settings window."""

    theme_changed = Signal(object)
    #: The whole look changed — theme, style, or one adjusted axis.
    appearance_changed = Signal(object)
    #: A source was removed, so the library behind this dialog is now stale.
    sources_changed = Signal()
    #: The artwork key changed, so the scraper needs rebuilding with it.
    artwork_key_changed = Signal()
    #: "Match now" was pressed; the main window owns the provider and threads.
    achievements_requested = Signal()

    def __init__(
        self,
        library,
        profiles: ProfileStore,
        theme: Theme,
        parent: Optional[QWidget] = None,
        preferences: Optional[Preferences] = None,
    ) -> None:
        super().__init__(parent)

        self.library = library
        self.profiles = profiles
        self.preferences = preferences if preferences is not None else Preferences.load()
        self.appearance = self.preferences.appearance()
        self.theme = theme

        # Dragging a slider fires a change per pixel. Restyling the whole
        # window on each one took ~160ms and made the sliders feel broken, so
        # changes are coalesced and applied once the value settles.
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(60)
        self._apply_timer.timeout.connect(self._apply)

        self.setWindowTitle("Settings")
        self.resize(680, 620)
        self.setStyleSheet(self.appearance.stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING, SPACING, SPACING, SPACING)

        from rose_gamelab.ui.widgets.retroarch_tab import RetroArchTab
        from rose_gamelab.ui.widgets.tools_tabs import (
            ControllersTab,
            SavesTab,
            SteamExportTab,
        )

        tabs = QTabWidget()
        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._startup_tab(), "Startup")
        tabs.addTab(self._profiles_tab(), "Launch Profiles")
        tabs.addTab(ControllersTab(theme), "Controllers")
        tabs.addTab(RetroArchTab(library, theme), "RetroArch")
        tabs.addTab(SavesTab(library, theme), "Saves")
        tabs.addTab(self._artwork_tab(), "Artwork")
        tabs.addTab(self._retroachievements_tab(), "RetroAchievements")
        tabs.addTab(self._sources_tab(), "Sources")
        tabs.addTab(self._remove_games_tab(), "Remove Games")
        tabs.addTab(SteamExportTab(library, theme), "Steam Export")
        tabs.addTab(self._about_tab(), "About")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    # ── Startup ───────────────────────────────────────────────────

    def _startup_tab(self) -> QWidget:
        """What the app does to itself when it opens."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        self.scan_on_start = QCheckBox("Check sources for new games when GameLab opens")
        self.scan_on_start.setChecked(self.preferences.scan_on_start)
        self.scan_on_start.toggled.connect(self._on_startup_changed)
        layout.addWidget(self.scan_on_start)

        scan_note = QLabel(
            "On, newly installed Steam games appear by themselves. Off, nothing "
            "is checked until you ask — use Add Source, or the refresh action, "
            "when you have installed something new."
        )
        scan_note.setObjectName("Subtle")
        scan_note.setWordWrap(True)
        layout.addWidget(scan_note)

        self.art_on_start = QCheckBox("Fetch missing cover art when GameLab opens")
        self.art_on_start.setChecked(self.preferences.art_on_start)
        self.art_on_start.toggled.connect(self._on_startup_changed)
        layout.addWidget(self.art_on_start)

        art_note = QLabel(
            "Separate from the scan on purpose: finding new games and reaching "
            "out to the network for pictures are different things to want."
        )
        art_note.setObjectName("Subtle")
        art_note.setWordWrap(True)
        layout.addWidget(art_note)

        achievements_note = QLabel(
            "Achievement settings live in the RetroAchievements tab."
        )
        achievements_note.setObjectName("Subtle")
        achievements_note.setWordWrap(True)
        layout.addWidget(achievements_note)

        layout.addStretch(1)
        return page

    def _fill_proton_versions(self) -> None:
        """List what is installed rather than asking somebody to remember it.

        Every build is a directory with its version in the name, so there is
        no reason to make anybody type "GE-Proton10-34" exactly.
        """
        from rose_gamelab.core import proton

        self.proton_version.addItem("Steam decides", None)

        try:
            versions = proton.installed()
        except Exception:
            versions = []

        for version in versions:
            self.proton_version.addItem(version.label, version.name)

        if not versions:
            self.proton_version.setPlaceholderText("GE-Proton10-34")

    def _select_proton(self, name) -> None:
        if not name:
            self.proton_version.setCurrentIndex(0)
            return

        for index in range(self.proton_version.count()):
            if self.proton_version.itemData(index) == name:
                self.proton_version.setCurrentIndex(index)
                return

        # Named in the profile but not installed any more — kept rather than
        # silently dropped, because losing somebody's choice is worse than
        # showing one that needs reinstalling.
        self.proton_version.addItem(f"{name}  (not installed)", name)
        self.proton_version.setCurrentIndex(self.proton_version.count() - 1)

    def _chosen_proton(self):
        data = self.proton_version.currentData()
        if data:
            return data
        # Typed rather than picked.
        typed = self.proton_version.currentText().strip()
        return typed or None if typed != "Steam decides" else None

    def _fill_resolutions(self) -> None:
        """Offer this machine's own screens first, then the usual sizes.

        A list that does not contain the resolution of the monitor somebody is
        looking at is a list that has failed at its one job.
        """
        from PySide6.QtGui import QGuiApplication

        from rose_gamelab.core.profiles import COMMON_RESOLUTIONS

        self.resolution.addItem("Leave to the game", None)

        seen: set[tuple[int, int]] = set()
        for screen in QGuiApplication.screens():
            size = (screen.geometry().width(), screen.geometry().height())
            if size in seen:
                continue
            seen.add(size)
            self.resolution.addItem(
                f"{size[0]} × {size[1]}  ({screen.name()})", size
            )

        for size in COMMON_RESOLUTIONS:
            if size in seen:
                continue
            seen.add(size)
            self.resolution.addItem(f"{size[0]} × {size[1]}", size)

    def _select_resolution(self, size) -> None:
        for index in range(self.resolution.count()):
            if self.resolution.itemData(index) == size:
                self.resolution.setCurrentIndex(index)
                return

        if size:
            # A resolution somebody typed by hand that no screen reports.
            self.resolution.addItem(f"{size[0]} × {size[1]}", size)
            self.resolution.setCurrentIndex(self.resolution.count() - 1)
        else:
            self.resolution.setCurrentIndex(0)

    def _on_resolution_chosen(self) -> None:
        # Saved through the same path as every other field on this form.
        if getattr(self, "_loading_profile", False):
            return
        self._save_profile()

    def _on_startup_changed(self) -> None:
        self.preferences.scan_on_start = self.scan_on_start.isChecked()
        self.preferences.art_on_start = self.art_on_start.isChecked()
        self.preferences.save()

    # ── Appearance ────────────────────────────────────────────────

    def _appearance_tab(self) -> QWidget:
        """Theme, style, and every individual aspect, mixable in any combination."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QWidget()
        form = QFormLayout(body)
        form.setSpacing(12)

        # ── Theme ────────────────────────────────────────────────
        self.theme_picker = QComboBox()
        for key, name in list_theme_names():
            self.theme_picker.addItem(name, key)
        self._select(self.theme_picker, self.preferences.theme)
        self.theme_picker.currentIndexChanged.connect(self._on_theme_changed)
        form.addRow("Colours", self.theme_picker)

        # ── Style ────────────────────────────────────────────────
        self.style_picker = QComboBox()
        for key, name in list_style_names():
            self.style_picker.addItem(name, key)
        self._select(self.style_picker, self.preferences.style)
        self.style_picker.currentIndexChanged.connect(self._on_style_changed)
        form.addRow("Shape", self.style_picker)

        note = QLabel(
            "Colours and shape are independent — any theme works with any "
            "style. Everything below adjusts one aspect on top of the style "
            "you picked, so you can build exactly the look you want."
        )
        note.setObjectName("Subtle")
        note.setWordWrap(True)
        form.addRow(note)

        # ── Individual axes ──────────────────────────────────────
        self.axis_widgets: dict[str, QWidget] = {}

        for axis, (label, kind) in STYLE_AXES.items():
            widget = self._axis_widget(axis, kind)
            if widget is not None:
                self.axis_widgets[axis] = widget
                form.addRow(label, widget)

        reset = QPushButton("Reset adjustments")
        reset.setToolTip("Back to the chosen style exactly as it ships.")
        reset.clicked.connect(self._reset_overrides)
        form.addRow("", reset)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        return page

    @staticmethod
    def _select(picker: QComboBox, key: str) -> None:
        index = picker.findData(key)
        if index >= 0:
            picker.setCurrentIndex(index)

    def _axis_widget(self, axis: str, kind: str) -> Optional[QWidget]:
        """One control for one style aspect, wired to update live."""
        value = self.preferences.value_for(axis)

        if kind == "int":
            low, high = STYLE_RANGES[axis]
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(low, high)
            # A pill style asks for a radius larger than any control; showing a
            # slider pinned to its maximum is honest about what it does.
            slider.setValue(max(low, min(high, int(value))))
            layout.addWidget(slider, 1)

            readout = QLabel(str(slider.value()))
            readout.setMinimumWidth(28)
            readout.setObjectName("Subtle")
            layout.addWidget(readout)

            def changed(new_value: int, axis=axis, readout=readout) -> None:
                # The number under the cursor updates immediately; the repaint
                # it implies is what gets coalesced.
                readout.setText(str(new_value))
                self._override(axis, new_value, immediate=False)

            slider.valueChanged.connect(changed)
            row.slider = slider          # so _reset_overrides can put it back
            row.readout = readout
            return row

        if kind == "bool":
            box = QCheckBox()
            box.setChecked(bool(value))
            box.toggled.connect(lambda on, axis=axis: self._override(axis, on))
            return box

        if kind == "choice" and axis == "cover_size":
            picker = QComboBox()
            for name in COVER_WIDTHS:
                picker.addItem(name.title(), name)
            self._select(picker, value)
            picker.currentIndexChanged.connect(
                lambda _i, axis=axis, picker=picker: self._override(axis, picker.currentData())
            )
            return picker

        return None

    # ── Applying ──────────────────────────────────────────────────

    def _on_theme_changed(self) -> None:
        self.preferences.theme = self.theme_picker.currentData()
        self._apply()

    def _on_style_changed(self) -> None:
        """Switching style drops the adjustments made to the previous one.

        Keeping them would be worse: a radius nudged for Sharp makes nonsense of
        Pill, and the user would have no way to tell which of their settings
        came from where.
        """
        self.preferences.style = self.style_picker.currentData()
        self.preferences.clear_overrides()
        self._sync_axis_widgets()
        self._apply()

    def _override(self, axis: str, value, *, immediate: bool = True) -> None:
        self.preferences.override(axis, value)
        if immediate:
            self._apply()
        else:
            self._apply_timer.start()

    def _reset_overrides(self) -> None:
        self.preferences.clear_overrides()
        self._sync_axis_widgets()
        self._apply()

    def _sync_axis_widgets(self) -> None:
        """Put every control back in step with what the style actually is."""
        for axis, widget in self.axis_widgets.items():
            value = self.preferences.value_for(axis)

            slider = getattr(widget, "slider", None)
            if slider is not None:
                low, high = STYLE_RANGES[axis]
                slider.blockSignals(True)
                slider.setValue(max(low, min(high, int(value))))
                slider.blockSignals(False)
                widget.readout.setText(str(slider.value()))
                continue

            if isinstance(widget, QCheckBox):
                widget.blockSignals(True)
                widget.setChecked(bool(value))
                widget.blockSignals(False)
                continue

            if isinstance(widget, QComboBox):
                widget.blockSignals(True)
                self._select(widget, value)
                widget.blockSignals(False)

    def _apply(self) -> None:
        """Repaint this dialog and the window behind it, and remember the choice."""
        self.appearance = self.preferences.appearance()
        self.theme = self.appearance.theme

        self.setStyleSheet(self.appearance.stylesheet())
        self.appearance_changed.emit(self.appearance)
        # Kept for anything still listening for colours alone.
        self.theme_changed.emit(self.appearance.theme)

        self.preferences.save()

    def closeEvent(self, event) -> None:
        """Apply anything still pending, so a quick close keeps the last nudge."""
        if self._apply_timer.isActive():
            self._apply_timer.stop()
            self._apply()
        super().closeEvent(event)

    # ── Artwork ───────────────────────────────────────────────────

    def _artwork_tab(self) -> QWidget:
        """Where the optional artwork key goes, and what it buys.

        Steam and the libretro archive need no credentials and cover most of a
        library between them. This is for the rest — launchers, fan games,
        storefront exclusives, dumps the archive has not got.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)

        note = QLabel(
            "GameLab finds most art on its own, with no account and no key.\n\n"
            "SteamGridDB covers what the free sources cannot: launchers like "
            "Sober, fan games, and console dumps the archive is missing. A key "
            "is free — create one at steamgriddb.com under Preferences → API."
        )
        note.setWordWrap(True)
        note.setObjectName("Subtle")
        layout.addWidget(note)

        row = QHBoxLayout()
        row.addWidget(QLabel("SteamGridDB key"))

        self.griddb_key = QLineEdit()
        self.griddb_key.setPlaceholderText("Paste your key here")
        self.griddb_key.setText(artwork_key() or "")
        # A credential should not sit on screen in plain text by default.
        self.griddb_key.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(self.griddb_key, 1)

        show = QCheckBox("Show")
        show.toggled.connect(
            lambda on: self.griddb_key.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        row.addWidget(show)
        layout.addLayout(row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        save = QPushButton("Save key")
        save.setObjectName("Primary")
        save.clicked.connect(self._save_artwork_key)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self.griddb_status = QLabel()
        self.griddb_status.setWordWrap(True)
        self.griddb_status.setObjectName("Subtle")
        self._show_key_status()
        layout.addWidget(self.griddb_status)

        layout.addStretch(1)
        return page

    # ── RetroAchievements ─────────────────────────────────────────

    def _retroachievements_tab(self) -> QWidget:
        """Achievements are tied to an account, so both halves are needed."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)

        note = QLabel(
            "Tracks achievements and your progress on retro games, and shows "
            "them on a game's page.\n\n"
            "Free — the key is on your RetroAchievements profile under "
            "Settings → Keys. Both the username and the key are needed, "
            "because progress is tied to your account."
        )
        note.setWordWrap(True)
        note.setObjectName("Subtle")
        layout.addWidget(note)

        user, key = retroachievements_credentials()

        row = QHBoxLayout()
        row.addWidget(QLabel("Username"))
        self.ra_username = QLineEdit()
        self.ra_username.setText(user or "")
        row.addWidget(self.ra_username, 1)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("API key"))
        self.ra_key = QLineEdit()
        self.ra_key.setText(key or "")
        self.ra_key.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(self.ra_key, 1)

        show = QCheckBox("Show")
        show.toggled.connect(
            lambda on: self.ra_key.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        row.addWidget(show)
        layout.addLayout(row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        save = QPushButton("Save credentials")
        save.setObjectName("Primary")
        save.clicked.connect(self._save_ra_credentials)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self.ra_status = QLabel()
        self.ra_status.setWordWrap(True)
        self.ra_status.setObjectName("Subtle")
        self._show_ra_status()
        layout.addWidget(self.ra_status)

        layout.addWidget(self._automatic_matching_box())

        # Hashing is exact and is implemented for five systems; everything else
        # RetroAchievements covers is matched by title instead. The old wording
        # here said matching only worked for those five, which was wrong and
        # would have told a PS2 owner not to bother.
        supported = QLabel(
            "Games are matched by content hash for NES, SNES, Game Boy, Game "
            "Boy Color and Mega Drive — exact, down to the dump. Everything "
            "else RetroAchievements supports, PlayStation and PS2 among them, "
            "is matched by title instead, and a near miss is refused rather "
            "than guessed at."
        )
        supported.setWordWrap(True)
        supported.setObjectName("Subtle")
        layout.addWidget(supported)

        layout.addStretch(1)
        return page

    def _automatic_matching_box(self) -> QWidget:
        """Everything about doing this without being asked."""
        box = QFrame()
        box.setObjectName("Card")
        layout = QVBoxLayout(box)
        layout.setSpacing(8)

        heading = QLabel("Automatically")
        heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(heading)

        self.achievements_on_start = QCheckBox(
            "Refresh my progress when GameLab opens"
        )
        self.achievements_on_start.setChecked(self.preferences.achievements_on_start)
        self.achievements_on_start.toggled.connect(self._on_achievements_changed)
        layout.addWidget(self.achievements_on_start)

        progress_note = QLabel(
            "Achievements are earned inside the emulator and nothing tells "
            "GameLab when it happens, so without this the numbers are from "
            "whenever you last opened that game."
        )
        progress_note.setObjectName("Subtle")
        progress_note.setWordWrap(True)
        layout.addWidget(progress_note)

        self.achievements_match_on_start = QCheckBox(
            "Look up games I have not checked yet, when GameLab opens"
        )
        self.achievements_match_on_start.setChecked(
            self.preferences.achievements_match_on_start
        )
        self.achievements_match_on_start.toggled.connect(self._on_achievements_changed)
        layout.addWidget(self.achievements_match_on_start)

        match_note = QLabel(
            "Games added later are matched without you opening them. A game "
            "found to have no achievement set is remembered, so it is never "
            "looked up twice — which is what stops this becoming a search for "
            "your whole library every time."
        )
        match_note.setObjectName("Subtle")
        match_note.setWordWrap(True)
        layout.addWidget(match_note)

        self.ra_counts = QLabel()
        self.ra_counts.setWordWrap(True)
        layout.addWidget(self.ra_counts)

        row = QHBoxLayout()
        row.addStretch(1)

        match_now = QPushButton("Match now")
        match_now.clicked.connect(self._match_achievements_now)
        row.addWidget(match_now)

        retry = QPushButton("Try the unmatched ones again")
        retry.clicked.connect(self._retry_unmatched)
        row.addWidget(retry)

        layout.addLayout(row)
        self._refresh_ra_counts()
        return box

    def _refresh_ra_counts(self) -> None:
        """Say where the library stands, so "nothing happened" is answerable."""
        linked = self.library.db.query_one(
            "SELECT COUNT(*) AS n FROM games WHERE ra_game_id IS NOT NULL"
        )["n"]
        nothing = self.library.db.query_one(
            "SELECT COUNT(*) AS n FROM games"
            " WHERE ra_game_id IS NULL AND ra_checked_at IS NOT NULL"
        )["n"]
        waiting = self.library.db.query_one(
            "SELECT COUNT(*) AS n FROM games"
            " WHERE ra_game_id IS NULL AND ra_checked_at IS NULL AND hidden = 0"
        )["n"]

        self.ra_counts.setText(
            f"{linked} game(s) matched  ·  {nothing} checked with no set  ·  "
            f"{waiting} not looked at yet"
        )

    def _on_achievements_changed(self) -> None:
        self.preferences.achievements_on_start = self.achievements_on_start.isChecked()
        self.preferences.achievements_match_on_start = (
            self.achievements_match_on_start.isChecked()
        )
        self.preferences.save()

    def _match_achievements_now(self) -> None:
        self.achievements_requested.emit()
        self.accept()

    def _retry_unmatched(self) -> None:
        """Forget every "no set here" answer so they are all tried again."""
        self.library.db.execute(
            "UPDATE games SET ra_checked_at = NULL"
            " WHERE ra_game_id IS NULL AND ra_checked_at IS NOT NULL"
        )
        self._refresh_ra_counts()

    def _show_ra_status(self) -> None:
        user, key = retroachievements_credentials()
        self.ra_status.setText(
            f"Signed in as {user}."
            if user and key else
            "Not set up — achievements already stored still show, but nothing "
            "new can be fetched."
        )

    def _save_ra_credentials(self) -> None:
        set_retroachievements_credentials(
            self.ra_username.text(), self.ra_key.text()
        )
        self._show_ra_status()
        self.artwork_key_changed.emit()

    def _show_key_status(self) -> None:
        if artwork_key():
            self.griddb_status.setText(
                "A key is set. It is also read from STEAMGRIDDB_API_KEY if you "
                "would rather not store it on disk."
            )
        else:
            self.griddb_status.setText(
                "No key set — the free sources are still used, so most games "
                "will still find art."
            )

    def _save_artwork_key(self) -> None:
        set_artwork_key(self.griddb_key.text())
        self._show_key_status()
        self.artwork_key_changed.emit()

    # ── Launch profiles ───────────────────────────────────────────

    def _profiles_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setSpacing(SPACING)

        self.profile_list = QListWidget()
        self.profile_list.setFixedWidth(190)
        self.profile_list.currentItemChanged.connect(self._on_profile_selected)
        layout.addWidget(self.profile_list)

        right = QVBoxLayout()

        self.profile_form = QFormLayout()
        self.profile_form.setSpacing(10)

        self.profile_name = QLineEdit()
        self.profile_form.addRow("Name", self.profile_name)

        self.use_gamemode = QCheckBox("Use gamemode (CPU governor tuning)")
        self.use_mangohud = QCheckBox("Show MangoHud overlay")
        self.use_gamescope = QCheckBox("Run inside Gamescope")
        for box in (self.use_gamemode, self.use_mangohud, self.use_gamescope):
            self.profile_form.addRow(box)

        # A picker rather than only a text box. The resolution is the thing
        # people actually want to set, and typing "-W 3440 -H 1440" is not
        # something anybody should have to know to do it.
        self.resolution = QComboBox()
        self._fill_resolutions()
        self.resolution.currentIndexChanged.connect(self._on_resolution_chosen)
        self.profile_form.addRow("Resolution", self.resolution)

        self.gamescope_args = QLineEdit()
        self.gamescope_args.setPlaceholderText("-f --hdr-enabled")
        self.profile_form.addRow("Gamescope options", self.gamescope_args)

        # Said plainly, because the alternative is somebody setting a
        # resolution here, launching a Steam game, and finding it ignored with
        # no explanation anywhere.
        wrappers = QLabel(
            "These apply to games GameLab starts itself — ROMs, and anything "
            "you added by hand. Steam, Heroic and Lutris start their own "
            "games, so set launch options there instead. For Steam that is "
            "Properties → Launch Options:\n"
            "    gamescope -W 3440 -H 1440 -f -- %command%"
        )
        wrappers.setWordWrap(True)
        wrappers.setObjectName("Subtle")
        self.profile_form.addRow("", wrappers)

        # Editable, so a build GameLab cannot see — one installed after this
        # window opened, or somewhere unusual — can still be typed in.
        self.proton_version = QComboBox()
        self.proton_version.setEditable(True)
        self._fill_proton_versions()
        self.profile_form.addRow("Proton version", self.proton_version)

        self.extra_args = QLineEdit()
        self.profile_form.addRow("Extra arguments", self.extra_args)

        self.env_vars = QLineEdit()
        self.env_vars.setPlaceholderText("DXVK_HUD=fps  RADV_PERFTEST=gpl")
        self.profile_form.addRow("Environment", self.env_vars)

        right.addLayout(self.profile_form)

        self.default_note = QLabel()
        self.default_note.setObjectName("Subtle")
        self.default_note.setWordWrap(True)
        right.addWidget(self.default_note)

        right.addStretch(1)

        buttons = QHBoxLayout()
        for label, slot in (
            ("New", self._new_profile),
            ("Make Default", self._make_default),
            ("Save", self._save_profile),
            ("Delete", self._delete_profile),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        right.addLayout(buttons)

        layout.addLayout(right, 1)

        self._reload_profiles()
        return page

    def _reload_profiles(self) -> None:
        self.profile_list.clear()
        for profile in self.profiles.list_profiles():
            label = f"{profile.name}  ★" if profile.is_default else profile.name
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, profile.id)
            self.profile_list.addItem(item)

        if self.profile_list.count():
            self.profile_list.setCurrentRow(0)

    def _current_profile_id(self) -> Optional[int]:
        item = self.profile_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_profile_selected(self) -> None:
        profile_id = self._current_profile_id()
        if profile_id is None:
            return

        profile = self.profiles.get(profile_id)
        if profile is None:
            return

        self.profile_name.setText(profile.name)
        # Filling the form fires the widgets' own change signals, and the
        # resolution picker saves when it changes — without this, opening a
        # profile would write it straight back before the user touched it.
        self._loading_profile = True
        try:
            self.use_gamemode.setChecked(profile.use_gamemode)
            self.use_mangohud.setChecked(profile.use_mangohud)
            self.use_gamescope.setChecked(profile.use_gamescope)
            self.gamescope_args.setText(
                with_resolution(profile.gamescope_args, None) or ""
            )
            self._select_resolution(parse_resolution(profile.gamescope_args))
            self._select_proton(profile.proton_version)
        finally:
            self._loading_profile = False
        self.extra_args.setText(profile.extra_args or "")
        self.env_vars.setText(
            " ".join(f"{k}={v}" for k, v in profile.env.items())
        )

        self.default_note.setText(
            "This is the default profile — it applies to every game without a "
            "profile of its own."
            if profile.is_default else ""
        )

        missing = profile.missing_tools()
        if missing:
            self.default_note.setText(
                self.default_note.text()
                + f"\n\nNot installed: {', '.join(missing)}. "
                "These options will be skipped until you install them."
            )

    def _parse_env(self) -> dict[str, str]:
        """Parse `KEY=value KEY2=value2` into a dict, ignoring malformed pairs."""
        env: dict[str, str] = {}
        for token in self.env_vars.text().split():
            if "=" in token:
                key, _, value = token.partition("=")
                if key:
                    env[key] = value
        return env

    def _save_profile(self) -> None:
        profile_id = self._current_profile_id()
        if profile_id is None:
            return

        self.profiles.update(
            profile_id,
            name=self.profile_name.text().strip() or "Unnamed",
            use_gamemode=self.use_gamemode.isChecked(),
            use_mangohud=self.use_mangohud.isChecked(),
            use_gamescope=self.use_gamescope.isChecked(),
            gamescope_args=with_resolution(
                self.gamescope_args.text().strip() or None,
                self.resolution.currentData(),
            ),
            proton_version=self._chosen_proton(),
            extra_args=self.extra_args.text().strip() or None,
            env=self._parse_env(),
        )
        self._reload_profiles()

    def _new_profile(self) -> None:
        self.profiles.create(LaunchProfile(name="New profile"))
        self._reload_profiles()
        self.profile_list.setCurrentRow(self.profile_list.count() - 1)

    def _make_default(self) -> None:
        profile_id = self._current_profile_id()
        if profile_id is not None:
            self.profiles.set_default(profile_id)
            self._reload_profiles()

    def _delete_profile(self) -> None:
        profile_id = self._current_profile_id()
        if profile_id is None:
            return

        if len(self.profiles.list_profiles()) <= 1:
            QMessageBox.information(
                self, "Cannot delete",
                "There must always be at least one launch profile.",
            )
            return

        self.profiles.delete(profile_id)
        self._reload_profiles()

    # ── Sources ───────────────────────────────────────────────────

    def _sources_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(SPACING)

        self.source_list = QListWidget()
        layout.addWidget(self.source_list, 1)

        self._reload_sources()

        note = QLabel(
            "Removing a source asks what to do with its games. Nothing on "
            "disk is ever deleted — only what GameLab keeps."
        )
        note.setObjectName("Subtle")
        note.setWordWrap(True)
        layout.addWidget(note)

        remove = QPushButton("Remove Source")
        remove.clicked.connect(self._remove_source)
        layout.addWidget(remove)

        return page

    def _reload_sources(self) -> None:
        self.source_list.clear()
        for row in self.library.list_sources():
            item = QListWidgetItem(
                f"{row['name']}  ·  {row['type']}  ·  {row['game_count']} games"
            )
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.source_list.addItem(item)

    def _remove_source(self) -> None:
        item = self.source_list.currentItem()
        if item is None:
            return

        source_id = item.data(Qt.ItemDataRole.UserRole)
        count = self.library.count_games_for_source(source_id)

        if count:
            # Removing the source but silently keeping its games is how a
            # library fills up with entries that no sidebar row matches. The
            # choice is the user's, so it is asked rather than assumed.
            box = QMessageBox(self)
            box.setWindowTitle("Remove source")
            box.setText(f"Remove this source and its {count} game"
                        f"{'s' if count != 1 else ''}?")
            box.setInformativeText(
                "The games disappear from your library along with their "
                "playtime and artwork.\n\n"
                "Nothing is deleted from your disk — rescanning the folder "
                "brings them back."
            )

            remove_all = box.addButton(
                f"Remove source and {count} game{'s' if count != 1 else ''}",
                QMessageBox.ButtonRole.DestructiveRole,
            )
            keep = box.addButton("Keep the games", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(remove_all)
            box.exec()

            clicked = box.clickedButton()
            if clicked not in (remove_all, keep):
                return
            remove_games = clicked is remove_all
        else:
            remove_games = False

        removed = self.library.remove_source(source_id, remove_games=remove_games)

        self._reload_sources()
        # The grid behind this dialog is now showing games that are gone.
        self.sources_changed.emit()

        if not remove_games and removed == 0 and count:
            QMessageBox.information(
                self, "Games kept",
                f"{count} game{'s' if count != 1 else ''} stayed in your "
                "library. Find them under “No source” in the sidebar.",
            )

    # ── Removing games in bulk ────────────────────────────────────

    def _remove_games_tab(self) -> QWidget:
        """Clear out a whole console's worth of entries at once.

        Removing games one at a time is fine for a mistake; it is useless when
        a bad scan imported hundreds. Picking a console — or the games left
        behind by a removed source — and clearing them is the way out.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(SPACING)

        heading = QLabel("Remove games from your library")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        note = QLabel(
            "Choose what to clear out. Your files are never deleted — only "
            "GameLab's entries, with their playtime and artwork. Rescanning "
            "the folder brings them back."
        )
        note.setObjectName("Subtle")
        note.setWordWrap(True)
        layout.addWidget(note)

        row = QHBoxLayout()
        row.addWidget(QLabel("Remove"))

        self.removal_picker = QComboBox()
        self.removal_picker.currentIndexChanged.connect(self._removal_chosen)
        row.addWidget(self.removal_picker, 1)
        layout.addLayout(row)

        self.removal_summary = QLabel()
        self.removal_summary.setWordWrap(True)
        self.removal_summary.setStyleSheet(
            f"color: {self.theme.text}; background-color: {self.theme.panel};"
            f" border-radius: {RADIUS}px; padding: 12px 14px; font-size: 13px;"
        )
        layout.addWidget(self.removal_summary)

        self.removal_button = QPushButton("Remove these games")
        self.removal_button.clicked.connect(self._remove_chosen_games)
        layout.addWidget(self.removal_button)

        layout.addStretch(1)

        self._reload_removal_options()
        return page

    def _reload_removal_options(self) -> None:
        """Fill the picker with what is actually in the library right now."""
        from rose_gamelab.core.emulator import get_system
        from rose_gamelab.core.library import NO_SOURCE

        self.removal_picker.blockSignals(True)
        self.removal_picker.clear()

        self.removal_picker.addItem("Choose a console or source…", None)

        for system_id, count in self.library.systems_in_library():
            system = get_system(system_id)
            name = system.name if system else system_id
            self.removal_picker.addItem(
                f"{name} — {count} game{'s' if count != 1 else ''}",
                ("system", system_id, count),
            )

        # Source names are folder basenames, so a PS2 and a PS3 collection are
        # both called "Roms". The path is what tells them apart.
        sources = [row for row in self.library.list_sources() if row["game_count"]]
        names = [row["name"] for row in sources]

        for row in sources:
            label = row["name"]
            if names.count(label) > 1 and row["path"]:
                label = f"{label} ({row['path']})"
            self.removal_picker.addItem(
                f"Everything from {label} — {row['game_count']} games",
                ("source", row["id"], row["game_count"]),
            )

        orphaned = self.library.count_orphaned_games()
        if orphaned:
            self.removal_picker.addItem(
                f"Games with no source — {orphaned}",
                ("source", NO_SOURCE, orphaned),
            )

        self.removal_picker.blockSignals(False)
        self._removal_chosen()

    def _removal_chosen(self) -> None:
        choice = self.removal_picker.currentData()

        if choice is None:
            self.removal_summary.setText(
                "Nothing selected. Pick a console to see how many entries it has."
            )
            self.removal_button.setEnabled(False)
            self.removal_button.setText("Remove these games")
            return

        _kind, _key, count = choice
        self.removal_summary.setText(
            f"This removes {count} entr{'ies' if count != 1 else 'y'} from your "
            f"library.\n\nThe files stay exactly where they are on disk."
        )
        self.removal_button.setEnabled(True)
        self.removal_button.setText(
            f"Remove {count} game{'s' if count != 1 else ''}"
        )

    def _remove_chosen_games(self) -> None:
        choice = self.removal_picker.currentData()
        if choice is None:
            return

        kind, key, count = choice
        label = self.removal_picker.currentText()

        confirmed = QMessageBox.question(
            self, "Remove games",
            f"Remove {count} game{'s' if count != 1 else ''}?\n\n{label}\n\n"
            "Nothing is deleted from your disk.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        if kind == "system":
            removed = self.library.remove_games_where(system=key)
        else:
            removed = self.library.remove_games_where(source_id=key)

        self._reload_removal_options()
        self._reload_sources()
        self.sources_changed.emit()

        QMessageBox.information(
            self, "Removed",
            f"{removed} game{'s' if removed != 1 else ''} removed from your "
            "library.",
        )

    # ── About ─────────────────────────────────────────────────────

    def _about_tab(self) -> QWidget:
        from rose_gamelab.ui.branding import rose_widget

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(rose_widget())

        text = QLabel(
            "<h2>Rose GameLab</h2>"
            "<p>Every game you own, from every source, in one library.</p>"
            "<p>Part of <b>Rose Open Source Endeavours</b>.</p>"
            "<p><b>No telemetry. No accounts. Works offline.</b></p>"
            "<p style='color:#888'>Licensed GPL-3.0-or-later.<br>"
            "Rose ASCII art by Joan G. Stark.</p>"
        )
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setWordWrap(True)
        layout.addWidget(text)

        return page
