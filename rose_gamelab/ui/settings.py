"""Settings: themes, launch profiles, sources and maintenance.

Everything here writes through to the database or the config file immediately;
there is no separate "apply" step that can silently fail to happen.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rose_gamelab.core.profiles import LaunchProfile, ProfileStore
from rose_gamelab.ui.theme import SPACING, Theme, get_theme, list_theme_names, stylesheet


class SettingsDialog(QDialog):
    """The settings window."""

    theme_changed = Signal(object)

    def __init__(
        self,
        library,
        profiles: ProfileStore,
        theme: Theme,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self.library = library
        self.profiles = profiles
        self.theme = theme

        self.setWindowTitle("Settings")
        self.resize(680, 560)
        self.setStyleSheet(stylesheet(theme))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING, SPACING, SPACING, SPACING)

        from rose_gamelab.ui.widgets.tools_tabs import (
            ControllersTab,
            SavesTab,
            SteamExportTab,
        )

        tabs = QTabWidget()
        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._profiles_tab(), "Launch Profiles")
        tabs.addTab(ControllersTab(theme), "Controllers")
        tabs.addTab(SavesTab(library, theme), "Saves")
        tabs.addTab(self._sources_tab(), "Sources")
        tabs.addTab(SteamExportTab(library, theme), "Steam Export")
        tabs.addTab(self._about_tab(), "About")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    # ── Appearance ────────────────────────────────────────────────

    def _appearance_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(SPACING)

        self.theme_picker = QComboBox()
        for key, name in list_theme_names():
            self.theme_picker.addItem(name, key)

        current = next(
            (i for i in range(self.theme_picker.count())
             if get_theme(self.theme_picker.itemData(i)).name == self.theme.name),
            0,
        )
        self.theme_picker.setCurrentIndex(current)
        # Applied live rather than on close, so the user can see each theme.
        self.theme_picker.currentIndexChanged.connect(self._on_theme_changed)

        form.addRow("Theme", self.theme_picker)

        note = QLabel(
            "Themes are plain JSON files. Drop your own into the themes folder "
            "to use colours from Matugen or any other palette tool."
        )
        note.setObjectName("Subtle")
        note.setWordWrap(True)
        form.addRow(note)

        return page

    def _on_theme_changed(self) -> None:
        theme = get_theme(self.theme_picker.currentData())
        self.theme = theme
        self.setStyleSheet(stylesheet(theme))
        self.theme_changed.emit(theme)

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

        self.gamescope_args = QLineEdit()
        self.gamescope_args.setPlaceholderText("-W 1920 -H 1080 -f")
        self.profile_form.addRow("Gamescope options", self.gamescope_args)

        self.proton_version = QLineEdit()
        self.proton_version.setPlaceholderText("GE-Proton9-20")
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
        self.use_gamemode.setChecked(profile.use_gamemode)
        self.use_mangohud.setChecked(profile.use_mangohud)
        self.use_gamescope.setChecked(profile.use_gamescope)
        self.gamescope_args.setText(profile.gamescope_args or "")
        self.proton_version.setText(profile.proton_version or "")
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
            gamescope_args=self.gamescope_args.text().strip() or None,
            proton_version=self.proton_version.text().strip() or None,
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
            "Removing a source keeps its games in your library, along with "
            "their playtime and artwork."
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

        self.library.remove_source(item.data(Qt.ItemDataRole.UserRole))
        self._reload_sources()

    # ── About ─────────────────────────────────────────────────────

    def _about_tab(self) -> QWidget:
        from rose_gamelab.ui.branding import rose_html

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        rose = QLabel()
        rose.setTextFormat(Qt.TextFormat.RichText)
        rose.setText(rose_html())
        rose.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(rose)

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
