"""The connected-controller readout, in the status bar and in Big Picture."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from rose_gamelab.core.controller import InputDevice
from rose_gamelab.core.controller_status import Battery, ControllerStatus
from rose_gamelab.ui.theme import THEMES
from rose_gamelab.ui.widgets.controller_indicator import (
    ControllerIndicator,
    battery_glyph,
    describe,
)


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


def status(name="PS4 Controller", percent=None, charging=None, wireless=False):
    battery = None
    if percent is not None:
        battery = Battery(
            percent=percent,
            status="Charging" if charging else "Discharging",
        )
    return ControllerStatus(
        device=InputDevice(name=name, vendor_id=1, product_id=2, bustype=3, version=1),
        name=name, recognised=True, battery=battery, wireless=wireless,
    )


# ── Wording ───────────────────────────────────────────────────────

def test_nothing_is_said_when_no_pad_is_connected():
    """Permanently displaying "No controller" nags someone playing on a
    keyboard on purpose."""
    assert describe([]) == ""


def test_one_pad_is_named():
    assert "PS4 Controller" in describe([status()])


def test_a_battery_percentage_is_shown():
    assert "64%" in describe([status(percent=64)])


def test_two_devices_are_both_named():
    """There is room for two; naming them beats an anonymous count."""
    text = describe([status(percent=90), status(name="Xbox", percent=15)])

    assert "PS4 Controller" in text
    assert "Xbox" in text


def test_several_pads_show_a_count_and_the_lowest_battery():
    """Three full readouts would overflow a status bar, and the pad about to
    die is the one worth the space."""
    text = describe([
        status(percent=90),
        status(name="Xbox", percent=15),
        status(name="Switch", percent=60),
    ])

    assert "3 controllers" in text
    assert "15%" in text
    assert "90%" not in text


def test_several_pads_without_batteries_just_count():
    text = describe([status(), status(name="Xbox"), status(name="Switch")])
    assert text == "🎮 3 controllers"


def test_charging_has_its_own_glyph():
    assert battery_glyph(20, charging=True) == "⚡"


def test_an_unknown_charge_shows_no_glyph():
    assert battery_glyph(None, charging=None) == ""


# ── The widget ────────────────────────────────────────────────────

def test_the_widget_hides_itself_when_nothing_is_connected(qt_app):
    indicator = ControllerIndicator(next(iter(THEMES.values())))
    indicator.set_statuses([status()])
    assert indicator.isVisibleTo(indicator.parentWidget() or indicator)

    indicator.set_statuses([])

    assert indicator.isHidden()
    assert indicator.text() == ""


def test_the_widget_lists_every_pad_in_its_tooltip(qt_app):
    indicator = ControllerIndicator(next(iter(THEMES.values())))
    indicator.set_statuses([status(percent=50), status(name="Xbox", percent=80)])

    tooltip = indicator.toolTip()

    assert "PS4 Controller" in tooltip
    assert "Xbox" in tooltip


def test_a_low_battery_is_coloured_as_a_warning(qt_app):
    theme = next(iter(THEMES.values()))
    indicator = ControllerIndicator(theme)

    indicator.set_statuses([status(percent=80)])
    normal = indicator.styleSheet()
    indicator.set_statuses([status(percent=9)])
    low = indicator.styleSheet()

    assert normal != low
    assert theme.warning in low


def test_a_low_but_charging_battery_is_not_a_warning(qt_app):
    """It is being dealt with; colouring it red is noise."""
    theme = next(iter(THEMES.values()))
    indicator = ControllerIndicator(theme)

    indicator.set_statuses([status(percent=9, charging=True)])

    assert theme.warning not in indicator.styleSheet()


# ── Mixed devices ─────────────────────────────────────────────────

def peripheral_status(kind, name, percent=None):
    from rose_gamelab.core.controller import InputDevice
    from rose_gamelab.core.controller_status import Battery, ControllerStatus

    return ControllerStatus(
        device=InputDevice(name=name, vendor_id=1, product_id=2, bustype=3, version=1),
        name=name, recognised=False,
        battery=Battery(percent=percent, status="Unknown") if percent else None,
        wireless=True, kind=kind,
    )


def test_a_mouse_is_not_drawn_as_a_gamepad():
    """A mouse with a controller icon is worse than no icon at all."""
    text = describe([peripheral_status("mouse", "Wireless Gaming Mouse", 86)])

    assert "🖱" in text
    assert "🎮" not in text
    assert "86%" in text


def test_a_keyboard_has_its_own_glyph():
    assert "⌨" in describe([peripheral_status("keyboard", "MX Keys", 40)])


def test_a_pad_and_a_mouse_are_both_named():
    text = describe([status(percent=70), peripheral_status("mouse", "Wireless Gaming Mouse", 86)])

    assert "🎮" in text
    assert "🖱" in text


def test_many_devices_count_only_the_pads():
    """The count is about controllers; a mouse is not a player."""
    text = describe([
        status(name="P1", percent=90),
        status(name="P2", percent=80),
        peripheral_status("mouse", "Wireless Gaming Mouse", 15),
    ])

    assert "2 controllers" in text
    assert "15%" in text     # the lowest, whatever it belongs to
