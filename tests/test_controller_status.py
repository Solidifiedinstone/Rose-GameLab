"""Live pad state: identity, connection and battery.

Battery comes from sysfs, so these build a fake one. The layout mirrors what a
real machine has — an input node several levels below the HID device that owns
the `power_supply` directory — because the walk up that tree is the part that
breaks if the assumption is wrong.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core import controller_status
from rose_gamelab.core.controller import InputDevice
from rose_gamelab.core.controller_status import (
    Battery,
    battery_for,
    fingerprint,
    snapshot,
    status_for,
)


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_status, "SYSFS_ROOT", tmp_path)
    return tmp_path


def make_device(sysfs_path="devices/pci/usb1/0003:054C:09CC.0003/input/input17", **kwargs):
    defaults = dict(
        name="Wireless Controller", vendor_id=0x054C, product_id=0x09CC,
        bustype=0x0003, version=0x0100, sysfs="/" + sysfs_path,
    )
    defaults.update(kwargs)
    return InputDevice(**defaults)


def give_battery(root, device, *, capacity="86", status="Discharging", level=None):
    """Create a power_supply node on the HID parent, as the kernel does."""
    hid = root / device.sysfs.lstrip("/")
    hid.mkdir(parents=True, exist_ok=True)
    node = hid.parent.parent / "power_supply" / "ps-controller-battery-aa:bb"
    node.mkdir(parents=True, exist_ok=True)
    if capacity is not None:
        (node / "capacity").write_text(capacity)
    if level is not None:
        (node / "capacity_level").write_text(level)
    (node / "status").write_text(status)
    return node


# ── Battery ───────────────────────────────────────────────────────

def test_battery_is_found_by_walking_up_from_the_input_device(sysfs):
    """Matching on device name fails for every pad whose battery node is named
    after a MAC address, which is most of them."""
    device = make_device()
    give_battery(sysfs, device)

    battery = battery_for(device)

    assert battery is not None
    assert battery.percent == 86
    assert battery.charging is False


def test_a_wired_pad_reports_no_battery(sysfs):
    """No battery node is not a flat battery, and must not look like one."""
    device = make_device()
    (sysfs / device.sysfs.lstrip("/")).mkdir(parents=True)

    assert battery_for(device) is None


def test_charging_is_read_from_the_kernel_status(sysfs):
    device = make_device()
    give_battery(sysfs, device, status="Charging")

    assert battery_for(device).charging is True


def test_full_counts_as_charging(sysfs):
    device = make_device()
    give_battery(sysfs, device, status="Full")

    assert battery_for(device).charging is True


def test_unknown_charging_state_is_not_reported_as_discharging(sysfs):
    """Wireless pads report Unknown while idle. Saying "discharging" would be
    inventing information the kernel does not have."""
    device = make_device()
    give_battery(sysfs, device, status="Unknown")

    assert battery_for(device).charging is None


def test_a_coarse_level_is_used_when_there_is_no_percentage(sysfs):
    """Some drivers report low/normal/high instead of a number."""
    device = make_device()
    give_battery(sysfs, device, capacity=None, level="Low", status="Discharging")

    assert battery_for(device).percent == 20


def test_a_nonsense_capacity_does_not_crash(sysfs):
    device = make_device()
    give_battery(sysfs, device, capacity="banana", status="Discharging")

    battery = battery_for(device)
    assert battery is not None
    assert battery.percent is None


def test_capacity_is_clamped(sysfs):
    device = make_device()
    give_battery(sysfs, device, capacity="140")

    assert battery_for(device).percent == 100


def test_a_device_with_no_sysfs_path_is_handled(sysfs):
    assert battery_for(make_device(sysfs="")) is None


def test_the_walk_does_not_escape_into_unrelated_hardware(sysfs):
    """A battery far above the pad belongs to something else — a laptop, a hub."""
    device = make_device(sysfs="/a/b/c/d/e/f/g/h/input/input1")
    (sysfs / "a/b/c/d/e/f/g/h/input/input1").mkdir(parents=True)
    stray = sysfs / "a" / "power_supply" / "BAT0"
    stray.mkdir(parents=True)
    (stray / "capacity").write_text("55")
    (stray / "status").write_text("Discharging")

    assert battery_for(device) is None


def test_low_battery_is_flagged():
    assert Battery(percent=15, status="Discharging").low
    assert not Battery(percent=55, status="Discharging").low
    assert not Battery(percent=None, status="Unknown").low


# ── Status ────────────────────────────────────────────────────────

def test_status_identifies_the_pad(sysfs):
    status = status_for(make_device())

    assert status.recognised
    assert "PS4" in status.name


def test_bluetooth_pads_are_marked_wireless(sysfs):
    assert status_for(make_device(bustype=0x0005)).wireless
    assert not status_for(make_device(bustype=0x0003)).wireless


def test_the_label_reads_as_one_line(sysfs):
    device = make_device()
    give_battery(sysfs, device, capacity="42", status="Charging")

    label = status_for(device).label

    assert "42%" in label
    assert "charging" in label


def test_a_wired_pad_label_has_no_battery_text(sysfs):
    device = make_device()
    (sysfs / device.sysfs.lstrip("/")).mkdir(parents=True)

    assert "%" not in status_for(device).label


# ── Snapshot and change detection ─────────────────────────────────

def test_snapshot_survives_detection_failing(monkeypatch):
    """This runs on a timer; an unreadable /proc must not take the UI down."""
    def explode():
        raise OSError("nope")

    monkeypatch.setattr(controller_status, "detect_controllers", explode)

    assert snapshot() == []


def test_snapshot_lists_connected_pads(monkeypatch, sysfs):
    monkeypatch.setattr(
        controller_status, "detect_controllers", lambda: [make_device()]
    )

    assert len(snapshot()) == 1


def test_fingerprint_changes_when_a_pad_arrives(sysfs):
    one = [status_for(make_device())]
    two = [*one, status_for(make_device(name="Second", product_id=0x028E))]

    assert fingerprint(one) != fingerprint(two)


def test_fingerprint_changes_when_the_battery_moves(sysfs):
    """A watcher should report a pad draining, not only one arriving."""
    device = make_device()
    give_battery(sysfs, device, capacity="80")
    before = fingerprint([status_for(device)])

    give_battery(sysfs, device, capacity="20")
    after = fingerprint([status_for(device)])

    assert before != after


def test_fingerprint_is_stable_when_nothing_changes(sysfs):
    device = make_device()
    give_battery(sysfs, device)

    assert fingerprint([status_for(device)]) == fingerprint([status_for(device)])
