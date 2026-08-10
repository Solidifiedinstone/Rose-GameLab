"""Launch profiles: reusable sets of runtime options.

A profile bundles the wrappers and environment a game runs under — Proton
version, Gamescope, MangoHud, gamemode, environment variables, extra arguments.
One profile can be marked default and applies to everything that has no
specific profile of its own, so the user configures MangoHud once rather than
per game.

Building the command line is a pure function of (option, profile), separated
from process spawning so it can be tested without launching anything.

Nothing here touches the network.
"""

from __future__ import annotations

import json
import shutil
import sqlite3

from dataclasses import dataclass, field
from typing import Any, Optional

from rose_gamelab.db.database import Database

# Wrapper order matters and is not arbitrary.
#
#   gamemoderun gamescope [args] -- mangohud <command>
#
# gamemode is outermost because it adjusts the CPU governor for everything it
# contains. gamescope creates the nested display that the game and its overlay
# must both live inside. mangohud sits closest to the game so it hooks the
# game's own Vulkan/GL calls rather than gamescope's compositing.
WRAPPER_ORDER = ("gamemode", "gamescope", "mangohud")


@dataclass
class LaunchProfile:
    """A reusable set of runtime options."""

    id: Optional[int] = None
    name: str = "Default"
    is_default: bool = False
    proton_version: Optional[str] = None
    use_gamemode: bool = False
    use_mangohud: bool = False
    use_gamescope: bool = False
    gamescope_args: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    extra_args: Optional[str] = None
    pre_launch: Optional[str] = None
    post_exit: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "LaunchProfile":
        try:
            env = json.loads(row["env"] or "{}")
        except (json.JSONDecodeError, TypeError):
            env = {}

        return cls(
            id=row["id"],
            name=row["name"],
            is_default=bool(row["is_default"]),
            proton_version=row["proton_version"],
            use_gamemode=bool(row["use_gamemode"]),
            use_mangohud=bool(row["use_mangohud"]),
            use_gamescope=bool(row["use_gamescope"]),
            gamescope_args=row["gamescope_args"],
            env=env if isinstance(env, dict) else {},
            extra_args=row["extra_args"],
            pre_launch=row["pre_launch"],
            post_exit=row["post_exit"],
        )

    # ── Command construction ──────────────────────────────────────

    def missing_tools(self) -> list[str]:
        """Wrappers this profile wants that are not installed.

        Checked before launching so the user gets told what to install,
        rather than the game silently failing to start.
        """
        wanted = {
            "gamemode": (self.use_gamemode, "gamemoderun"),
            "gamescope": (self.use_gamescope, "gamescope"),
            "mangohud": (self.use_mangohud, "mangohud"),
        }
        return [
            binary for _, (enabled, binary) in wanted.items()
            if enabled and shutil.which(binary) is None
        ]

    def wrap(self, command: list[str], *, skip_missing: bool = True) -> list[str]:
        """Wrap a command with this profile's enabled wrappers.

        Wrappers that are not installed are skipped rather than causing the
        launch to fail outright — a missing MangoHud should cost you an
        overlay, not your game.
        """
        result = list(command)

        if self.use_mangohud and (not skip_missing or shutil.which("mangohud")):
            result = ["mangohud", *result]

        if self.use_gamescope and (not skip_missing or shutil.which("gamescope")):
            args = self.gamescope_args.split() if self.gamescope_args else []
            result = ["gamescope", *args, "--", *result]

        if self.use_gamemode and (not skip_missing or shutil.which("gamemoderun")):
            result = ["gamemoderun", *result]

        return result

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        """Apply this profile's environment on top of the inherited one.

        The inherited environment is used as-is apart from these additions.
        Notably we do NOT force SDL_VIDEODRIVER: the previous implementation
        pinned it to x11, which breaks native Wayland sessions.
        """
        env = dict(base)
        env.update({str(k): str(v) for k, v in self.env.items()})

        if self.proton_version:
            env.setdefault("PROTON_VERSION", self.proton_version)

        return env


class ProfileStore:
    """Storage and retrieval of launch profiles."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, profile: LaunchProfile) -> int:
        cursor = self.db.execute(
            "INSERT INTO launch_profiles"
            " (name, is_default, proton_version, use_gamemode, use_mangohud,"
            "  use_gamescope, gamescope_args, env, extra_args, pre_launch, post_exit)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                profile.name, int(profile.is_default), profile.proton_version,
                int(profile.use_gamemode), int(profile.use_mangohud),
                int(profile.use_gamescope), profile.gamescope_args,
                json.dumps(profile.env), profile.extra_args,
                profile.pre_launch, profile.post_exit,
            ),
        )
        profile_id = int(cursor.lastrowid)

        if profile.is_default:
            self.set_default(profile_id)

        return profile_id

    def get(self, profile_id: int) -> Optional[LaunchProfile]:
        row = self.db.query_one(
            "SELECT * FROM launch_profiles WHERE id = ?", (profile_id,)
        )
        return LaunchProfile.from_row(row) if row else None

    def get_default(self) -> Optional[LaunchProfile]:
        row = self.db.query_one("SELECT * FROM launch_profiles WHERE is_default = 1")
        return LaunchProfile.from_row(row) if row else None

    def list_profiles(self) -> list[LaunchProfile]:
        return [
            LaunchProfile.from_row(row)
            for row in self.db.query(
                "SELECT * FROM launch_profiles ORDER BY is_default DESC, name"
            )
        ]

    def set_default(self, profile_id: int) -> None:
        """Make one profile the default, clearing any previous one."""
        with self.db.transaction() as cur:
            cur.execute("UPDATE launch_profiles SET is_default = 0")
            cur.execute(
                "UPDATE launch_profiles SET is_default = 1 WHERE id = ?", (profile_id,)
            )

    def update(self, profile_id: int, **fields: Any) -> None:
        allowed = {
            "name", "proton_version", "use_gamemode", "use_mangohud",
            "use_gamescope", "gamescope_args", "extra_args", "pre_launch",
            "post_exit",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}

        if "env" in fields:
            updates["env"] = json.dumps(fields["env"] or {})

        for key in ("use_gamemode", "use_mangohud", "use_gamescope"):
            if key in updates:
                updates[key] = int(bool(updates[key]))

        if not updates:
            return

        assignments = ", ".join(f"{k} = ?" for k in updates)
        self.db.execute(
            f"UPDATE launch_profiles SET {assignments} WHERE id = ?",
            (*updates.values(), profile_id),
        )

    def delete(self, profile_id: int) -> None:
        """Delete a profile. Games using it fall back to the default."""
        self.db.execute("DELETE FROM launch_profiles WHERE id = ?", (profile_id,))

    def for_game(self, launch_option: sqlite3.Row) -> LaunchProfile:
        """The profile a launch option should run under.

        Resolution order: the option's own profile, then the configured
        default, then a plain profile with nothing enabled. There is always a
        profile, so callers never have to handle None.
        """
        keys = set(launch_option.keys())
        if "profile_id" in keys and launch_option["profile_id"] is not None:
            profile = self.get(launch_option["profile_id"])
            if profile:
                return profile

        return self.get_default() or LaunchProfile(name="None")

    def ensure_default_exists(self) -> LaunchProfile:
        """Create a starter default profile on first run if there is none."""
        existing = self.get_default()
        if existing:
            return existing

        profile = LaunchProfile(
            name="Default",
            is_default=True,
            # gamemode is the one wrapper that helps essentially every game and
            # costs nothing when absent, so it is the only opt-out default.
            use_gamemode=True,
        )
        profile.id = self.create(profile)
        return profile
