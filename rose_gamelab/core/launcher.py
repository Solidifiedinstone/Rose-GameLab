"""Launching games and tracking how long they were played.

Every way of starting a game — an emulator, a native binary, a Steam app, a
Heroic game — funnels through here so profiles, environment and playtime work
identically regardless of source.

A note on playtime honesty: launching a Steam game runs `steam steam://run/ID`,
which hands off to the already-running Steam client and exits almost
immediately. Timing that process would record a two-second session for a
four-hour play. Rather than invent a number, launches that cannot be
timed are marked `tracks_playtime = False` and no session is recorded. The
interface says playtime is tracked by Steam instead of showing a wrong figure.

Nothing here touches the network.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from rose_gamelab.core import emulator_detect
from rose_gamelab.core.emulator import get_system
from rose_gamelab.core.library import Library
from rose_gamelab.core.profiles import LaunchProfile, ProfileStore

logger = logging.getLogger(__name__)

# Launch kinds that hand off to another program and exit immediately, so the
# process we spawn is not the game and cannot be timed.
HANDOFF_KINDS = {"steam", "heroic", "lutris"}

# Where RetroArch keeps its libretro cores. Checked in order, and the Flatpak
# location is included because a Flatpak RetroArch keeps cores inside its own
# sandbox and nothing lands in the usual paths.
LIBRETRO_CORE_DIRS = (
    "~/.config/retroarch/cores",
    "~/.local/share/retroarch/cores",
    "~/.var/app/org.libretro.RetroArch/config/retroarch/cores",
    "/usr/lib/libretro",
    "/usr/lib64/libretro",
    "/usr/local/lib/libretro",
)


class LaunchError(Exception):
    """Raised when a game cannot be started. The message is shown to the user,
    so it must say what is wrong and what to do about it."""


@dataclass
class GameProcess:
    """A running game."""

    process: subprocess.Popen
    game_id: int
    session_id: Optional[int] = None
    tracks_playtime: bool = True
    command: list[str] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.process.poll() is None

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self) -> None:
        """Ask the game to close, then force it if it will not.

        The process is spawned in its own session, so signals reach the whole
        process group — emulators frequently fork helpers that would otherwise
        survive and keep a window open.
        """
        if self.process.poll() is not None:
            return

        try:
            os.killpg(os.getpgid(self.process.pid), 15)  # SIGTERM
        except (OSError, ProcessLookupError):
            self.process.terminate()

        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.process.pid), 9)  # SIGKILL
            except (OSError, ProcessLookupError):
                self.process.kill()


def build_command(
    *,
    kind: str,
    target: str,
    profile: LaunchProfile,
    emulator: Optional[str] = None,
    emulator_path: Optional[list[str] | str] = None,
    args: Optional[str] = None,
    system: Optional[str] = None,
    retroarch_path: Optional[list[str] | str] = None,
    core_path: Optional[str] = None,
    silent_steam: bool = False,
) -> list[str]:
    """Build the full command line for a launch. Pure function, no side effects.

    Separated from spawning so the exact command can be tested, logged, and
    shown to the user in the interface before anything runs.
    """
    command: list[str]

    if kind == "steam":
        # Route through the Steam client so Proton, cloud saves and the overlay
        # all engage. Launching the executable directly bypasses every one —
        # and for any game using Steamworks DRM it simply fails, because the
        # game asks a running client to authorise it.
        steam = shutil.which("steam") or "steam"
        command = [steam]

        # `-silent` starts the client straight to the tray with no window and
        # no library UI. The client still runs — that is not optional for most
        # games — but nothing of it appears on screen.
        if silent_steam:
            command.append("-silent")

        command.append(
            target if target.startswith("steam://") else f"steam://run/{target}"
        )
        # Steam applies its own launch options; wrappers here would apply to the
        # client, not the game.
        return command

    if kind == "heroic":
        # Heroic games are launched by URL so Heroic itself sets up the
        # Wine/Proton prefix, cloud saves and DLC state. Running the game's
        # own executable directly skips all of that.
        heroic = shutil.which("heroic")
        command = [heroic, target] if heroic else ["xdg-open", target]

    elif kind == "lutris":
        # Lutris carries the whole runner configuration per game; only the
        # Lutris client knows how to apply it.
        lutris = shutil.which("lutris")
        if lutris is None:
            raise LaunchError(
                "Lutris is not installed, so this game cannot be launched. "
                "Install Lutris, or add another launch option for it."
            )
        command = [lutris, target]

    elif kind in ("native", "gog", "custom"):
        # A URL that slipped through as 'native' still needs a handler rather
        # than being exec'd as a file path.
        if "://" in target or target.startswith(("lutris:", "heroic:")):
            command = ["xdg-open", target]
        elif Path(target).is_file():
            # An executable, whose name may legitimately contain spaces.
            command = [target]
        else:
            # A command line rather than a path — desktop entries routinely
            # carry one, e.g. `env GDK_BACKEND=wayland an-anime-game-launcher`.
            # Exec'ing that as a single filename fails with "no such file".
            command = shlex.split(target)

    elif kind == "emulator":
        if core_path and retroarch_path:
            # RetroArch with an explicit libretro core. A list, because a
            # Flatpak RetroArch is `flatpak run org.libretro.RetroArch`.
            retroarch = (
                list(retroarch_path)
                if isinstance(retroarch_path, (list, tuple))
                else [retroarch_path]
            )
            command = [*retroarch, "-L", core_path, target]
        elif emulator_path:
            # A list, since Flatpak emulators are `flatpak run <id> <rom>`.
            command = [*emulator_path, target] if isinstance(emulator_path, list) else [emulator_path, target]
        else:
            raise LaunchError(
                f"No emulator configured for {system or 'this system'}. "
                f"Set one in Settings, or install {emulator or 'a suitable emulator'}."
            )
    else:
        raise LaunchError(f"Unknown launch type: {kind}")

    if args:
        command.extend(shlex.split(args))

    if profile.extra_args:
        command.extend(shlex.split(profile.extra_args))

    return profile.wrap(command)


class Launcher:
    """Starts games and records play sessions."""

    def __init__(
        self,
        library: Library,
        profiles: ProfileStore,
        *,
        emulator_paths: Optional[dict[str, str]] = None,
        retroarch_path: Optional[str] = None,
        libretro_core_dir: Optional[str] = None,
        silent_steam: bool = False,
    ) -> None:
        self.library = library
        self.profiles = profiles
        # User-configured emulator binaries, keyed by system id.
        self.emulator_paths = emulator_paths or {}
        self.retroarch_path = retroarch_path
        self.libretro_core_dir = libretro_core_dir
        # Start the Steam client to the tray instead of opening its window.
        self.silent_steam = silent_steam
        self.running: dict[int, GameProcess] = {}

    # ── Resolution ────────────────────────────────────────────────

    def resolve_emulator(self, system_id: str) -> Optional[list[str]]:
        """How to run this system's emulator, as a command, or None.

        Returns a COMMAND rather than a path, because a large share of Linux
        emulators are Flatpaks whose invocation is `flatpak run <id>` and which
        put nothing on PATH at all. Checking only PATH reported "not installed"
        on machines where the emulator was installed and working.

        A user-configured path always wins.
        """
        configured = self.emulator_paths.get(system_id)
        if configured and Path(configured).is_file():
            return [configured]

        option = emulator_detect.best_for(system_id)
        if option is not None and option.kind != "retroarch":
            return list(option.command)

        # RetroArch is handled through the core path instead, so that the
        # right libretro core is passed rather than bare RetroArch.
        return None

    def resolve_retroarch(self) -> Optional[list[str]]:
        """How to run RetroArch here, as a command, or None.

        A configured path wins; otherwise RetroArch is detected the same way
        every other emulator is, which is what makes a Flatpak install usable.
        """
        if self.retroarch_path:
            return [self.retroarch_path]

        command = emulator_detect.retroarch_command()
        return list(command) if command else None

    def resolve_core(
        self, system_id: str, *, search_default_dirs: bool = False
    ) -> Optional[str]:
        """Find a libretro core for a system.

        Only the configured core directory is searched unless
        `search_default_dirs` is set, which keeps a user who pointed GameLab at
        a core folder getting exactly the cores in it. The standard locations
        are searched as a fallback for systems with no standalone emulator
        installed: before that, GameLab listed RetroArch as the emulator for
        those systems and then refused to launch, because nothing ever set a
        core directory in the first place.
        """
        system = get_system(system_id)
        if not system or not system.default_core:
            return None

        # 'default_core' holds a standalone emulator id for the modern systems,
        # and there is no libretro core by that name to find.
        if system_id in emulator_detect.NO_LIBRETRO_CORE:
            return None

        if self.libretro_core_dir:
            directories = [self.libretro_core_dir]
        elif search_default_dirs:
            directories = list(LIBRETRO_CORE_DIRS)
        else:
            return None

        for entry in directories:
            directory = Path(entry).expanduser()
            for suffix in (".so", ".dll", ".dylib"):
                candidate = directory / f"{system.default_core}_libretro{suffix}"
                if candidate.is_file():
                    return str(candidate)

        return None

    # ── Launching ─────────────────────────────────────────────────

    def launch(
        self,
        game_id: int,
        *,
        launch_option_id: Optional[int] = None,
        on_exit: Optional[Callable[[int, int], None]] = None,
    ) -> GameProcess:
        """Launch a game. Returns immediately with a handle to the process.

        `on_exit`, if given, is called as (game_id, seconds_played) once the
        game closes. Playtime is recorded automatically for launches that can
        be timed.
        """
        game = self.library.get(game_id)
        if game is None:
            raise LaunchError(f"Game {game_id} is not in the library.")

        options = self.library.launch_options_for(game_id)
        if not options:
            raise LaunchError(f"{game.title} has no way to launch configured.")

        if launch_option_id is not None:
            option = next((o for o in options if o["id"] == launch_option_id), None)
            if option is None:
                raise LaunchError("That launch option no longer exists.")
        else:
            option = options[0]  # ordered primary-first

        target = option["target"]
        kind = option["kind"]

        # A missing file is the most common failure; say so plainly rather than
        # letting the emulator fail with its own opaque message.
        if kind == "emulator" and not Path(target).exists():
            raise LaunchError(
                f"The file for {game.title} is missing:\n{target}\n\n"
                "It may be on a drive that is not connected."
            )

        profile = self.profiles.for_game(option)

        missing = profile.missing_tools()
        if missing:
            logger.warning(
                "profile %s wants tools that are not installed: %s",
                profile.name, ", ".join(missing),
            )

        emulator_path = (
            self.resolve_emulator(game.system) if kind == "emulator" else None
        )
        # Fall back to a libretro core only when no standalone emulator is
        # installed — a standalone emulator is the better run of the two, and
        # is what detection already picked.
        core_path = (
            self.resolve_core(game.system, search_default_dirs=emulator_path is None)
            if kind == "emulator"
            else None
        )

        command = build_command(
            kind=kind,
            target=target,
            profile=profile,
            emulator=option["emulator"],
            emulator_path=emulator_path,
            args=option["args"],
            system=game.system,
            retroarch_path=self.resolve_retroarch() if core_path else None,
            core_path=core_path,
            silent_steam=self.silent_steam,
        )

        env = profile.environment(dict(os.environ))

        if profile.pre_launch:
            self._run_hook(profile.pre_launch, env)

        logger.info("launching %s: %s", game.title, " ".join(command))

        try:
            process = subprocess.Popen(
                command,
                env=env,
                cwd=option["working_dir"] or None,
                # Own process group, so terminate() reaches forked helpers.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise LaunchError(f"Could not run {command[0]}: not found on this system.") from exc
        except PermissionError as exc:
            raise LaunchError(f"Not permitted to run {command[0]}.") from exc

        tracks = kind not in HANDOFF_KINDS
        session_id = (
            self.library.start_session(game_id, option["id"]) if tracks else None
        )

        running = GameProcess(
            process=process,
            game_id=game_id,
            session_id=session_id,
            tracks_playtime=tracks,
            command=command,
        )
        self.running[game_id] = running

        self._watch(running, profile, env, on_exit)
        return running

    def _watch(
        self,
        running: GameProcess,
        profile: LaunchProfile,
        env: dict[str, str],
        on_exit: Optional[Callable[[int, int], None]],
    ) -> None:
        """Wait for the game in the background and close out its session."""

        def wait_and_finish() -> None:
            running.process.wait()

            seconds = 0
            if running.session_id is not None:
                seconds = self.library.end_session(running.session_id)

            self.running.pop(running.game_id, None)

            if profile.post_exit:
                self._run_hook(profile.post_exit, env)

            if on_exit:
                try:
                    on_exit(running.game_id, seconds)
                except Exception:
                    logger.exception("on_exit callback failed")

        # Daemon: a game still running must not keep GameLab from quitting.
        threading.Thread(target=wait_and_finish, daemon=True).start()

    @staticmethod
    def _run_hook(command: str, env: dict[str, str]) -> None:
        """Run a pre-launch or post-exit shell hook.

        Failures are logged and ignored: a broken hook should not stop a game
        from starting, or wedge the launcher after it exits.
        """
        try:
            subprocess.run(command, shell=True, env=env, timeout=30, check=False)
        except subprocess.TimeoutExpired:
            logger.warning("hook timed out: %s", command)
        except Exception:
            logger.exception("hook failed: %s", command)

    # ── Running games ─────────────────────────────────────────────

    def is_running(self, game_id: int) -> bool:
        running = self.running.get(game_id)
        return running is not None and running.is_running

    def stop(self, game_id: int) -> None:
        running = self.running.get(game_id)
        if running:
            running.terminate()

    def stop_all(self) -> None:
        for running in list(self.running.values()):
            running.terminate()
