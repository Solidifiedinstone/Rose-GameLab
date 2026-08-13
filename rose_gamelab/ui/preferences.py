"""Interface preferences that survive a restart.

GameLab had nowhere to put these. The theme was an argument to the main window,
defaulted in code, and forgotten the moment the process ended — so choosing one
in Settings changed the running window and nothing else. Every appearance
setting needs the same treatment, so they all live here.

Stored as JSON under the user's config directory, next to everything else they
own. A file that cannot be read is not an error: the defaults are perfectly
usable, and refusing to start because a preferences file got truncated would be
absurd.

Only the NAMES of a theme and style are stored, plus whatever individual axes
the user overrode. That way a built-in theme that gets improved in a later
release improves for existing users too, while their own adjustments survive.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from rose_gamelab.ui.theme import (
    COVER_WIDTHS,
    DEFAULT_STYLE,
    DEFAULT_THEME,
    STYLES,
    THEMES,
    Appearance,
    get_style,
    get_theme,
)

logger = logging.getLogger(__name__)


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "rose-gamelab"


DEFAULT_PATH = config_dir() / "preferences.json"

#: Credentials live apart from preferences, in their own file with tight
#: permissions. Sharing a preferences file — to copy a theme to another machine,
#: or to paste into a bug report — should never hand over an API key with it.
CREDENTIALS_PATH = config_dir() / "credentials.json"


def artwork_key(path: Optional[Path] = None) -> Optional[str]:
    """The SteamGridDB key, from the environment or the credentials file.

    The environment wins, so anyone who would rather not write a credential to
    disk at all can set `STEAMGRIDDB_API_KEY` and never touch the settings field.
    """
    from_env = (os.environ.get("STEAMGRIDDB_API_KEY") or "").strip()
    if from_env:
        return from_env

    try:
        data = json.loads(Path(path or CREDENTIALS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    key = data.get("steamgriddb_api_key") if isinstance(data, dict) else None
    return key.strip() or None if isinstance(key, str) else None


def retroachievements_credentials(
    path: Optional[Path] = None,
) -> tuple[Optional[str], Optional[str]]:
    """(username, api key) for RetroAchievements, environment first."""
    user = (os.environ.get("RA_USERNAME") or "").strip()
    key = (os.environ.get("RA_API_KEY") or "").strip()
    if user and key:
        return user, key

    try:
        data = json.loads(Path(path or CREDENTIALS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (user or None, key or None)

    if not isinstance(data, dict):
        return (user or None, key or None)

    stored_user = data.get("retroachievements_username")
    stored_key = data.get("retroachievements_api_key")
    return (
        user or (stored_user.strip() or None if isinstance(stored_user, str) else None),
        key or (stored_key.strip() or None if isinstance(stored_key, str) else None),
    )


def set_retroachievements_credentials(
    username: Optional[str], key: Optional[str], path: Optional[Path] = None
) -> None:
    """Store (or clear) the RetroAchievements username and key."""
    _write_credentials({
        "retroachievements_username": (username or "").strip(),
        "retroachievements_api_key": (key or "").strip(),
    }, path)


def _write_credentials(values: dict, path: Optional[Path] = None) -> None:
    """Merge values into the credentials file, dropping empty ones."""
    path = Path(path or CREDENTIALS_PATH)

    data: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass

    for name, value in values.items():
        if value:
            data[name] = value
        else:
            data.pop(name, None)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".part")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Readable only by its owner: these are credentials.
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except OSError as exc:
        logger.warning("could not save credentials to %s: %s", path, exc)


def set_artwork_key(key: Optional[str], path: Optional[Path] = None) -> None:
    """Store (or clear, with an empty value) the SteamGridDB key."""
    _write_credentials({"steamgriddb_api_key": key}, path)

#: Style axes a user can override individually, and the values each accepts.
#: The settings screen is generated from this, so adding an axis to `Style` and
#: listing it here is all it takes to make it adjustable.
STYLE_AXES: dict[str, tuple[str, str]] = {
    "radius": ("Corners", "int"),
    "spacing": ("Spacing", "int"),
    "padding": ("Padding", "int"),
    "font_size": ("Text size", "int"),
    "heading_size": ("Heading size", "int"),
    "border_width": ("Border weight", "int"),
    "cover_size": ("Cover size", "choice"),
    "elevated_panels": ("Raised panels", "bool"),
}

#: Sensible bounds for each numeric axis, so a slider cannot produce an
#: interface that is unusable and unfixable-from-within.
STYLE_RANGES: dict[str, tuple[int, int]] = {
    "radius": (0, 40),
    "spacing": (4, 40),
    "padding": (2, 24),
    "font_size": (9, 24),
    "heading_size": (14, 44),
    "border_width": (0, 4),
}


@dataclass
class Preferences:
    """Everything the user chose about how GameLab looks."""

    theme: str = DEFAULT_THEME
    style: str = DEFAULT_STYLE
    #: Per-axis style overrides — the 'mix and match' half. Sparse: only what
    #: the user actually changed is kept, so the rest tracks the chosen style.
    style_overrides: dict[str, Any] = field(default_factory=dict)

    # ── Startup ───────────────────────────────────────────────────

    #: Check sources for new games when the app opens. On by default, because a
    #: launcher that misses games you installed yesterday is doing its job
    #: badly — but a settled library does not need checking every single time,
    #: and the scan is noticeable, so it can be turned off.
    scan_on_start: bool = True
    #: Fetch art for anything missing it, unprompted. Kept separate: someone can
    #: reasonably want new games found but not want the network touched.
    art_on_start: bool = True
    #: Refresh RetroAchievements progress when the app opens, so the numbers on
    #: a game page are what the account says rather than whatever was true the
    #: last time somebody opened that one game by hand. Does nothing at all
    #: without credentials, so it is on by default and simply stays quiet for
    #: everyone who has not set them.
    achievements_on_start: bool = True
    #: Look up games never checked before, so achievements appear for games
    #: added since without anybody opening them. Separate from the refresh
    #: above because they cost different things: refreshing progress is one
    #: cheap call per matched game, while matching is a hash or a rate-limited
    #: search per game that has never been looked at.
    achievements_match_on_start: bool = True

    # ── Appearance ────────────────────────────────────────────────

    def appearance(self) -> Appearance:
        """The theme and style this adds up to."""
        return Appearance(
            theme=get_theme(self.theme),
            style=get_style(self.style).with_overrides(**self.style_overrides),
        )

    def override(self, axis: str, value: Any) -> None:
        """Set one style axis. Passing None clears it back to the style's own."""
        if axis not in STYLE_AXES:
            raise KeyError(f"unknown style axis: {axis}")

        if value is None:
            self.style_overrides.pop(axis, None)
            return

        low_high = STYLE_RANGES.get(axis)
        if low_high is not None:
            low, high = low_high
            value = max(low, min(high, int(value)))
        elif axis == "cover_size" and value not in COVER_WIDTHS:
            raise ValueError(f"unknown cover size: {value}")

        self.style_overrides[axis] = value

    def clear_overrides(self) -> None:
        """Back to the chosen style exactly as it ships."""
        self.style_overrides.clear()

    def value_for(self, axis: str) -> Any:
        """The effective value of one axis — the override, or the style's own."""
        return getattr(self.appearance().style, axis)

    # ── Persistence ───────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "theme": self.theme,
            "style": self.style,
            "style_overrides": dict(self.style_overrides),
            "scan_on_start": self.scan_on_start,
            "art_on_start": self.art_on_start,
            "achievements_on_start": self.achievements_on_start,
            "achievements_match_on_start": self.achievements_match_on_start,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Preferences":
        """Build from stored data, ignoring anything unrecognised.

        A name that no longer exists falls back to the default rather than
        failing — themes get renamed, and a stale one should cost the user their
        colour scheme, not their launcher.
        """
        theme = data.get("theme")
        style = data.get("style")
        overrides = data.get("style_overrides")

        prefs = cls(
            theme=theme if theme in THEMES else DEFAULT_THEME,
            style=style if style in STYLES else DEFAULT_STYLE,
        )

        for name in (
            "scan_on_start", "art_on_start",
            "achievements_on_start", "achievements_match_on_start",
        ):
            if name in data:
                setattr(prefs, name, bool(data[name]))

        if isinstance(overrides, dict):
            for axis, value in overrides.items():
                try:
                    prefs.override(axis, value)
                except (KeyError, ValueError, TypeError):
                    logger.debug("ignoring unusable style override %r=%r", axis, value)

        return prefs

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Preferences":
        """Read saved preferences. Defaults when there are none or they are broken."""
        path = Path(path) if path else DEFAULT_PATH

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            logger.warning("could not read %s, using defaults: %s", path, exc)
            return cls()

        if not isinstance(data, dict):
            return cls()
        return cls.from_dict(data)

    def save(self, path: Optional[Path] = None) -> None:
        """Write preferences out.

        Written to a temporary file and moved into place, so an interrupted
        write never leaves a half-written file that reads as corrupt next start.
        """
        path = Path(path) if path else DEFAULT_PATH

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".part")
            temporary.write_text(
                json.dumps(self.to_dict(), indent=2), encoding="utf-8"
            )
            temporary.replace(path)
        except OSError as exc:
            # Losing a preference is a nuisance; crashing over it is worse.
            logger.warning("could not save preferences to %s: %s", path, exc)
