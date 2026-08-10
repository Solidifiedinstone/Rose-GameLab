"""Central configuration — everything is persisted as YAML, everything is tweakable."""

import copy
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_CONFIG = {
    # Where GameLab stores its own state
    "config_dir": str(Path.home() / ".config" / "rose-gamelab"),

    # ── Library paths ─────────────────────────────────────────────
    # Each source gets its own entry; these are what the user scans
    "sources": [],                          # list of source dicts (see Source schema)

    # ── Emulator paths ────────────────────────────────────────────
    # Maps system id -> path to emulator binary
    "emulators": {
        "snes":        None,                # SNES9X
        "gba":         None,                # mGBA
        "nds":         None,                #melonDS / DeSmuME
        "ps1":         None,                # DuckStation / PyGameStation
        "ps2":         None,                # PCSX2
        "psp":         None,                # PPSSPP
        "3ds":         None,                # Citra
        "wii":         None,                # Dolphin
        "wiiu":        None,                # Cemu
        "switch":      None,                # Yuzu / Ryujinx / Sudomod Switch
        "xbox":        None,                # Xemu
        "dreamcast":   None,                # Flycast /redream
        "ps4":         None,                # ShadPS4
        "ps3":         None,                # RPCS3
        "xenia":       None,                # Xenia / Xenia Canary
        "n64":         None,                # ParaLLEl / Mupen64Plus
        "wslan":         None,               # WenQu / Mupen64Plus
       "vba":         None,                # BGB / Gambatte
        "atari2600":   None,               # Stella
        "atari800":    None,               # Atari800
        "intellivision": None,             # NTSCJ/FreeIntv
        "fds":         None,               # FCEUX (Famicom Disk System)
        "master_system": None,             # Genesis Plus GX / Gambatte
        "megadrive":   None,               # Same as above
        "pc_engine":   None,               # PCEJS / Mednafen
        "sega_sat":    None,               # Supermodel / YabaSanshiro
        "arcade":      None,               # MAME
        "msx":         None,               # fMSX
        "collec":      None,               # BlueMSX
        "virtualboy":  None,               # VBAM
        "nintendo_3ds": None,              # Citra
        # Add more as needed
    },

    # ── Controller ────────────────────────────────────────────────
    "controller": {
        "input_profile": "default",           # which controller profile (see ctrl_profiles below)
        "autoconfigure": True,                # auto-detect & assign on launch
        "hotkey_combo": "R2 + Select + Start", # toggle overlay
        "save_on_exit": True,                 # persist mapping changes
    },
    "controller_profiles": {},               # named profiles -> keybindings / layout

    # ── Global emulator defaults ──────────────────────────────────
    "emulator_defaults": {
        "fullscreen": True,
        "save_state_on_exit": True,
        "load_state_on_start": True,
        "use_libretro_core": False,           # if True, use libretro frontend for all systems
        "libretro_core_dir": str(Path.home() / ".local" / "share" / "retroarch" / "core"),
        "retroarch_bin": None,                 # path to retroarch binary (fallback frontend)
    },

    # ── Styling ───────────────────────────────────────────────────
    "theme": "btop++",                        # theme preset name
    "colors": {
        "background": "#1a1b26",
        "surface": "#24283b",
        "panel": "#292e42",
        "accent": "#7aa2f7",
        "text": "#c0caf5",
        "text_dim": "#565a78",
        "success": "#9ece6a",
        "warning": "#e0af68",
        "error": "#f7768e",
    },

    # ── Behavior ──────────────────────────────────────────────────
    "behavior": {
        "sort_by": "name",                    # name, last_played, platform, added
        "sort_order": "asc",                  # asc, desc
        "show_system_grouping": True,          # group ROMs by console in library view
        "scan_on_startup": True,               # re-scan sources when app starts
        "auto_import_steam": True,             # pull Steam games automatically
        "auto_import_heroic": True,
        "auto_import_gog": True,
    },

    # ── Game metadata cache ───────────────────────────────────────
    "game_cache": {},                         # game_id -> {cover, metadata, last_played, play_count}

    # ── Hotkey overlays ───────────────────────────────────────────
    "overlay": {
        "show_fps": False,
        "show_input_indicators": True,
        "show_save_state_info": False,
    },
}


class Config:
    """Immutable-ish config wrapper that loads from / saves to a YAML file."""

    def __init__(self, config_dir: Optional[str] = None) -> None:
        self._settings = copy.deepcopy(DEFAULT_CONFIG)
        self._raw: dict = {}

        if config_dir:
            self._settings["config_dir"] = config_dir

        conf_dir = Path(self._settings["config_dir"])
        conf_dir.mkdir(parents=True, exist_ok=True)
        self._file = conf_dir / "settings.yaml"
        self._load()

    # ── Public helpers ──────────────────────────────────────────

    @property
    def config_dir(self) -> str:
        return self._settings["config_dir"]

    @property
    def sources(self) -> list:
        return self._settings["sources"]

    @property
    def emulators(self) -> dict:
        return self._settings["emulators"]

    @property
    def controller(self) -> dict:
        return self._settings["controller"]

    @property
    def controller_profiles(self) -> dict:
        return self._settings["controller_profiles"]

    @property
    def emulator_defaults(self) -> dict:
        return self._settings["emulator_defaults"]

    @property
    def theme(self) -> str:
        return self._settings["theme"]

    @property
    def colors(self) -> dict:
        return self._settings["colors"]

    @property
    def behavior(self) -> dict:
        return self._settings["behavior"]

    @property
    def game_cache(self) -> dict:
        return self._settings["game_cache"]

    def save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file, "w") as f:
            yaml.dump(self._raw, f, default_flow_style=False, sort_keys=False)

    def _load(self) -> None:
        if self._file.exists():
            with open(self._file) as f:
                loaded: dict = yaml.safe_load(f) or {}
                # Deep merge — don't clobber defaults
                self._raw = self._deep_merge(DEFAULT_CONFIG, loaded)
                self._settings = self._raw
        else:
            self._raw = copy.deepcopy(DEFAULT_CONFIG)
            self._settings = self._raw

    def _deep_merge(self, base: dict, override: dict) -> dict:
        merged = copy.deepcopy(base)
        for k, v in override.items():
            if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k] = self._deep_merge(merged[k], v)
            else:
                merged[k] = copy.deepcopy(v)
        return merged

    # ── Low-level set / get ─────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        val = self._settings
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k, default)
            else:
                return default
        return val

    def set(self, key: str, value: Any) -> None:
        keys = key.split(".")
        val = self._settings
        for k in keys[:-1]:
            if k not in val or not isinstance(val[k], dict):
                val[k] = {}
            val = val[k]
        val[keys[-1]] = value

    def add_source(self, source: dict) -> None:
        """Add a source entry and persist."""
        self._settings["sources"].append(source)
        self._raw["sources"] = self._settings["sources"]
        self.save()

    def remove_source(self, idx: int) -> None:
        self._settings["sources"].pop(idx)
        self._raw["sources"] = self._settings["sources"]
        self.save()

    def set_emulator(self, system: str, path: Optional[str]) -> None:
        self._settings["emulators"][system] = path
        self._raw["emulators"] = self._settings["emulators"]
        self.save()

    def update_game_cache(self, game_id: str, data: dict) -> None:
        self._settings["game_cache"][game_id] = data
        self._raw["game_cache"] = self._settings["game_cache"]
        self.save()

    def get_game_cache(self, game_id: str) -> dict:
        return self._settings["game_cache"].get(game_id, {})
