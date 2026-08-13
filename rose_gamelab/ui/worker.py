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

#: Threads that would not stop in time. Held only so Python cannot collect a
#: QThread while it is still running; they are never used again.
_ABANDONED: list = []


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
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the work to stop at its next progress report.

        `QThread.quit()` only ends a thread's *event loop*; it cannot interrupt
        a slot that is already running. Work that loops over a library and
        never checks anything therefore runs to completion no matter what,
        which is what left threads alive at shutdown.
        """
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:
        try:
            result = self._work(self._report)
        except Cancelled:
            logger.debug("background work cancelled")
            return
        except Exception as exc:
            logger.exception("background work failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)

    def _report(self, message: str, done: int = 0, total: int = 0) -> None:
        # Raised from inside the work's own progress call, so any loop that
        # reports progress becomes cancellable without knowing it exists.
        if self._cancelled:
            raise Cancelled()
        self.progress.emit(message, done, total)


class Cancelled(Exception):
    """Raised inside worker code when the work has been asked to stop."""


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
        """End the thread. Safe to call more than once.

        The work is asked to cancel first, because `quit()` alone only ends the
        thread's event loop and cannot interrupt a slot that is mid-run.

        If it still has not stopped, the reference is KEPT rather than dropped.
        Dropping it lets Python collect a QThread that is still running, which
        Qt reports as "destroyed while thread is still running" and which can
        take the process down — a real risk when someone closes GameLab a
        second after opening it, while a startup job is in flight.
        """
        if self._thread is None:
            return

        if self._worker is not None:
            self._worker.cancel()

        self._thread.quit()
        if not self._thread.wait(3000):
            logger.warning("a background job did not stop; leaving it to finish")
            _ABANDONED.append((self._thread, self._worker))

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
