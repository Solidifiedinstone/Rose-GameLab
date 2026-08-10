"""Tests for content hashing and ROM header detection."""

from __future__ import annotations

import hashlib
import zlib

from rose_gamelab.core.hashing import (
    detect_header_size,
    hash_file,
    quick_signature,
    should_hash,
)


def write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ── Correctness against the standard library ──────────────────────

def test_hashes_match_reference_implementations(tmp_path):
    data = b"ROM CONTENTS" * 1000
    path = write(tmp_path, "game.gba", data)

    result = hash_file(path)

    assert result.crc32 == f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    assert result.md5 == hashlib.md5(data).hexdigest()
    assert result.sha1 == hashlib.sha1(data).hexdigest()
    assert result.size == len(data)


def test_crc32_is_lowercase_eight_digit_hex(tmp_path):
    """No-Intro indexes CRC32 in this exact form; a mismatch means no lookups."""
    path = write(tmp_path, "game.gba", b"\x00")
    crc = hash_file(path).crc32

    assert len(crc) == 8
    assert crc == crc.lower()


def test_handles_data_larger_than_one_chunk(tmp_path):
    data = bytes(range(256)) * 8192  # 2 MiB, spans multiple reads
    path = write(tmp_path, "big.iso", data)

    assert hash_file(path).sha1 == hashlib.sha1(data).hexdigest()


def test_empty_file_does_not_crash(tmp_path):
    result = hash_file(write(tmp_path, "empty.nes", b""))
    assert result.size == 0


# ── Header detection ──────────────────────────────────────────────

def test_detects_ines_header(tmp_path):
    path = write(tmp_path, "mario.nes", b"NES\x1a" + bytes(12) + b"ROMDATA")
    assert detect_header_size(path, path.read_bytes()) == 16


def test_nes_hashes_exclude_the_header(tmp_path):
    """No-Intro hashes NES ROMs headerless; including it matches nothing."""
    rom = b"ACTUAL ROM DATA" * 100
    path = write(tmp_path, "mario.nes", b"NES\x1a" + bytes(12) + rom)

    result = hash_file(path)

    assert result.had_header
    assert result.header_size == 16
    assert result.sha1 == hashlib.sha1(rom).hexdigest()
    assert result.size == len(rom)


def test_headerless_nes_rom_is_hashed_whole(tmp_path):
    rom = b"NO HEADER HERE" * 100
    path = write(tmp_path, "game.nes", rom)

    result = hash_file(path)

    assert not result.had_header
    assert result.sha1 == hashlib.sha1(rom).hexdigest()


def test_detects_fds_header(tmp_path):
    path = write(tmp_path, "game.fds", b"FDS\x1a" + bytes(12) + b"DATA")
    assert detect_header_size(path, path.read_bytes()) == 16


def test_detects_snes_copier_header_by_size(tmp_path):
    """SNES headers have no magic number: size % 1024 == 512 is the tell."""
    path = write(tmp_path, "game.smc", bytes(512) + bytes(32 * 1024))
    assert detect_header_size(path, path.read_bytes()) == 512


def test_unheadered_snes_rom_is_not_trimmed(tmp_path):
    path = write(tmp_path, "game.sfc", bytes(32 * 1024))
    assert detect_header_size(path, path.read_bytes()) == 0


def test_detects_lynx_header(tmp_path):
    path = write(tmp_path, "game.lnx", b"LYNX" + bytes(60) + b"DATA")
    assert detect_header_size(path, path.read_bytes()) == 64


def test_skip_header_can_be_disabled(tmp_path):
    raw = b"NES\x1a" + bytes(12) + b"ROM"
    path = write(tmp_path, "game.nes", raw)

    result = hash_file(path, skip_header=False)

    assert result.header_size == 0
    assert result.sha1 == hashlib.sha1(raw).hexdigest()


def test_whole_file_hash_always_covers_the_header(tmp_path):
    raw = b"NES\x1a" + bytes(12) + b"ROM"
    path = write(tmp_path, "game.nes", raw)

    result = hash_file(path)

    assert result.whole_file_sha1 == hashlib.sha1(raw).hexdigest()
    assert result.sha1 != result.whole_file_sha1


# ── Progress reporting ────────────────────────────────────────────

def test_progress_callback_reaches_total(tmp_path):
    path = write(tmp_path, "game.iso", b"x" * (3 * 1024 * 1024))

    seen = []
    hash_file(path, progress=lambda done, total: seen.append((done, total)))

    assert seen
    assert seen[-1][0] == seen[-1][1]


# ── Quick signature ───────────────────────────────────────────────

def test_quick_signature_is_stable(tmp_path):
    path = write(tmp_path, "game.iso", b"content" * 5000)
    assert quick_signature(path) == quick_signature(path)


def test_quick_signature_changes_with_content(tmp_path):
    a = write(tmp_path, "a.iso", b"content" * 5000)
    b = write(tmp_path, "b.iso", b"different" * 5000)
    assert quick_signature(a) != quick_signature(b)


def test_quick_signature_detects_a_tail_edit(tmp_path):
    """Same size, same head — only the end differs."""
    a = write(tmp_path, "a.iso", b"x" * 500_000 + b"AAAA")
    b = write(tmp_path, "b.iso", b"x" * 500_000 + b"BBBB")
    assert quick_signature(a) != quick_signature(b)


# ── Size gating ───────────────────────────────────────────────────

def test_small_files_are_worth_hashing(tmp_path):
    assert should_hash(write(tmp_path, "game.sfc", b"x" * 1024))


def test_oversized_files_are_skipped(tmp_path):
    path = write(tmp_path, "huge.iso", b"x" * 2048)
    assert not should_hash(path, max_size=1024)


def test_missing_file_is_not_hashable(tmp_path):
    assert not should_hash(tmp_path / "gone.iso")
