"""Saved pad layouts, who is player one, and per-game overrides.

Three things that all need the same storage. A *profile* is a pad's layout,
keyed by its SDL GUID so it follows the hardware rather than the USB port.
*Player order* records which pad is player one, which matters the moment two
people sit down. A *per-game override* pins a particular pad to a game — an
arcade stick for arcade, a Pro Controller for Switch — regardless of what else
is connected.

Profiles are keyed by GUID and stored as the rendered SDL mapping line, which
is deliberate on both counts: the GUID is what SDL and the community database
both match on, and the rendered line keeps working unchanged if the canonical
button model ever grows.

An honest note on player order, because it is easy to promise more than Linux
delivers. RetroArch takes an explicit joypad index per player, so ordering is
applied exactly there. SDL-based emulators enumerate pads in kernel device
order and offer no environment variable to reorder them — so for those, this
records the user's intent, drives GameLab's own interface, and is written into
the config of any emulator whose format can express it. It does not silently
claim to have reordered something it cannot.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from rose_gamelab.core import controller_db
from rose_gamelab.core.controller import InputDevice, sdl_guid
from rose_gamelab.db.database import Database

logger = logging.getLogger(__name__)

#: Player slots offered. Four is what the consoles being emulated support, and
#: what every emulator here exposes.
MAX_PLAYERS = 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ControllerProfile:
    """A saved pad layout."""

    id: Optional[int] = None
    name: str = ""
    guid: str = ""
    device_name: str = ""
    mapping: str = ""
    source: str = "user"
    player: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ControllerProfile":
        return cls(
            id=row["id"], name=row["name"], guid=row["guid"],
            device_name=row["device_name"], mapping=row["mapping"],
            source=row["source"], player=row["player"],
        )

    @property
    def recognised(self) -> bool:
        return self.source == "database"


def guid_for(device: InputDevice) -> str:
    return sdl_guid(device.bustype, device.vendor_id, device.product_id, device.version)


class ControllerProfileStore:
    """Storage for pad layouts, player order and per-game overrides."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Profiles ──────────────────────────────────────────────────

    def save(self, profile: ControllerProfile) -> int:
        """Create or update the profile for a pad.

        Keyed on GUID rather than id, so re-binding a pad the user has already
        bound replaces its layout instead of leaving two profiles for the same
        hardware that disagree about the buttons.
        """
        existing = self.for_guid(profile.guid)
        now = _now()

        if existing is not None:
            self.db.execute(
                "UPDATE controller_profiles"
                "   SET name = ?, device_name = ?, mapping = ?, source = ?,"
                "       updated_at = ?"
                " WHERE id = ?",
                (profile.name, profile.device_name, profile.mapping,
                 profile.source, now, existing.id),
            )
            return existing.id

        cursor = self.db.execute(
            "INSERT INTO controller_profiles"
            " (name, guid, device_name, mapping, source, player, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (profile.name, profile.guid, profile.device_name, profile.mapping,
             profile.source, profile.player, now, now),
        )
        return int(cursor.lastrowid)

    def get(self, profile_id: int) -> Optional[ControllerProfile]:
        row = self.db.query_one(
            "SELECT * FROM controller_profiles WHERE id = ?", (profile_id,)
        )
        return ControllerProfile.from_row(row) if row else None

    def for_guid(self, guid: str) -> Optional[ControllerProfile]:
        row = self.db.query_one(
            "SELECT * FROM controller_profiles WHERE guid = ?", (guid,)
        )
        return ControllerProfile.from_row(row) if row else None

    def for_device(self, device: InputDevice) -> Optional[ControllerProfile]:
        return self.for_guid(guid_for(device))

    def list_profiles(self) -> list[ControllerProfile]:
        """Every saved profile, players first and in player order."""
        rows = self.db.query(
            "SELECT * FROM controller_profiles"
            " ORDER BY player IS NULL, player, name"
        )
        return [ControllerProfile.from_row(row) for row in rows]

    def delete(self, profile_id: int) -> None:
        self.db.execute("DELETE FROM controller_profiles WHERE id = ?", (profile_id,))

    # ── Automatic binding ─────────────────────────────────────────

    def bind(self, device: InputDevice) -> ControllerProfile:
        """Return the profile for a pad, creating one if it has none.

        This is what makes plugging a pad in Just Work: an unknown pad is
        identified against the community database, given a profile, and from
        then on the user's own edits to that profile are what is used.

        A profile the user has edited is never overwritten here — that is the
        whole point of having saved it.
        """
        existing = self.for_device(device)
        if existing is not None:
            return existing

        resolution = controller_db.resolve(device)
        profile = ControllerProfile(
            name=resolution.name,
            guid=guid_for(device),
            device_name=device.name,
            mapping=resolution.sdl_mapping,
            source=resolution.source,
            player=self.next_free_player(),
        )
        profile.id = self.save(profile)
        logger.info(
            "bound %s as player %s (%s)", profile.name, profile.player, profile.source
        )
        return profile

    def bind_all(self, devices) -> list[ControllerProfile]:
        return [self.bind(device) for device in devices]

    # ── Player order ──────────────────────────────────────────────

    def next_free_player(self) -> Optional[int]:
        """The lowest unused player slot, or None when all are taken."""
        taken = {
            row["player"] for row in
            self.db.query("SELECT player FROM controller_profiles WHERE player IS NOT NULL")
        }
        for slot in range(1, MAX_PLAYERS + 1):
            if slot not in taken:
                return slot
        return None

    def assign_player(self, profile_id: int, player: Optional[int]) -> None:
        """Put a pad in a player slot, displacing whoever held it.

        Swapping rather than failing: someone dragging player 2 onto player 1
        means "these two swap", and refusing the change because the slot is
        occupied would make reordering impossible without clearing first.
        """
        if player is not None and not (1 <= player <= MAX_PLAYERS):
            raise ValueError(f"player must be between 1 and {MAX_PLAYERS}")

        target = self.get(profile_id)
        if target is None:
            return

        occupant = None
        if player is not None:
            row = self.db.query_one(
                "SELECT id FROM controller_profiles WHERE player = ?", (player,)
            )
            if row is not None and row["id"] != profile_id:
                occupant = row["id"]

        # Both slots are emptied before either is filled. The unique index
        # rejects two pads holding one slot even for the instant a swap is
        # half-done, so "free the occupant, then move it" is not enough — the
        # pad being moved still holds the slot the occupant is moving into.
        self.db.execute(
            "UPDATE controller_profiles SET player = NULL WHERE id = ?", (profile_id,)
        )
        if occupant is not None:
            self.db.execute(
                "UPDATE controller_profiles SET player = NULL WHERE id = ?", (occupant,)
            )
            self.db.execute(
                "UPDATE controller_profiles SET player = ?, updated_at = ? WHERE id = ?",
                (target.player, _now(), occupant),
            )

        self.db.execute(
            "UPDATE controller_profiles SET player = ?, updated_at = ? WHERE id = ?",
            (player, _now(), profile_id),
        )

    def players(self) -> dict[int, ControllerProfile]:
        """Assigned pads, keyed by player number."""
        return {
            profile.player: profile
            for profile in self.list_profiles()
            if profile.player is not None
        }

    # ── Per-game overrides ────────────────────────────────────────

    def set_for_game(self, game_id: int, profile_id: int, *, player: int = 1) -> None:
        self.db.execute(
            "INSERT INTO game_controller_profiles (game_id, profile_id, player)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(game_id, player) DO UPDATE SET profile_id = excluded.profile_id",
            (game_id, profile_id, player),
        )

    def clear_for_game(self, game_id: int, *, player: Optional[int] = None) -> None:
        if player is None:
            self.db.execute(
                "DELETE FROM game_controller_profiles WHERE game_id = ?", (game_id,)
            )
        else:
            self.db.execute(
                "DELETE FROM game_controller_profiles WHERE game_id = ? AND player = ?",
                (game_id, player),
            )

    def for_game(self, game_id: int) -> dict[int, ControllerProfile]:
        """Pads pinned to a game, keyed by player number."""
        rows = self.db.query(
            "SELECT p.*, g.player AS assigned_player"
            "  FROM game_controller_profiles g"
            "  JOIN controller_profiles p ON p.id = g.profile_id"
            " WHERE g.game_id = ?"
            " ORDER BY g.player",
            (game_id,),
        )
        return {row["assigned_player"]: ControllerProfile.from_row(row) for row in rows}

    # ── What a launch needs ───────────────────────────────────────

    def mappings_for(self, devices, *, game_id: Optional[int] = None) -> list[str]:
        """SDL mapping lines for the connected pads, in player order.

        A game's own override wins, then the pad's saved profile, then a fresh
        resolution against the community database. Pads with no player slot
        follow the assigned ones, because an unassigned pad is still a pad
        somebody may pick up.
        """
        overrides = self.for_game(game_id) if game_id is not None else {}
        by_guid = {profile.guid: profile for profile in overrides.values()}

        entries: list[tuple[int, str]] = []
        for device in devices:
            guid = guid_for(device)
            profile = by_guid.get(guid) or self.for_guid(guid)

            if profile is not None:
                mapping = profile.mapping
                slot = profile.player
            else:
                mapping = controller_db.resolve(device).sdl_mapping
                slot = None

            # Unassigned pads sort last, in the order the kernel listed them.
            entries.append((slot if slot is not None else MAX_PLAYERS + 1, mapping))

        return [mapping for _slot, mapping in sorted(entries, key=lambda e: e[0])]

    def sdl_environment(self, devices, *, game_id: Optional[int] = None) -> dict[str, str]:
        """Environment for a launch, honouring saved profiles and overrides."""
        mappings = self.mappings_for(devices, game_id=game_id)
        if not mappings:
            return {}

        return {
            "SDL_GAMECONTROLLERCONFIG": "\n".join(mappings),
            "SDL_JOYSTICK_HIDAPI": "0",
        }
