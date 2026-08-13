"""Per-system overrides: which emulator, and what to pass it.

Detection picks an emulator per system and is right nearly always, but "nearly"
is the problem. Someone with both Xenia Edge and Xenia Canary installed has a
reason for wanting a particular one on a particular game's system. Someone who
wants PCSX2 to start fullscreen every time should not have to open PCSX2 to say
so, and certainly should not have to say it again per game.

Both were half-built already: `Launcher` took an `emulator_paths` mapping that
nothing ever filled in, so detection's choice was final in practice. This is the
storage that makes that parameter mean something.

Arguments are stored as a string and split with `shlex`, the same as launch
profiles, so quoting behaves the way it does in a shell rather than in some
invented dialect.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rose_gamelab.db.database import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SystemSetting:
    """What the user has chosen for one system."""

    system: str
    emulator_path: Optional[str] = None
    extra_args: Optional[str] = None

    @property
    def has_override(self) -> bool:
        return bool(self.emulator_path or self.extra_args)

    @property
    def argument_list(self) -> list[str]:
        """`extra_args` split the way a shell would split it.

        Invalid quoting yields nothing rather than raising: a stray quote in a
        settings box should cost the arguments, not the launch.
        """
        if not self.extra_args:
            return []
        try:
            return shlex.split(self.extra_args)
        except ValueError as exc:
            logger.warning("ignoring unparseable arguments for %s: %s", self.system, exc)
            return []


class SystemSettingsStore:
    """Storage for per-system emulator choices and arguments."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, system: str) -> SystemSetting:
        """Settings for a system. Always returns one, empty when unset."""
        row = self.db.query_one(
            "SELECT * FROM system_settings WHERE system = ?", (system,)
        )
        if row is None:
            return SystemSetting(system=system)

        return SystemSetting(
            system=row["system"],
            emulator_path=row["emulator_path"],
            extra_args=row["extra_args"],
        )

    def all(self) -> dict[str, SystemSetting]:
        return {
            row["system"]: SystemSetting(
                system=row["system"],
                emulator_path=row["emulator_path"],
                extra_args=row["extra_args"],
            )
            for row in self.db.query("SELECT * FROM system_settings")
        }

    def set(
        self,
        system: str,
        *,
        emulator_path: Optional[str] = None,
        extra_args: Optional[str] = None,
    ) -> None:
        """Record a choice. Empty strings clear rather than store nothing."""
        self.db.execute(
            "INSERT INTO system_settings (system, emulator_path, extra_args, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(system) DO UPDATE SET"
            "   emulator_path = excluded.emulator_path,"
            "   extra_args    = excluded.extra_args,"
            "   updated_at    = excluded.updated_at",
            (
                system,
                (emulator_path or "").strip() or None,
                (extra_args or "").strip() or None,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )

    def clear(self, system: str) -> None:
        self.db.execute("DELETE FROM system_settings WHERE system = ?", (system,))

    # ── What a launch needs ───────────────────────────────────────

    def emulator_paths(self) -> dict[str, str]:
        """System id -> configured emulator, for the ones that exist.

        A path that no longer exists is dropped rather than returned: an
        emulator uninstalled since it was chosen should fall back to detection,
        not fail the launch with "no such file".
        """
        paths = {}
        for system, setting in self.all().items():
            if setting.emulator_path and Path(setting.emulator_path).exists():
                paths[system] = setting.emulator_path
            elif setting.emulator_path:
                logger.info(
                    "configured emulator for %s no longer exists: %s",
                    system, setting.emulator_path,
                )
        return paths

    def arguments_for(self, system: str) -> list[str]:
        return self.get(system).argument_list
