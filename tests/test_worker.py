"""Background work, and what happens when the window closes during it.

The dangerous case is not work failing — it is work still running when
everything around it goes away. `QThread.quit()` only ends a thread's event
loop; it cannot interrupt a slot that is already running, so a job looping over
a library keeps going regardless. Dropping the reference then lets Python
collect a QThread that is still running, which Qt reports as "destroyed while
thread is still running" and which can take the process down.

Closing GameLab a second after opening it — while the startup scan, art fetch
and achievement refresh are all in flight — is exactly when that happens.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from rose_gamelab.db.database import Database
from rose_gamelab.ui.main_window import MainWindow
from rose_gamelab.ui.preferences import Preferences
from rose_gamelab.ui.worker import _ABANDONED, Cancelled, Worker


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path):
    database = Database(tmp_path / "library.db")
    preferences = Preferences()
    # None of the startup jobs: this file is about the ones it starts itself.
    preferences.scan_on_start = False
    preferences.art_on_start = False
    preferences.achievements_on_start = False
    made = MainWindow(database, preferences=preferences)
    yield made
    made.close()
    database.close()


def test_a_worker_stops_at_its_next_progress_report():
    """Any loop that reports progress becomes cancellable without knowing it."""
    steps = []

    def work(report):
        for index in range(10):
            report(f"step {index}")
            steps.append(index)
        return "finished"

    worker = Worker(work)
    worker.cancel()

    with pytest.raises(Cancelled):
        work(worker._report)

    assert steps == []


def test_work_that_is_not_cancelled_runs_normally():
    worker = Worker(lambda report: "done")
    assert not worker.cancelled


def test_closing_during_a_long_job_stops_it(window, qt_app):
    finished = []

    def slow(report):
        for index in range(300):
            report(f"working {index}", index, 300)
            time.sleep(0.01)
        finished.append(True)
        return index

    before = len(_ABANDONED)
    window._run(slow, "long job", lambda result: None)
    qt_app.processEvents()

    window.close()

    assert window._thread is None
    assert len(_ABANDONED) == before   # nothing was left running behind us
    assert not finished                # it stopped early rather than finishing


def test_a_finished_job_still_reports_its_result(window, qt_app):
    results = []
    window._run(lambda report: 42, "quick job", results.append)

    deadline = time.monotonic() + 5
    while window._thread is not None and time.monotonic() < deadline:
        qt_app.processEvents()

    assert results == [42]


def test_a_failing_job_does_not_leave_the_thread_running(window, qt_app, monkeypatch):
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: None
    )

    def explode(report):
        raise RuntimeError("boom")

    window._run(explode, "doomed job", lambda result: None)

    deadline = time.monotonic() + 5
    while window._thread is not None and time.monotonic() < deadline:
        qt_app.processEvents()

    assert window._thread is None
