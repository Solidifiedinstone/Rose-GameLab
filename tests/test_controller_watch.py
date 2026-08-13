"""Noticing pads arriving and leaving while GameLab runs."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from rose_gamelab.core import controller_status
from rose_gamelab.core.controller import InputDevice
from rose_gamelab.ui import controller_watch
from rose_gamelab.ui.controller_watch import ControllerWatcher


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


def pad(name="Wireless Controller", product=0x09CC, sysfs="/devices/usb1/input/input1"):
    return InputDevice(
        name=name, vendor_id=0x054C, product_id=product,
        bustype=0x0003, version=0x0100, sysfs=sysfs,
    )


@pytest.fixture
def connected(monkeypatch):
    """A mutable list standing in for what is plugged in."""
    devices: list[InputDevice] = []
    monkeypatch.setattr(controller_status, "detect_controllers", lambda: list(devices))
    # No sysfs in the test environment, so no batteries — irrelevant here.
    monkeypatch.setattr(controller_status, "battery_for", lambda _device: None)
    return devices


@pytest.fixture
def watcher(qt_app, connected):
    # No real /dev/input watching: these tests drive refresh() directly, which
    # is what the filesystem event ends up calling anyway.
    made = ControllerWatcher(directory="")
    yield made
    made.stop()


def test_nothing_connected_is_reported_as_nothing(watcher):
    watcher.refresh()
    assert watcher.statuses == []
    assert not watcher.any_connected


def test_a_pad_arriving_is_announced(watcher, connected):
    seen = []
    watcher.connected.connect(seen.append)

    connected.append(pad())
    watcher.refresh()

    assert len(seen) == 1
    assert watcher.any_connected


def test_a_pad_leaving_is_announced(watcher, connected):
    connected.append(pad())
    watcher.refresh()

    seen = []
    watcher.disconnected.connect(seen.append)
    connected.clear()
    watcher.refresh()

    assert len(seen) == 1
    assert not watcher.any_connected


def test_the_full_list_is_emitted_on_change(watcher, connected):
    seen = []
    watcher.changed.connect(seen.append)

    connected.append(pad())
    watcher.refresh()
    connected.append(pad(name="Second", product=0x028E, sysfs="/devices/usb2/input/input2"))
    watcher.refresh()

    assert [len(batch) for batch in seen] == [1, 2]


def test_nothing_is_emitted_when_nothing_changed(watcher, connected):
    connected.append(pad())
    watcher.refresh()

    seen = []
    watcher.changed.connect(seen.append)
    watcher.refresh()
    watcher.refresh()

    assert seen == []


def test_a_draining_battery_is_not_a_reconnection(watcher, connected, monkeypatch):
    """Regression risk: keying pads by their whole status would make every
    battery tick look like the pad being unplugged and plugged back in."""
    from rose_gamelab.core.controller_status import Battery

    level = {"percent": 80}
    monkeypatch.setattr(
        controller_status, "battery_for",
        lambda _device: Battery(percent=level["percent"], status="Discharging"),
    )
    connected.append(pad())
    watcher.refresh()

    arrivals, departures = [], []
    watcher.connected.connect(arrivals.append)
    watcher.disconnected.connect(departures.append)

    level["percent"] = 79
    watcher.refresh()

    assert arrivals == []
    assert departures == []


def test_a_draining_battery_still_updates_the_list(watcher, connected, monkeypatch):
    """It is not a reconnection, but the interface must still see the new level."""
    from rose_gamelab.core.controller_status import Battery

    level = {"percent": 80}
    monkeypatch.setattr(
        controller_status, "battery_for",
        lambda _device: Battery(percent=level["percent"], status="Discharging"),
    )
    connected.append(pad())
    watcher.refresh()

    seen = []
    watcher.changed.connect(seen.append)
    level["percent"] = 30
    watcher.refresh()

    assert len(seen) == 1
    assert seen[0][0].battery.percent == 30


def test_swapping_one_pad_for_another_reports_both(watcher, connected):
    connected.append(pad())
    watcher.refresh()

    arrivals, departures = [], []
    watcher.connected.connect(arrivals.append)
    watcher.disconnected.connect(departures.append)

    connected.clear()
    connected.append(pad(name="Xbox", product=0x028E, sysfs="/devices/usb9/input/input9"))
    watcher.refresh()

    assert len(arrivals) == 1
    assert len(departures) == 1


def test_detection_failing_does_not_raise(watcher, monkeypatch):
    def explode():
        raise OSError("no /proc for you")

    monkeypatch.setattr(controller_status, "detect_controllers", explode)
    watcher.refresh()  # must not raise

    assert watcher.statuses == []


def test_filesystem_events_are_coalesced(qt_app, connected, monkeypatch):
    """Plugging in one pad creates several device nodes at once."""
    scans = []
    watcher = ControllerWatcher(directory="", settle_ms=10)
    monkeypatch.setattr(
        controller_status, "snapshot", lambda: scans.append(1) or []
    )

    watcher._device_directory_changed("/dev/input")
    watcher._device_directory_changed("/dev/input")
    watcher._device_directory_changed("/dev/input")

    assert scans == []  # nothing yet: it waits for the device to settle
    watcher.stop()


def test_start_reports_what_is_already_connected(watcher, connected):
    connected.append(pad())

    seen = []
    watcher.changed.connect(seen.append)
    watcher.start()

    assert len(seen) == 1
