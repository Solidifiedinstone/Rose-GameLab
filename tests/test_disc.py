"""Tests for optical disc ripping and burning.

No test here touches a real drive, spins a disc, or burns anything. Everything
is exercised against captured tool output and synthetic kernel files.

Where a sample line is marked VERIFIED, it was reconstructed from the printf
format strings inside the binary installed on the development machine. Where it
is marked UNVERIFIED, the tool was not installed and the format comes from that
tool's documentation — those tests pin the parser's behaviour, not the tool's.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core import disc
from rose_gamelab.core.disc import (
    DiscBurner,
    DiscError,
    DiscProgress,
    DiscRipper,
    MissingToolError,
    OpticalDrive,
    ToolSpec,
    detect_drives,
    first_available,
    format_msf,
    parse_cdparanoia_progress,
    parse_cdrdao_progress,
    parse_cdrom_info,
    parse_cdrskin_progress,
    parse_ddrescue_progress,
    parse_growisofs_progress,
    parse_msf,
    require_any,
    require_tool,
    toc_to_cue,
    tool_status,
)


# ── /proc/sys/dev/cdrom/info ──────────────────────────────────────
# Verbatim shape of the kernel's file (include/linux/cdrom.h, cdrom_print_info).
# Values below are a plausible single writer drive.

SINGLE_DRIVE_INFO = """CD-ROM information, Id: cdrom.c 3.20 2003/12/17

drive name:\t\tsr0
drive speed:\t\t48
drive # of slots:\t1
Can close tray:\t\t1
Can open tray:\t\t1
Can lock tray:\t\t1
Can change speed:\t1
Can select disk:\t0
Can read multisession:\t1
Can read MCN:\t\t1
Reports media changed:\t1
Can play audio:\t\t1
Can write CD-R:\t\t1
Can write CD-RW:\t1
Can read DVD:\t\t1
Can write DVD-R:\t1
Can write DVD-RAM:\t0
Can read MRW:\t\t1
Can write MRW:\t\t1
Can write RAM:\t\t1
"""

# Two drives: the kernel adds one column per drive to every row, in the same
# order as the `drive name` line. Here sr1 is a read-only drive.
TWO_DRIVE_INFO = """CD-ROM information, Id: cdrom.c 3.20 2003/12/17

drive name:\t\tsr1\tsr0
drive speed:\t\t24\t48
drive # of slots:\t1\t1
Can close tray:\t\t1\t1
Can open tray:\t\t1\t1
Can lock tray:\t\t1\t1
Can change speed:\t1\t1
Can select disk:\t0\t0
Can read multisession:\t1\t1
Can read MCN:\t\t1\t1
Reports media changed:\t1\t1
Can play audio:\t\t1\t1
Can write CD-R:\t\t0\t1
Can write CD-RW:\t0\t1
Can read DVD:\t\t1\t1
Can write DVD-R:\t0\t1
Can write DVD-RAM:\t0\t0
Can read MRW:\t\t1\t1
Can write MRW:\t\t0\t1
Can write RAM:\t\t0\t1
"""


def test_parses_a_single_drive():
    drives = parse_cdrom_info(SINGLE_DRIVE_INFO)

    assert len(drives) == 1
    assert drives[0]["drive name"] == "sr0"
    assert drives[0]["drive speed"] == 48
    assert drives[0]["can write cd-r"] is True
    assert drives[0]["can write dvd-ram"] is False


def test_parses_two_drives_keeping_column_order():
    """Column 0 is not necessarily sr0 — the kernel lists drives in reverse
    registration order, so capabilities must follow the name column."""
    drives = parse_cdrom_info(TWO_DRIVE_INFO)

    assert [d["drive name"] for d in drives] == ["sr1", "sr0"]

    by_name = {d["drive name"]: d for d in drives}
    assert by_name["sr0"]["can write cd-r"] is True
    assert by_name["sr1"]["can write cd-r"] is False
    assert by_name["sr0"]["drive speed"] == 48
    assert by_name["sr1"]["drive speed"] == 24


def test_capability_rows_become_booleans_not_strings():
    drives = parse_cdrom_info(SINGLE_DRIVE_INFO)
    flags = [v for k, v in drives[0].items() if k.startswith("can ")]

    assert flags
    assert all(isinstance(v, bool) for v in flags)


def test_no_drive_section_returns_empty():
    """A file without a `drive name` row describes no drives."""
    assert parse_cdrom_info("CD-ROM information, Id: cdrom.c 3.20\n\n") == []


def test_empty_file_returns_empty():
    assert parse_cdrom_info("") == []


def test_unknown_rows_are_kept_verbatim():
    """A newer kernel adding a row must not make this return less."""
    text = SINGLE_DRIVE_INFO + "Some future field:\thello\n"
    assert parse_cdrom_info(text)[0]["some future field"] == "hello"


def test_short_rows_do_not_invent_values():
    """A row with fewer columns than drives leaves the key absent rather than
    guessing — a fabricated capability could make GameLab try to burn on a
    read-only drive."""
    text = TWO_DRIVE_INFO.replace("Can write CD-R:\t\t0\t1", "Can write CD-R:\t\t0")
    drives = parse_cdrom_info(text)

    assert drives[0]["can write cd-r"] is False   # sr1, column 0, present
    assert "can write cd-r" not in drives[1]      # sr0, column 1, absent


# ── Drive detection ───────────────────────────────────────────────

def test_detect_drives_reads_the_proc_file(tmp_path):
    info = tmp_path / "info"
    info.write_text(SINGLE_DRIVE_INFO)
    dev = tmp_path / "dev"
    dev.mkdir()

    drives = detect_drives(proc_info=info, dev_dir=dev, probe=False)

    assert len(drives) == 1
    assert drives[0].name == "sr0"
    assert drives[0].path == dev / "sr0"
    assert drives[0].can_write_cd
    assert drives[0].can_read_dvd


def test_detect_drives_returns_empty_when_there_is_no_drive(tmp_path):
    """The normal case on a modern desktop. Must be an honest empty list, not
    an error and not a fabricated drive."""
    dev = tmp_path / "dev"
    dev.mkdir()

    assert detect_drives(proc_info=tmp_path / "missing", dev_dir=dev, probe=False) == []


def test_detect_drives_falls_back_to_dev_nodes(tmp_path):
    """No /proc file (sr_mod not loaded yet) but a device node exists."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "sr0").write_bytes(b"")

    drives = detect_drives(proc_info=tmp_path / "missing", dev_dir=dev, probe=False)

    assert [d.name for d in drives] == ["sr0"]
    # Nothing is known about its capabilities, and it does not pretend otherwise.
    assert drives[0].capabilities == {}
    assert not drives[0].can_write


def test_dev_nodes_do_not_duplicate_proc_entries(tmp_path):
    info = tmp_path / "info"
    info.write_text(SINGLE_DRIVE_INFO)
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "sr0").write_bytes(b"")

    assert len(detect_drives(proc_info=info, dev_dir=dev, probe=False)) == 1


def test_read_only_drive_reports_it_cannot_write():
    drive = OpticalDrive(
        path=disc.Path("/dev/sr1"),
        name="sr1",
        capabilities=parse_cdrom_info(TWO_DRIVE_INFO)[0],
    )
    assert not drive.can_write


def test_drive_without_a_disc_is_not_reported_as_loaded():
    assert not OpticalDrive(path=disc.Path("/dev/sr0"), name="sr0", status="no disc").has_disc


# ── Tool detection ────────────────────────────────────────────────

def fake_which(present: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_tool_status_reports_every_tool(monkeypatch):
    monkeypatch.setattr(disc.shutil, "which", fake_which(set()))
    assert {t.name for t in tool_status()} == set(disc.TOOLS)


def test_tool_status_distinguishes_present_from_absent(monkeypatch):
    monkeypatch.setattr(disc.shutil, "which", fake_which({"cdparanoia", "dd"}))

    status = {t.name: t for t in tool_status()}

    assert status["cdparanoia"].available
    assert status["dd"].available
    assert not status["cdrdao"].available
    assert status["cdrdao"].path is None


def test_missing_tool_error_names_the_tool_and_a_package(monkeypatch):
    monkeypatch.setattr(disc.shutil, "which", fake_which(set()))

    with pytest.raises(MissingToolError) as caught:
        require_tool("cdrdao")

    message = str(caught.value)
    assert "cdrdao" in message
    assert "pacman -S cdrdao" in message
    assert caught.value.tool == "cdrdao"


def test_missing_tool_error_explains_what_the_tool_is_for(monkeypatch):
    """'cdrdao is not installed' alone does not tell the user why they care."""
    monkeypatch.setattr(disc.shutil, "which", fake_which(set()))

    with pytest.raises(MissingToolError) as caught:
        require_tool("ddrescue")

    assert "scratched" in str(caught.value)


def test_every_tool_has_an_install_hint_for_the_main_package_managers():
    for spec in disc.TOOLS.values():
        assert {"pacman", "apt", "dnf"} <= set(spec.packages)
        assert spec.install_hint().strip()


def test_first_available_prefers_the_earlier_tool(monkeypatch):
    monkeypatch.setattr(disc.shutil, "which", fake_which({"ddrescue", "dd"}))
    assert first_available("ddrescue", "dd") == "ddrescue"


def test_first_available_falls_through_to_the_installed_one(monkeypatch):
    monkeypatch.setattr(disc.shutil, "which", fake_which({"dd"}))
    assert first_available("ddrescue", "dd") == "dd"


def test_first_available_returns_none_when_nothing_is_installed(monkeypatch):
    monkeypatch.setattr(disc.shutil, "which", fake_which(set()))
    assert first_available("ddrescue", "dd") is None


def test_require_any_lists_every_alternative_when_none_are_installed(monkeypatch):
    monkeypatch.setattr(disc.shutil, "which", fake_which(set()))

    with pytest.raises(DiscError) as caught:
        require_any("cdrskin", "wodim")

    message = str(caught.value)
    assert "cdrskin" in message and "wodim" in message
    assert "pacman -S libburn" in message


def test_install_hint_without_packages_still_names_the_tool():
    spec = ToolSpec("weirdtool", "do something")
    assert "weirdtool" in spec.install_hint()


# ── Progress: ddrescue ────────────────────────────────────────────
# UNVERIFIED against a live run — ddrescue is not installed on the development
# machine. Field names are from GNU ddrescue 1.2x/1.30's documented status block.

DDRESCUE_STATUS = [
    "GNU ddrescue 1.30",
    "Press Ctrl-C to interrupt",
    "     ipos:   12058 kB, non-trimmed:        0 B,  current rate:   3080 kB/s",
    "     opos:   12058 kB, non-scraped:        0 B,  average rate:   2411 kB/s",
    "non-tried:  681543 kB,  bad-sector:        0 B,    error rate:       0 B/s",
    "  rescued:   12058 kB,   bad areas:        0,        run time:          5s",
    "pct rescued:    1.73%, read errors:        0,  remaining time:          4m",
    "                              time since last successful read:         n/s",
    "Copying non-tried blocks... Pass 1 (forwards)",
]


def test_ddrescue_percentage_is_read_from_its_own_output():
    update = parse_ddrescue_progress(DDRESCUE_STATUS[6])

    assert update is not None
    assert update.percent == pytest.approx(1.73)
    assert update.read_errors == 0


def test_ddrescue_rescued_bytes_are_converted_from_its_units():
    update = parse_ddrescue_progress(DDRESCUE_STATUS[5])

    assert update is not None
    assert update.bytes_done == 12_058_000  # kB is decimal in ddrescue's output


def test_ddrescue_megabyte_and_gigabyte_units():
    assert parse_ddrescue_progress("  rescued:     681 MB,").bytes_done == 681_000_000
    assert parse_ddrescue_progress("  rescued:    4.38 GB,").bytes_done == 4_380_000_000


def test_ddrescue_reports_read_errors_when_the_disc_is_damaged():
    line = "pct rescued:   98.20%, read errors:       17,  remaining time:          1m"
    update = parse_ddrescue_progress(line)

    assert update.read_errors == 17
    assert update.percent == pytest.approx(98.20)


def test_ddrescue_banner_lines_produce_no_progress():
    """A line with no measurement in it must not become a progress update."""
    for line in (DDRESCUE_STATUS[0], DDRESCUE_STATUS[1], DDRESCUE_STATUS[8]):
        assert parse_ddrescue_progress(line) is None


def test_whole_ddrescue_block_yields_only_real_measurements():
    updates = [u for u in (parse_ddrescue_progress(l) for l in DDRESCUE_STATUS) if u]

    assert updates
    assert all(u.percent is None or 0 <= u.percent <= 100 for u in updates)


# ── Progress: cdrskin / wodim ─────────────────────────────────────
# VERIFIED: assembled from the printf format strings in the cdrskin binary
# installed on the development machine (libburn 1.5.8):
#   "Track %-2.2d: %s MB written %s[buf %3d%%]  %4.1fx."  with  "%4d of %4d"
#   and "(fifo %3d%%) "

CDRSKIN_LINE = "Track 01:    5 of  650 MB written (fifo 100%) [buf  99%]   8.0x."
CDRSKIN_NO_TOTAL = "Track 01:   12 MB written [buf  99%]   8.0x."


def test_cdrskin_progress_is_the_exact_ratio_the_tool_printed():
    update = parse_cdrskin_progress(CDRSKIN_LINE)

    assert update is not None
    assert update.stage == "burning"
    assert update.percent == pytest.approx(5 / 650 * 100)
    assert update.bytes_total == 650 * 1024 * 1024


def test_cdrskin_without_a_total_reports_no_percentage():
    """The tool did not say how much there is, so neither do we."""
    update = parse_cdrskin_progress(CDRSKIN_NO_TOTAL)

    assert update is not None
    assert update.percent is None
    assert update.bytes_done == 12 * 1024 * 1024


def test_cdrskin_progress_climbs_monotonically():
    lines = [
        "Track 01:    0 of  650 MB written (fifo   0%) [buf 100%]   0.0x.",
        "Track 01:  120 of  650 MB written (fifo 100%) [buf  98%]   8.0x.",
        "Track 01:  650 of  650 MB written (fifo 100%) [buf  97%]   8.0x.",
    ]
    percents = [parse_cdrskin_progress(l).percent for l in lines]

    assert percents == sorted(percents)
    assert percents[-1] == pytest.approx(100.0)


def test_cdrskin_summary_lines_are_not_progress():
    assert parse_cdrskin_progress("cdrskin: fifo size : 4194304") is None
    assert parse_cdrskin_progress("Operation starts.") is None


def test_cdrskin_zero_total_does_not_divide_by_zero():
    assert parse_cdrskin_progress("Track 01:    0 of    0 MB written").percent is None


# ── Progress: growisofs ───────────────────────────────────────────
# UNVERIFIED on the development machine — growisofs (dvd+rw-tools) is not
# installed there. Format is dvd+rw-tools' documented output.

def test_growisofs_byte_counts_are_exact():
    line = "  4784128/681574400 ( 0.7%) @0.6x, remaining 21:33 RBU 100.0% UBU  12.5%"
    update = parse_growisofs_progress(line)

    assert update is not None
    assert update.bytes_done == 4784128
    assert update.bytes_total == 681574400
    assert update.percent == pytest.approx(0.7)


def test_growisofs_percent_only_line():
    update = parse_growisofs_progress("1.23% done, estimate finish Mon Jan  1 12:00:00 2024")

    assert update.percent == pytest.approx(1.23)
    assert update.bytes_done is None


def test_growisofs_noise_is_not_progress():
    assert parse_growisofs_progress("Executing 'builtin_dd if=x of=/dev/sr0'") is None


# ── Progress: cdrdao ──────────────────────────────────────────────
# UNVERIFIED — cdrdao is not installed on the development machine and its live
# output could not be captured. These tests pin the parser, not the tool. The
# authoritative progress for a cdrdao rip is the size of the file it is writing.

def test_cdrdao_track_lines_become_stage_messages():
    update = parse_cdrdao_progress("Reading track 03 (MODE1_RAW)...")

    assert update is not None
    assert update.stage == "ripping"
    assert "Track 3" in update.message
    # No percentage was printed, so none is reported.
    assert update.percent is None


def test_cdrdao_percentage_is_used_only_when_actually_printed():
    assert parse_cdrdao_progress(" 42% ").percent == pytest.approx(42.0)


def test_cdrdao_rejects_an_impossible_percentage():
    assert parse_cdrdao_progress("999% something") is None


def test_cdrdao_unrelated_lines_produce_nothing():
    assert parse_cdrdao_progress("Cdrdao version 1.2.6 - (C) Andreas Mueller") is None


# ── Progress: cdparanoia ──────────────────────────────────────────
# VERIFIED format: the string "##: %d [%s] @ %ld" was read out of the
# cdparanoia binary installed on the development machine (release 10.2).

def test_cdparanoia_progress_line_is_parsed():
    update = parse_cdparanoia_progress("##: 0 [read] @ 24234")

    assert update is not None
    assert update.bytes_done == 24234 * 4  # 16-bit stereo samples


def test_cdparanoia_percentage_needs_a_total_from_the_toc():
    """cdparanoia's line carries a position but never a total, so without one
    there is nothing honest to divide by."""
    assert parse_cdparanoia_progress("##: 0 [read] @ 24234").percent is None

    update = parse_cdparanoia_progress("##: 0 [read] @ 24234", total_sectors=1000)
    assert update.percent == pytest.approx(24234 / (1000 * 588) * 100)


def test_cdparanoia_negative_position_is_a_status_marker_not_an_offset():
    update = parse_cdparanoia_progress("##: -2 [wrote] @ -1")

    assert update is not None
    assert update.bytes_done is None
    assert "wrote" in update.message


def test_cdparanoia_other_output_is_ignored():
    assert parse_cdparanoia_progress("Ripping from sector 0 (track 1 [0:00.00])") is None


# ── MSF timecodes ─────────────────────────────────────────────────

def test_msf_round_trips():
    for text in ("00:00:00", "00:02:00", "59:12:33", "74:00:00"):
        assert format_msf(parse_msf(text)) == text


def test_msf_frame_arithmetic():
    assert parse_msf("00:00:00") == 0
    assert parse_msf("00:02:00") == 150      # the standard 2-second pregap
    assert parse_msf("01:00:00") == 60 * 75


def test_malformed_timecode_is_a_loud_failure():
    """A silently-wrong offset produces a game whose music desyncs."""
    for bad in ("", "1:2", "aa:bb:cc", "00-02-00"):
        with pytest.raises(ValueError):
            parse_msf(bad)


# ── cdrdao TOC to .cue ────────────────────────────────────────────

SINGLE_TRACK_TOC = """CD_ROM

// Track 1
TRACK MODE1_RAW
NO COPY
DATAFILE "game.bin" 59:12:33
"""

MIXED_MODE_TOC = """CD_ROM

// Track 1
TRACK MODE1_RAW
NO COPY
DATAFILE "game.bin" 04:00:00

// Track 2
TRACK AUDIO
NO COPY
NO PRE_EMPHASIS
TWO_CHANNEL_AUDIO
START 00:02:00
FILE "game.bin" 04:00:00 03:00:00

// Track 3
TRACK AUDIO
NO COPY
FILE "game.bin" 07:00:00 02:30:00
"""


def test_single_track_cue_points_at_the_bin():
    cue = toc_to_cue(SINGLE_TRACK_TOC, "game.bin")

    assert cue.splitlines()[0] == 'FILE "game.bin" BINARY'
    assert "TRACK 01 MODE1/2352" in cue
    assert "INDEX 01 00:00:00" in cue


def test_track_offsets_accumulate_across_tracks():
    """Track 2 starts where track 1 ended; a wrong offset here is a game that
    plays the wrong music."""
    cue = toc_to_cue(MIXED_MODE_TOC, "game.bin")

    assert "TRACK 02 AUDIO" in cue
    # Track 1 is 4 minutes, so track 2's pregap begins at 04:00:00 and its
    # audio proper 2 seconds later.
    assert "INDEX 00 04:00:00" in cue
    assert "INDEX 01 04:02:00" in cue
    # Track 3 begins after track 1 (4:00) plus track 2 (3:00).
    assert "INDEX 01 07:00:00" in cue


def test_track_modes_map_to_cue_sector_sizes():
    cue = toc_to_cue(MIXED_MODE_TOC, "game.bin")

    assert "MODE1/2352" in cue     # raw-read data track
    assert "AUDIO" in cue


def test_comments_are_ignored():
    cue = toc_to_cue("// leading comment\n" + SINGLE_TRACK_TOC, "game.bin")
    assert "leading comment" not in cue


def test_final_track_without_a_length_is_fine():
    """Nothing comes after it, so its missing length costs nothing."""
    toc = 'CD_ROM\n\nTRACK MODE1_RAW\nDATAFILE "game.bin"\n'
    assert "INDEX 01 00:00:00" in toc_to_cue(toc, "game.bin")


def test_missing_length_on_a_middle_track_refuses_rather_than_guesses():
    toc = (
        'CD_ROM\n\nTRACK MODE1_RAW\nDATAFILE "game.bin"\n'
        '\nTRACK AUDIO\nFILE "game.bin" 0 03:00:00\n'
    )
    with pytest.raises(DiscError) as caught:
        toc_to_cue(toc, "game.bin")

    assert "toc2cue" in str(caught.value)   # tells the user how to finish the job


def test_unknown_track_mode_refuses_and_says_the_data_is_safe():
    toc = 'CD_ROM\n\nTRACK SOMETHING_NEW\nDATAFILE "game.bin" 01:00:00\n'

    with pytest.raises(DiscError) as caught:
        toc_to_cue(toc, "game.bin")

    assert "SOMETHING_NEW" in str(caught.value)
    assert ".bin" in str(caught.value)


def test_empty_toc_is_an_error_not_an_empty_cue():
    """cdrdao exiting 0 with an empty TOC means the rip did not happen."""
    with pytest.raises(DiscError):
        toc_to_cue("CD_ROM\n", "game.bin")


def test_byte_length_is_converted_to_sectors():
    toc = 'CD_ROM\n\nTRACK MODE1_RAW\nDATAFILE "game.bin" 705600\n\nTRACK AUDIO\nFILE "game.bin" 0 01:00:00\n'
    cue = toc_to_cue(toc, "game.bin")

    # 705600 bytes / 2352 = 300 sectors = 4 seconds.
    assert "INDEX 01 00:04:00" in cue


# ── Line splitting ────────────────────────────────────────────────

def test_carriage_returns_split_lines():
    """Every one of these tools redraws its status with \\r. Waiting for a
    newline would mean no progress at all until the rip finished."""
    remainder, lines = disc._split_lines(b"first\rsecond\rthird")

    assert lines == ["first", "second"]
    assert remainder == b"third"


def test_partial_line_is_kept_for_the_next_read():
    remainder, lines = disc._split_lines(b"complete\npart")

    assert lines == ["complete"]
    assert remainder == b"part"


def test_crlf_does_not_produce_blank_lines():
    _remainder, lines = disc._split_lines(b"one\r\ntwo\r\n")
    assert lines == ["one", "two"]


def test_undecodable_bytes_do_not_crash():
    _remainder, lines = disc._split_lines(b"caf\xe9\n")
    assert lines and "caf" in lines[0]


# ── File-size progress ────────────────────────────────────────────

def test_file_size_progress_measures_the_real_file(tmp_path):
    target = tmp_path / "out.iso"
    target.write_bytes(b"x" * 500)

    update = disc._FileSizeProgress(target, 1000, "ripping")()

    assert update.bytes_done == 500
    assert update.percent == pytest.approx(50.0)


def test_file_size_progress_reports_no_percent_without_a_total(tmp_path):
    target = tmp_path / "out.iso"
    target.write_bytes(b"x" * 500)

    update = disc._FileSizeProgress(target, None, "ripping")()

    assert update.bytes_done == 500
    assert update.percent is None


def test_file_size_progress_before_the_file_exists(tmp_path):
    assert disc._FileSizeProgress(tmp_path / "nothing.iso", 1000, "ripping")() is None


def test_file_size_progress_never_exceeds_one_hundred(tmp_path):
    """A padded write can exceed the expected total; the bar must not overflow."""
    target = tmp_path / "out.iso"
    target.write_bytes(b"x" * 2000)

    assert disc._FileSizeProgress(target, 1000, "ripping")().percent == 100.0


# ── Cancellation ──────────────────────────────────────────────────

def test_ripper_starts_uncancelled():
    assert not DiscRipper().cancelled


def test_cancel_sets_the_flag():
    ripper = DiscRipper()
    ripper.cancel()
    assert ripper.cancelled


def test_reset_clears_the_flag():
    burner = DiscBurner()
    burner.cancel()
    burner.reset()
    assert not burner.cancelled


def test_cancel_is_visible_from_another_thread():
    import threading

    burner = DiscBurner()
    threading.Thread(target=burner.cancel).start()

    for _ in range(200):
        if burner.cancelled:
            break
        import time
        time.sleep(0.01)

    assert burner.cancelled


# ── Refusals and honest failures ──────────────────────────────────

def test_burning_a_missing_image_says_so(tmp_path):
    with pytest.raises(DiscError) as caught:
        DiscBurner().burn(tmp_path / "nope.iso")

    assert "no image file" in str(caught.value).lower()


def test_burning_an_empty_image_is_refused(tmp_path):
    image = tmp_path / "empty.iso"
    image.write_bytes(b"")

    with pytest.raises(DiscError) as caught:
        DiscBurner().burn(image)

    assert "empty" in str(caught.value).lower()


def test_burning_a_cue_says_it_is_not_implemented(tmp_path):
    """Multi-track burning is not built. Say that, rather than writing the cue
    sheet itself to the disc as if it were an image."""
    cue = tmp_path / "game.cue"
    cue.write_text('FILE "game.bin" BINARY\n')

    with pytest.raises(DiscError) as caught:
        DiscBurner().burn(cue)

    assert "not implemented" in str(caught.value).lower()


def test_no_drive_produces_a_message_that_says_where_it_looked(tmp_path, monkeypatch):
    monkeypatch.setattr(disc, "default_drive", lambda: None)
    image = tmp_path / "game.iso"
    image.write_bytes(b"x" * 4096)

    with pytest.raises(DiscError) as caught:
        DiscBurner().burn(image)

    message = str(caught.value)
    assert "No optical drive" in message
    assert "/dev/sr*" in message


def test_burning_to_a_device_that_does_not_exist_says_so(tmp_path):
    image = tmp_path / "game.iso"
    image.write_bytes(b"x" * 4096)

    with pytest.raises(DiscError) as caught:
        DiscBurner().burn(image, device=tmp_path / "sr9")

    assert "no drive" in str(caught.value).lower()


def test_ripping_without_cdrdao_names_cdrdao(monkeypatch, tmp_path):
    monkeypatch.setattr(disc.shutil, "which", fake_which(set()))

    with pytest.raises(MissingToolError) as caught:
        DiscRipper().rip_cd(tmp_path / "out")

    assert caught.value.tool == "cdrdao"


def test_iso_rip_without_ddrescue_or_dd_lists_both(monkeypatch, tmp_path):
    monkeypatch.setattr(disc.shutil, "which", fake_which(set()))

    with pytest.raises(DiscError) as caught:
        DiscRipper().rip_iso(tmp_path / "out")

    message = str(caught.value)
    assert "ddrescue" in message and "dd" in message


def test_tool_failure_message_quotes_what_the_tool_said():
    message = disc._tool_failure(
        "cdrskin", 1, ["cdrskin: FATAL : Cannot open device", "Aborting."]
    )

    assert "exit code 1" in message
    assert "Cannot open device" in message


def test_tool_failure_admits_when_there_was_no_output():
    message = disc._tool_failure("cdrdao", 2, [])
    assert "no output" in message


# ── Result objects ────────────────────────────────────────────────

def test_rip_result_prefers_the_cue_for_the_library():
    from rose_gamelab.core.disc import RipResult

    result = RipResult(image_path=disc.Path("/x/game.bin"), cue_path=disc.Path("/x/game.cue"))
    assert result.library_path == disc.Path("/x/game.cue")


def test_rip_result_falls_back_to_the_image_when_there_is_no_cue():
    from rose_gamelab.core.disc import RipResult

    result = RipResult(image_path=disc.Path("/x/game.iso"))
    assert result.library_path == disc.Path("/x/game.iso")


def test_unverified_burn_is_not_reported_as_failed():
    """verified=None means 'not checked', which is different from 'wrong'."""
    from rose_gamelab.core.disc import BurnResult

    result = BurnResult(image_path=disc.Path("/x/g.iso"), device=disc.Path("/dev/sr0"))
    assert result.verified is None
    assert result.ok


def test_failed_verification_is_reported_as_failed():
    from rose_gamelab.core.disc import BurnResult

    result = BurnResult(
        image_path=disc.Path("/x/g.iso"), device=disc.Path("/dev/sr0"), verified=False
    )
    assert not result.ok


def test_progress_percent_defaults_to_unknown_not_zero():
    """A stalled operation must look stalled, not look like it is at 0% and
    working."""
    assert DiscProgress(stage="ripping").percent is None
