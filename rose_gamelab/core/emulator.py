"""Base classes and registry for emulators / system providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class RomType(Enum):
    """Supported ROM file extensions per system."""
    SNES = [".sfc", ".smc", ".z64"]
    GBA = [".gba"]
    GBC = [".gbc"]
    GB = [".gb"]
    NDS = [".nds", ".ds"]
    IPS = [".ips"]  # patch
    PS1 = [".bin", ".img", ".ccd", ".iso", ".cue"]
    PS2 = [".bin", ".iso", ".chd"]
    PSP = [".elf", ".prx", ".pbp"]
    Wii = [".iso", ".wbfs", ".ciso"]
    WiiU = [".czp", ".rpx"]
    Switch = [".nro", ".nso", ".nsp", ".xci"]
    N64 = [".n64", ".z64", ".v64"]
    Xbox = [".xbe"]
    Dreamcast = [".binary", ".bi0"]
    PS4 = [".elf", ".bin", ".ps4"]
    PS3 = [".elf", ".prx", ".self"]
    Arcade = [".zip"]
    VBA = [".vb"]
    Atari2600 = [".bin"]
    FDS = [".fds"]
    MasterSystem = [".sms", ".md", ".sgd"]
    SegaSat = [".cdi", ".cue"]
    PC_Engine = [".img", ".ccd"]
    Genesis = [".smd", ".gen", ".md", ".bin"]
    MSX = [".rom", ".msx"]
    VirtualBoy = [".vb"]
    Unknown = []

    @classmethod
    def from_extension(cls, ext: str) -> Optional["RomType"]:
        ext_lower = ext.lower() if ext else ""
        for rt in cls:
            if rt != cls.Unknown and ext_lower in rt.value:
                return rt
        return None

    @property
    def extensions(self) -> list[str]:
        return self.value


@dataclass
class EmulatorDef:
    """Metadata about an emulated system."""
    id: str                       # e.g. "snes", "gba"
    name: str                     # e.g. "Super Nintendo", "Game Boy Advance"
    icon: str = ""                # emoji or icon name
    rom_extensions: list[str] = field(default_factory=list)
    default_core: str = ""         # e.g. "snes9x", "mgba"
    launch_args_template: str = "" # template for building launch command
    needs_controller: bool = True  # most emulators benefit from controller input

    def matches_rom(self, path: Path) -> bool:
        return path.suffix.lower() in self.rom_extensions or len(self.rom_extensions) == 0


@dataclass
class GameEntry:
    """A single game/ROM entry in the library."""
    id: str                              # unique ID (folder+filename hash)
    name: str                            # display name
    system: str                          # system ID (e.g. "snes")
    path: str                            # path to ROM or executable
    cover_art: str = ""                  # optional local path to cover image
    metadata: dict = field(default_factory=dict)  # last_played, play_count, custom notes, etc.
    source: str = ""                     # where it came from (source ID)
    is_steam: bool = False               # whether it's a Steam game, not a ROM
    is_heroic: bool = False
    is_gog: bool = False

    @property
    def last_played(self) -> Optional[str]:
        return self.metadata.get("last_played")

    @property
    def play_count(self) -> int:
        return self.metadata.get("play_count", 0)


# ── Registry ───────────────────────────────────────────────────

SYSTEMS: dict[str, EmulatorDef] = {
    "snes": EmulatorDef(id="snes", name="Super Nintendo", icon="🎮",
                        rom_extensions=RomType.SNES.extensions, default_core="snes9x"),
    "gba": EmulatorDef(id="gba", name="Game Boy Advance", icon="🕹️",
                       rom_extensions=RomType.GBA.extensions, default_core="mgba"),
    "gbc": EmulatorDef(id="gbc", name="Game Boy Color", icon="🕹️",
                       rom_extensions=RomType.GBC.extensions, default_core="mgba"),
    "gb": EmulatorDef(id="gb", name="Game Boy", icon="🕹️",
                      rom_extensions=RomType.GB.extensions, default_core="mgba"),
    "nds": EmulatorDef(id="nds", name="Nintendo DS", icon="📱",
                       rom_extensions=RomType.NDS.extensions, default_core="melonds"),
    "ps1": EmulatorDef(id="ps1", name="PlayStation", icon="🎮",
                       rom_extensions=RomType.PS1.extensions, default_core="dxvk-psx"),
    "ps2": EmulatorDef(id="ps2", name="PlayStation 2", icon="🎮",
                       rom_extensions=RomType.PS2.extensions, default_core="pcsx2"),
    "psp": EmulatorDef(id="psp", name="PlayStation Portable", icon="🎮",
                       rom_extensions=RomType.PSP.extensions, default_core="ppsspp"),
    "3ds": EmulatorDef(id="3ds", name="Nintendo 3DS", icon="📱",
                       rom_extensions=[".3ds", ".cxi", ".ncp", ".ncf"], default_core="citra"),
    "wii": EmulatorDef(id="wii", name="Nintendo Wii", icon="📺",
                       rom_extensions=RomType.Wii.extensions, default_core="dolphin"),
    "wiiu": EmulatorDef(id="wiiu", name="Nintendo Wii U", icon="📺",
                        rom_extensions=RomType.WiiU.extensions, default_core="cemu"),
    "switch": EmulatorDef(id="switch", name="Nintendo Switch", icon="🎮",
                          rom_extensions=RomType.Switch.extensions, default_core="ryujinx"),
    "xbox": EmulatorDef(id="xbox", name="Xbox Original", icon="🎮",
                        rom_extensions=RomType.Xbox.extensions, default_core="xemu"),
    "dreamcast": EmulatorDef(id="dreamcast", name="Sega Dreamcast", icon="💿",
                             rom_extensions=RomType.Dreamcast.extensions, default_core="flycast"),
    "ps4": EmulatorDef(id="ps4", name="PlayStation 4", icon="🎮",
                       rom_extensions=RomType.Unknown.extensions, default_core="shadps4"),
    "ps3": EmulatorDef(id="ps3", name="PlayStation 3", icon="🎮",
                       rom_extensions=RomType.Unknown.extensions, default_core="rpcs3"),
    "xenia": EmulatorDef(id="xenia", name="Xbox 360", icon="🎮",
                         rom_extensions=RomType.Unknown.extensions, default_core="xenia_canary"),
    "n64": EmulatorDef(id="n64", name="Nintendo 64", icon="🎮",
                       rom_extensions=RomType.N64.extensions, default_core="para64"),
    "vba": EmulatorDef(id="vba", name="Virtual Boy", icon="🔴",
                       rom_extensions=RomType.VBA.extensions, default_core="vbam"),
    "atari2600": EmulatorDef(id="atari2600", name="Atari 2600", icon="👾",
                             rom_extensions=RomType.Atari2600.extensions, default_core="stella"),
    "fds": EmulatorDef(id="fds", name="Famicom Disk System", icon="💾",
                       rom_extensions=RomType.FDS.extensions, default_core="fceux"),
    "master_system": EmulatorDef(id="master_system", name="Sega Master System", icon="🕹️",
                                 rom_extensions=RomType.MasterSystem.extensions, default_core="genesis_plus_gx"),
    "megadrive": EmulatorDef(id="megadrive", name="Sega Mega Drive / Genesis", icon="🕹️",
                             rom_extensions=RomType.Genesis.extensions, default_core="genesis_plus_gx"),
    "pc_engine": EmulatorDef(id="pc_engine", name="TurboGrafx-16 / PC Engine", icon="💿",
                             rom_extensions=RomType.PC_Engine.extensions, default_core="mednafen_pce"),
    "sega_sat": EmulatorDef(id="sega_sat", name="Sega Saturn", icon="💿",
                            rom_extensions=RomType.SegaSat.extensions, default_core="yabassan"),
    "arcade": EmulatorDef(id="arcade", name="Arcade (NEOGEO, etc.)", icon="👾",
                          rom_extensions=RomType.Arcade.extensions, default_core="mame"),
    "msx": EmulatorDef(id="msx", name="MSX", icon="🖥️",
                       rom_extensions=RomType.MSX.extensions, default_core="fmsx"),
}


def get_system(system_id: str) -> Optional[EmulatorDef]:
    return SYSTEMS.get(system_id)


def list_systems() -> list[EmulatorDef]:
    return list(SYSTEMS.values())
