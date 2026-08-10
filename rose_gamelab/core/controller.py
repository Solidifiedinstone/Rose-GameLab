"""Controller detection and one-mapping-fits-all-emulators translation.

The problem this solves: every emulator has its own controller configuration
screen, its own file format, and its own idea of what "the A button" means. A
user with one gamepad and eight emulators configures that gamepad eight times,
and every one of them disagrees about whether A is the bottom face button or
the right one. GameLab's answer is a single pivot — the user describes their
physical pad ONCE against a canonical model, and this module renders that
description into whatever each emulator actually wants to read.

Detection deliberately uses no library at all. The previous version of this
file was written against SDL entry points that do not exist, wrapped every call
in a bare `except`, and therefore returned an empty list on every machine while
looking healthy. `/proc/bus/input/devices` is plain text, is present on every
Linux kernel, lists vendor/product/name/handlers for every input device, and
cannot be absent without the machine being fundamentally broken. So we parse
that. When it cannot be read we raise — a launcher that reports "no controllers
found" when the real answer is "I could not look" is worse than useless.

The highest-leverage output by far is `to_sdl_mapping()`. Most modern emulators
(PCSX2, DuckStation, Dolphin, PPSSPP, Flycast, melonDS, RPCS3, Ryujinx, and
RetroArch's sdl2 joypad driver) sit on SDL's gamepad layer, and SDL reads the
`SDL_GAMECONTROLLERCONFIG` environment variable before consulting its built-in
database. Exporting one string into the child process's environment therefore
fixes the button layout for that entire set at once, with no file written
anywhere. The per-emulator file exporters below are for the bindings SDL cannot
express — which button does what *in the emulated machine*.

Nothing here touches the network or the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

PROC_INPUT_DEVICES = Path("/proc/bus/input/devices")
DEV_INPUT_BY_ID = Path("/dev/input/by-id")

# USB vendor ids, as reported by the kernel in /proc/bus/input/devices.
VENDOR_MICROSOFT = 0x045E
VENDOR_SONY = 0x054C
VENDOR_NINTENDO = 0x057E
VENDOR_8BITDO = 0x2DC8
VENDOR_VALVE = 0x28DE
VENDOR_LOGITECH = 0x046D

# Linux input bus types (linux/input.h), needed verbatim for the SDL GUID.
BUS_USB = 0x0003
BUS_BLUETOOTH = 0x0005


class ControllerDetectionError(RuntimeError):
    """Raised when we could not enumerate input devices at all.

    Distinct from "enumerated successfully, found no gamepads". Callers must
    surface the reason to the user rather than showing an empty list.
    """


# ── Detected hardware ─────────────────────────────────────────────

@dataclass(frozen=True)
class InputDevice:
    """One device as the kernel describes it in /proc/bus/input/devices."""

    name: str
    vendor_id: int
    product_id: int
    bustype: int = 0
    version: int = 0
    # Every handler token the kernel listed, e.g. {"event20", "js0", "kbd"}.
    handlers: frozenset[str] = frozenset()
    sysfs: str = ""
    phys: str = ""
    uniq: str = ""

    @property
    def event_path(self) -> Optional[Path]:
        """/dev/input/eventN for this device, or None if it has no event node."""
        for handler in sorted(self.handlers):
            if re.fullmatch(r"event\d+", handler):
                return Path("/dev/input") / handler
        return None

    @property
    def joystick_path(self) -> Optional[Path]:
        """/dev/input/jsN — the legacy joydev node, if the kernel created one."""
        for handler in sorted(self.handlers):
            if re.fullmatch(r"js\d+", handler):
                return Path("/dev/input") / handler
        return None

    @property
    def is_gamepad(self) -> bool:
        """Whether the kernel treats this device as a joystick/gamepad.

        The tell is the `jsN` handler. joydev only binds to devices whose
        capability bits look like a joystick, so this is the kernel's own
        classification rather than a guess of ours — far more reliable than
        pattern-matching device names, which range from "Microsoft X-Box 360
        pad" to "Wireless Controller" to bare vendor strings.

        Wheels and flight sticks also get a `jsN` node. That is correct: they
        are things a user may want to map.
        """
        return self.joystick_path is not None

    @property
    def controller_type(self) -> ControllerType:
        return identify_controller(self.vendor_id, self.product_id, self.name)


class ControllerType(str, Enum):
    """Recognised controller families, used to pick a sensible starting layout."""

    XBOX_360 = "xbox360"
    XBOX_ONE = "xbox_one"          # also Series X|S, same protocol family
    DUALSHOCK_3 = "dualshock3"
    DUALSHOCK_4 = "dualshock4"
    DUALSENSE = "dualsense"
    SWITCH_PRO = "switch_pro"
    JOYCON = "joycon"
    EIGHTBITDO = "8bitdo"
    STEAM = "steam"
    GENERIC = "generic"

    @property
    def label(self) -> str:
        return _TYPE_LABELS[self]


_TYPE_LABELS = {
    ControllerType.XBOX_360: "Xbox 360 Controller",
    ControllerType.XBOX_ONE: "Xbox One / Series Controller",
    ControllerType.DUALSHOCK_3: "DualShock 3",
    ControllerType.DUALSHOCK_4: "DualShock 4",
    ControllerType.DUALSENSE: "DualSense",
    ControllerType.SWITCH_PRO: "Switch Pro Controller",
    ControllerType.JOYCON: "Joy-Con",
    ControllerType.EIGHTBITDO: "8BitDo Controller",
    ControllerType.STEAM: "Steam Controller",
    ControllerType.GENERIC: "Gamepad",
}

# Product ids are the reliable discriminator within a vendor. These lists are
# not exhaustive — every revision and regional variant gets its own id — so
# identification falls back to the device name, and ultimately to GENERIC.
# GENERIC is not a failure: the user maps it once and it works like any other.
_SONY_PRODUCTS = {
    0x0268: ControllerType.DUALSHOCK_3,
    0x05C4: ControllerType.DUALSHOCK_4,   # DS4 v1
    0x09CC: ControllerType.DUALSHOCK_4,   # DS4 v2
    0x0BA0: ControllerType.DUALSHOCK_4,   # DS4 USB dongle
    0x0CE6: ControllerType.DUALSENSE,
    0x0DF2: ControllerType.DUALSENSE,     # DualSense Edge
}

_NINTENDO_PRODUCTS = {
    0x2006: ControllerType.JOYCON,        # Joy-Con (L)
    0x2007: ControllerType.JOYCON,        # Joy-Con (R)
    0x2009: ControllerType.SWITCH_PRO,
    0x200E: ControllerType.JOYCON,        # charging grip
}

# Microsoft ids split cleanly by era: the 360 family sits in 0x028x/0x029x,
# everything from the Xbox One onward is 0x02Dx-0x02Fx or 0x0Bxx.
_XBOX_360_PRODUCTS = {0x028E, 0x028F, 0x0291, 0x02A0, 0x02A1, 0x0719}


def identify_controller(vendor_id: int, product_id: int, name: str = "") -> ControllerType:
    """Classify a pad from its USB ids, falling back to its reported name.

    Name matching matters more than it looks: many third-party pads (and 8BitDo
    in its Xinput mode) clone Microsoft's vendor id outright, and Bluetooth
    connections often report a generic id with a descriptive name.
    """
    if vendor_id == VENDOR_MICROSOFT:
        if product_id in _XBOX_360_PRODUCTS:
            return ControllerType.XBOX_360
        # Xbox One (2013) onward, including Series and the Elite pads.
        if 0x02D0 <= product_id <= 0x02FF or 0x0B00 <= product_id <= 0x0BFF:
            return ControllerType.XBOX_ONE
    elif vendor_id == VENDOR_SONY:
        found = _SONY_PRODUCTS.get(product_id)
        if found:
            return found
    elif vendor_id == VENDOR_NINTENDO:
        found = _NINTENDO_PRODUCTS.get(product_id)
        if found:
            return found
    elif vendor_id == VENDOR_8BITDO:
        return ControllerType.EIGHTBITDO
    elif vendor_id == VENDOR_VALVE:
        return ControllerType.STEAM

    lowered = name.lower()
    if "8bitdo" in lowered:
        return ControllerType.EIGHTBITDO
    if "dualsense" in lowered or "ps5" in lowered:
        return ControllerType.DUALSENSE
    if "dualshock 4" in lowered or "wireless controller" in lowered:
        return ControllerType.DUALSHOCK_4
    if "dualshock 3" in lowered or "playstation(r)3" in lowered:
        return ControllerType.DUALSHOCK_3
    if "pro controller" in lowered:
        return ControllerType.SWITCH_PRO
    if "joy-con" in lowered:
        return ControllerType.JOYCON
    if "360" in lowered and "box" in lowered:
        return ControllerType.XBOX_360
    if "xbox" in lowered or "x-box" in lowered:
        return ControllerType.XBOX_ONE
    if "steam controller" in lowered:
        return ControllerType.STEAM

    return ControllerType.GENERIC


# ── Parsing /proc/bus/input/devices ───────────────────────────────
#
# The file is blank-line-separated stanzas of prefixed lines:
#
#   I: Bus=0003 Vendor=045e Product=028e Version=0114
#   N: Name="Microsoft X-Box 360 pad"
#   P: Phys=usb-0000:00:14.0-2/input0
#   S: Sysfs=/devices/.../input/input20
#   U: Uniq=
#   H: Handlers=event20 js0
#   B: PROP=0
#   ...
#
# All the ids are hex without a 0x prefix.

_ID_RE = re.compile(
    r"Bus=([0-9a-fA-F]+)\s+Vendor=([0-9a-fA-F]+)\s+"
    r"Product=([0-9a-fA-F]+)\s+Version=([0-9a-fA-F]+)"
)
_NAME_RE = re.compile(r'Name="(.*)"')


def parse_proc_input_devices(text: str) -> list[InputDevice]:
    """Parse the contents of /proc/bus/input/devices into InputDevice records.

    Pure: takes the text, not the path, so it is testable without hardware.
    Stanzas without a parseable `I:` line are skipped — a truncated read should
    cost us one device, not the whole enumeration.
    """
    devices: list[InputDevice] = []

    for stanza in text.split("\n\n"):
        if not stanza.strip():
            continue

        ids: Optional[re.Match[str]] = None
        name = ""
        handlers: set[str] = set()
        sysfs = phys = uniq = ""

        for line in stanza.splitlines():
            line = line.strip()
            if line.startswith("I:"):
                ids = _ID_RE.search(line)
            elif line.startswith("N:"):
                match = _NAME_RE.search(line)
                if match:
                    name = match.group(1)
            elif line.startswith("H:"):
                _, _, rest = line.partition("Handlers=")
                handlers.update(rest.split())
            elif line.startswith("S:"):
                sysfs = line.partition("Sysfs=")[2].strip()
            elif line.startswith("P:"):
                phys = line.partition("Phys=")[2].strip()
            elif line.startswith("U:"):
                uniq = line.partition("Uniq=")[2].strip()

        if ids is None:
            continue

        devices.append(
            InputDevice(
                name=name,
                bustype=int(ids.group(1), 16),
                vendor_id=int(ids.group(2), 16),
                product_id=int(ids.group(3), 16),
                version=int(ids.group(4), 16),
                handlers=frozenset(handlers),
                sysfs=sysfs,
                phys=phys,
                uniq=uniq,
            )
        )

    return devices


def read_input_devices(path: Path = PROC_INPUT_DEVICES) -> list[InputDevice]:
    """Read and parse the kernel's input device table.

    Raises ControllerDetectionError with a usable reason when the file cannot
    be read, rather than returning an empty list that the caller would
    misreport as "no controller connected".
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as error:
        raise ControllerDetectionError(
            f"{path} does not exist — controller detection needs a Linux kernel "
            f"with procfs mounted."
        ) from error
    except PermissionError as error:
        raise ControllerDetectionError(
            f"Not permitted to read {path}. In a sandbox or container, mount "
            f"/proc and /dev/input to enable controller detection."
        ) from error
    except OSError as error:
        raise ControllerDetectionError(f"Could not read {path}: {error}") from error

    return parse_proc_input_devices(text)


def detect_controllers(path: Path = PROC_INPUT_DEVICES) -> list[InputDevice]:
    """Every gamepad the kernel currently knows about.

    An empty list here genuinely means nothing is plugged in; failures raise.
    """
    return [device for device in read_input_devices(path) if device.is_gamepad]


def joystick_symlinks(directory: Path = DEV_INPUT_BY_ID) -> list[Path]:
    """Stable /dev/input/by-id/*-event-joystick paths for attached pads.

    The eventN numbering changes every time a device is replugged; the by-id
    symlink does not, so this is what to persist in a config file. Missing
    directory means no USB/Bluetooth input devices at all, which is a legitimate
    empty answer rather than an error.
    """
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.name.endswith("-event-joystick"))


# ── The canonical model ───────────────────────────────────────────
#
# This is the pivot the whole module exists for. The user maps their physical
# pad onto these names once; every exporter below reads only from here.
#
# The nominal layout is positional, described in Xbox terms because that is the
# layout SDL, RetroArch's own docs and most emulators use as their reference:
#
#            Y                A = bottom face button
#          X   B              B = right face button
#            A                X = left face button
#                             Y = top face button
#
# A Nintendo pad's physically-labelled A sits where B is here, and that is the
# point: GameLab stores positions, and each exporter renames them to whatever
# that emulator calls the button in that position.

class CanonicalButton(str, Enum):
    """GameLab's abstract digital inputs."""

    A = "a"                  # bottom face
    B = "b"                  # right face
    X = "x"                  # left face
    Y = "y"                  # top face
    DPAD_UP = "dpad_up"
    DPAD_DOWN = "dpad_down"
    DPAD_LEFT = "dpad_left"
    DPAD_RIGHT = "dpad_right"
    L1 = "l1"                # left shoulder
    R1 = "r1"                # right shoulder
    L3 = "l3"                # left stick click
    R3 = "r3"                # right stick click
    START = "start"
    SELECT = "select"        # Back / Share / Create / Minus
    GUIDE = "guide"          # Xbox / PS / Home — also GameLab's menu hotkey


class CanonicalAxis(str, Enum):
    """GameLab's abstract analog inputs.

    Triggers live here, not among the buttons, because every target format
    treats them as axes and a digital-only trigger is expressed as an axis
    binding with no travel. Putting them in the button enum would mean every
    exporter needed a special case.
    """

    LEFT_X = "left_x"
    LEFT_Y = "left_y"
    RIGHT_X = "right_x"
    RIGHT_Y = "right_y"
    L2 = "l2"                # left trigger
    R2 = "r2"                # right trigger


class InputKind(str, Enum):
    BUTTON = "button"
    AXIS = "axis"
    HAT = "hat"


# SDL hat bit values (SDL_HAT_UP etc). Needed for the `hN.M` mapping token.
_HAT_BITS = {"up": 1, "right": 2, "down": 4, "left": 8}


@dataclass(frozen=True)
class PhysicalInput:
    """One concrete thing on the user's controller.

    `direction` applies to axes: +1 or -1 selects one half of the travel (a
    trigger that rests at -1, or a stick pushed one way), and 0 means the whole
    axis (what a stick binding wants). `inverted` flips an axis whose hardware
    reports it backwards.
    """

    kind: InputKind
    index: int
    direction: int = 0
    hat_direction: str = ""
    inverted: bool = False

    @staticmethod
    def button(index: int) -> PhysicalInput:
        return PhysicalInput(InputKind.BUTTON, index)

    @staticmethod
    def axis(index: int, direction: int = 0, inverted: bool = False) -> PhysicalInput:
        return PhysicalInput(InputKind.AXIS, index, direction=direction, inverted=inverted)

    @staticmethod
    def hat(index: int, hat_direction: str) -> PhysicalInput:
        if hat_direction not in _HAT_BITS:
            raise ValueError(f"hat direction must be one of {sorted(_HAT_BITS)}")
        return PhysicalInput(InputKind.HAT, index, hat_direction=hat_direction)

    # ── serialisation ──
    def to_dict(self) -> dict:
        data: dict = {"kind": self.kind.value, "index": self.index}
        # Omit defaults so stored config stays readable by a human editing it.
        if self.direction:
            data["direction"] = self.direction
        if self.hat_direction:
            data["hat_direction"] = self.hat_direction
        if self.inverted:
            data["inverted"] = True
        return data

    @staticmethod
    def from_dict(data: dict) -> PhysicalInput:
        return PhysicalInput(
            kind=InputKind(data["kind"]),
            index=int(data["index"]),
            direction=int(data.get("direction", 0)),
            hat_direction=str(data.get("hat_direction", "")),
            inverted=bool(data.get("inverted", False)),
        )

    # ── rendering ──
    def sdl_token(self) -> str:
        """This input in SDL mapping-string syntax: b0, a2, +a1, a3~, h0.4."""
        if self.kind is InputKind.BUTTON:
            return f"b{self.index}"
        if self.kind is InputKind.HAT:
            return f"h{self.index}.{_HAT_BITS[self.hat_direction]}"
        sign = "+" if self.direction > 0 else "-" if self.direction < 0 else ""
        return f"{sign}a{self.index}{'~' if self.inverted else ''}"

    def retroarch_suffix(self) -> str:
        """Which RetroArch key suffix this input requires — `btn` or `axis`.

        RetroArch encodes the input kind in the key name, not the value:
        `input_l2_btn = "6"` and `input_l2_axis = "+2"` are the same binding on
        different hardware. Hats are still `_btn` keys, with an `h0up` value.
        """
        return "axis" if self.kind is InputKind.AXIS else "btn"

    def retroarch_value(self) -> str:
        """This input as a RetroArch autoconfig value (unquoted)."""
        if self.kind is InputKind.BUTTON:
            return str(self.index)
        if self.kind is InputKind.HAT:
            return f"h{self.index}{self.hat_direction}"
        # A full axis has no sign in RetroArch's grammar; a binding that needs
        # one direction and was given a full axis takes the positive half.
        sign = "-" if self.direction < 0 else "+"
        return f"{sign}{self.index}"


@dataclass
class ControllerMapping:
    """A complete description of one controller in GameLab's own terms.

    Everything an exporter needs lives here, including the USB ids — SDL's
    mapping string is keyed by a GUID derived from them, so a mapping without
    its ids cannot be exported.
    """

    name: str
    vendor_id: int = 0
    product_id: int = 0
    bustype: int = BUS_USB
    version: int = 0
    controller_type: ControllerType = ControllerType.GENERIC
    buttons: dict[CanonicalButton, PhysicalInput] = field(default_factory=dict)
    axes: dict[CanonicalAxis, PhysicalInput] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """A JSON-serialisable dict, for storage in the config file."""
        return {
            "name": self.name,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "bustype": self.bustype,
            "version": self.version,
            "controller_type": self.controller_type.value,
            "buttons": {k.value: v.to_dict() for k, v in self.buttons.items()},
            "axes": {k.value: v.to_dict() for k, v in self.axes.items()},
        }

    @staticmethod
    def from_dict(data: dict) -> ControllerMapping:
        """Rebuild a mapping from stored JSON.

        Unknown button or axis names are dropped rather than raising: a config
        written by a newer GameLab must still load in an older one, minus the
        bindings it does not understand.
        """
        buttons: dict[CanonicalButton, PhysicalInput] = {}
        for key, value in (data.get("buttons") or {}).items():
            try:
                buttons[CanonicalButton(key)] = PhysicalInput.from_dict(value)
            except ValueError:
                continue

        axes: dict[CanonicalAxis, PhysicalInput] = {}
        for key, value in (data.get("axes") or {}).items():
            try:
                axes[CanonicalAxis(key)] = PhysicalInput.from_dict(value)
            except ValueError:
                continue

        try:
            controller_type = ControllerType(data.get("controller_type", "generic"))
        except ValueError:
            controller_type = ControllerType.GENERIC

        return ControllerMapping(
            name=str(data.get("name", "")),
            vendor_id=int(data.get("vendor_id", 0)),
            product_id=int(data.get("product_id", 0)),
            bustype=int(data.get("bustype", BUS_USB)),
            version=int(data.get("version", 0)),
            controller_type=controller_type,
            buttons=buttons,
            axes=axes,
        )


# ── Starting layouts ──────────────────────────────────────────────
#
# These exist so the user's "configure once" starts from something that already
# works rather than from nothing. They are STARTING POINTS, not truth: the
# button indices a pad reports depend on the kernel driver bound to it, not
# just the hardware, so the user confirms them in the mapping UI.
#
# Two are taken from verified sources and the rest fall back to the xpad
# layout, which is what the majority of PC gamepads emulate.

def _xpad_layout() -> tuple[dict, dict]:
    """Linux `xpad` driver layout — Xbox 360/One pads and most clones.

    Verified against libretro's `udev/Microsoft X-Box 360 pad.cfg` and the
    Xbox 360 rows of SDL_GameControllerDB.
    """
    buttons = {
        CanonicalButton.A: PhysicalInput.button(0),
        CanonicalButton.B: PhysicalInput.button(1),
        CanonicalButton.X: PhysicalInput.button(2),
        CanonicalButton.Y: PhysicalInput.button(3),
        CanonicalButton.L1: PhysicalInput.button(4),
        CanonicalButton.R1: PhysicalInput.button(5),
        CanonicalButton.SELECT: PhysicalInput.button(6),
        CanonicalButton.START: PhysicalInput.button(7),
        CanonicalButton.GUIDE: PhysicalInput.button(8),
        CanonicalButton.L3: PhysicalInput.button(9),
        CanonicalButton.R3: PhysicalInput.button(10),
        CanonicalButton.DPAD_UP: PhysicalInput.hat(0, "up"),
        CanonicalButton.DPAD_DOWN: PhysicalInput.hat(0, "down"),
        CanonicalButton.DPAD_LEFT: PhysicalInput.hat(0, "left"),
        CanonicalButton.DPAD_RIGHT: PhysicalInput.hat(0, "right"),
    }
    axes = {
        CanonicalAxis.LEFT_X: PhysicalInput.axis(0),
        CanonicalAxis.LEFT_Y: PhysicalInput.axis(1),
        # Triggers are whole axes, not half ones: xpad reports them resting at
        # the minimum and travelling to the maximum, so binding only the
        # positive half would ignore the first half of the pull.
        CanonicalAxis.L2: PhysicalInput.axis(2),
        CanonicalAxis.RIGHT_X: PhysicalInput.axis(3),
        CanonicalAxis.RIGHT_Y: PhysicalInput.axis(4),
        CanonicalAxis.R2: PhysicalInput.axis(5),
    }
    return buttons, axes


def _hid_playstation_layout() -> tuple[dict, dict]:
    """Linux `hid-playstation` driver layout — DualSense, and DS4 on recent kernels.

    Verified against the `PS5 Controller` Linux row of SDL_GameControllerDB:
    a:b1,b:b2,x:b0,y:b3, triggers on a3/a4, right stick on a2/a5.
    """
    buttons = {
        CanonicalButton.X: PhysicalInput.button(0),
        CanonicalButton.A: PhysicalInput.button(1),
        CanonicalButton.B: PhysicalInput.button(2),
        CanonicalButton.Y: PhysicalInput.button(3),
        CanonicalButton.L1: PhysicalInput.button(4),
        CanonicalButton.R1: PhysicalInput.button(5),
        CanonicalButton.SELECT: PhysicalInput.button(8),
        CanonicalButton.START: PhysicalInput.button(9),
        CanonicalButton.L3: PhysicalInput.button(10),
        CanonicalButton.R3: PhysicalInput.button(11),
        CanonicalButton.GUIDE: PhysicalInput.button(12),
        CanonicalButton.DPAD_UP: PhysicalInput.hat(0, "up"),
        CanonicalButton.DPAD_DOWN: PhysicalInput.hat(0, "down"),
        CanonicalButton.DPAD_LEFT: PhysicalInput.hat(0, "left"),
        CanonicalButton.DPAD_RIGHT: PhysicalInput.hat(0, "right"),
    }
    axes = {
        CanonicalAxis.LEFT_X: PhysicalInput.axis(0),
        CanonicalAxis.LEFT_Y: PhysicalInput.axis(1),
        CanonicalAxis.RIGHT_X: PhysicalInput.axis(2),
        CanonicalAxis.L2: PhysicalInput.axis(3),
        CanonicalAxis.R2: PhysicalInput.axis(4),
        CanonicalAxis.RIGHT_Y: PhysicalInput.axis(5),
    }
    return buttons, axes


# Only families we have a verified layout for get a bespoke entry. DualShock 4,
# Switch Pro and 8BitDo pads report different indices depending on which kernel
# driver claims them (hid-playstation vs hid-sony, hid-nintendo vs generic HID,
# and 8BitDo's several firmware modes), so guessing one would produce a mapping
# that is wrong on half the machines. They take the xpad layout as a starting
# point and the user corrects it — which is exactly the workflow this module
# is built around.
_LAYOUTS = {
    ControllerType.XBOX_360: _xpad_layout,
    ControllerType.XBOX_ONE: _xpad_layout,
    ControllerType.DUALSENSE: _hid_playstation_layout,
}


def default_mapping(device: InputDevice) -> ControllerMapping:
    """A starting mapping for a detected device.

    Always returns a complete mapping — never None — because a plausible
    starting point the user can correct beats an empty screen. Which layout it
    used is visible in `controller_type`.
    """
    controller_type = device.controller_type
    buttons, axes = _LAYOUTS.get(controller_type, _xpad_layout)()

    return ControllerMapping(
        name=device.name,
        vendor_id=device.vendor_id,
        product_id=device.product_id,
        bustype=device.bustype,
        version=device.version,
        controller_type=controller_type,
        buttons=buttons,
        axes=axes,
    )


# ── Export: SDL (the one that covers the most emulators) ──────────

# Canonical name -> SDL gamecontroller output name. Verified against SDL2's
# map_StringForControllerButton / map_StringForControllerAxis tables.
_SDL_BUTTONS = {
    CanonicalButton.A: "a",
    CanonicalButton.B: "b",
    CanonicalButton.X: "x",
    CanonicalButton.Y: "y",
    CanonicalButton.SELECT: "back",
    CanonicalButton.GUIDE: "guide",
    CanonicalButton.START: "start",
    CanonicalButton.L3: "leftstick",
    CanonicalButton.R3: "rightstick",
    CanonicalButton.L1: "leftshoulder",
    CanonicalButton.R1: "rightshoulder",
    CanonicalButton.DPAD_UP: "dpup",
    CanonicalButton.DPAD_DOWN: "dpdown",
    CanonicalButton.DPAD_LEFT: "dpleft",
    CanonicalButton.DPAD_RIGHT: "dpright",
}

_SDL_AXES = {
    CanonicalAxis.LEFT_X: "leftx",
    CanonicalAxis.LEFT_Y: "lefty",
    CanonicalAxis.RIGHT_X: "rightx",
    CanonicalAxis.RIGHT_Y: "righty",
    CanonicalAxis.L2: "lefttrigger",
    CanonicalAxis.R2: "righttrigger",
}


def sdl_guid(bustype: int, vendor_id: int, product_id: int, version: int) -> str:
    """The 32-hex-character GUID SDL derives from a Linux evdev device.

    Layout, from SDL2's `SDL_CreateJoystickGUID` (the vendor != 0 branch), as
    16 bytes printed in order: bustype LE16, name-CRC LE16, vendor LE16, 0000,
    product LE16, 0000, version LE16, driver signature and data.

    Bytes 2-3 are emitted as zero on purpose. SDL only fills that field in when
    it computes a GUID itself, and it zeroes it before comparing against
    database entries — so every real row in SDL_GameControllerDB carries 0000
    there, and a CRC we computed would simply have to be masked off again.
    """
    parts = [bustype, 0, vendor_id, 0, product_id, 0, version, 0]
    # Each 16-bit field little-endian: low byte first.
    return "".join(f"{value & 0xFF:02x}{(value >> 8) & 0xFF:02x}" for value in parts)


def to_sdl_mapping(mapping: ControllerMapping, *, platform: str = "Linux") -> str:
    """Render a canonical mapping as an SDL_GAMECONTROLLERCONFIG string.

    This is the single highest-leverage output in the module. SDL parses this
    variable before consulting its bundled database, so exporting it into an
    emulator's environment retargets that emulator's entire notion of the
    gamepad layout without touching one of its config files. PCSX2, DuckStation,
    Dolphin, PPSSPP, Flycast, melonDS, RPCS3, Ryujinx and RetroArch's sdl2
    joypad driver all read gamepad state through this layer.

    What it does NOT cover is which emulated button each SDL button drives —
    that is per-emulator and needs the file exporters below.

    Fields are emitted alphabetically by output name, matching the convention
    in SDL_GameControllerDB, and the string ends with the trailing comma that
    every real entry carries.
    """
    guid = sdl_guid(
        mapping.bustype, mapping.vendor_id, mapping.product_id, mapping.version
    )
    # Commas terminate fields, so a comma in a device name would corrupt the
    # string. Real device names do not contain one, but a user-renamed profile
    # might.
    name = mapping.name.replace(",", " ").strip() or "Gamepad"

    pairs = [
        (_SDL_BUTTONS[button], physical.sdl_token())
        for button, physical in mapping.buttons.items()
        if button in _SDL_BUTTONS
    ]
    pairs += [
        (_SDL_AXES[axis], physical.sdl_token())
        for axis, physical in mapping.axes.items()
        if axis in _SDL_AXES
    ]

    fields = ",".join(f"{out}:{token}" for out, token in sorted(pairs))
    return f"{guid},{name},{fields},platform:{platform},"


def sdl_environment(mapping: ControllerMapping) -> dict[str, str]:
    """Environment overrides to hand to any SDL-based emulator process.

    `SDL_GAMECONTROLLERCONFIG` holds the mapping. `SDL_JOYSTICK_HIDAPI=0` is
    set alongside it because SDL's HIDAPI drivers bypass the evdev device this
    mapping was measured against and re-report the pad under a different GUID,
    which would silently ignore everything above.
    """
    return {
        "SDL_GAMECONTROLLERCONFIG": to_sdl_mapping(mapping),
        "SDL_JOYSTICK_HIDAPI": "0",
    }


# ── Export: RetroArch autoconfig ──────────────────────────────────
#
# RetroArch's virtual pad ("RetroPad") is SNES-shaped, so its A/B/X/Y sit in
# different places from ours: RetroPad B is the bottom face button and A is the
# right one. Getting this backwards is the single most common way an autoconfig
# ends up with a swapped A/B, so the translation is spelled out here rather
# than left implicit.
_RETROARCH_BUTTONS = {
    CanonicalButton.A: "b",          # bottom face -> RetroPad B
    CanonicalButton.B: "a",          # right face  -> RetroPad A
    CanonicalButton.X: "y",          # left face   -> RetroPad Y
    CanonicalButton.Y: "x",          # top face    -> RetroPad X
    CanonicalButton.DPAD_UP: "up",
    CanonicalButton.DPAD_DOWN: "down",
    CanonicalButton.DPAD_LEFT: "left",
    CanonicalButton.DPAD_RIGHT: "right",
    CanonicalButton.L1: "l",
    CanonicalButton.R1: "r",
    CanonicalButton.L3: "l3",
    CanonicalButton.R3: "r3",
    CanonicalButton.START: "start",
    CanonicalButton.SELECT: "select",
    CanonicalButton.GUIDE: "menu_toggle",
}

# Sticks are the one place RetroArch does not take a whole axis: each direction
# is its own key, so one canonical axis becomes two lines.
_RETROARCH_STICK_AXES = {
    CanonicalAxis.LEFT_X: ("l_x_plus", "l_x_minus"),
    CanonicalAxis.LEFT_Y: ("l_y_plus", "l_y_minus"),
    CanonicalAxis.RIGHT_X: ("r_x_plus", "r_x_minus"),
    CanonicalAxis.RIGHT_Y: ("r_y_plus", "r_y_minus"),
}

# Order the emitted keys the way libretro's own autoconfig files do, so a
# GameLab-written file diffs cleanly against an upstream one.
_RETROARCH_KEY_ORDER = [
    "b", "y", "select", "start", "up", "down", "left", "right",
    "a", "x", "l", "r", "l2", "r2", "l3", "r3",
    "l_x_plus", "l_x_minus", "l_y_plus", "l_y_minus",
    "r_x_plus", "r_x_minus", "r_y_plus", "r_y_minus",
    "menu_toggle",
]


def to_retroarch_autoconfig(mapping: ControllerMapping, *, driver: str = "udev") -> str:
    """Render a RetroArch autoconfig .cfg for this controller.

    `driver` is the RetroArch input driver the file is for; the file must land
    in that driver's subdirectory of RetroArch's `autoconfig` dir (see
    `retroarch_autoconfig_filename`). `udev` is the desktop-Linux default.

    RetroArch matches an autoconfig to a pad by `input_device` name and, when
    present, the vendor/product ids — which it wants in DECIMAL, not hex. All
    values are quoted, including the numeric ones.
    """
    lines = [
        f'input_driver = "{driver}"',
        f'input_device = "{mapping.name}"',
    ]
    if mapping.vendor_id:
        lines.append(f'input_vendor_id = "{mapping.vendor_id}"')
    if mapping.product_id:
        lines.append(f'input_product_id = "{mapping.product_id}"')
    lines.append("")

    # Collect key -> (suffix, value) first so ordering is applied once.
    entries: dict[str, tuple[str, str]] = {}

    for button, physical in mapping.buttons.items():
        key = _RETROARCH_BUTTONS.get(button)
        if key:
            entries[key] = (physical.retroarch_suffix(), physical.retroarch_value())

    for axis, physical in mapping.axes.items():
        if axis in (CanonicalAxis.L2, CanonicalAxis.R2):
            key = "l2" if axis is CanonicalAxis.L2 else "r2"
            entries[key] = (physical.retroarch_suffix(), physical.retroarch_value())
            continue

        plus_key, minus_key = _RETROARCH_STICK_AXES[axis]
        # An inverted axis means the hardware reports the directions the other
        # way round, so the two halves swap.
        plus, minus = ("-", "+") if physical.inverted else ("+", "-")
        entries[plus_key] = ("axis", f"{plus}{physical.index}")
        entries[minus_key] = ("axis", f"{minus}{physical.index}")

    for key in _RETROARCH_KEY_ORDER:
        entry = entries.get(key)
        if entry:
            suffix, value = entry
            lines.append(f'input_{key}_{suffix} = "{value}"')

    return "\n".join(lines) + "\n"


def retroarch_autoconfig_filename(mapping: ControllerMapping) -> str:
    """The filename RetroArch expects, relative to `autoconfig/<driver>/`.

    RetroArch looks the file up by the device name it was given, so the stem
    must match `input_device` exactly; only path separators are replaced.
    """
    safe = re.sub(r"[/\\]", "_", mapping.name).strip() or "Gamepad"
    return f"{safe}.cfg"


# ── Export: DuckStation and PCSX2 ─────────────────────────────────
#
# Both emulators descend from the same input code, so they share a grammar:
#
#   <PadButton> = SDL-<controller index>/<element>
#
# with `+`/`-`/`Full` prefixes on axis elements and a trailing `~` for
# inversion. Crucially the element names are SDL GAMEPAD names, not raw button
# indices — meaning these files do not depend on the physical inputs at all.
# What makes them correct for a given pad is SDL knowing its layout, which is
# precisely what `to_sdl_mapping()` above provides. The two exports are
# partners: the ini says "Cross is the south face button", the SDL string says
# "the south face button is b0 on this hardware".
#
# The face-button names are where the two diverge and are NOT interchangeable:
# DuckStation writes Xbox-style A/B/X/Y, PCSX2 writes FaceSouth/East/West/North.
# Both verified against real config files and against the ConvertKeyToString
# settings path in each project's SDLInputSource.

_DUCKSTATION_ELEMENTS = {
    CanonicalButton.A: "A",
    CanonicalButton.B: "B",
    CanonicalButton.X: "X",
    CanonicalButton.Y: "Y",
    CanonicalButton.SELECT: "Back",
    CanonicalButton.START: "Start",
    CanonicalButton.GUIDE: "Guide",
    CanonicalButton.L1: "LeftShoulder",
    CanonicalButton.R1: "RightShoulder",
    CanonicalButton.L3: "LeftStick",
    CanonicalButton.R3: "RightStick",
    CanonicalButton.DPAD_UP: "DPadUp",
    CanonicalButton.DPAD_DOWN: "DPadDown",
    CanonicalButton.DPAD_LEFT: "DPadLeft",
    CanonicalButton.DPAD_RIGHT: "DPadRight",
}

_PCSX2_ELEMENTS = dict(_DUCKSTATION_ELEMENTS) | {
    CanonicalButton.A: "FaceSouth",
    CanonicalButton.B: "FaceEast",
    CanonicalButton.X: "FaceWest",
    CanonicalButton.Y: "FaceNorth",
}

# Which canonical input drives each PlayStation pad button. Identical for both
# emulators; only the element vocabulary differs.
_PS_DIGITAL_BINDINGS: list[tuple[str, CanonicalButton]] = [
    ("Up", CanonicalButton.DPAD_UP),
    ("Right", CanonicalButton.DPAD_RIGHT),
    ("Down", CanonicalButton.DPAD_DOWN),
    ("Left", CanonicalButton.DPAD_LEFT),
    ("Triangle", CanonicalButton.Y),
    ("Circle", CanonicalButton.B),
    ("Cross", CanonicalButton.A),
    ("Square", CanonicalButton.X),
    ("Select", CanonicalButton.SELECT),
    ("Start", CanonicalButton.START),
    ("L1", CanonicalButton.L1),
    ("R1", CanonicalButton.R1),
    ("L3", CanonicalButton.L3),
    ("R3", CanonicalButton.R3),
    ("Analog", CanonicalButton.GUIDE),
]

# Stick directions are half-axis elements and are the same strings in both.
_PS_STICK_BINDINGS: list[tuple[str, str]] = [
    ("LUp", "-LeftY"),
    ("LRight", "+LeftX"),
    ("LDown", "+LeftY"),
    ("LLeft", "-LeftX"),
    ("RUp", "-RightY"),
    ("RRight", "+RightX"),
    ("RDown", "+RightY"),
    ("RLeft", "-RightX"),
]


def _stenzek_pad_section(
    mapping: ControllerMapping,
    elements: dict[CanonicalButton, str],
    *,
    port: int,
    sdl_index: int,
    pad_type: str,
) -> str:
    lines = [f"[Pad{port}]", f"Type = {pad_type}"]

    for key, button in _PS_DIGITAL_BINDINGS:
        if button in mapping.buttons:
            lines.append(f"{key} = SDL-{sdl_index}/{elements[button]}")

    if CanonicalAxis.L2 in mapping.axes:
        lines.append(f"L2 = SDL-{sdl_index}/+LeftTrigger")
    if CanonicalAxis.R2 in mapping.axes:
        lines.append(f"R2 = SDL-{sdl_index}/+RightTrigger")

    have_axis = {
        "LeftX": CanonicalAxis.LEFT_X in mapping.axes,
        "LeftY": CanonicalAxis.LEFT_Y in mapping.axes,
        "RightX": CanonicalAxis.RIGHT_X in mapping.axes,
        "RightY": CanonicalAxis.RIGHT_Y in mapping.axes,
    }
    for key, element in _PS_STICK_BINDINGS:
        if have_axis[element.lstrip("+-")]:
            lines.append(f"{key} = SDL-{sdl_index}/{element}")

    # Rumble is not part of the canonical model — it has no buttons to map —
    # but the pad is useless without it and the element names are fixed.
    lines.append(f"LargeMotor = SDL-{sdl_index}/LargeMotor")
    lines.append(f"SmallMotor = SDL-{sdl_index}/SmallMotor")

    return "\n".join(lines) + "\n"


def to_duckstation_pad_section(
    mapping: ControllerMapping, *, port: int = 1, sdl_index: int = 0
) -> str:
    """A `[PadN]` section for DuckStation's settings.ini.

    `sdl_index` is DuckStation's SDL controller index, which is the order pads
    were connected in — 0 for the only pad on the machine.

    DuckStation reads bindings from `~/.config/duckstation/settings.ini`, or
    from a named profile in `~/.config/duckstation/inputprofiles/`. Writing a
    profile is the safer target: it cannot disturb the user's other settings.
    """
    return _stenzek_pad_section(
        mapping, _DUCKSTATION_ELEMENTS,
        port=port, sdl_index=sdl_index, pad_type="AnalogController",
    )


def to_pcsx2_pad_section(
    mapping: ControllerMapping, *, port: int = 1, sdl_index: int = 0
) -> str:
    """A `[PadN]` section for PCSX2's PCSX2.ini (v2.x, Qt).

    Same grammar as DuckStation but with PCSX2's positional face-button names.
    Lives in `inis/PCSX2.ini` under the PCSX2 data directory, or in a profile
    under `inputprofiles/`.
    """
    return _stenzek_pad_section(
        mapping, _PCSX2_ELEMENTS,
        port=port, sdl_index=sdl_index, pad_type="DualShock2",
    )


# ── Deliberately not implemented ──────────────────────────────────
#
# Dolphin, PPSSPP, mGBA, Snes9x, Mesen, Flycast, melonDS and the rest have no
# exporter here, and none is stubbed out returning something plausible. Their
# formats were not verified against real config files or upstream source during
# this work, and a config file that is confidently wrong costs the user more
# time than no file at all — they would have to discover it is wrong before
# they could fix it.
#
# They are not left unserved, either: all of them read gamepad state through
# SDL, so `sdl_environment()` already gives them a correct button layout. What
# is missing for them is only the "which emulated button" half, which their own
# configuration screens handle in about thirty seconds each. When someone
# verifies a format against a real file, add it beside the two above.


def export_all(mapping: ControllerMapping) -> dict[str, str]:
    """Every export we can produce for this mapping, keyed by target.

    `sdl` is the environment-variable string that covers most emulators at
    once; the rest are file contents the caller writes to the right place.
    Targets whose formats are unverified are absent by design (see above), so
    the caller can tell "we have nothing for this emulator" from "we produced
    an empty config".
    """
    return {
        "sdl": to_sdl_mapping(mapping),
        "retroarch": to_retroarch_autoconfig(mapping),
        "duckstation": to_duckstation_pad_section(mapping),
        "pcsx2": to_pcsx2_pad_section(mapping),
    }


def describe_detection(devices: Iterable[InputDevice]) -> str:
    """A one-line-per-pad human summary, for logs and the settings screen."""
    lines = []
    for device in devices:
        lines.append(
            f"{device.name} "
            f"[{device.vendor_id:04x}:{device.product_id:04x}] "
            f"{device.controller_type.label} "
            f"-> {device.event_path or 'no event node'}"
        )
    return "\n".join(lines) if lines else "No gamepads detected."
