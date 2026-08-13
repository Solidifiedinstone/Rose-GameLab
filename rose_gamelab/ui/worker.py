"""Running slow work off the interface thread.

Every window that scans, hashes or downloads needs the same thing: run a
callable somewhere else, report progress, come back to the interface thread to
show the result. Getting the connection types wrong here crashes the process
rather than merely misbehaving, so it lives in one place instead of being
rewritten per window.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal

logger = logging.getLogger(__name__)


class Worker(QObject):
    """Runs one callable on a worker thread and reports back.

    Qt widgets may only be touched from the interface thread, so workers emit
    signals rather than updating anything themselves.
    """

    progress = Signal(str, int, int)   # message, done, total
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, work) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:
        try:
            result = self._work(self._report)
        except Exception as exc:
            logger.exception("background work failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)

    def _report(self, message: str, done: int = 0, total: int = 0) -> None:
        self.progress.emit(message, done, total)


class BackgroundJob(QObject):
    """One piece of background work, owned by the widget that started it.

    Callbacks are delivered on the interface thread. The thread is torn down
    from `_finish`, which is why every connection below is explicitly queued
    and connected to a *bound method*: a lambda has no thread affinity, so Qt
    falls back to a direct connection and runs the handler on the worker
    thread — where waiting on that same thread kills the process.
    """

    def __init__(
        self,
        work,
        *,
        on_done: Callable[[object], None],
        on_progress: Optional[Callable[[str, int, int], None]] = None,
        on_failed: Optional[Callable[[str], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)

        self._on_done = on_done
        self._on_progress = on_progress
        self._on_failed = on_failed

        self._thread = QThread()
        self._worker = Worker(work)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(
            self._progressed, Qt.ConnectionType.QueuedConnection
        )
        self._worker.failed.connect(
            self._failed, Qt.ConnectionType.QueuedConnection
        )
        self._worker.finished.connect(
            self._finish, Qt.ConnectionType.QueuedConnection
        )

    def start(self) -> None:
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def stop(self) -> None:
        """Wait for the thread to end. Safe to call more than once."""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
            self._worker = None

    # ── Interface-thread callbacks ────────────────────────────────

    def _progressed(self, message: str, done: int, total: int) -> None:
        if self._on_progress is not None:
            self._on_progress(message, done, total)

    def _failed(self, message: str) -> None:
        self.stop()
        if self._on_failed is not None:
            self._on_failed(message)

    def _finish(self, result) -> None:
        self.stop()
        self._on_done(result)
