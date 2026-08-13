"""Recognising a gamepad from the community mapping database.

`controller.py` can describe a pad's layout and render it for every emulator,
but it only knows the pads someone wrote a table for — the Xbox, PlayStation,
Switch and 8BitDo families. That leaves out the ones retro players actually
reach for: a SNES pad on a Raphnet adapter, a PS2 pad on a Mayflash, an NES
controller on a clone dongle. Those are hundreds of distinct USB ids and no
hand-written table will ever hold them all.

SDL_GameControllerDB is the community's answer — a few thousand pads, each
keyed by the same GUID `controller.sdl_guid` already computes. It is vendored
in `data/gamecontrollerdb.txt` (zlib licence, see the .LICENSE beside it) so
recognition works with no network and no account, which is the rule for
everything else here too.

Lookup mirrors SDL's own matching, from `SDL_PrivateMatchControllerMappingForGUID`:
the name-CRC field is cleared, an exact match is tried first, and a device whose
GUID carries a version falls back to matching with the version zeroed. Getting
that wrong means a pad that SDL itself would recognise reads as unknown here.

A miss is not a failure. `controller.default_mapping` still returns a complete
layout for the family, and the user can still correct any of it by hand — the
database only removes the need to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from rose_gamelab.core.controller import (
    ControllerMapping,
    InputDevice,
    default_mapping,
    sdl_guid,
    to_sdl_mapping,
)

logger = logging.getLogger(__name__)

DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "gamecontrollerdb.txt"

#: Fields that describe how to match an entry rather than how the pad is laid
#: out. They are dropped when re-emitting: the mapping is re-keyed to the GUID
#: of the device actually plugged in, so the original match constraints would
#: only narrow it back down again.
_MATCHING_FIELDS = ("platform", "crc", "sdk", "hint")

# Where the GUID's name-CRC and version sit, as hex-string character offsets.
# Each 16-bit field is four characters, little-endian, per `sdl_guid`.
_CRC_SLICE = slice(4, 8)
_VERSION_SLICE = slice(24, 28)


@dataclass(frozen=True)
class DatabaseEntry:
    """One pad from the database."""

    guid: str
    name: str
    #: Output name -> physical token, e.g. {"a": "b0", "dpup": "h0.1"}.
    fields: dict[str, str]

    def render(self, *, guid: str, name: str, platform: str = "Linux") -> str:
        """This layout as an SDL mapping string, keyed to a specific device.

        The database GUID is deliberately not reused. It may differ from the
        plugged-in device's in the version field — that is the whole point of
        the version-insensitive fallback — and SDL matches the string we export
        against the *device*, so keying it to the device is what guarantees a
        hit rather than a near miss.
        """
        safe_name = name.replace(",", " ").strip() or self.name
        body = ",".join(f"{key}:{value}" for key, value in sorted(self.fields.items()))
        return f"{guid},{safe_name},{body},platform:{platform},"


def _clear(guid: str, where: slice) -> str:
    return guid[: where.start] + "0000" + guid[where.stop :]


def normalise(guid: str) -> str:
    """A GUID with the name-CRC cleared, which is how entries are stored.

    SDL clears this field before every comparison because database entries
    never carry one.
    """
    guid = guid.strip().lower()
    if len(guid) != 32:
        return guid
    return _clear(guid, _CRC_SLICE)


def _without_version(guid: str) -> str:
    if len(guid) != 32:
        return guid
    return _clear(guid, _VERSION_SLICE)


def parse_database(text: str) -> dict[str, DatabaseEntry]:
    """Parse gamecontrollerdb.txt into entries, keyed by normalised GUID.

    Only the Linux rows are kept. The Windows and macOS rows describe the same
    pads through a different driver stack, so their button numbers do not apply
    here — loading them would silently produce wrong layouts.

    Malformed rows are skipped rather than raising: this file is community
    maintained and grows every week, and one bad line must not cost the user
    every other pad in it.
    """
    entries: dict[str, DatabaseEntry] = {}

    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [part for part in line.split(",") if part]
        if len(parts) < 3:
            continue

        guid, name, *rest = parts
        fields: dict[str, str] = {}
        platform = ""

        for field in rest:
            key, separator, value = field.partition(":")
            if not separator:
                continue
            if key == "platform":
                platform = value
                continue
            if key in _MATCHING_FIELDS:
                continue
            fields[key] = value

        if platform != "Linux" or not fields:
            continue

        key = normalise(guid)
        if len(key) != 32:
            logger.debug("skipping malformed GUID on line %d", number)
            continue

        # First entry wins: the file is ordered with the better-maintained
        # rows first, and later duplicates are usually narrower variants.
        entries.setdefault(key, DatabaseEntry(guid=key, name=name, fields=fields))

    return entries


@lru_cache(maxsize=1)
def load_database(path: Optional[Path] = None) -> dict[str, DatabaseEntry]:
    """The vendored database, parsed once.

    An unreadable database is logged and treated as empty: recognition degrades
    to the built-in family layouts, which is a worse experience but a working
    one, and is far better than refusing to launch anything.
    """
    source = Path(path) if path else DATABASE_PATH
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("could not read the controller database at %s: %s", source, exc)
        return {}

    entries = parse_database(text)
    logger.debug("loaded %d Linux controller mappings", len(entries))
    return entries


def lookup(guid: str, database: Optional[dict[str, DatabaseEntry]] = None) -> Optional[DatabaseEntry]:
    """Find a pad by GUID, the way SDL finds one.

    Exact match first; then, since the version field distinguishes hardware
    revisions that almost always share a layout, a match with it zeroed.
    """
    entries = load_database() if database is None else database
    key = normalise(guid)

    exact = entries.get(key)
    if exact is not None:
        return exact

    wanted = _without_version(key)
    if wanted == key:
        return None

    for candidate_guid, entry in entries.items():
        if _without_version(candidate_guid) == wanted:
            return entry

    return None


@dataclass(frozen=True)
class Resolution:
    """How a connected pad was identified, and what to hand to SDL."""

    device: InputDevice
    sdl_mapping: str
    #: 'database' when the community database knew this pad, 'builtin' when it
    #: fell back to the family layout. Shown in the interface, because "we
    #: guessed" and "this is the known-good layout" are different promises.
    source: str
    name: str

    @property
    def recognised(self) -> bool:
        return self.source == "database"


def resolve(device: InputDevice) -> Resolution:
    """Work out the button layout for one connected pad.

    Never returns None. A pad nobody has ever mapped still gets its family's
    layout, which is right far more often than it is wrong.
    """
    guid = sdl_guid(device.bustype, device.vendor_id, device.product_id, device.version)
    entry = lookup(guid)

    if entry is not None:
        return Resolution(
            device=device,
            sdl_mapping=entry.render(guid=guid, name=device.name),
            source="database",
            name=entry.name,
        )

    mapping: ControllerMapping = default_mapping(device)
    return Resolution(
        device=device,
        sdl_mapping=to_sdl_mapping(mapping),
        source="builtin",
        name=device.controller_type.label,
    )


def resolve_all(devices) -> list[Resolution]:
    return [resolve(device) for device in devices]


def sdl_environment_for(devices) -> dict[str, str]:
    """Environment for a launched game, covering every connected pad.

    SDL reads `SDL_GAMECONTROLLERCONFIG` as newline-separated mappings and adds
    each at user priority, above its own built-in database — so one variable
    configures every pad at once, which is what makes multiplayer work without
    touching a single emulator's settings.

    `SDL_JOYSTICK_HIDAPI=0` travels with it for the reason
    `controller.sdl_environment` gives: HIDAPI re-reports pads under a different
    GUID and would ignore everything here.
    """
    resolutions = resolve_all(devices)
    if not resolutions:
        return {}

    return {
        "SDL_GAMECONTROLLERCONFIG": "\n".join(r.sdl_mapping for r in resolutions),
        "SDL_JOYSTICK_HIDAPI": "0",
    }
