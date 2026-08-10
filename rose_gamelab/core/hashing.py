"""Content hashing for ROM identification.

Games are identified by what they contain, not what they are called. A file
named `smb3(U)[!].nes` and one named `Super Mario Bros 3.nes` with identical
contents are the same game, and a file that gets renamed must not lose its
artwork, playtime or achievements.

Header handling is the part that makes this actually work. The No-Intro and
Redump datasets hash ROM *data*, excluding copier headers that some dumps
carry. Hashing the raw file for a headered dump produces a checksum that
matches nothing. So we detect and skip known header formats, and record both
hashes when a header is present.

Nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import zlib

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Read in 1 MiB blocks: large enough that syscall overhead is negligible,
# small enough to stay cache-friendly and keep progress updates responsive.
CHUNK_SIZE = 1024 * 1024

# Hashing a 40 GB PS3 dump to identify it is not worth the disk read. Above
# this size we hash a sample instead (see hash_file's `sample_large` flag).
LARGE_FILE_THRESHOLD = 2 * 1024 * 1024 * 1024  # 2 GiB


@dataclass(frozen=True)
class FileHashes:
    """Checksums for one file.

    `crc32`, `md5` and `sha1` are of the ROM data proper — with any copier
    header skipped, so they can be looked up directly in No-Intro/Redump.
    `header_size` records how many bytes were skipped (0 when headerless).
    `whole_file_sha1` is the hash including the header, used to detect when a
    file on disk is unchanged so we can skip re-hashing it.
    """

    crc32: str
    md5: str
    sha1: str
    size: int
    header_size: int = 0
    whole_file_sha1: Optional[str] = None

    @property
    def had_header(self) -> bool:
        return self.header_size > 0


# ── Header detection ──────────────────────────────────────────────

def detect_header_size(path: Path, data: bytes) -> int:
    """Return the number of leading bytes that are a copier header, not ROM data.

    `data` is the first chunk of the file, already read.
    """
    suffix = path.suffix.lower()

    # iNES / NES 2.0: 16-byte header beginning with "NES\x1a".
    # No-Intro hashes NES ROMs WITHOUT this header.
    if suffix in (".nes",) and data[:4] == b"NES\x1a":
        return 16

    # FDS: 16-byte header beginning with "FDS\x1a".
    if suffix == ".fds" and data[:4] == b"FDS\x1a":
        return 16

    # SNES copier headers are exactly 512 bytes and have no magic number.
    # The reliable test is size: a headered SNES ROM is 512 bytes more than
    # a power-of-two-ish bank multiple. ROM data is always a multiple of 32 KiB.
    if suffix in (".smc", ".sfc", ".swc", ".fig"):
        size = path.stat().st_size
        if size % 1024 == 512:
            return 512

    # Atari Lynx: 64-byte "LYNX" header.
    if suffix == ".lnx" and data[:4] == b"LYNX":
        return 64

    # Atari 7800: 128-byte header containing "ATARI7800" at offset 1.
    if suffix == ".a78" and data[1:10] == b"ATARI7800":
        return 128

    return 0


# ── Hashing ───────────────────────────────────────────────────────

def hash_file(
    path: str | Path,
    *,
    skip_header: bool = True,
    progress: Optional[Callable[[int, int], None]] = None,
) -> FileHashes:
    """Compute CRC32, MD5 and SHA-1 for a file in a single pass.

    All three are computed together because the disk read dominates the cost:
    CRC32 is what No-Intro indexes on, MD5 and SHA-1 disambiguate collisions
    and are what Redump publishes.

    `progress`, if given, is called as (bytes_done, total_bytes).

    Raises OSError if the file cannot be read — callers should treat that as
    "not yet hashed" rather than "no match".
    """
    path = Path(path)
    total = path.stat().st_size

    crc = 0
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    whole = hashlib.sha1()

    header_size = 0
    done = 0
    first_chunk = True

    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            whole.update(chunk)

            if first_chunk:
                first_chunk = False
                if skip_header:
                    header_size = detect_header_size(path, chunk)
                    chunk = chunk[header_size:]

            crc = zlib.crc32(chunk, crc)
            md5.update(chunk)
            sha1.update(chunk)

            done += len(chunk) + (header_size if done == 0 else 0)
            if progress:
                progress(min(done, total), total)

    return FileHashes(
        # No-Intro writes CRC32 as lowercase 8-digit hex.
        crc32=f"{crc & 0xFFFFFFFF:08x}",
        md5=md5.hexdigest(),
        sha1=sha1.hexdigest(),
        size=total - header_size,
        header_size=header_size,
        whole_file_sha1=whole.hexdigest(),
    )


def quick_signature(path: str | Path) -> str:
    """A cheap identity for a file, for detecting changes without a full read.

    Combines size with hashes of the first and last 64 KiB. Used to decide
    whether a previously-hashed file needs re-hashing after a rescan. This is
    NOT a content hash and must never be used for dataset lookups.
    """
    path = Path(path)
    size = path.stat().st_size
    window = 64 * 1024

    digest = hashlib.sha1()
    digest.update(str(size).encode())

    with path.open("rb") as handle:
        digest.update(handle.read(window))
        if size > window * 2:
            handle.seek(-window, 2)
            digest.update(handle.read(window))

    return digest.hexdigest()


def should_hash(path: str | Path, *, max_size: int = LARGE_FILE_THRESHOLD) -> bool:
    """Whether full hashing is worth the disk read for this file.

    Disc-based dumps for modern systems run to tens of gigabytes and are not
    present in No-Intro anyway (Redump indexes the disc, not the loose file),
    so hashing them costs minutes and buys nothing.
    """
    try:
        return Path(path).stat().st_size <= max_size
    except OSError:
        return False
