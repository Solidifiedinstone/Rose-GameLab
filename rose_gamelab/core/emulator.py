"""System registry and core library types.

`SYSTEMS` maps a system id to what GameLab needs to know about it: what its
ROMs look like on disk, what emulator usually runs it, and how it should be
labelled in the interface.

Extensions are declared per system rather than through a shared enum. An enum
cannot express this correctly: two members with identical values become
aliases for each other, so `VirtualBoy = [".vb"]` and `VBA = [".vb"]` silently
collapsed into one member in the previous version.

Note that extensions overlap heavily across systems — `.bin` and `.iso` belong
to half a dozen platforms. Extension matching narrows the candidates; content
hashing (see core/hashing.py) is what actually identifies a game.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class EmulatorDef:
    """Metadata about an emulated system."""

    id: str
    name: str
    icon: str = ""
    rom_extensions: tuple[str, ...] = ()
    #: The libretro core that runs this system, WITHOUT the `_libretro` suffix
    #: — or, for the modern systems that have no core, the standalone emulator
    #: that does. `emulator_detect.NO_LIBRETRO_CORE` says which is which.
    #:
    #: Core names are the ones the libretro buildbot actually publishes. Six of
    #: these said "beetle_*", which is what the cores are called upstream but
    #: not what they are distributed as (`mednafen_*`), and DuckStation's
    #: libretro port is published as `swanstation`. Nothing failed loudly: the
    #: launcher looked for a file that never existed and quietly reported that
    #: no emulator was configured.
    default_core: str = ""
    manufacturer: str = ""
    year: Optional[int] = None
    # Systems whose games are disc images tend to arrive as multi-file rips and
    # benefit from cuesheet handling and m3u playlists.
    disc_based: bool = False

    def matches_rom(self, path: Path) -> bool:
        """Whether a file could plausibly be a ROM for this system.

        A system with no declared extensions matches nothing — it must not
        match everything, or one misconfigured entry swallows the library.
        """
        if not self.rom_extensions:
            return False
        return path.suffix.lower() in self.rom_extensions


@dataclass
class GameEntry:
    """A game discovered by a source, before it enters the library database."""

    id: str
    name: str
    system: str
    path: str
    cover_art: str = ""
    metadata: dict = field(default_factory=dict)
    source: str = ""
    is_steam: bool = False
    is_heroic: bool = False
    is_gog: bool = False

    @property
    def last_played(self) -> Optional[str]:
        return self.metadata.get("last_played")

    @property
    def play_count(self) -> int:
        return self.metadata.get("play_count", 0)


# ── Registry ──────────────────────────────────────────────────────

def _system(
    id: str,
    name: str,
    icon: str,
    extensions: tuple[str, ...],
    core: str = "",
    manufacturer: str = "",
    year: Optional[int] = None,
    disc_based: bool = False,
) -> EmulatorDef:
    return EmulatorDef(
        id=id, name=name, icon=icon, rom_extensions=extensions,
        default_core=core, manufacturer=manufacturer, year=year,
        disc_based=disc_based,
    )


SYSTEMS: dict[str, EmulatorDef] = {
    # ── Nintendo ──────────────────────────────────────────────────
    "nes": _system(
        "nes", "Nintendo Entertainment System", "🎮",
        (".nes", ".unf", ".unif"), "mesen", "Nintendo", 1983,
    ),
    "fds": _system(
        "fds", "Famicom Disk System", "💾",
        (".fds",), "mesen", "Nintendo", 1986,
    ),
    "snes": _system(
        "snes", "Super Nintendo", "🎮",
        (".sfc", ".smc", ".swc", ".fig", ".bs"), "snes9x", "Nintendo", 1990,
    ),
    "n64": _system(
        "n64", "Nintendo 64", "🎮",
        (".n64", ".z64", ".v64", ".ndd"), "mupen64plus_next", "Nintendo", 1996,
    ),
    "gc": _system(
        "gc", "Nintendo GameCube", "💿",
        (".gcm", ".gcz", ".rvz", ".iso", ".ciso"), "dolphin", "Nintendo", 2001,
        disc_based=True,
    ),
    "wii": _system(
        "wii", "Nintendo Wii", "📺",
        (".wbfs", ".rvz", ".wad", ".iso", ".gcz", ".ciso"), "dolphin", "Nintendo", 2006,
        disc_based=True,
    ),
    "wiiu": _system(
        "wiiu", "Nintendo Wii U", "📺",
        (".wud", ".wux", ".wua", ".rpx"), "cemu", "Nintendo", 2012,
        disc_based=True,
    ),
    "switch": _system(
        "switch", "Nintendo Switch", "🎮",
        (".nsp", ".xci", ".nca", ".nro"), "ryujinx", "Nintendo", 2017,
    ),
    "gb": _system(
        "gb", "Game Boy", "🕹️", (".gb",), "gambatte", "Nintendo", 1989,
    ),
    "gbc": _system(
        "gbc", "Game Boy Color", "🕹️", (".gbc",), "gambatte", "Nintendo", 1998,
    ),
    "gba": _system(
        "gba", "Game Boy Advance", "🕹️", (".gba", ".agb"), "mgba", "Nintendo", 2001,
    ),
    "nds": _system(
        "nds", "Nintendo DS", "📱", (".nds", ".dsi", ".ids"), "melonds", "Nintendo", 2004,
    ),
    "3ds": _system(
        "3ds", "Nintendo 3DS", "📱",
        (".3ds", ".3dsx", ".cci", ".cxi", ".cia"), "azahar", "Nintendo", 2011,
    ),
    "virtualboy": _system(
        "virtualboy", "Virtual Boy", "🔴", (".vb", ".vboy"), "mednafen_vb", "Nintendo", 1995,
    ),

    # ── Sony ──────────────────────────────────────────────────────
    "ps1": _system(
        "ps1", "PlayStation", "💿",
        (".cue", ".chd", ".pbp", ".m3u", ".ccd", ".mds", ".exe"),
        "swanstation", "Sony", 1994, disc_based=True,
    ),
    "ps2": _system(
        "ps2", "PlayStation 2", "💿",
        (".iso", ".chd", ".cso", ".gz", ".bin", ".mdf", ".nrg"),
        "pcsx2", "Sony", 2000, disc_based=True,
    ),
    # No .bin/.self/.elf: those are files INSIDE a PS3 game, never a game on
    # their own, and listing them made every shader cache and localisation blob
    # in a dump look like a title. PS3 dumps are folders — see
    # core.folder_games — and the loose-file forms are .iso and .pkg.
    "ps3": _system(
        "ps3", "PlayStation 3", "💿",
        (".iso", ".pkg"), "rpcs3", "Sony", 2006,
        disc_based=True,
    ),
    "ps4": _system(
        "ps4", "PlayStation 4", "🎮", (".pkg", ".elf", ".bin"), "shadps4", "Sony", 2013,
    ),
    "psp": _system(
        "psp", "PlayStation Portable", "🎮",
        # .prx is a PSP shared library, not a game.
        (".iso", ".cso", ".pbp", ".chd", ".elf"), "ppsspp", "Sony", 2004,
    ),
    "psvita": _system(
        "psvita", "PlayStation Vita", "🎮", (".vpk", ".psvimg"), "vita3k", "Sony", 2011,
    ),

    # ── Microsoft ─────────────────────────────────────────────────
    "xbox": _system(
        "xbox", "Xbox", "🎮", (".xbe", ".iso"), "xemu", "Microsoft", 2001,
        disc_based=True,
    ),
    "xbox360": _system(
        "xbox360", "Xbox 360", "🎮", (".iso", ".xex"), "xenia", "Microsoft", 2005,
        disc_based=True,
    ),

    # ── Sega ──────────────────────────────────────────────────────
    "master_system": _system(
        "master_system", "Sega Master System", "🕹️",
        (".sms",), "genesis_plus_gx", "Sega", 1985,
    ),
    "megadrive": _system(
        "megadrive", "Sega Mega Drive / Genesis", "🕹️",
        (".md", ".gen", ".smd", ".bin", ".68k"), "genesis_plus_gx", "Sega", 1988,
    ),
    "segacd": _system(
        "segacd", "Sega CD / Mega CD", "💿",
        (".cue", ".chd", ".iso"), "genesis_plus_gx", "Sega", 1991, disc_based=True,
    ),
    "sega32x": _system(
        "sega32x", "Sega 32X", "🕹️", (".32x",), "picodrive", "Sega", 1994,
    ),
    "saturn": _system(
        "saturn", "Sega Saturn", "💿",
        (".cue", ".chd", ".ccd", ".mds", ".iso"), "mednafen_saturn", "Sega", 1994,
        disc_based=True,
    ),
    "dreamcast": _system(
        "dreamcast", "Sega Dreamcast", "💿",
        (".gdi", ".cdi", ".chd", ".cue"), "flycast", "Sega", 1998, disc_based=True,
    ),
    "gamegear": _system(
        "gamegear", "Sega Game Gear", "🕹️", (".gg",), "genesis_plus_gx", "Sega", 1990,
    ),

    # ── Other ─────────────────────────────────────────────────────
    "atari2600": _system(
        "atari2600", "Atari 2600", "👾", (".a26", ".bin"), "stella", "Atari", 1977,
    ),
    "atari7800": _system(
        "atari7800", "Atari 7800", "👾", (".a78",), "prosystem", "Atari", 1986,
    ),
    "lynx": _system(
        "lynx", "Atari Lynx", "👾", (".lnx", ".o"), "mednafen_lynx", "Atari", 1989,
    ),
    "jaguar": _system(
        "jaguar", "Atari Jaguar", "👾", (".j64", ".jag", ".rom"), "virtualjaguar", "Atari", 1993,
    ),
    "pc_engine": _system(
        "pc_engine", "TurboGrafx-16 / PC Engine", "🕹️",
        (".pce", ".sgx"), "mednafen_pce", "NEC", 1987,
    ),
    "pc_engine_cd": _system(
        "pc_engine_cd", "TurboGrafx-CD / PC Engine CD", "💿",
        (".cue", ".chd", ".ccd"), "mednafen_pce", "NEC", 1988, disc_based=True,
    ),
    "neogeo": _system(
        "neogeo", "Neo Geo", "👾", (".zip", ".7z"), "fbneo", "SNK", 1990,
    ),
    "ngp": _system(
        "ngp", "Neo Geo Pocket", "🕹️", (".ngp", ".ngc"), "mednafen_ngp", "SNK", 1998,
    ),
    "wonderswan": _system(
        "wonderswan", "WonderSwan", "🕹️", (".ws", ".wsc"), "mednafen_wswan", "Bandai", 1999,
    ),
    "3do": _system(
        "3do", "3DO", "💿", (".cue", ".chd", ".iso"), "opera", "Panasonic", 1993,
        disc_based=True,
    ),
    "arcade": _system(
        "arcade", "Arcade (MAME / FinalBurn)", "👾", (".zip", ".7z", ".chd"), "mame", "", None,
    ),
    "msx": _system(
        "msx", "MSX", "🖥️", (".rom", ".mx1", ".mx2", ".dsk", ".cas"), "bluemsx", "", 1983,
    ),
    "c64": _system(
        "c64", "Commodore 64", "🖥️",
        (".d64", ".t64", ".prg", ".crt", ".tap"), "vice_x64", "Commodore", 1982,
    ),
    "amiga": _system(
        "amiga", "Commodore Amiga", "🖥️",
        (".adf", ".ipf", ".lha", ".hdf"), "puae", "Commodore", 1985,
    ),
    "dos": _system(
        "dos", "MS-DOS", "🖥️", (".exe", ".com", ".bat", ".conf"), "dosbox_pure", "", 1981,
    ),
    "scummvm": _system(
        "scummvm", "ScummVM", "🖥️", (".scummvm",), "scummvm", "", None,
    ),

    # ── Native PC games (Steam, GOG, Heroic, custom) ──────────────
    "pc": _system("pc", "PC", "🖥️", (), "", "", None),
}


def get_system(system_id: str) -> Optional[EmulatorDef]:
    return SYSTEMS.get(system_id)


def list_systems() -> list[EmulatorDef]:
    """All systems, ordered for display: manufacturer, then release year."""
    return sorted(
        SYSTEMS.values(),
        key=lambda s: (s.manufacturer or "~", s.year or 9999, s.name),
    )


def systems_for_extension(suffix: str) -> list[EmulatorDef]:
    """Every system a file extension could belong to.

    Returns more than one entry for ambiguous extensions like `.iso` and
    `.bin`; the caller disambiguates by folder, by user choice, or by hash.
    """
    suffix = suffix.lower()
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    return [s for s in SYSTEMS.values() if suffix in s.rom_extensions]


def all_rom_extensions() -> set[str]:
    """Every extension any system recognises — used to pre-filter directory scans."""
    return {ext for system in SYSTEMS.values() for ext in system.rom_extensions}
