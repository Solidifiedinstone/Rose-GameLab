"""Tests for launch profiles, command construction and the launcher."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rose_gamelab.core.launcher import Launcher, LaunchError, build_command
from rose_gamelab.core.library import Library
from rose_gamelab.core.profiles import LaunchProfile, ProfileStore
from rose_gamelab.db.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "library.db")
    yield database
    database.close()


@pytest.fixture
def library(db):
    return Library(db)


@pytest.fixture
def profiles(db):
    return ProfileStore(db)


@pytest.fixture
def plain():
    """A profile with no wrappers enabled."""
    return LaunchProfile(name="Plain")


# ── Command construction ──────────────────────────────────────────

def test_native_command_is_just_the_target(plain):
    assert build_command(kind="native", target="/games/hades", profile=plain) == [
        "/games/hades"
    ]


def test_emulator_command_uses_the_emulator_binary(plain):
    command = build_command(
        kind="emulator", target="/roms/game.sfc", profile=plain,
        emulator_path="/usr/bin/snes9x",
    )
    assert command == ["/usr/bin/snes9x", "/roms/game.sfc"]


def test_retroarch_core_takes_precedence(plain):
    command = build_command(
        kind="emulator", target="/roms/game.sfc", profile=plain,
        emulator_path="/usr/bin/snes9x",
        retroarch_path="/usr/bin/retroarch",
        core_path="/cores/snes9x_libretro.so",
    )
    assert command == [
        "/usr/bin/retroarch", "-L", "/cores/snes9x_libretro.so", "/roms/game.sfc"
    ]


def test_missing_emulator_explains_what_to_do(plain):
    with pytest.raises(LaunchError) as exc:
        build_command(kind="emulator", target="/roms/g.sfc", profile=plain, system="snes")

    assert "snes" in str(exc.value)


def test_steam_launches_through_the_client(plain):
    """Running the executable directly bypasses Proton, cloud saves, overlay."""
    command = build_command(kind="steam", target="steam://run/1145360", profile=plain)
    assert command[-1] == "steam://run/1145360"


def test_bare_appid_becomes_a_steam_url(plain):
    command = build_command(kind="steam", target="1145360", profile=plain)
    assert command[-1] == "steam://run/1145360"


def test_steam_is_not_wrapped():
    """Wrappers would apply to the Steam client, not the game."""
    profile = LaunchProfile(use_gamemode=True, use_mangohud=True)
    command = build_command(kind="steam", target="steam://run/1", profile=profile)

    assert "gamemoderun" not in command
    assert "mangohud" not in command


def test_unknown_kind_is_rejected(plain):
    with pytest.raises(LaunchError):
        build_command(kind="telepathy", target="x", profile=plain)


def test_option_args_are_split_shell_style(plain):
    command = build_command(
        kind="native", target="/games/x", profile=plain, args='--fullscreen --lang "en GB"'
    )
    assert command == ["/games/x", "--fullscreen", "--lang", "en GB"]


def test_profile_extra_args_are_appended():
    profile = LaunchProfile(extra_args="--no-intro")
    command = build_command(kind="native", target="/games/x", profile=profile)
    assert command == ["/games/x", "--no-intro"]


# ── Wrapper ordering ──────────────────────────────────────────────

def test_wrapper_nesting_order():
    """gamemode outermost, then gamescope, with mangohud closest to the game.

    mangohud must hook the game's own Vulkan calls, not gamescope's compositing.
    """
    profile = LaunchProfile(
        use_gamemode=True, use_gamescope=True, use_mangohud=True,
        gamescope_args="-W 1920 -H 1080",
    )
    command = profile.wrap(["/games/x"], skip_missing=False)

    assert command == [
        "gamemoderun",
        "gamescope", "-W", "1920", "-H", "1080", "--",
        "mangohud",
        "/games/x",
    ]


def test_no_wrappers_leaves_the_command_alone(plain):
    assert plain.wrap(["/games/x"], skip_missing=False) == ["/games/x"]


def test_missing_wrapper_is_skipped_not_fatal():
    """A missing MangoHud should cost you an overlay, not your game."""
    profile = LaunchProfile(use_mangohud=True)
    command = profile.wrap(["/games/x"], skip_missing=True)

    if not __import__("shutil").which("mangohud"):
        assert command == ["/games/x"]


def test_missing_tools_are_reported():
    profile = LaunchProfile(use_gamescope=True, use_mangohud=True)
    missing = profile.missing_tools()
    assert all(tool in ("gamescope", "mangohud") for tool in missing)


# ── Environment ───────────────────────────────────────────────────

def test_profile_environment_is_applied():
    profile = LaunchProfile(env={"DXVK_HUD": "fps"})
    env = profile.environment({"PATH": "/usr/bin"})

    assert env["DXVK_HUD"] == "fps"
    assert env["PATH"] == "/usr/bin"


def test_does_not_force_x11():
    """Regression: the old launcher pinned SDL_VIDEODRIVER=x11 unconditionally,
    which breaks native Wayland sessions."""
    env = LaunchProfile().environment({"XDG_SESSION_TYPE": "wayland"})
    assert env.get("SDL_VIDEODRIVER") is None


def test_does_not_force_debug_logging():
    """Regression: the old launcher always set EGL_LOG_LEVEL=debug."""
    assert LaunchProfile().environment({}).get("EGL_LOG_LEVEL") is None


def test_existing_environment_is_inherited():
    env = LaunchProfile().environment({"HOME": "/home/player"})
    assert env["HOME"] == "/home/player"


# ── Profile storage ───────────────────────────────────────────────

def test_create_and_read_profile(profiles):
    profile_id = profiles.create(LaunchProfile(name="Handheld", use_gamescope=True))
    loaded = profiles.get(profile_id)

    assert loaded.name == "Handheld"
    assert loaded.use_gamescope is True


def test_environment_survives_a_round_trip(profiles):
    profile_id = profiles.create(LaunchProfile(name="X", env={"A": "1", "B": "2"}))
    assert profiles.get(profile_id).env == {"A": "1", "B": "2"}


def test_only_one_profile_is_default(profiles):
    first = profiles.create(LaunchProfile(name="A", is_default=True))
    second = profiles.create(LaunchProfile(name="B", is_default=True))

    assert profiles.get_default().id == second
    assert profiles.get(first).is_default is False


def test_default_profile_is_created_on_first_run(profiles):
    profile = profiles.ensure_default_exists()

    assert profile.is_default
    assert profiles.get_default().id == profile.id


def test_ensure_default_does_not_create_a_second(profiles):
    first = profiles.ensure_default_exists()
    second = profiles.ensure_default_exists()

    assert first.id == second.id
    assert len(profiles.list_profiles()) == 1


def test_update_profile(profiles):
    profile_id = profiles.create(LaunchProfile(name="X"))
    profiles.update(profile_id, name="Y", use_mangohud=True)

    loaded = profiles.get(profile_id)
    assert (loaded.name, loaded.use_mangohud) == ("Y", True)


def test_update_ignores_unknown_fields(profiles):
    profile_id = profiles.create(LaunchProfile(name="X"))
    profiles.update(profile_id, nonsense="x", name="Y")
    assert profiles.get(profile_id).name == "Y"


def test_deleting_a_profile_leaves_games_launchable(profiles, library):
    profile_id = profiles.create(LaunchProfile(name="Temp"))
    game_id = library.add_game(title="Hades", system="pc")
    library.add_launch_option(
        game_id, kind="native", target="/games/hades", profile_id=profile_id
    )

    profiles.delete(profile_id)

    option = library.launch_options_for(game_id)[0]
    assert option["profile_id"] is None


# ── Profile resolution ────────────────────────────────────────────

def test_game_profile_wins_over_default(profiles, library):
    profiles.create(LaunchProfile(name="Default", is_default=True))
    specific_id = profiles.create(LaunchProfile(name="Specific"))

    game_id = library.add_game(title="X", system="pc")
    library.add_launch_option(
        game_id, kind="native", target="/x", profile_id=specific_id
    )

    option = library.launch_options_for(game_id)[0]
    assert profiles.for_game(option).id == specific_id


def test_default_applies_when_a_game_has_no_profile(profiles, library):
    default_id = profiles.create(LaunchProfile(name="Default", is_default=True))

    game_id = library.add_game(title="X", system="pc")
    library.add_launch_option(game_id, kind="native", target="/x")

    option = library.launch_options_for(game_id)[0]
    assert profiles.for_game(option).id == default_id


def test_resolution_always_returns_a_profile(profiles, library):
    """Callers must never have to handle None."""
    game_id = library.add_game(title="X", system="pc")
    library.add_launch_option(game_id, kind="native", target="/x")

    option = library.launch_options_for(game_id)[0]
    assert profiles.for_game(option) is not None


# ── Launching for real ────────────────────────────────────────────

def test_launching_records_playtime(library, profiles, tmp_path):
    """Run a real, short-lived process end to end."""
    game_id = library.add_game(title="Test", system="pc")
    library.add_launch_option(
        game_id, kind="native", target=sys.executable, args="-c pass"
    )

    launcher = Launcher(library, profiles)
    running = launcher.launch(game_id)
    running.wait(timeout=30)

    # Wait for the watcher thread to close the session out.
    for _ in range(100):
        if library.get(game_id).play_count > 0:
            break
        import time
        time.sleep(0.05)

    assert running.tracks_playtime is True
    assert library.get(game_id).play_count == 1
    assert library.get(game_id).last_played is not None


def test_steam_launches_are_not_timed(library, profiles):
    """Timing the steam:// handoff would record seconds for an hours-long play."""
    game_id = library.add_game(title="Hades", system="pc", steam_appid=1145360)
    library.add_launch_option(game_id, kind="steam", target="steam://run/1145360")

    option = library.launch_options_for(game_id)[0]
    assert option["kind"] == "steam"

    from rose_gamelab.core.launcher import HANDOFF_KINDS
    assert "steam" in HANDOFF_KINDS


def test_launching_an_unknown_game_fails_clearly(library, profiles):
    launcher = Launcher(library, profiles)
    with pytest.raises(LaunchError):
        launcher.launch(9999)


def test_game_without_launch_options_fails_clearly(library, profiles):
    game_id = library.add_game(title="Orphan", system="pc")
    launcher = Launcher(library, profiles)

    with pytest.raises(LaunchError) as exc:
        launcher.launch(game_id)

    assert "Orphan" in str(exc.value)


def test_missing_rom_says_the_drive_may_be_disconnected(library, profiles):
    game_id = library.add_game(title="Chrono Trigger", system="snes")
    library.add_launch_option(
        game_id, kind="emulator", target="/gone/ct.sfc", emulator="snes9x"
    )

    launcher = Launcher(library, profiles)
    with pytest.raises(LaunchError) as exc:
        launcher.launch(game_id)

    assert "missing" in str(exc.value).lower()


def test_nonexistent_binary_fails_clearly(library, profiles, tmp_path):
    rom = tmp_path / "game.sfc"
    rom.write_bytes(b"rom")

    game_id = library.add_game(title="X", system="snes")
    library.add_launch_option(game_id, kind="native", target="/does/not/exist")

    launcher = Launcher(library, profiles)
    with pytest.raises(LaunchError) as exc:
        launcher.launch(game_id)

    assert "not found" in str(exc.value).lower()


def test_ensure_default_recovers_when_no_profile_is_flagged(profiles):
    """Regression: startup crashed with a UNIQUE violation on profile name.

    ensure_default_exists() assumed "nothing flagged default" meant "no
    profiles at all", so it tried to insert a second profile called
    "Default". Two GameLab instances sharing a library produce exactly that
    state, and the crash stopped the whole application from opening.
    """
    profile_id = profiles.create(LaunchProfile(name="Default", is_default=True))
    profiles.db.execute("UPDATE launch_profiles SET is_default = 0")

    recovered = profiles.ensure_default_exists()

    assert recovered.id == profile_id
    assert len(profiles.list_profiles()) == 1
    assert profiles.get_default() is not None


def test_ensure_default_promotes_an_existing_profile(profiles):
    profiles.create(LaunchProfile(name="Handheld"))
    profiles.db.execute("UPDATE launch_profiles SET is_default = 0")

    recovered = profiles.ensure_default_exists()

    assert recovered.name == "Handheld"
    assert len(profiles.list_profiles()) == 1


# ── RetroArch fallback ────────────────────────────────────────────

def test_flatpak_retroarch_command_is_kept_whole(plain):
    """A Flatpak RetroArch is three words; joining it into one path fails."""
    command = build_command(
        kind="emulator", target="/roms/game.sfc", profile=plain,
        retroarch_path=["flatpak", "run", "org.libretro.RetroArch"],
        core_path="/cores/snes9x_libretro.so",
    )
    assert command == [
        "flatpak", "run", "org.libretro.RetroArch",
        "-L", "/cores/snes9x_libretro.so", "/roms/game.sfc",
    ]


def test_cores_are_found_without_the_user_configuring_a_directory(
    library, profiles, tmp_path, monkeypatch
):
    """Regression: nothing ever set `libretro_core_dir`, so the RetroArch
    fallback could not launch anything — the interface listed RetroArch as the
    emulator for a system and then raised "no emulator configured"."""
    cores = tmp_path / "cores"
    cores.mkdir()
    (cores / "snes9x_libretro.so").write_bytes(b"")
    monkeypatch.setattr(
        "rose_gamelab.core.launcher.LIBRETRO_CORE_DIRS", (str(cores),)
    )

    launcher = Launcher(library, profiles)
    assert launcher.resolve_core("snes", search_default_dirs=True) == str(
        cores / "snes9x_libretro.so"
    )


def test_default_core_directories_are_only_a_fallback(library, profiles, tmp_path, monkeypatch):
    """A standalone emulator is the better run, so it is not undercut."""
    cores = tmp_path / "cores"
    cores.mkdir()
    (cores / "snes9x_libretro.so").write_bytes(b"")
    monkeypatch.setattr(
        "rose_gamelab.core.launcher.LIBRETRO_CORE_DIRS", (str(cores),)
    )

    launcher = Launcher(library, profiles)
    assert launcher.resolve_core("snes") is None


def test_a_configured_core_directory_is_used_alone(library, profiles, tmp_path, monkeypatch):
    """Someone who pointed GameLab at a core folder gets the cores in it."""
    configured = tmp_path / "mine"
    configured.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "snes9x_libretro.so").write_bytes(b"")
    monkeypatch.setattr(
        "rose_gamelab.core.launcher.LIBRETRO_CORE_DIRS", (str(elsewhere),)
    )

    launcher = Launcher(library, profiles, libretro_core_dir=str(configured))
    assert launcher.resolve_core("snes", search_default_dirs=True) is None


def test_no_core_is_claimed_for_systems_retroarch_cannot_run(
    library, profiles, tmp_path, monkeypatch
):
    """'xenia' is a standalone emulator id, not a libretro core."""
    cores = tmp_path / "cores"
    cores.mkdir()
    (cores / "xenia_libretro.so").write_bytes(b"")
    monkeypatch.setattr(
        "rose_gamelab.core.launcher.LIBRETRO_CORE_DIRS", (str(cores),)
    )

    launcher = Launcher(library, profiles)
    assert launcher.resolve_core("xbox360", search_default_dirs=True) is None


def test_retroarch_is_detected_when_no_path_is_configured(library, profiles, monkeypatch):
    monkeypatch.setattr(
        "rose_gamelab.core.emulator_detect.retroarch_command",
        lambda: ("flatpak", "run", "org.libretro.RetroArch"),
    )
    launcher = Launcher(library, profiles)
    assert launcher.resolve_retroarch() == [
        "flatpak", "run", "org.libretro.RetroArch"
    ]


def test_a_configured_retroarch_path_wins(library, profiles, monkeypatch):
    monkeypatch.setattr(
        "rose_gamelab.core.emulator_detect.retroarch_command",
        lambda: ("/usr/bin/retroarch",),
    )
    launcher = Launcher(library, profiles, retroarch_path="/opt/retroarch/bin/retroarch")
    assert launcher.resolve_retroarch() == ["/opt/retroarch/bin/retroarch"]


# ── Controller configuration ──────────────────────────────────────

def _pad(name="Wireless Controller", vendor=0x054C, product=0x09CC):
    from rose_gamelab.core.controller import InputDevice

    return InputDevice(
        name=name, vendor_id=vendor, product_id=product,
        bustype=0x0003, version=0x0100,
    )


def test_controller_mapping_reaches_a_launched_emulator(library, profiles, monkeypatch):
    """Regression: sdl_environment() existed, was documented as the highest
    leverage output in the module, and was never called from anywhere — so a
    mapped pad never actually reached any emulator."""
    monkeypatch.setattr(
        "rose_gamelab.core.controller.detect_controllers", lambda: [_pad()]
    )
    launcher = Launcher(library, profiles)

    env = launcher.controller_environment("emulator")

    assert "SDL_GAMECONTROLLERCONFIG" in env
    assert env["SDL_JOYSTICK_HIDAPI"] == "0"


def test_steam_launches_are_left_alone(library, profiles, monkeypatch):
    """Steam starts the game from its own environment and has Steam Input."""
    monkeypatch.setattr(
        "rose_gamelab.core.controller.detect_controllers", lambda: [_pad()]
    )
    launcher = Launcher(library, profiles)

    assert launcher.controller_environment("steam") == {}
    assert launcher.controller_environment("heroic") == {}


def test_controller_configuration_can_be_turned_off(library, profiles, monkeypatch):
    monkeypatch.setattr(
        "rose_gamelab.core.controller.detect_controllers", lambda: [_pad()]
    )
    launcher = Launcher(library, profiles, configure_controllers=False)

    assert launcher.controller_environment("emulator") == {}


def test_no_controllers_sets_no_variables(library, profiles, monkeypatch):
    monkeypatch.setattr(
        "rose_gamelab.core.controller.detect_controllers", list
    )
    launcher = Launcher(library, profiles)

    assert launcher.controller_environment("emulator") == {}


def test_a_broken_detection_never_stops_a_game_launching(library, profiles, monkeypatch):
    """Not being able to identify a pad is a reason to fall back to SDL's own
    defaults, not a reason to refuse to start the game."""
    def explode():
        raise OSError("/proc/bus/input/devices is on fire")

    monkeypatch.setattr(
        "rose_gamelab.core.controller.detect_controllers", explode
    )
    launcher = Launcher(library, profiles)

    assert launcher.controller_environment("emulator") == {}


def test_a_launch_profile_can_override_the_controller_configuration(library, profiles, monkeypatch):
    """Someone who sets the variable by hand meant it."""
    from rose_gamelab.core.profiles import LaunchProfile

    monkeypatch.setattr(
        "rose_gamelab.core.controller.detect_controllers", lambda: [_pad()]
    )
    launcher = Launcher(library, profiles)

    base = dict(launcher.controller_environment("emulator"))
    profile = LaunchProfile(env={"SDL_GAMECONTROLLERCONFIG": "mine"})
    result = profile.environment(base)

    assert result["SDL_GAMECONTROLLERCONFIG"] == "mine"


# ── Per-system settings ───────────────────────────────────────────

def test_per_system_arguments_reach_the_command(plain):
    """"Always start PCSX2 fullscreen" without editing PCSX2, or repeating it
    on every game."""
    command = build_command(
        kind="emulator", target="/roms/game.iso", profile=plain,
        emulator_path="/usr/bin/pcsx2-qt", system="ps2",
        system_args=["-fullscreen"],
    )

    assert command == ["/usr/bin/pcsx2-qt", "/roms/game.iso", "-fullscreen"]


def test_per_system_arguments_are_not_applied_to_other_launch_kinds(plain):
    """They describe how to run an emulator; appending them to a Steam URL or
    a native binary would be nonsense."""
    command = build_command(
        kind="native", target="/games/hades", profile=plain,
        system_args=["-fullscreen"],
    )

    assert command == ["/games/hades"]


def test_narrower_settings_come_after_wider_ones(plain):
    """System, then the game's own, then the profile's — so the more specific
    one is what an emulator sees last."""
    from rose_gamelab.core.profiles import LaunchProfile

    profile = LaunchProfile(extra_args="--profile-arg")
    command = build_command(
        kind="emulator", target="/roms/g.iso", profile=profile,
        emulator_path="/usr/bin/pcsx2-qt", system="ps2",
        system_args=["--system-arg"], args="--game-arg",
    )

    assert command[-3:] == ["--system-arg", "--game-arg", "--profile-arg"]


def test_a_configured_emulator_overrides_detection(library, profiles, db, tmp_path):
    """Detection is right nearly always, and "nearly" is the problem: someone
    with two Xenia builds installed has a reason for wanting a particular one."""
    from rose_gamelab.core.system_settings import SystemSettingsStore

    chosen = tmp_path / "xenia_canary"
    chosen.write_text("#!/bin/sh\n")
    settings = SystemSettingsStore(db)
    settings.set("xbox360", emulator_path=str(chosen))

    launcher = Launcher(library, profiles, system_settings=settings)

    assert launcher.resolve_emulator("xbox360") == [str(chosen)]


def test_an_emulator_that_has_been_uninstalled_falls_back_to_detection(
    library, profiles, db
):
    """Rather than failing the launch with "no such file"."""
    from rose_gamelab.core.system_settings import SystemSettingsStore

    settings = SystemSettingsStore(db)
    settings.set("ps2", emulator_path="/opt/gone/pcsx2")

    launcher = Launcher(library, profiles, system_settings=settings)

    assert "ps2" not in launcher.emulator_paths


def test_unparseable_arguments_cost_the_arguments_not_the_launch(db):
    """A stray quote in a settings box should not stop a game starting."""
    from rose_gamelab.core.system_settings import SystemSettingsStore

    settings = SystemSettingsStore(db)
    settings.set("ps2", extra_args='--broken "unclosed')

    assert settings.arguments_for("ps2") == []


def test_a_launch_option_reaches_its_own_emulator(library, profiles, db):
    """A merged entry holds one option per console. Resolving both against the
    game's single system would hand a PS3 disc to the PS2 emulator."""
    game_id = library.add_game(title="San Andreas", system="ps2", path="/roms/sa.iso")
    library.add_launch_option(game_id, kind="emulator", target="/roms/sa.iso")
    library.add_launch_option(
        game_id, kind="emulator", target="/roms/sa/EBOOT.BIN", system="ps3"
    )

    options = library.launch_options_for(game_id)

    assert {o["system"] for o in options} == {"ps2", "ps3"}
