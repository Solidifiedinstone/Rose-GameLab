"""Folder-based games: PS3 and friends.

The bug these exist to prevent: a PS3 collection of forty games importing as
three hundred entries, because every shader cache and localisation blob inside
each game folder has a "ROM extension".
"""

from __future__ import annotations

import struct

import pytest

from rose_gamelab.core import folder_games
from rose_gamelab.core.folder_games import (
    GAMECUBE_MAGIC,
    WII_MAGIC,
    detect,
    game_root_for,
    read_param_sfo,
)


def make_file(path, data: bytes = b"\x00" * 16):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ── PS3 ───────────────────────────────────────────────────────────

def ps3_disc_folder(root, name="Demon's Souls (USA)"):
    """A JB folder / disc dump, as every real PS3 release is laid out."""
    game = root / name
    make_file(game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")
    make_file(game / "PS3_DISC.SFB")
    # The debris that used to become games.
    for junk in ("COALESCED_INT.bin", "GLOBALSHADERCACHE-PS3.bin", "audiof.bin"):
        make_file(game / "PS3_GAME" / "USRDIR" / junk)
    return game


def test_ps3_disc_folder_is_one_game(tmp_path):
    game = ps3_disc_folder(tmp_path)

    found = detect(game)

    assert found is not None
    assert found.system_id == "ps3"
    assert found.title == "Demon's Souls (USA)"
    assert found.entry == game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"


def test_ps3_installed_layout_is_recognised(tmp_path):
    """RPCS3 installs titles as dev_hdd0/game/<TITLEID>/USRDIR/EBOOT.BIN."""
    game = tmp_path / "BLUS30443"
    make_file(game / "USRDIR" / "EBOOT.BIN")
    make_file(game / "PARAM.SFO")

    found = detect(game)

    assert found is not None
    assert found.system_id == "ps3"


def test_internal_directories_are_not_games(tmp_path):
    """PS3_GAME holds USRDIR/EBOOT.BIN, which is the installed-game marker."""
    game = ps3_disc_folder(tmp_path)

    assert detect(game / "PS3_GAME") is None
    assert detect(game / "PS3_GAME" / "USRDIR") is None


def test_walking_up_finds_the_game_not_its_innards(tmp_path):
    game = ps3_disc_folder(tmp_path)

    for inside in (
        game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN",
        game / "PS3_GAME" / "USRDIR" / "COALESCED_INT.bin",
        game / "PS3_GAME",
    ):
        found = game_root_for(inside)
        assert found is not None, inside
        assert found.root == game


def test_a_folder_that_is_not_a_game_is_not_claimed(tmp_path):
    plain = tmp_path / "Screenshots"
    make_file(plain / "shot.png")

    assert detect(plain) is None
    assert game_root_for(plain) is None


def test_incomplete_dump_is_not_claimed(tmp_path):
    """Marker present, launch file missing: report nothing rather than guess."""
    game = tmp_path / "Broken (USA)"
    (game / "PS3_GAME" / "USRDIR").mkdir(parents=True)

    assert detect(game) is None


@pytest.mark.parametrize("marker, entry", [
    ("ps3_game/usrdir/eboot.bin", "ps3_game/usrdir/eboot.bin"),
    ("PS3_GAME/USRDIR/EBOOT.bin", "PS3_GAME/USRDIR/EBOOT.bin"),
    ("Ps3_Game/UsrDir/Eboot.Bin", "Ps3_Game/UsrDir/Eboot.Bin"),
])
def test_ps3_folders_are_recognised_whatever_the_case(tmp_path, marker, entry):
    """Dumps disagree about case, and Linux is the only OS that notices.

    A folder copied through Windows, extracted by a different tool, or renamed
    by its uploader arrives in any of these forms — and rejecting them is why
    PS3 games so often imported as nothing at all.
    """
    game = tmp_path / "Demon's Souls (USA)"
    make_file(game / marker)

    found = detect(game)

    assert found is not None
    assert found.system_id == "ps3"
    assert found.entry == game / entry


def test_ps3_disc_without_an_eboot_launches_from_the_folder(tmp_path):
    """PS3_DISC.SFB only sits at the top of a PS3 disc, so this is certain.

    RPCS3 takes the directory. Importing it beats dropping a game the user can
    plainly see is there.
    """
    game = tmp_path / "Ni no Kuni (USA)"
    make_file(game / "PS3_DISC.SFB")
    make_file(game / "PS3_GAME" / "PARAM.SFO")

    found = detect(game)

    assert found is not None
    assert found.system_id == "ps3"
    assert found.entry == game


def test_a_complete_dump_prefers_the_eboot_over_the_folder(tmp_path):
    """The fallback must never win where there is a real file to launch."""
    game = ps3_disc_folder(tmp_path)

    assert detect(game).entry == game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"


def test_internal_directories_are_not_games_whatever_the_case(tmp_path):
    game = tmp_path / "Game"
    make_file(game / "ps3_game" / "usrdir" / "eboot.bin")

    assert detect(game) is not None
    assert detect(game / "ps3_game") is None
    assert detect(game / "ps3_game" / "usrdir") is None


# ── Other folder systems ──────────────────────────────────────────

def test_psp_extracted_umd(tmp_path):
    game = tmp_path / "Daxter"
    make_file(game / "PSP_GAME" / "SYSDIR" / "EBOOT.BIN")

    found = detect(game)

    assert found is not None and found.system_id == "psp"


def test_vita_app_folder(tmp_path):
    game = tmp_path / "PCSE00001"
    make_file(game / "sce_sys" / "param.sfo")
    make_file(game / "eboot.bin")

    found = detect(game)

    assert found is not None and found.system_id == "psvita"


def test_wiiu_folder_launches_the_rpx(tmp_path):
    game = tmp_path / "Splatoon"
    make_file(game / "meta" / "meta.xml")
    make_file(game / "code" / "Splatoon.rpx")
    make_file(game / "content" / "data.bin")

    found = detect(game)

    assert found is not None and found.system_id == "wiiu"
    assert found.entry.name == "Splatoon.rpx"


def test_xbox360_folder(tmp_path):
    game = tmp_path / "Halo 3"
    make_file(game / "default.xex")

    found = detect(game)

    assert found is not None and found.system_id == "xbox360"


def extracted_disc(root, name, magic, offset):
    """An extracted GameCube/Wii disc, whose magic word names the platform."""
    game = root / name
    header = bytearray(0x20)
    struct.pack_into(">I", header, offset, magic)
    make_file(game / "sys" / "boot.bin", bytes(header))
    make_file(game / "sys" / "main.dol")
    (game / "files").mkdir()
    return game


def test_extracted_wii_and_gamecube_are_told_apart(tmp_path):
    wii = extracted_disc(tmp_path, "Twilight Princess", WII_MAGIC, 0x18)
    gamecube = extracted_disc(tmp_path, "Wind Waker", GAMECUBE_MAGIC, 0x1C)

    assert detect(wii).system_id == "wii"
    assert detect(gamecube).system_id == "gc"


def test_every_layout_names_a_real_system():
    """A layout filed under an id no system has can never find an emulator."""
    from rose_gamelab.core.emulator import SYSTEMS

    unknown = [
        layout.system_id for layout in folder_games.LAYOUTS
        if layout.system_id not in SYSTEMS
    ]
    assert unknown == []


def test_extracted_disc_without_known_magic_is_not_guessed(tmp_path):
    """Neither magic word: better unrecognised than filed under the wrong one."""
    game = extracted_disc(tmp_path, "Mystery", 0xDEADBEEF, 0x18)

    assert detect(game) is None


# ── PARAM.SFO ─────────────────────────────────────────────────────

def build_sfo(values: dict[str, object]) -> bytes:
    """Assemble a PARAM.SFO, so the reader is tested against the real format."""
    keys, data = b"", b""
    entries = b""

    for key, value in values.items():
        key_offset = len(keys)
        keys += key.encode() + b"\x00"

        if isinstance(value, int):
            fmt, blob = 0x0404, struct.pack("<I", value)
        else:
            fmt, blob = 0x0204, value.encode() + b"\x00"

        entries += struct.pack(
            "<HHIII", key_offset, fmt, len(blob), len(blob), len(data)
        )
        data += blob

    header_size = 20 + len(entries)
    key_table = header_size
    data_table = key_table + len(keys)

    # The version field sits between the magic and the table offsets.
    header = (
        folder_games.SFO_MAGIC
        + struct.pack("<I", 0x0101)
        + struct.pack("<III", key_table, data_table, len(values))
    )
    return header + entries + keys + data


def test_param_sfo_is_read(tmp_path):
    sfo = tmp_path / "PARAM.SFO"
    sfo.write_bytes(build_sfo({
        "TITLE": "Demon's Souls",
        "TITLE_ID": "BLUS30443",
        "PARENTAL_LEVEL": 5,
    }))

    values = read_param_sfo(sfo)

    assert values["TITLE"] == "Demon's Souls"
    assert values["TITLE_ID"] == "BLUS30443"
    assert values["PARENTAL_LEVEL"] == 5


@pytest.mark.parametrize("data", [b"", b"not an sfo at all", b"\x00PSF"])
def test_unreadable_param_sfo_returns_nothing(tmp_path, data):
    """Metadata is a bonus; a corrupt file must never stop an import."""
    sfo = tmp_path / "PARAM.SFO"
    sfo.write_bytes(data)

    assert read_param_sfo(sfo) == {}


def test_missing_param_sfo_returns_nothing(tmp_path):
    assert read_param_sfo(tmp_path / "nope.sfo") == {}


def test_title_id_is_found_for_a_ps3_folder(tmp_path):
    game = ps3_disc_folder(tmp_path)
    (game / "PS3_GAME" / "PARAM.SFO").write_bytes(
        build_sfo({"TITLE_ID": "BLUS30443"})
    )

    assert folder_games.title_id_for(detect(game)) == "BLUS30443"


# ── Artwork inside the dump ───────────────────────────────────────
#
# The one art source that cannot miss. Not a lookup, a guess or a name match —
# the publisher's own image, shipped in the game, already on disk. The archives
# carry a few dozen PS3 titles between them; this covers every dump there is.

def test_a_ps3_dump_carries_its_own_cover(tmp_path):
    game = ps3_disc_folder(tmp_path)
    make_file(game / "PS3_GAME" / "ICON0.PNG", b"\x89PNG\r\n\x1a\n")

    found = folder_games.artwork_in(detect(game))

    assert found == game / "PS3_GAME" / "ICON0.PNG"


def test_the_background_is_found_separately(tmp_path):
    game = ps3_disc_folder(tmp_path)
    make_file(game / "PS3_GAME" / "PIC1.PNG", b"\x89PNG\r\n\x1a\n")

    assert folder_games.artwork_in(detect(game), "hero").name == "PIC1.PNG"


def test_internal_artwork_is_found_whatever_the_case(tmp_path):
    """Dumps disagree about case here exactly as they do everywhere else."""
    game = tmp_path / "Game"
    make_file(game / "ps3_game" / "usrdir" / "eboot.bin")
    make_file(game / "ps3_game" / "icon0.png", b"\x89PNG\r\n\x1a\n")

    assert folder_games.artwork_in(detect(game)) is not None


def test_a_dump_without_internal_art_reports_nothing(tmp_path):
    game = ps3_disc_folder(tmp_path)
    assert folder_games.artwork_in(detect(game)) is None


def test_psp_and_vita_carry_theirs_too(tmp_path):
    psp = tmp_path / "Daxter"
    make_file(psp / "PSP_GAME" / "SYSDIR" / "EBOOT.BIN")
    make_file(psp / "PSP_GAME" / "ICON0.PNG", b"\x89PNG\r\n\x1a\n")
    assert folder_games.artwork_in(detect(psp)) is not None

    vita = tmp_path / "PCSE00001"
    make_file(vita / "sce_sys" / "param.sfo")
    make_file(vita / "eboot.bin")
    make_file(vita / "sce_sys" / "icon0.png", b"\x89PNG\r\n\x1a\n")
    assert folder_games.artwork_in(detect(vita)) is not None


def test_a_system_with_no_internal_art_is_not_guessed_at(tmp_path):
    game = tmp_path / "Halo 3"
    make_file(game / "default.xex")

    assert folder_games.artwork_in(detect(game)) is None
