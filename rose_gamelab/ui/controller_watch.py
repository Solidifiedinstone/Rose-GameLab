"""Noticing controllers arriving, leaving, and running low.

Detection used to happen only when the Controllers screen was opened, which
means a pad plugged in after GameLab started did not exist as far as the
interface was concerned. For a couch launcher that is the wrong way round: the
pad is usually picked up *after* sitting down.

Two mechanisms, because neither alone is enough. A filesystem watcher on
`/dev/input` reacts the instant udev creates or removes a device node, which is
what makes plugging a pad in feel immediate. A slow timer catches what the
watcher cannot see: battery levels changing, and Bluetooth pads that reconnect
without their node being recreated.

The node appears slightly before the driver has finished with it, so the
watcher waits a moment before looking. Reading a half-initialised device gives a
pad with no name and no vendor id, which would be reported as an unrecognised
controller and then replaced a second later — visible, and wrong.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from rose_gamelab.core import controller_status
from rose_gamelab.core.controller_status import ControllerStatus

logger = logging.getLogger(__name__)

DEVICE_DIRECTORY = "/dev/input"

#: How often to re-read regardless of filesystem events. Battery percentages
#: move slowly, so this is deliberately unhurried — it is not how plugging a
#: pad in is noticed.
POLL_MS = 15_000

#: Time for udev and the driver to finish setting a device up.
SETTLE_MS = 400


class ControllerWatcher(QObject):
    """Reports the connected pads, and when that set changes."""

    #: The full current list, whenever anything about it changes.
    changed = Signal(list)
    #: One pad, when it appears or disappears.
    connected = Signal(object)
    disconnected = Signal(object)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        poll_ms: int = POLL_MS,
        settle_ms: int = SETTLE_MS,
        directory: str = DEVICE_DIRECTORY,
    ) -> None:
        super().__init__(parent)

        self._statuses: list[ControllerStatus] = []
        self._fingerprint: tuple = ()
        self._directory = directory

        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(settle_ms)
        self._settle.timeout.connect(self.refresh)

        self._poll = QTimer(self)
        self._poll.setInterval(poll_ms)
        self._poll.timeout.connect(self.refresh)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._device_directory_changed)

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Begin watching, and report what is already connected."""
        if self._directory:
            self._watcher.addPath(self._directory)
        self._poll.start()
        self.refresh()

    def stop(self) -> None:
        self._poll.stop()
        self._settle.stop()
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())

    # ── Current state ─────────────────────────────────────────────

    @property
    def statuses(self) -> list[ControllerStatus]:
        return list(self._statuses)

    @property
    def any_connected(self) -> bool:
        return bool(self._statuses)

    def _device_directory_changed(self, _path: str) -> None:
        # Coalesce: plugging in one pad creates several nodes at once, and a
        # re-read per node would be several redundant scans.
        self._settle.start()

    def refresh(self) -> None:
        """Re-read the connected devices and emit what changed."""
        # The wider snapshot: pads, plus any wireless peripheral reporting a
        # battery. Only what this returns is displayed — what a *game* is told
        # about still comes from `snapshot()`, which is pads alone.
        statuses = controller_status.battery_snapshot()
        fingerprint = controller_status.fingerprint(statuses)

        if fingerprint == self._fingerprint:
            return

        previous = {self._key(s): s for s in self._statuses}
        current = {self._key(s): s for s in statuses}

        self._statuses = statuses
        self._fingerprint = fingerprint

        for key, status in current.items():
            if key not in previous:
                logger.info("controller connected: %s", status.name)
                self.connected.emit(status)

        for key, status in previous.items():
            if key not in current:
                logger.info("controller disconnected: %s", status.name)
                self.disconnected.emit(status)

        self.changed.emit(statuses)

    @staticmethod
    def _key(status: ControllerStatus) -> tuple:
        """Identity of a pad, excluding anything that changes while connected.

        The battery level is deliberately left out: a pad draining from 80% to
        79% must not read as one pad leaving and another arriving.
        """
        device = status.device
        return (device.vendor_id, device.product_id, device.sysfs, device.name)
