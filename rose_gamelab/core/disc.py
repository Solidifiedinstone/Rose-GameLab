"""Ripping physical discs into images, and burning DRM-free images back.

A large part of a Linux game collection arrives on plastic: PlayStation, Saturn,
Sega CD and PC Engine CD discs the user owns, and GOG installers they want a
physical backup of. Emulators want those discs as files, and a shelf of discs
that GameLab cannot see is a shelf of games the user has to remember on their
own. So: rip a disc to an image the library can index, and burn an image the
user already owns back to a blank.

Everything here is a thin, honest wrapper around external tools. GameLab does
not implement CD reading itself — cdrdao, ddrescue and libburn have decades of
drive-quirk handling in them that this project will not reproduce. What GameLab
adds is: finding the drive, telling the user precisely which tool is missing and
how to install it, converting cdrdao's TOC into the .cue emulators actually
want, reporting progress the tools genuinely emitted, and hashing the result so
a ripped disc lands in the library like any other file.

On format: CDs are ripped to a .bin/.cue pair, not .iso. An .iso of a PlayStation
disc loses the audio tracks and the raw 2352-byte sectors, and most emulators
will either refuse it or play it without music. Data-only DVDs are ripped to
.iso, which is the correct container for them.

On burning and DRM: this module will not detect DRM, because DRM detection
cannot be done reliably and a wrong answer in either direction is worse than no
answer. There is no copy-protection circumvention here of any kind — the burn
path takes an image file the caller names explicitly, and writes it. It is for
the user's own DRM-free images: their GOG installers, their own rips, their
homebrew. Deciding whether an image is theirs to burn is the user's call, and
the interface should say so.

On progress: every percentage in this module came out of a tool's own output or
off the size of the file being written. Nothing is interpolated, nothing is
timed, and a stage that cannot report progress reports `percent=None` rather
than a number that moves to look busy.

Nothing here touches the network.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import selectors
import shutil
import subprocess
import threading

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from rose_gamelab.core.hashing import FileHashes, hash_file

logger = logging.getLogger(__name__)

ProgressCallback = Callable[["DiscProgress"], None]

# ── Kernel constants ──────────────────────────────────────────────
# From include/uapi/linux/cdrom.h. Used via ioctl on an O_NONBLOCK fd, which
# is the only way to ask "is there a disc in the drive" — /proc/sys/dev/cdrom/
# info reports the drive's capabilities, never the media currently loaded.
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327

_DRIVE_STATUS = {
    0: "unknown",
    1: "no disc",
    2: "tray open",
    3: "not ready",
    4: "disc loaded",
}

_DISC_TYPE = {
    100: "audio",
    101: "data",
    102: "data",
    103: "xa",
    104: "xa",
    105: "mixed",
}

PROC_CDROM_INFO = Path("/proc/sys/dev/cdrom/info")

# A CD sector as read raw, and as seen by a filesystem. Both are needed: the
# raw size is what a .bin holds, the cooked size is what an .iso holds.
RAW_SECTOR_BYTES = 2352
DATA_SECTOR_BYTES = 2048
FRAMES_PER_SECOND = 75  # CD MSF timing


# ── Errors ────────────────────────────────────────────────────────

class DiscError(Exception):
    """Raised when a disc operation cannot proceed.

    The message is shown to the user, so it must say what is wrong and what to
    do about it — never just the tool's exit code.
    """


class MissingToolError(DiscError):
    """A required external tool is not installed.

    Carries the tool name and package so the interface can offer a real install
    instruction instead of 'operation failed'.
    """

    def __init__(self, tool: "ToolSpec") -> None:
        super().__init__(tool.install_hint())
        self.tool = tool.name
        self.packages = tool.packages


class DiscCancelled(DiscError):
    """The user stopped the operation. Partial output is left on disk."""


# ── External tools ────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolSpec:
    """One external program this module can drive.

    `packages` maps a package manager to the package that provides the binary,
    so the message a user sees names something they can actually install. The
    Arch/Artix entries were checked against this machine's repositories; the
    others are from each distribution's published package names and have not
    been verified here.
    """

    name: str
    purpose: str
    packages: dict[str, str] = field(default_factory=dict)

    def path(self) -> Optional[str]:
        return shutil.which(self.name)

    @property
    def available(self) -> bool:
        return self.path() is not None

    def install_hint(self) -> str:
        if not self.packages:
            return f"{self.name} is not installed, and it is needed to {self.purpose}."

        lines = [f"{self.name} is not installed, and it is needed to {self.purpose}.", "Install it with:"]
        for manager, package in self.packages.items():
            lines.append(f"  {manager}: {package}")
        return "\n".join(lines)


TOOLS: dict[str, ToolSpec] = {
    "cdrdao": ToolSpec(
        "cdrdao",
        "rip a CD to a .bin/.cue pair with its audio tracks intact",
        {"pacman": "pacman -S cdrdao", "apt": "apt install cdrdao", "dnf": "dnf install cdrdao"},
    ),
    "cdrskin": ToolSpec(
        "cdrskin",
        "burn an image to a CD or DVD",
        {"pacman": "pacman -S libburn", "apt": "apt install cdrskin", "dnf": "dnf install cdrskin"},
    ),
    "wodim": ToolSpec(
        "wodim",
        "burn an image to a CD (alternative to cdrskin)",
        {"pacman": "pacman -S cdrtools", "apt": "apt install wodim", "dnf": "dnf install wodim"},
    ),
    "ddrescue": ToolSpec(
        "ddrescue",
        "rip a data disc to .iso, retrying scratched sectors instead of aborting",
        {"pacman": "pacman -S ddrescue", "apt": "apt install gddrescue", "dnf": "dnf install ddrescue"},
    ),
    "dd": ToolSpec(
        "dd",
        "rip a data disc to .iso (fallback; gives up on the first bad sector)",
        {"pacman": "pacman -S coreutils", "apt": "apt install coreutils", "dnf": "dnf install coreutils"},
    ),
    "growisofs": ToolSpec(
        "growisofs",
        "burn an image to a DVD",
        {"pacman": "pacman -S dvd+rw-tools", "apt": "apt install dvd+rw-tools", "dnf": "dnf install dvd+rw-tools"},
    ),
    "cdparanoia": ToolSpec(
        "cdparanoia",
        "rip audio tracks with error correction",
        {"pacman": "pacman -S cdparanoia", "apt": "apt install cdparanoia", "dnf": "dnf install cdparanoia"},
    ),
}


@dataclass(frozen=True)
class ToolStatus:
    """Whether one tool is present, and what to do if it is not."""

    name: str
    purpose: str
    path: Optional[str]
    packages: dict[str, str]

    @property
    def available(self) -> bool:
        return self.path is not None

    @property
    def install_hint(self) -> str:
        return TOOLS[self.name].install_hint()


def tool_status(names: Optional[Sequence[str]] = None) -> list[ToolStatus]:
    """Report which external tools are installed, in registry order.

    Reported for every tool whether present or not, so the interface can show a
    complete picture rather than only complaining at the moment of failure.
    """
    wanted = list(names) if names else list(TOOLS)
    result = []
    for name in wanted:
        spec = TOOLS[name]
        result.append(ToolStatus(spec.name, spec.purpose, spec.path(), dict(spec.packages)))
    return result


def require_tool(name: str) -> str:
    """Return the path to a tool, or raise a MissingToolError naming the package."""
    spec = TOOLS[name]
    path = spec.path()
    if path is None:
        raise MissingToolError(spec)
    return path


def first_available(*names: str) -> Optional[str]:
    """The first of several interchangeable tools that is installed."""
    for name in names:
        path = TOOLS[name].path()
        if path is not None:
            return name
    return None


def require_any(*names: str) -> str:
    """The first installed tool of an interchangeable set, or an error listing all."""
    found = first_available(*names)
    if found:
        return found

    hints = "\n\n".join(TOOLS[name].install_hint() for name in names)
    raise DiscError(
        "None of the tools that can do this are installed "
        f"({', '.join(names)}). Install any one of them:\n\n{hints}"
    )


# ── Drives ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OpticalDrive:
    """One optical drive, its capabilities, and what is currently in it.

    Capabilities come from the kernel's own report and describe the *drive*.
    `status` and `disc_type` describe the *media*, and are None/"unknown" when
    the drive could not be opened — a drive another process is using, or one the
    user has no permission on, is reported honestly rather than as empty.
    """

    path: Path
    name: str
    capabilities: dict[str, bool] = field(default_factory=dict)
    speed: Optional[int] = None
    status: str = "unknown"
    disc_type: Optional[str] = None
    size_bytes: Optional[int] = None
    #: Set when the drive exists but could not be queried, with the reason.
    probe_error: Optional[str] = None

    @property
    def has_disc(self) -> bool:
        return self.status == "disc loaded"

    @property
    def can_write_cd(self) -> bool:
        return bool(self.capabilities.get("can write cd-r") or self.capabilities.get("can write cd-rw"))

    @property
    def can_write_dvd(self) -> bool:
        return bool(self.capabilities.get("can write dvd-r") or self.capabilities.get("can write dvd-ram"))

    @property
    def can_read_dvd(self) -> bool:
        return bool(self.capabilities.get("can read dvd"))

    @property
    def can_write(self) -> bool:
        return self.can_write_cd or self.can_write_dvd


def parse_cdrom_info(text: str) -> list[dict[str, object]]:
    """Parse /proc/sys/dev/cdrom/info into one dict per drive.

    The file is a table transposed on its side: every line is `label:` followed
    by one tab-separated value per drive, in the same column order as the
    `drive name:` line. Note the kernel lists drives in reverse registration
    order, so column 0 is not necessarily sr0 — the name column is the only
    thing that says which device a column belongs to.

    Values that are plainly 0/1 become booleans; numeric ones stay integers.
    Unknown labels are kept verbatim so a newer kernel adding a row does not
    make this return less than it could.
    """
    rows: dict[str, list[str]] = {}

    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, rest = line.partition(":")
        label = label.strip().lower()
        if not label or label.startswith("cd-rom information"):
            continue
        rows[label] = rest.split()

    names = rows.pop("drive name", [])
    if not names:
        return []

    drives: list[dict[str, object]] = []

    for index, name in enumerate(names):
        entry: dict[str, object] = {"drive name": name}
        for label, values in rows.items():
            if index >= len(values):
                # A short row means the kernel reported fewer columns than
                # drives. Leave the key absent rather than guessing a value.
                continue
            raw = values[index]
            if label.startswith(("can ", "reports ")):
                entry[label] = raw == "1"
            elif raw.lstrip("-").isdigit():
                entry[label] = int(raw)
            else:
                entry[label] = raw
        drives.append(entry)

    return drives


def probe_media(device: Path) -> tuple[str, Optional[str], Optional[int], Optional[str]]:
    """Ask the drive what media it has: (status, disc type, size, error).

    Opened O_NONBLOCK so this returns immediately on an empty drive instead of
    blocking until a disc is inserted. The size comes from seeking to the end of
    the device, which is the readable extent of the current disc.
    """
    try:
        fd = os.open(str(device), os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        return "unknown", None, None, f"cannot open {device}: {exc.strerror or exc}"

    try:
        try:
            status = _DRIVE_STATUS.get(fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0), "unknown")
        except OSError as exc:
            return "unknown", None, None, f"drive status unavailable: {exc.strerror or exc}"

        if status != "disc loaded":
            return status, None, None, None

        disc_type: Optional[str] = None
        try:
            disc_type = _DISC_TYPE.get(fcntl.ioctl(fd, CDROM_DISC_STATUS, 0))
        except OSError as exc:
            # Audio-only and some mixed-mode discs make this fail on certain
            # drives. The disc is still readable, so this is not fatal.
            logger.debug("disc status unavailable for %s: %s", device, exc)

        size: Optional[int] = None
        try:
            size = os.lseek(fd, 0, os.SEEK_END) or None
        except OSError as exc:
            logger.debug("cannot size %s: %s", device, exc)

        return status, disc_type, size, None
    finally:
        os.close(fd)


def detect_drives(
    *,
    proc_info: Path = PROC_CDROM_INFO,
    dev_dir: Path = Path("/dev"),
    probe: bool = True,
) -> list[OpticalDrive]:
    """Every optical drive on this machine.

    Returns an empty list when there is no drive, which on a modern desktop is
    the normal case — callers must treat that as "this machine cannot rip", not
    as an error. /proc/sys/dev/cdrom/info is preferred because it carries the
    drive's capabilities; /dev/sr* is a fallback for the case where the file is
    absent but a device node exists.

    `probe=False` skips the ioctl, for callers that only want the drive list and
    do not want to touch the hardware.
    """
    entries: list[dict[str, object]] = []

    try:
        entries = parse_cdrom_info(proc_info.read_text())
    except FileNotFoundError:
        # No CD-ROM driver is loaded. Either there is no drive, or sr_mod is
        # not loaded yet. Fall through to the device nodes.
        pass
    except OSError as exc:
        logger.warning("cannot read %s: %s", proc_info, exc)

    known = {str(entry.get("drive name", "")) for entry in entries}
    for node in sorted(dev_dir.glob("sr[0-9]*")):
        if node.name not in known:
            entries.append({"drive name": node.name})

    drives: list[OpticalDrive] = []

    for entry in entries:
        name = str(entry.get("drive name", ""))
        if not name:
            continue

        device = dev_dir / name
        capabilities = {k: v for k, v in entry.items() if isinstance(v, bool)}
        speed = entry.get("drive speed")

        status, disc_type, size, error = ("unknown", None, None, None)
        if probe:
            status, disc_type, size, error = probe_media(device)

        drives.append(
            OpticalDrive(
                path=device,
                name=name,
                capabilities=capabilities,
                speed=speed if isinstance(speed, int) else None,
                status=status,
                disc_type=disc_type,
                size_bytes=size,
                probe_error=error,
            )
        )

    return drives


def default_drive() -> Optional[OpticalDrive]:
    """The drive to use when the user has not chosen one: first with a disc,
    otherwise the first drive, otherwise None."""
    drives = detect_drives()
    for drive in drives:
        if drive.has_disc:
            return drive
    return drives[0] if drives else None


# ── Progress ──────────────────────────────────────────────────────

@dataclass
class DiscProgress:
    """One progress update, carrying only what the tool actually reported.

    `percent` is None whenever the tool has not given a figure. It is never
    filled in from elapsed time or from an estimate — a stalled rip must look
    stalled, because that is the moment the user needs to know.
    """

    stage: str
    message: str = ""
    percent: Optional[float] = None
    bytes_done: Optional[int] = None
    bytes_total: Optional[int] = None
    #: Raised by the ripper when the tool reported unreadable sectors.
    read_errors: Optional[int] = None


_SIZE_UNITS = {
    "B": 1,
    "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
    "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
    # ddrescue writes "KB"/"kB" interchangeably across versions.
    "KB": 1000,
}

_DDRESCUE_PCT = re.compile(r"pct\s+rescued:\s*([\d.]+)\s*%", re.I)
_DDRESCUE_RESCUED = re.compile(r"\brescued:\s*([\d.]+)\s*([kKMGT]i?B|B)\b")
_DDRESCUE_ERRORS = re.compile(r"read\s+errors:\s*(\d+)", re.I)


def _to_bytes(value: str, unit: str) -> int:
    return int(float(value) * _SIZE_UNITS.get(unit, 1))


def parse_ddrescue_progress(line: str) -> Optional[DiscProgress]:
    """Parse one line of GNU ddrescue's status block.

    ddrescue redraws a multi-line status block in place, so a caller must split
    its output on \\r as well as \\n. The two fields that matter are
    `pct rescued` and `rescued`; both are genuine measurements of bytes copied.

    NOT verified against a live run: ddrescue is not installed on the machine
    this was written on. The field names are from ddrescue 1.2x/1.30's
    documented status output.
    """
    percent = None
    done = None
    errors = None

    match = _DDRESCUE_PCT.search(line)
    if match:
        percent = float(match.group(1))

    match = _DDRESCUE_RESCUED.search(line)
    if match:
        done = _to_bytes(match.group(1), match.group(2))

    match = _DDRESCUE_ERRORS.search(line)
    if match:
        errors = int(match.group(1))

    if percent is None and done is None and errors is None:
        return None

    return DiscProgress(
        stage="ripping",
        message=line.strip(),
        percent=percent,
        bytes_done=done,
        read_errors=errors,
    )


# cdrskin/cdrecord assemble their status line from these format strings, read
# straight out of the installed /usr/bin/cdrskin binary (libburn 1.5.8):
#     "Track %-2.2d: %s MB written %s[buf %3d%%]  %4.1fx."   with "%4d of %4d"
# producing, for real:
#     Track 01:    5 of  650 MB written (fifo 100%) [buf  99%]   8.0x.
# and, when the total is not known in advance:
#     Track 01:    5 MB written [buf  99%]   8.0x.
_CDRSKIN_OF = re.compile(r"Track\s+(\d+):\s+(\d+)\s+of\s+(\d+)\s+MB written", re.I)
_CDRSKIN_PLAIN = re.compile(r"Track\s+(\d+):\s+(\d+)\s+MB written", re.I)


def parse_cdrskin_progress(line: str) -> Optional[DiscProgress]:
    """Parse a cdrskin/cdrecord/wodim burn status line.

    Format verified against the format strings in this machine's cdrskin binary.
    The percentage is the exact ratio of the two figures the tool printed, so it
    is unit-independent. The byte counts assume cdrecord's traditional
    MB = 1 MiB; that convention is not stated in the tool's output, so treat the
    byte figures as approximate and the percentage as exact.
    """
    match = _CDRSKIN_OF.search(line)
    if match:
        done_mb, total_mb = int(match.group(2)), int(match.group(3))
        percent = (done_mb / total_mb * 100) if total_mb else None
        return DiscProgress(
            stage="burning",
            message=line.strip(),
            percent=percent,
            bytes_done=done_mb * 1024 * 1024,
            bytes_total=total_mb * 1024 * 1024,
        )

    match = _CDRSKIN_PLAIN.search(line)
    if match:
        # No total: the tool is writing a stream of unknown length. Report the
        # bytes it has written and no percentage, rather than inventing one.
        return DiscProgress(
            stage="burning",
            message=line.strip(),
            percent=None,
            bytes_done=int(match.group(2)) * 1024 * 1024,
        )

    return None


# growisofs (dvd+rw-tools) prints an exact byte pair plus a percentage:
#     4784128/681574400 ( 0.7%) @0.6x, remaining 21:33 RBU 100.0% UBU  12.5%
# and, while formatting/finalising:
#     1.23% done, estimate finish Mon Jan  1 12:00:00 2024
# NOT verified on this machine: growisofs is not installed here.
_GROWISOFS_BYTES = re.compile(r"(\d+)/(\d+)\s*\(\s*([\d.]+)%\s*\)")
_GROWISOFS_PCT = re.compile(r"([\d.]+)%\s+done")


def parse_growisofs_progress(line: str) -> Optional[DiscProgress]:
    """Parse a growisofs DVD burn status line."""
    match = _GROWISOFS_BYTES.search(line)
    if match:
        return DiscProgress(
            stage="burning",
            message=line.strip(),
            percent=float(match.group(3)),
            bytes_done=int(match.group(1)),
            bytes_total=int(match.group(2)),
        )

    match = _GROWISOFS_PCT.search(line)
    if match:
        return DiscProgress(stage="burning", message=line.strip(), percent=float(match.group(1)))

    return None


# cdrdao's plain-text progress. UNVERIFIED — cdrdao is not installed on the
# machine this was written on and its output could not be captured. These
# patterns cover the stage lines cdrdao is documented to print; the authoritative
# progress for a cdrdao rip comes from the growing size of its output file
# (see _FileSizeProgress), which is a real measurement and not an estimate.
_CDRDAO_TRACK = re.compile(r"(?:Reading|Analyzing|Copying)\s+track\s+(\d+)", re.I)
_CDRDAO_PCT = re.compile(r"^\s*(\d{1,3})\s*%")


def parse_cdrdao_progress(line: str) -> Optional[DiscProgress]:
    """Parse a cdrdao read-cd status line.

    Returns a stage message for the track lines, and a percentage only when
    cdrdao actually printed one. See the note above: this parser is not verified
    against a live cdrdao, so callers should not depend on it for the percentage.
    """
    match = _CDRDAO_TRACK.search(line)
    if match:
        return DiscProgress(stage="ripping", message=f"Track {int(match.group(1))}: {line.strip()}")

    match = _CDRDAO_PCT.match(line)
    if match:
        value = int(match.group(1))
        if value <= 100:
            return DiscProgress(stage="ripping", message=line.strip(), percent=float(value))

    return None


# cdparanoia's machine-readable progress, enabled by -e. The format string
# "##: %d [%s] @ %ld" was read out of this machine's /usr/bin/cdparanoia
# (release 10.2). The position is the paranoia callback's sample index — 588
# stereo samples per CD sector — so bytes = position * 4.
_CDPARANOIA = re.compile(r"^##:\s*(-?\d+)\s*\[([^\]]*)\]\s*@\s*(-?\d+)")

SAMPLES_PER_SECTOR = 588
BYTES_PER_SAMPLE = 4  # 16-bit stereo


def parse_cdparanoia_progress(line: str, *, total_sectors: Optional[int] = None) -> Optional[DiscProgress]:
    """Parse one cdparanoia `-e` progress line.

    A percentage is produced only when `total_sectors` is supplied by the
    caller from the disc TOC — cdparanoia's progress line carries a position but
    never a total, so without one there is nothing honest to divide by.
    """
    match = _CDPARANOIA.match(line.strip())
    if not match:
        return None

    function, label, position = int(match.group(1)), match.group(2), int(match.group(3))

    if position < 0:
        # Negative positions are status markers, not offsets.
        return DiscProgress(stage="ripping", message=label)

    done = position * BYTES_PER_SAMPLE
    total = None
    percent = None
    if total_sectors:
        total = total_sectors * SAMPLES_PER_SECTOR * BYTES_PER_SAMPLE
        percent = min(100.0, done / total * 100) if total else None

    return DiscProgress(
        stage="ripping",
        message=f"{label} (fn {function})",
        percent=percent,
        bytes_done=done,
        bytes_total=total,
    )


# ── cdrdao TOC to .cue ────────────────────────────────────────────

_TOC_MODES = {
    "AUDIO": "AUDIO",
    "MODE1": "MODE1/2048",
    "MODE1_RAW": "MODE1/2352",
    "MODE2": "MODE2/2336",
    "MODE2_RAW": "MODE2/2352",
    "MODE2_FORM1": "MODE2/2048",
    "MODE2_FORM2": "MODE2/2324",
    "MODE2_FORM_MIX": "MODE2/2336",
}


def parse_msf(text: str) -> int:
    """Convert an MM:SS:FF timecode to a frame (sector) count.

    Raises ValueError on anything that is not a timecode, so a malformed TOC is
    a loud failure rather than a silently wrong cue sheet.
    """
    parts = text.strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not an MSF timecode: {text!r}")
    minutes, seconds, frames = (int(p) for p in parts)
    return (minutes * 60 + seconds) * FRAMES_PER_SECOND + frames


def format_msf(frames: int) -> str:
    """Convert a frame count back to the MM:SS:FF a cue sheet wants."""
    minutes, rest = divmod(frames, 60 * FRAMES_PER_SECOND)
    seconds, frame = divmod(rest, FRAMES_PER_SECOND)
    return f"{minutes:02d}:{seconds:02d}:{frame:02d}"


def toc_to_cue(toc_text: str, binary_name: str) -> str:
    """Convert a cdrdao .toc into the .cue sheet emulators expect.

    cdrdao writes a .toc; DuckStation, RetroArch's Beetle/SwanStation cores,
    Flycast and Mednafen all want a .cue. cdrdao ships `toc2cue` to do this, but
    requiring a second binary for a text transform is not worth it, and doing it
    here means the conversion is testable without a disc in the drive.

    Track lengths are taken from the FILE/DATAFILE lines and accumulated to
    place each INDEX. A `START` directive becomes an INDEX 00 pregap.

    Raises DiscError when the TOC does not carry enough length information to
    place a later track — a cue sheet with a wrong offset produces a game that
    desyncs its audio, which is worse than refusing to write one.
    """
    tracks: list[dict[str, object]] = []
    current: Optional[dict[str, object]] = None

    for raw_line in toc_text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line:
            continue

        if line.upper().startswith("TRACK "):
            mode = line.split()[1].upper()
            current = {"mode": mode, "length": None, "pregap": 0}
            tracks.append(current)
            continue

        if current is None:
            continue

        upper = line.upper()

        if upper.startswith("START"):
            parts = line.split()
            # Bare "START" means the pregap runs to the start of the data,
            # which we cannot resolve without the file offsets; treat a bare
            # START as no explicit pregap rather than guessing.
            if len(parts) > 1:
                current["pregap"] = parse_msf(parts[1])
            continue

        if upper.startswith(("FILE ", "DATAFILE ", "AUDIOFILE ")):
            # DATAFILE "x.bin" <length>
            # FILE "x.bin" <start> <length>
            _, _, rest = line.partition('"')
            _, _, after = rest.partition('"')
            fields = after.split()
            if upper.startswith("DATAFILE"):
                length = fields[0] if fields else None
            else:
                length = fields[1] if len(fields) > 1 else None

            if length and ":" in length:
                current["length"] = parse_msf(length)
            elif length and length.isdigit():
                # A bare number in a cdrdao TOC is a byte count, not frames.
                current["length"] = int(length) // RAW_SECTOR_BYTES
            continue

    if not tracks:
        raise DiscError(
            "The TOC cdrdao produced has no tracks in it. The rip did not "
            "complete — check that the disc is readable and try again."
        )

    lines = [f'FILE "{binary_name}" BINARY']
    offset = 0

    for number, track in enumerate(tracks, start=1):
        mode = str(track["mode"])
        cue_mode = _TOC_MODES.get(mode)
        if cue_mode is None:
            raise DiscError(
                f"Track {number} of this disc uses a mode GameLab does not know "
                f"how to write into a cue sheet ({mode}). The .bin and .toc files "
                "are still on disk and can be converted with cdrdao's toc2cue."
            )

        lines.append(f"  TRACK {number:02d} {cue_mode}")

        pregap = int(track["pregap"] or 0)
        if pregap:
            lines.append(f"    INDEX 00 {format_msf(offset)}")
            lines.append(f"    INDEX 01 {format_msf(offset + pregap)}")
        else:
            lines.append(f"    INDEX 01 {format_msf(offset)}")

        length = track["length"]
        if length is None:
            if number != len(tracks):
                raise DiscError(
                    f"The TOC does not give a length for track {number}, so the "
                    "position of the tracks after it cannot be worked out. The "
                    ".bin and .toc are on disk; convert them with cdrdao's toc2cue."
                )
            break

        offset += int(length)

    return "\n".join(lines) + "\n"


# ── Running a tool ────────────────────────────────────────────────

class _Runner:
    """Runs an external tool, streaming its output and honouring cancellation.

    Output is read as bytes and split on both newline and carriage return,
    because every one of these tools redraws a status line in place with \\r and
    a plain readline() would block until the tool finally emitted a newline —
    which for a 40-minute rip is at the very end.
    """

    #: How long to block in select before re-checking the cancel flag.
    POLL_SECONDS = 0.25
    #: Lines kept for the error message when a tool fails.
    TAIL_LINES = 30

    def __init__(self, cancel: threading.Event) -> None:
        self._cancel = cancel

    def run(
        self,
        command: Sequence[str],
        *,
        parser: Callable[[str], Optional[DiscProgress]],
        progress: Optional[ProgressCallback] = None,
        extra: Optional[Callable[[], Optional[DiscProgress]]] = None,
    ) -> tuple[int, list[str]]:
        """Run `command`, returning (exit code, tail of its output).

        `extra`, if given, is polled between reads for progress that comes from
        somewhere other than the tool's output — the size of the file being
        written, for instance.
        """
        logger.info("running: %s", " ".join(command))

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # Own session, so cancelling reaches helpers the tool forked.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise DiscError(f"Could not run {command[0]}: not found on this system.") from exc
        except PermissionError as exc:
            raise DiscError(
                f"Not permitted to run {command[0]}. Burning and ripping usually "
                "need your user to be in the 'optical' or 'cdrom' group."
            ) from exc

        tail: list[str] = []
        buffer = b""
        cancelled = False

        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)

        try:
            while True:
                if self._cancel.is_set() and not cancelled:
                    cancelled = True
                    self._terminate(process)

                if selector.select(self.POLL_SECONDS):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    buffer += chunk
                    buffer, lines = _split_lines(buffer)
                    for line in lines:
                        tail.append(line)
                        del tail[:-self.TAIL_LINES]
                        if progress:
                            update = parser(line)
                            if update:
                                progress(update)
                elif process.poll() is not None:
                    break

                if progress and extra:
                    update = extra()
                    if update:
                        progress(update)
        finally:
            selector.close()
            process.stdout.close()
            code = process.wait()

        if buffer:
            tail.append(buffer.decode("utf-8", "replace"))

        if cancelled or self._cancel.is_set():
            raise DiscCancelled(
                "Stopped at your request. The partly-written file has been left "
                "on disk so you can delete it or resume."
            )

        return code, tail

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        """Stop a tool and everything it forked."""
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), 15)  # SIGTERM
        except (OSError, ProcessLookupError):
            process.terminate()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), 9)  # SIGKILL
            except (OSError, ProcessLookupError):
                process.kill()


def _split_lines(buffer: bytes) -> tuple[bytes, list[str]]:
    """Split a byte buffer on \\n and \\r, keeping any trailing partial line."""
    normalised = buffer.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    parts = normalised.split(b"\n")
    remainder = parts.pop()
    return remainder, [p.decode("utf-8", "replace") for p in parts if p.strip()]


class _FileSizeProgress:
    """Real progress derived from how large the output file has actually grown.

    This is a measurement, not an estimate: the number reported is the size the
    file has on disk at that instant. It exists because cdrdao's own textual
    progress could not be verified, and because for `dd` there is no textual
    progress at all short of signalling it.
    """

    def __init__(self, path: Path, total: Optional[int], stage: str) -> None:
        self.path = path
        self.total = total
        self.stage = stage

    def __call__(self) -> Optional[DiscProgress]:
        try:
            done = self.path.stat().st_size
        except OSError:
            return None

        percent = (done / self.total * 100) if self.total else None
        return DiscProgress(
            stage=self.stage,
            message=f"{done:,} bytes written",
            percent=min(100.0, percent) if percent is not None else None,
            bytes_done=done,
            bytes_total=self.total,
        )


# ── Results ───────────────────────────────────────────────────────

@dataclass
class RipResult:
    """What a rip actually produced."""

    image_path: Path
    #: The .cue for a CD rip; None for an .iso.
    cue_path: Optional[Path] = None
    #: The .toc cdrdao wrote, kept so a failed cue conversion can be redone.
    toc_path: Optional[Path] = None
    hashes: Optional[FileHashes] = None
    size_bytes: int = 0
    tool: str = ""
    read_errors: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def library_path(self) -> Path:
        """The file to add to the library: the cue when there is one."""
        return self.cue_path or self.image_path


@dataclass
class BurnResult:
    """What a burn actually did, including whether it was checked afterwards."""

    image_path: Path
    device: Path
    tool: str = ""
    written_bytes: int = 0
    #: True only when the disc was read back and matched. False means it was
    #: checked and did not match. None means verification was not attempted.
    verified: Optional[bool] = None
    verify_message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verified is not False


# ── The worker ────────────────────────────────────────────────────

class DiscJob:
    """Base for the ripper and the burner: cancellation and tool running.

    Cancellation follows the same shape as metadata.scraper.Scraper — a
    threading.Event the UI sets from another thread, checked by the worker.
    """

    def __init__(self) -> None:
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Ask an in-flight operation to stop. Safe to call from another thread."""
        self._cancel.set()

    def reset(self) -> None:
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def _runner(self) -> _Runner:
        return _Runner(self._cancel)

    def _resolve_device(self, device: Optional[str | Path]) -> Path:
        if device is not None:
            path = Path(device)
            if not path.exists():
                raise DiscError(f"There is no drive at {path}.")
            return path

        drive = default_drive()
        if drive is None:
            raise DiscError(
                "No optical drive was found on this machine. GameLab looked in "
                f"{PROC_CDROM_INFO} and for /dev/sr* devices and found neither. "
                "If a drive is plugged in, the sr_mod kernel module may not be loaded."
            )
        return drive.path


class DiscRipper(DiscJob):
    """Reads a physical disc into an image file the library can index."""

    def rip(
        self,
        destination: str | Path,
        *,
        device: Optional[str | Path] = None,
        audio: Optional[bool] = None,
        progress: Optional[ProgressCallback] = None,
        hash_result: bool = True,
    ) -> RipResult:
        """Rip whatever is in the drive, choosing the right format for it.

        Discs with audio tracks — every PlayStation, Saturn, Sega CD and PC
        Engine CD game worth ripping — go to .bin/.cue. Pure data discs go to
        .iso. `audio` overrides the automatic choice when the drive could not
        report the disc type.
        """
        self.reset()
        device_path = self._resolve_device(device)

        if audio is None:
            audio = self._disc_has_audio(device_path)

        if audio:
            return self.rip_cd(destination, device=device_path, progress=progress, hash_result=hash_result)
        return self.rip_iso(destination, device=device_path, progress=progress, hash_result=hash_result)

    @staticmethod
    def _disc_has_audio(device: Path) -> bool:
        """Whether the loaded disc has audio tracks, per the kernel.

        Defaults to True when the drive will not say: ripping a data-only disc
        to .bin/.cue wastes space but loses nothing, whereas ripping a mixed
        disc to .iso silently throws the music away.
        """
        status, disc_type, _size, _error = probe_media(device)
        if status != "disc loaded":
            raise DiscError(
                f"There is no disc in {device} ({status}). Put a disc in and try again."
            )
        if disc_type is None:
            return True
        return disc_type in ("audio", "mixed", "xa")

    def rip_cd(
        self,
        destination: str | Path,
        *,
        device: Optional[str | Path] = None,
        progress: Optional[ProgressCallback] = None,
        hash_result: bool = True,
    ) -> RipResult:
        """Rip a CD to a .bin/.cue pair with cdrdao.

        Read raw, at 2352 bytes per sector, so audio tracks and subchannel-heavy
        data tracks survive. The .toc cdrdao writes is converted to a .cue and
        both are kept.
        """
        self.reset()
        cdrdao = require_tool("cdrdao")
        device_path = self._resolve_device(device)

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        bin_path = destination.with_suffix(".bin")
        toc_path = destination.with_suffix(".toc")
        cue_path = destination.with_suffix(".cue")

        _status, _type, size, _error = probe_media(device_path)

        if progress:
            progress(DiscProgress(stage="reading toc", message=f"Reading the table of contents from {device_path}"))

        command = [
            cdrdao, "read-cd",
            "--read-raw",          # 2352-byte sectors: keeps audio and subheaders
            "--datafile", str(bin_path),
            "--device", str(device_path),
            "--driver", "generic-mmc:0x20000",
            "-n",                  # never wait for a keypress; this is not interactive
            str(toc_path),
        ]

        code, tail = self._runner.run(
            command,
            parser=parse_cdrdao_progress,
            progress=progress,
            extra=_FileSizeProgress(bin_path, size, "ripping") if progress else None,
        )

        if code != 0:
            raise DiscError(_tool_failure("cdrdao", code, tail))

        if not bin_path.is_file():
            raise DiscError(
                f"cdrdao reported success but wrote no data file at {bin_path}. "
                "Nothing was ripped."
            )

        result = RipResult(image_path=bin_path, toc_path=toc_path, tool="cdrdao")
        result.size_bytes = bin_path.stat().st_size

        try:
            cue_path.write_text(toc_to_cue(toc_path.read_text(), bin_path.name))
            result.cue_path = cue_path
        except (OSError, DiscError, ValueError) as exc:
            # The data is ripped and safe; only the cue sheet failed. Say so
            # rather than throwing away a 40-minute read.
            result.warnings.append(
                f"The disc was ripped to {bin_path}, but the .cue sheet could not "
                f"be written: {exc} You can create one with cdrdao's toc2cue."
            )

        if hash_result:
            result.hashes = self._hash(bin_path, progress)

        return result

    def rip_iso(
        self,
        destination: str | Path,
        *,
        device: Optional[str | Path] = None,
        progress: Optional[ProgressCallback] = None,
        hash_result: bool = True,
    ) -> RipResult:
        """Rip a data CD or DVD to .iso, with ddrescue if it is installed.

        ddrescue is strongly preferred: it retries bad sectors and keeps a map
        file, so a scratched disc yields a mostly-good image instead of nothing.
        `dd` is the fallback and gives up on the first read error — which is
        said plainly in the result's warnings, not hidden.
        """
        self.reset()
        tool = require_any("ddrescue", "dd")
        device_path = self._resolve_device(device)

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        iso_path = destination.with_suffix(".iso")

        status, _type, size, _error = probe_media(device_path)
        if status != "disc loaded":
            raise DiscError(f"There is no disc in {device_path} ({status}). Put a disc in and try again.")

        result = RipResult(image_path=iso_path, tool=tool)

        # ddrescue reports a running count of unreadable sectors. Capture it as
        # updates go past so the result can say the disc was damaged.
        def watch(update: DiscProgress) -> None:
            if update.read_errors:
                result.read_errors = max(result.read_errors, update.read_errors)
            if progress:
                progress(update)

        relay = watch if (progress or tool == "ddrescue") else None

        if tool == "ddrescue":
            mapfile = iso_path.with_suffix(".iso.mapfile")
            command = [
                require_tool("ddrescue"),
                "-b", str(DATA_SECTOR_BYTES),
                "-r3",  # three retries per bad sector before giving up on it
                str(device_path), str(iso_path), str(mapfile),
            ]
            code, tail = self._runner.run(
                command,
                parser=parse_ddrescue_progress,
                progress=relay,
                extra=_FileSizeProgress(iso_path, size, "ripping") if progress else None,
            )
        else:
            result.warnings.append(
                "ddrescue is not installed, so dd was used instead. dd stops at "
                "the first unreadable sector; a scratched disc will produce a "
                "truncated image. " + TOOLS["ddrescue"].install_hint()
            )
            command = [
                require_tool("dd"),
                f"if={device_path}", f"of={iso_path}",
                f"bs={DATA_SECTOR_BYTES}", "conv=noerror,sync", "status=none",
            ]
            # dd prints nothing useful while running, so all progress here comes
            # from the size of the file it is writing — a real measurement.
            code, tail = self._runner.run(
                command,
                parser=lambda line: None,
                progress=progress,
                extra=_FileSizeProgress(iso_path, size, "ripping") if progress else None,
            )

        if code != 0:
            raise DiscError(_tool_failure(tool, code, tail))

        if not iso_path.is_file() or iso_path.stat().st_size == 0:
            raise DiscError(f"{tool} wrote no data to {iso_path}. Nothing was ripped.")

        result.size_bytes = iso_path.stat().st_size

        if size and result.size_bytes < size:
            result.warnings.append(
                f"The image is {result.size_bytes:,} bytes but the drive reported "
                f"the disc as {size:,} bytes. Part of the disc could not be read."
            )

        if result.read_errors:
            result.warnings.append(
                f"{tool} hit {result.read_errors} read error(s). The image is "
                "incomplete where those sectors were — the disc may be scratched."
            )

        if hash_result:
            result.hashes = self._hash(iso_path, progress)

        return result

    @staticmethod
    def _hash(path: Path, progress: Optional[ProgressCallback]) -> FileHashes:
        """Hash the ripped image with the library's own hasher.

        Uses core.hashing so a ripped disc is identified exactly the same way a
        downloaded ROM is, and matches Redump lookups without a second code path.
        """
        def relay(done: int, total: int) -> None:
            if progress:
                progress(DiscProgress(
                    stage="hashing",
                    message=f"Checksumming {path.name}",
                    percent=(done / total * 100) if total else None,
                    bytes_done=done,
                    bytes_total=total,
                ))

        return hash_file(path, progress=relay if progress else None)


class DiscBurner(DiscJob):
    """Writes an image the user owns to a blank disc, and checks the result.

    This deliberately does no DRM detection and contains no copy-protection
    circumvention. It takes an image path the caller names and writes it. It is
    for the user's own DRM-free images — their GOG installers, their own rips,
    their homebrew — and the interface calling it should say exactly that.
    """

    def burn(
        self,
        image: str | Path,
        *,
        device: Optional[str | Path] = None,
        speed: Optional[int] = None,
        dvd: Optional[bool] = None,
        verify: bool = True,
        progress: Optional[ProgressCallback] = None,
    ) -> BurnResult:
        """Burn `image` to the disc in `device`.

        `verify` reads the disc back afterwards and compares its checksum to the
        image. It doubles the time and is worth it: a burn that failed silently
        is a coaster the user only discovers halfway through a game.
        """
        self.reset()

        image_path = Path(image)
        if not image_path.is_file():
            raise DiscError(f"There is no image file at {image_path}.")

        size = image_path.stat().st_size
        if size == 0:
            raise DiscError(f"{image_path} is empty; there is nothing to burn.")

        # Checked before the drive is touched: a format GameLab cannot burn is
        # a refusal regardless of what hardware is attached.
        if image_path.suffix.lower() == ".cue":
            raise DiscError(
                "Burning a .cue/.bin pair track-by-track is not implemented. "
                "GameLab burns single-image files (.iso, and .bin written as a "
                "single data track). Use cdrdao directly for multi-track audio discs."
            )

        device_path = self._resolve_device(device)

        if dvd is None:
            # Anything over the largest CD-R is necessarily a DVD burn.
            dvd = size > 900 * 1000 * 1000

        drive = next((d for d in detect_drives() if d.path == device_path), None)
        if drive is not None and not drive.can_write:
            raise DiscError(
                f"{device_path} cannot write discs — the kernel reports it as a "
                "read-only drive. Burning needs a writer."
            )

        result = BurnResult(image_path=image_path, device=device_path, written_bytes=0)

        if dvd:
            tool = "growisofs"
            growisofs = require_tool("growisofs")
            command = [growisofs, "-dvd-compat", f"-Z{device_path}={image_path}"]
            if speed:
                command.insert(1, f"-speed={speed}")
            parser: Callable[[str], Optional[DiscProgress]] = parse_growisofs_progress
        else:
            tool = require_any("cdrskin", "wodim")
            command = [
                require_tool(tool),
                "-v",                     # without this the tools print no progress at all
                f"dev={device_path}",
                "-sao",                   # session-at-once: no two-second gap between tracks
                str(image_path),
            ]
            if speed:
                command.insert(3, f"speed={speed}")
            parser = parse_cdrskin_progress

        result.tool = tool

        if progress:
            progress(DiscProgress(stage="burning", message=f"Writing {image_path.name} to {device_path}"))

        code, tail = self._runner.run(command, parser=parser, progress=progress)

        if code != 0:
            raise DiscError(_tool_failure(tool, code, tail))

        result.written_bytes = size

        if verify:
            self._verify(result, progress)

        return result

    def _verify(self, result: BurnResult, progress: Optional[ProgressCallback]) -> None:
        """Read the burned disc back and compare it to the image.

        Compares exactly as many bytes as the image holds. Discs are padded up
        to a whole number of sectors, so the disc is often longer than the image
        and the trailing padding is correctly ignored — but a disc that is
        *shorter*, or that differs anywhere in those bytes, is a failed burn.
        """
        if progress:
            progress(DiscProgress(stage="verifying", message="Reading the disc back to check it"))

        # Some drives need the tray cycled before the freshly written disc is
        # readable. If the read fails, say that rather than declaring a failure.
        status, _type, _size, error = probe_media(result.device)
        if status != "disc loaded":
            result.verified = None
            result.verify_message = (
                f"The burn finished, but the disc could not be read back to check it "
                f"({error or status}). Eject and re-insert the disc, then verify again."
            )
            return

        total = result.written_bytes
        expected = hashlib.sha1()
        actual = hashlib.sha1()
        done = 0

        try:
            with result.image_path.open("rb") as source, open(result.device, "rb") as disc:
                while done < total:
                    if self._cancel.is_set():
                        raise DiscCancelled("Verification stopped at your request. The disc is written.")

                    want = min(1024 * 1024, total - done)
                    from_image = source.read(want)
                    from_disc = disc.read(want)

                    if not from_disc or len(from_disc) < len(from_image):
                        result.verified = False
                        result.verify_message = (
                            f"The disc is shorter than the image: only {done + len(from_disc):,} "
                            f"of {total:,} bytes could be read back. The burn did not complete."
                        )
                        return

                    expected.update(from_image)
                    actual.update(from_disc)
                    done += len(from_image)

                    if progress:
                        progress(DiscProgress(
                            stage="verifying",
                            message="Comparing the disc to the image",
                            percent=done / total * 100,
                            bytes_done=done,
                            bytes_total=total,
                        ))
        except OSError as exc:
            result.verified = None
            result.verify_message = (
                f"The burn finished, but the disc could not be read back to check it: {exc}"
            )
            return

        if expected.hexdigest() == actual.hexdigest():
            result.verified = True
            result.verify_message = f"Verified: {total:,} bytes on the disc match the image exactly."
        else:
            result.verified = False
            result.verify_message = (
                "The disc does not match the image. The burn failed and this disc "
                "should not be trusted."
            )


def _tool_failure(tool: str, code: int, tail: Sequence[str]) -> str:
    """A failure message that shows what the tool actually said.

    The tool's own last lines are included verbatim: they name the real problem
    ('medium not blank', 'no permission on /dev/sr0') far better than any
    message this module could invent from an exit code.
    """
    lines = [f"{tool} failed (exit code {code})."]
    said = [line for line in tail if line.strip()]
    if said:
        lines.append("")
        lines.append(f"What {tool} said:")
        lines.extend(f"  {line}" for line in said[-10:])
    else:
        lines.append(f"{tool} produced no output explaining why.")
    return "\n".join(lines)
