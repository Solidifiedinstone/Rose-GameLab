"""Live state of the pads plugged in right now: what they are, and their charge.

Two questions the interface needs to answer continuously, neither of which
`controller.py` covers because both change while GameLab is running: which pads
are connected, and how much battery each has left. Finding out a pad is dying
mid-boss-fight is exactly the thing a couch interface exists to prevent.

Battery is read from sysfs rather than any library. The kernel exposes a
`power_supply` directory on the HID device that owns an input node — hid-sony,
hid-playstation, xpad and hid-nintendo all populate it — so the reliable route
is to start at the input device's own sysfs path and walk up until one appears.
Matching by device name instead would fail on every pad whose battery node is
named after a MAC address, which is most of them.

A pad with no battery node is not an error and not a flat battery: wired pads
have nothing to report, and `Battery` is None for them. The distinction matters,
because "no battery" and "0%" must not look the same on screen.

Nothing here raises. Reading sysfs is best-effort by nature — nodes appear and
vanish as pads connect, and a pad that disconnects between listing and reading
is normal, not exceptional.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rose_gamelab.core import controller_db
from rose_gamelab.core.controller import (
    BUS_BLUETOOTH,
    ControllerDetectionError,
    InputDevice,
    detect_controllers,
)

logger = logging.getLogger(__name__)

SYSFS_ROOT = Path("/sys")

# How far up the device tree to look for a battery. An input node sits a few
# levels below the HID device that owns it; beyond that we would be walking
# into the USB hub, whose battery — if it had one — is not the pad's.
_MAX_WALK = 6

# power_supply `status` values that mean "taking on charge", from
# Documentation/ABI/testing/sysfs-class-power.
_CHARGING = {"charging", "full"}


@dataclass(frozen=True)
class Battery:
    """Charge state of one pad."""

    percent: Optional[int]
    #: The raw kernel status: Charging, Discharging, Full, Not charging, Unknown.
    status: str

    @property
    def charging(self) -> Optional[bool]:
        """True, False, or None when the kernel says Unknown.

        Wireless mice and pads frequently report Unknown while idle, so this
        must be a third state rather than a default of False — telling someone
        their pad is discharging when the kernel does not know is a lie.
        """
        lowered = self.status.strip().lower()
        if lowered in _CHARGING:
            return True
        if lowered == "discharging":
            return False
        return None

    @property
    def low(self) -> bool:
        return self.percent is not None and self.percent <= 20


@dataclass(frozen=True)
class ControllerStatus:
    """One connected pad, as the interface needs to show it."""

    device: InputDevice
    #: The pad's identity — from the community database where it is known.
    name: str
    recognised: bool
    battery: Optional[Battery]
    wireless: bool

    @property
    def label(self) -> str:
        """A single line fit to put on screen."""
        parts = [self.name]
        if self.battery and self.battery.percent is not None:
            charge = f"{self.battery.percent}%"
            if self.battery.charging:
                charge += " charging"
            parts.append(charge)
        elif self.wireless:
            parts.append("wireless")
        return "  ·  ".join(parts)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _power_supply_directory(sysfs: str) -> Optional[Path]:
    """Find the battery node belonging to an input device, if it has one."""
    if not sysfs:
        return None

    current = SYSFS_ROOT / sysfs.lstrip("/")
    for _ in range(_MAX_WALK):
        candidate = current / "power_supply"
        try:
            if candidate.is_dir():
                children = sorted(child for child in candidate.iterdir())
                if children:
                    return children[0]
        except OSError:
            return None

        parent = current.parent
        if parent in (current, SYSFS_ROOT):
            return None
        current = parent

    return None


def battery_for(device: InputDevice) -> Optional[Battery]:
    """Charge state for one pad, or None when it has no battery to report."""
    node = _power_supply_directory(device.sysfs)
    if node is None:
        return None

    raw = _read(node / "capacity")
    percent: Optional[int] = None
    if raw:
        try:
            percent = max(0, min(100, int(raw)))
        except ValueError:
            percent = None

    if percent is None:
        # Some drivers report a coarse level instead of a percentage.
        percent = {
            "full": 100, "high": 75, "normal": 50, "low": 20, "critical": 5,
        }.get(_read(node / "capacity_level").lower())

    status = _read(node / "status") or "Unknown"

    if percent is None and status == "Unknown":
        return None

    return Battery(percent=percent, status=status)


def status_for(device: InputDevice) -> ControllerStatus:
    """Identify one pad and read its charge."""
    resolution = controller_db.resolve(device)

    return ControllerStatus(
        device=device,
        name=resolution.name,
        recognised=resolution.recognised,
        battery=battery_for(device),
        wireless=device.bustype == BUS_BLUETOOTH,
    )


def snapshot() -> list[ControllerStatus]:
    """Every pad connected right now.

    Returns an empty list when detection fails, rather than raising: this is
    called on a timer to keep an indicator up to date, and an unreadable
    `/proc/bus/input/devices` must not take the interface down with it.
    """
    try:
        devices = detect_controllers()
    except (ControllerDetectionError, OSError):
        logger.exception("could not enumerate controllers")
        return []

    return [status_for(device) for device in devices]


def fingerprint(statuses: list[ControllerStatus]) -> tuple:
    """A comparable summary, for noticing that something changed.

    Deliberately includes the battery percentage as well as the set of pads, so
    a watcher polling this reports a pad draining, not only one arriving.
    """
    return tuple(
        (
            status.device.name,
            status.device.vendor_id,
            status.device.product_id,
            status.device.sysfs,
            status.battery.percent if status.battery else None,
        )
        for status in statuses
    )
