"""Launches emulators with the proper args + environment.

The launcher is the bridge between a GameEntry and the actual
emulator process. Each game type (ROM, Steam, Epic, GOG) gets
a different launch strategy, but they all funnel through this
class so you get consistent controller / overlay handling.
"""

from __future__ import annotations

import os
import subprocess
import platform
from pathlib import Path
from typing import Optional

from rose_gamelab.config import Config
from rose_gamelab.core.emulator import GameEntry, SYSTEMS


class EmulatorProcess:
    """Thin wrapper around an emulator subprocess so we can track / kill it."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._cleaned_up = False

    @property
    def is_running(self) -> bool:
        return self.proc.poll() is None

    def terminate(self) -> None:
        """Safe termination — try graceful first, then force."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                except OSError:
                    pass

    def __del__(self) -> None:
        if not self._cleaned_up and self.is_running:
            try:
                self.proc.kill()
            except OSError:
                pass


class Launcher:
    """Orchestrates spawning emulator processes with correct arguments."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._active_procs: list[EmulatorProcess] = []

    # ── Entry point ─────────────────────────────────────────────

    def launch(self, game: GameEntry) -> Optional[EmulatorProcess]:
        """Launch the appropriate backend for a given game."""
        if game.is_steam or game.is_heroic or game.is_gog:
            return self._launch_store_game(game)
        return self._launch_rom(game)

    # ── ROM launchers ───────────────────────────────────────────

    def _launch_rom(self, game: GameEntry) -> Optional[EmulatorProcess]:
        emitter = game.system  # "snes", "gba", etc.
        system = SYSTEMS.get(emitter)
        if not system:
            raise ValueError(f"Unknown system: {emitter}")

        # Find emulator binary
        emulator_name = system.default_core
        emulator_path = self.config.get(f"emulators.{emitter}")
        if not emulator_path:
            # Try to find it on PATH
            emulator_path = self._find_on_path(emulator_name)

        if not emulator_path or not os.path.isfile(emulator_path):
            raise ValueError(f"Emulator not found for {emitter}: '{emulator_name}'")

        # Build the command
        cmd = self._build_rom_command(emulator_path, game)

        # Launch with proper env
        env = self._setup_env()
        proc = subprocess.Popen(cmd, env=env, start_new_session=platform.system() != "Windows")
        return EmulatorProcess(proc)

    def _build_rom_command(self, emulator_path: str, game: GameEntry) -> list[str]:
        """Build the command to launch a ROM.

        Strategy: try per-emulator launch first. If no custom args set,
        fall back to retroarch with the matching core (universal fallback).
        """
        system = SYSTEMS.get(game.system)
        if not system:
            return [emulator_path, game.path]

        # Check if user configured custom launch args
        custom_args = self.config.get("emulator_args", {}).get(game.system, "")
        if custom_args:
            return [emulator_path, custom_args.format(rom=game.path)]

        # Try retroarch as universal backend
        retroarch = self.config.emulator_defaults.get("retroarch_bin")
        if retroarch and Path(retroarch).is_file():
            core = self.config.get("emulator_defaults.libretro_core_dir", "/dev/null")
            core_file = self._get_libretro_core(game.system)
            if core_file:
                return [
                    retroarch,
                    "-l", core_file,
                    game.path,
                ]

        # No retroarch fallback — just emulator + ROM
        return [emulator_path, game.path]

    def _get_libretro_core(self, system: str) -> Optional[str]:
        """Return the libretro core binary path for a system."""
        system_to_core = {
            "snes": "snes9x",
            "gba": "mgba",
            "gbc": "mgba",
            "gb": "mgba",
            "nds": "melonds",
            "ps1": "pcsx_rearmed",
            "psp": "ppsspp",  # not libretro by default
            "wii": "dolphin",  # not libretro
            "dreamcast": "flycast",
            "n64": "mupen64plus",
            "arcade": "mame",
        }
        core_name = system_to_core.get(system)
        if not core_name:
            return None

        core_dir = self.config.emulator_defaults.get("libretro_core_dir")
        if not core_dir:
            return None

        for ext in (".so", ".dll", ".dylib"):
            candidate = Path(core_dir) / f"{core_name}{ext}"
            if candidate.exists():
                return str(candidate)

        return None

    # ── Store game launchers ────────────────────────────────────

    def _launch_store_game(self, game: GameEntry) -> Optional[EmulatorProcess]:
        """Launch games from Steam, Heroic, or GOG."""
        cmd = [game.path]
        if game.is_steam:
            # Use steam://run/<appid>
            app_id = game.metadata.get("steam_app_id")
            if app_id:
                cmd = ["steam", f"steam://run/{app_id}"]
        # For Heroic/GOG, game.path should already be the launcher or binary
        env = self._setup_env()
        proc = subprocess.Popen(cmd, env=env, start_new_session=platform.system() != "Windows")
        return EmulatorProcess(proc)

    # ── Helpers ─────────────────────────────────────────────────

    def _find_on_path(self, name: str) -> Optional[str]:
        """Find an executable in PATH."""
        import shutil
        return shutil.which(name)

    def _setup_env(self) -> dict:
        """Environment setup for emulator processes."""
        env = os.environ.copy()
        env["SDL_VIDEODRIVER"] = "x11" or env.get("SDL_VIDEODRIVER", "")
        env["EGL_LOG_LEVEL"] = "debug"  # verbose log if needed
        return env
