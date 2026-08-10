"""Tests for controller detection and the unified mapping system.

Every test here runs against a synthetic /proc/bus/input/devices fixture or a
mapping built in-process. Nothing requires a controller to be plugged in, and
nothing reads the real /proc — otherwise the suite would pass or fail depending
on what happened to be connected to the machine running it.
"""

from __future__ import annotations

import json

import pytest

from rose_gamelab.core.controller import (
    BUS_BLUETOOTH,
    BUS_USB,
    CanonicalAxis,
    CanonicalButton,
    ControllerDetectionError,
    ControllerMapping,
    ControllerType,
    InputKind,
    PhysicalInput,
    default_mapping,
    detect_controllers,
    export_all,
    identify_controller,
    joystick_symlinks,
    parse_proc_input_devices,
    read_input_devices,
    retroarch_autoconfig_filename,
    sdl_environment,
    sdl_guid,
    to_duckstation_pad_section,
    to_pcsx2_pad_section,
    to_retroarch_autoconfig,
    to_sdl_mapping,
)

# A realistic /proc/bus/input/devices: a keyboard with no joystick node, a
# wired Xbox 360 pad on xpad, and a DualSense on hid-playstation. The stanza
# shapes and capability bitmasks are copied from real kernel output.
PROC_FIXTURE = """\
I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
P: Phys=LNXPWRBN/button/input0
S: Sysfs=/devices/LNXSYSTM:00/LNXPWRBN:00/input/input1
U: Uniq=
H: Handlers=kbd event1\x20
B: PROP=0
B: EV=3

I: Bus=0003 Vendor=045e Product=028e Version=0114
N: Name="Microsoft X-Box 360 pad"
P: Phys=usb-0000:00:14.0-2/input0
S: Sysfs=/devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0/input/input20
U: Uniq=
H: Handlers=event20 js0\x20
B: PROP=0
B: EV=20000b
B: KEY=7cdb000000000000 0 0 0 0
B: ABS=3003f

I: Bus=0003 Vendor=054c Product=0ce6 Version=8111
N: Name="Sony Interactive Entertainment DualSense Wireless Controller"
P: Phys=usb-0000:00:14.0-3/input3
S: Sysfs=/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3:1.3/input/input25
U: Uniq=a0:ab:51:11:22:33
H: Handlers=event25 js1\x20
B: PROP=0
B: EV=20000b
B: KEY=7fdb000000000000 0 0 0 0
B: ABS=30627

I: Bus=0003 Vendor=046d Product=c52b Version=0111
N: Name="Logitech USB Receiver"
P: Phys=usb-0000:00:14.0-4/input2
S: Sysfs=/devices/pci0000:00/0000:00:14.0/usb1/1-4/1-4:1.2/input/input30
U: Uniq=
H: Handlers=kbd event30 mouse2\x20
B: PROP=0
B: EV=12001f
"""


# ── Parsing /proc/bus/input/devices ───────────────────────────────

def test_parses_every_stanza():
    devices = parse_proc_input_devices(PROC_FIXTURE)
    assert len(devices) == 4


def test_extracts_name_vendor_product_and_event_path():
    xbox = next(d for d in parse_proc_input_devices(PROC_FIXTURE) if d.vendor_id == 0x045E)

    assert xbox.name == "Microsoft X-Box 360 pad"
    assert xbox.vendor_id == 0x045E
    assert xbox.product_id == 0x028E
    assert xbox.bustype == BUS_USB
    assert xbox.version == 0x0114
    assert str(xbox.event_path) == "/dev/input/event20"
    assert str(xbox.joystick_path) == "/dev/input/js0"


def test_ids_are_parsed_as_hex_not_decimal():
    """`Vendor=045e` is hex; reading it as decimal would identify nothing."""
    devices = parse_proc_input_devices(PROC_FIXTURE)
    assert any(d.vendor_id == 1118 for d in devices)  # 0x045e


def test_gamepads_are_those_with_a_joystick_node():
    gamepads = [d for d in parse_proc_input_devices(PROC_FIXTURE) if d.is_gamepad]
    names = {d.name for d in gamepads}

    assert names == {
        "Microsoft X-Box 360 pad",
        "Sony Interactive Entertainment DualSense Wireless Controller",
    }


def test_keyboards_and_mice_are_not_gamepads():
    devices = parse_proc_input_devices(PROC_FIXTURE)
    receiver = next(d for d in devices if d.name == "Logitech USB Receiver")

    assert not receiver.is_gamepad
    assert receiver.joystick_path is None


def test_stanza_without_an_id_line_is_skipped_not_fatal():
    text = 'N: Name="Broken"\nH: Handlers=js9\n\n' + PROC_FIXTURE
    assert len(parse_proc_input_devices(text)) == 4


def test_device_with_no_event_node_reports_none():
    text = 'I: Bus=0003 Vendor=1234 Product=5678 Version=0001\nN: Name="Odd"\nH: Handlers=js4\n'
    device = parse_proc_input_devices(text)[0]

    assert device.event_path is None
    assert str(device.joystick_path) == "/dev/input/js4"


def test_empty_input_yields_no_devices():
    assert parse_proc_input_devices("") == []


# ── Detection failures are reported, never swallowed ──────────────

def test_missing_proc_file_raises_with_a_reason(tmp_path):
    with pytest.raises(ControllerDetectionError) as error:
        read_input_devices(tmp_path / "not-here")

    assert "does not exist" in str(error.value)


def test_detection_reads_from_the_given_path(tmp_path):
    path = tmp_path / "devices"
    path.write_text(PROC_FIXTURE)

    assert len(detect_controllers(path)) == 2


def test_no_gamepads_is_an_empty_list_not_an_error(tmp_path):
    path = tmp_path / "devices"
    path.write_text(PROC_FIXTURE.split("\n\n")[0] + "\n")

    assert detect_controllers(path) == []


def test_missing_by_id_directory_is_not_an_error(tmp_path):
    assert joystick_symlinks(tmp_path / "absent") == []


def test_joystick_symlinks_selects_only_joystick_event_nodes(tmp_path):
    for name in (
        "usb-Microsoft_Controller-event-joystick",
        "usb-Microsoft_Controller-joystick",
        "usb-Some_Keyboard-event-kbd",
    ):
        (tmp_path / name).write_text("")

    found = [p.name for p in joystick_symlinks(tmp_path)]
    assert found == ["usb-Microsoft_Controller-event-joystick"]


# ── Controller identification ─────────────────────────────────────

@pytest.mark.parametrize(
    "vendor,product,expected",
    [
        (0x045E, 0x028E, ControllerType.XBOX_360),   # 360 wired
        (0x045E, 0x028F, ControllerType.XBOX_360),   # 360 wireless
        (0x045E, 0x02D1, ControllerType.XBOX_ONE),
        (0x045E, 0x0B12, ControllerType.XBOX_ONE),   # Series X|S
        (0x054C, 0x0268, ControllerType.DUALSHOCK_3),
        (0x054C, 0x05C4, ControllerType.DUALSHOCK_4),
        (0x054C, 0x09CC, ControllerType.DUALSHOCK_4),
        (0x054C, 0x0CE6, ControllerType.DUALSENSE),
        (0x057E, 0x2009, ControllerType.SWITCH_PRO),
        (0x057E, 0x2006, ControllerType.JOYCON),
        (0x2DC8, 0x6001, ControllerType.EIGHTBITDO),
        (0x28DE, 0x1102, ControllerType.STEAM),
    ],
)
def test_identifies_controllers_by_usb_ids(vendor, product, expected):
    assert identify_controller(vendor, product) == expected


def test_unknown_ids_fall_back_to_the_device_name():
    """Bluetooth pads and clones routinely report ids we do not know."""
    assert identify_controller(0x0000, 0x0000, "DualSense Wireless Controller") == (
        ControllerType.DUALSENSE
    )
    assert identify_controller(0x0000, 0x0000, "8BitDo SN30 Pro") == (
        ControllerType.EIGHTBITDO
    )
    assert identify_controller(0x0000, 0x0000, "Pro Controller") == (
        ControllerType.SWITCH_PRO
    )


def test_unrecognised_pad_is_generic_not_an_error():
    assert identify_controller(0x1234, 0x5678, "Some Gamepad") == ControllerType.GENERIC


def test_detected_devices_carry_their_type():
    devices = parse_proc_input_devices(PROC_FIXTURE)
    types = {d.name: d.controller_type for d in devices if d.is_gamepad}

    assert types["Microsoft X-Box 360 pad"] == ControllerType.XBOX_360
    assert types[
        "Sony Interactive Entertainment DualSense Wireless Controller"
    ] == ControllerType.DUALSENSE


# ── The canonical model round-trips through JSON ──────────────────

def xbox_device():
    return next(d for d in parse_proc_input_devices(PROC_FIXTURE) if d.vendor_id == 0x045E)


def test_mapping_survives_a_json_round_trip():
    original = default_mapping(xbox_device())

    restored = ControllerMapping.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored == original


def test_round_trip_preserves_every_input_kind():
    mapping = ControllerMapping(
        name="Test Pad",
        vendor_id=0x045E,
        product_id=0x028E,
        buttons={
            CanonicalButton.A: PhysicalInput.button(0),
            CanonicalButton.DPAD_UP: PhysicalInput.hat(0, "up"),
        },
        axes={
            CanonicalAxis.LEFT_X: PhysicalInput.axis(0),
            CanonicalAxis.LEFT_Y: PhysicalInput.axis(1, inverted=True),
            CanonicalAxis.L2: PhysicalInput.axis(2, direction=+1),
        },
    )

    restored = ControllerMapping.from_dict(json.loads(json.dumps(mapping.to_dict())))

    assert restored.buttons[CanonicalButton.DPAD_UP].kind is InputKind.HAT
    assert restored.buttons[CanonicalButton.DPAD_UP].hat_direction == "up"
    assert restored.axes[CanonicalAxis.LEFT_Y].inverted is True
    assert restored.axes[CanonicalAxis.L2].direction == 1
    assert restored == mapping


def test_serialised_form_is_plain_json_types():
    """It has to live inside the config file, so no enums may leak through."""
    data = default_mapping(xbox_device()).to_dict()
    reloaded = json.loads(json.dumps(data))

    assert reloaded == data
    assert all(isinstance(key, str) for key in reloaded["buttons"])


def test_unknown_binding_names_are_dropped_not_fatal():
    """A config written by a newer GameLab must still load."""
    data = default_mapping(xbox_device()).to_dict()
    data["buttons"]["paddle7"] = {"kind": "button", "index": 42}
    data["controller_type"] = "some_future_pad"

    restored = ControllerMapping.from_dict(data)

    assert restored.controller_type == ControllerType.GENERIC
    assert CanonicalButton.A in restored.buttons


def test_hat_direction_must_be_valid():
    with pytest.raises(ValueError):
        PhysicalInput.hat(0, "diagonal")


# ── SDL mapping strings ───────────────────────────────────────────

def test_sdl_guid_matches_the_known_xbox_360_guid():
    """The exact GUID SDL_GameControllerDB carries for a wired 360 pad."""
    assert sdl_guid(BUS_USB, 0x045E, 0x028E, 0x0110) == (
        "030000005e0400008e02000010010000"
    )


def test_sdl_guid_matches_the_known_8bitdo_pro_3_guid():
    """Second published entry, to pin the byte order rather than one lucky case."""
    assert sdl_guid(BUS_USB, 0x2DC8, 0x6009, 0x0111) == (
        "03000000c82d00000960000011010000"
    )


def test_sdl_guid_encodes_the_bluetooth_bus_distinctly():
    """The same pad over Bluetooth is a different GUID — bus type leads the id."""
    wired = sdl_guid(BUS_USB, 0x2DC8, 0x6009, 0x0111)
    wireless = sdl_guid(BUS_BLUETOOTH, 0x2DC8, 0x6009, 0x0111)

    assert wireless.startswith("0500")
    assert wireless[4:] == wired[4:]


def test_sdl_guid_is_thirty_two_hex_characters():
    guid = sdl_guid(BUS_USB, 0x054C, 0x0CE6, 0x8111)
    assert len(guid) == 32
    assert int(guid, 16) >= 0


def test_sdl_mapping_for_an_xbox_360_pad_is_exactly_the_known_good_string():
    device = xbox_device()
    mapping = default_mapping(device)
    # Pin the version so the GUID matches the published database entry.
    mapping.version = 0x0110

    assert to_sdl_mapping(mapping) == (
        "030000005e0400008e02000010010000,Microsoft X-Box 360 pad,"
        "a:b0,b:b1,back:b6,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,dpup:h0.1,"
        "guide:b8,leftshoulder:b4,leftstick:b9,lefttrigger:a2,leftx:a0,lefty:a1,"
        "rightshoulder:b5,rightstick:b10,righttrigger:a5,rightx:a3,righty:a4,"
        "start:b7,x:b2,y:b3,platform:Linux,"
    )


def test_sdl_mapping_for_a_dualsense_matches_the_published_layout():
    device = next(
        d for d in parse_proc_input_devices(PROC_FIXTURE) if d.vendor_id == 0x054C
    )
    mapping = default_mapping(device)
    mapping.name = "PS5 Controller"
    mapping.version = 0x0111

    # Character-for-character the `PS5 Controller` Linux row of
    # SDL_GameControllerDB, GUID included.
    assert to_sdl_mapping(mapping) == (
        "030000004c050000e60c000011010000,PS5 Controller,"
        "a:b1,b:b2,back:b8,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,dpup:h0.1,"
        "guide:b12,leftshoulder:b4,leftstick:b10,lefttrigger:a3,leftx:a0,lefty:a1,"
        "rightshoulder:b5,rightstick:b11,righttrigger:a4,rightx:a2,righty:a5,"
        "start:b9,x:b0,y:b3,platform:Linux,"
    )


def test_sdl_mapping_encodes_half_axes_and_inversion():
    mapping = ControllerMapping(
        name="Odd Pad",
        vendor_id=0x1234,
        product_id=0x5678,
        buttons={CanonicalButton.DPAD_UP: PhysicalInput.axis(1, direction=-1)},
        axes={CanonicalAxis.RIGHT_X: PhysicalInput.axis(3, inverted=True)},
    )

    result = to_sdl_mapping(mapping)

    assert "dpup:-a1" in result
    assert "rightx:a3~" in result


def test_sdl_mapping_fields_are_alphabetical():
    """SDL_GameControllerDB convention; also what makes the output diffable."""
    fields = to_sdl_mapping(default_mapping(xbox_device())).split(",")[2:-2]
    names = [f.split(":")[0] for f in fields]

    assert names == sorted(names)


def test_commas_in_a_device_name_cannot_corrupt_the_string():
    mapping = default_mapping(xbox_device())
    mapping.name = "Pad, Wireless"

    result = to_sdl_mapping(mapping)

    # Field 1 is the name, and it must still be one field.
    assert result.split(",")[1] == "Pad  Wireless"
    # guid + name + 21 bindings + platform, each field comma-terminated.
    assert result.count(",") == 24


def test_sdl_environment_carries_the_mapping_and_disables_hidapi():
    env = sdl_environment(default_mapping(xbox_device()))

    assert env["SDL_GAMECONTROLLERCONFIG"].endswith("platform:Linux,")
    assert env["SDL_JOYSTICK_HIDAPI"] == "0"


# ── RetroArch autoconfig ──────────────────────────────────────────

def test_retroarch_autoconfig_matches_the_upstream_xbox_360_profile():
    """Byte-comparable with libretro's own `udev/Microsoft X-Box 360 pad.cfg`.

    Upstream also emits `_label` lines; those are cosmetic and omitted here.
    """
    mapping = default_mapping(xbox_device())

    assert to_retroarch_autoconfig(mapping) == (
        'input_driver = "udev"\n'
        'input_device = "Microsoft X-Box 360 pad"\n'
        'input_vendor_id = "1118"\n'
        'input_product_id = "654"\n'
        "\n"
        'input_b_btn = "0"\n'
        'input_y_btn = "2"\n'
        'input_select_btn = "6"\n'
        'input_start_btn = "7"\n'
        'input_up_btn = "h0up"\n'
        'input_down_btn = "h0down"\n'
        'input_left_btn = "h0left"\n'
        'input_right_btn = "h0right"\n'
        'input_a_btn = "1"\n'
        'input_x_btn = "3"\n'
        'input_l_btn = "4"\n'
        'input_r_btn = "5"\n'
        'input_l2_axis = "+2"\n'
        'input_r2_axis = "+5"\n'
        'input_l3_btn = "9"\n'
        'input_r3_btn = "10"\n'
        'input_l_x_plus_axis = "+0"\n'
        'input_l_x_minus_axis = "-0"\n'
        'input_l_y_plus_axis = "+1"\n'
        'input_l_y_minus_axis = "-1"\n'
        'input_r_x_plus_axis = "+3"\n'
        'input_r_x_minus_axis = "-3"\n'
        'input_r_y_plus_axis = "+4"\n'
        'input_r_y_minus_axis = "-4"\n'
        'input_menu_toggle_btn = "8"\n'
    )


def test_retroarch_face_buttons_are_translated_to_retropad_positions():
    """RetroPad is SNES-shaped: our A (bottom) is its B, our B (right) is its A."""
    mapping = ControllerMapping(
        name="Pad",
        buttons={
            CanonicalButton.A: PhysicalInput.button(11),
            CanonicalButton.B: PhysicalInput.button(12),
            CanonicalButton.X: PhysicalInput.button(13),
            CanonicalButton.Y: PhysicalInput.button(14),
        },
    )
    text = to_retroarch_autoconfig(mapping)

    assert 'input_b_btn = "11"' in text
    assert 'input_a_btn = "12"' in text
    assert 'input_y_btn = "13"' in text
    assert 'input_x_btn = "14"' in text


def test_retroarch_ids_are_written_in_decimal():
    """RetroArch reads these as decimal; hex here means the file never matches."""
    text = to_retroarch_autoconfig(default_mapping(xbox_device()))

    assert 'input_vendor_id = "1118"' in text
    assert "045e" not in text


def test_retroarch_omits_ids_it_does_not_have():
    text = to_retroarch_autoconfig(ControllerMapping(name="Homebrew Pad"))

    assert "input_vendor_id" not in text
    assert 'input_device = "Homebrew Pad"' in text


def test_retroarch_digital_dpad_uses_button_keys():
    mapping = ControllerMapping(
        name="Pad", buttons={CanonicalButton.DPAD_UP: PhysicalInput.button(11)}
    )
    assert 'input_up_btn = "11"' in to_retroarch_autoconfig(mapping)


def test_retroarch_digital_trigger_uses_a_btn_key_not_an_axis_key():
    """RetroArch encodes the input kind in the key name, not the value."""
    mapping = ControllerMapping(
        name="Pad", axes={CanonicalAxis.L2: PhysicalInput.button(6)}
    )
    text = to_retroarch_autoconfig(mapping)

    assert 'input_l2_btn = "6"' in text
    assert "input_l2_axis" not in text


def test_retroarch_inverted_stick_swaps_the_two_halves():
    mapping = ControllerMapping(
        name="Pad", axes={CanonicalAxis.LEFT_Y: PhysicalInput.axis(1, inverted=True)}
    )
    text = to_retroarch_autoconfig(mapping)

    assert 'input_l_y_plus_axis = "-1"' in text
    assert 'input_l_y_minus_axis = "+1"' in text


def test_retroarch_driver_is_selectable():
    text = to_retroarch_autoconfig(default_mapping(xbox_device()), driver="sdl2")
    assert text.startswith('input_driver = "sdl2"')


def test_retroarch_filename_matches_the_device_name():
    mapping = default_mapping(xbox_device())
    assert retroarch_autoconfig_filename(mapping) == "Microsoft X-Box 360 pad.cfg"


def test_retroarch_filename_strips_path_separators():
    mapping = ControllerMapping(name="Evil/Pad")
    assert retroarch_autoconfig_filename(mapping) == "Evil_Pad.cfg"


# ── DuckStation and PCSX2 ─────────────────────────────────────────

def test_duckstation_section_matches_the_real_settings_ini_format():
    mapping = default_mapping(xbox_device())

    assert to_duckstation_pad_section(mapping) == (
        "[Pad1]\n"
        "Type = AnalogController\n"
        "Up = SDL-0/DPadUp\n"
        "Right = SDL-0/DPadRight\n"
        "Down = SDL-0/DPadDown\n"
        "Left = SDL-0/DPadLeft\n"
        "Triangle = SDL-0/Y\n"
        "Circle = SDL-0/B\n"
        "Cross = SDL-0/A\n"
        "Square = SDL-0/X\n"
        "Select = SDL-0/Back\n"
        "Start = SDL-0/Start\n"
        "L1 = SDL-0/LeftShoulder\n"
        "R1 = SDL-0/RightShoulder\n"
        "L3 = SDL-0/LeftStick\n"
        "R3 = SDL-0/RightStick\n"
        "Analog = SDL-0/Guide\n"
        "L2 = SDL-0/+LeftTrigger\n"
        "R2 = SDL-0/+RightTrigger\n"
        "LUp = SDL-0/-LeftY\n"
        "LRight = SDL-0/+LeftX\n"
        "LDown = SDL-0/+LeftY\n"
        "LLeft = SDL-0/-LeftX\n"
        "RUp = SDL-0/-RightY\n"
        "RRight = SDL-0/+RightX\n"
        "RDown = SDL-0/+RightY\n"
        "RLeft = SDL-0/-RightX\n"
        "LargeMotor = SDL-0/LargeMotor\n"
        "SmallMotor = SDL-0/SmallMotor\n"
    )


def test_pcsx2_uses_positional_face_button_names():
    """PCSX2 writes FaceSouth/East/West/North where DuckStation writes A/B/X/Y."""
    text = to_pcsx2_pad_section(default_mapping(xbox_device()))

    assert "[Pad1]\nType = DualShock2\n" in text
    assert "Cross = SDL-0/FaceSouth" in text
    assert "Circle = SDL-0/FaceEast" in text
    assert "Square = SDL-0/FaceWest" in text
    assert "Triangle = SDL-0/FaceNorth" in text
    # The names the two emulators agree on are unchanged.
    assert "Select = SDL-0/Back" in text
    assert "L2 = SDL-0/+LeftTrigger" in text


def test_duckstation_and_pcsx2_face_names_are_not_interchangeable():
    mapping = default_mapping(xbox_device())

    assert "Cross = SDL-0/A" in to_duckstation_pad_section(mapping)
    assert "Cross = SDL-0/A\n" not in to_pcsx2_pad_section(mapping)


def test_pad_port_and_sdl_index_are_selectable():
    text = to_pcsx2_pad_section(default_mapping(xbox_device()), port=2, sdl_index=4)

    assert text.startswith("[Pad2]\n")
    assert "Cross = SDL-4/FaceSouth" in text


def test_unmapped_inputs_are_simply_absent():
    """A pad with no right stick must not emit half-written RUp/RDown lines."""
    mapping = ControllerMapping(
        name="Stickless Pad",
        buttons={CanonicalButton.A: PhysicalInput.button(0)},
    )
    text = to_duckstation_pad_section(mapping)

    assert "Cross = SDL-0/A" in text
    assert "RUp" not in text
    assert "Circle" not in text
    assert "L2" not in text


# ── The whole export set ──────────────────────────────────────────

def test_export_all_produces_every_verified_target():
    exports = export_all(default_mapping(xbox_device()))

    assert set(exports) == {"sdl", "retroarch", "duckstation", "pcsx2"}
    assert all(value.strip() for value in exports.values())


def test_unverified_emulators_are_absent_rather_than_guessed():
    """No stub output for formats we could not confirm — see the module notes."""
    exports = export_all(default_mapping(xbox_device()))

    for unverified in ("dolphin", "ppsspp", "mgba", "flycast", "melonds"):
        assert unverified not in exports


def test_one_mapping_drives_every_target():
    """The point of the module: configure once, export everywhere."""
    mapping = default_mapping(xbox_device())
    mapping.buttons[CanonicalButton.A] = PhysicalInput.button(9)

    exports = export_all(mapping)

    assert "a:b9" in exports["sdl"]
    assert 'input_b_btn = "9"' in exports["retroarch"]
    # The Stenzek-lineage emulators bind SDL element names, so their files do
    # not change when the physical index does — the SDL string carries it.
    assert "Cross = SDL-0/A" in exports["duckstation"]
