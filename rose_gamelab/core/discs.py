"""Multi-disc game detection and grouping.

A three-disc PlayStation game arrives as three files and should appear in the
library as ONE game with one cover, not three entries the user has to mentally
merge. This module recognises the disc-naming conventions used by No-Intro,
Redump and the common ripping tools, groups the files, and writes the `.m3u`
playlist that lets emulators swap discs in-game.

Playlist support is why the m3u matters: RetroArch, DuckStation, PCSX2 and
Flycast all accept an m3u and expose "next disc" in their own menu, so the user
never returns to the launcher mid-game.

Nothing here touches the network.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Disc markers, most explicit first. Each pattern must capture the disc number
# (or letter) in a group named `num`.
#
# Real-world forms these cover:
#   Final Fantasy VII (USA) (Disc 1).cue      <- Redump
#   Final Fantasy VII (Disc 1 of 3).bin
#   Metal Gear Solid [CD1].bin
#   Game (Disk 2).d64
#   Grandia (USA) (Disc A).cue                <- lettered discs
#   Game.cd1.chd
_DISC_PATTERNS = [
    re.compile(r"[\(\[]\s*(?:disc|disk|cd|dvd)\s*(?P<num>\d+|[a-z])\s*(?:of\s*\d+\s*)?[\)\]]", re.I),
    re.compile(r"[\s._-]+(?:disc|disk|cd|dvd)[\s._-]*(?P<num>\d+)(?=[\s._\-\)\]]|$)", re.I),
]

# Files that are part of a disc image but are not themselves the entry point.
# When a .cue exists we index the .cue, not the .bin tracks it references.
TRACK_SUFFIXES = {".bin", ".img", ".iso", ".raw"}
CUESHEET_SUFFIXES = {".cue", ".ccd", ".gdi", ".mds"}

# Regional and dump-status tags that are not part of a game's identity.
_TAG_RE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.I)
# Titles are often stored as "Legend of Zelda, The" in dat files.
_TRAILING_ARTICLE_RE = re.compile(r",\s*(the|a|an)$", re.I)


@dataclass
class DiscFile:
    """One file that belongs to a (possibly multi-disc) game."""

    path: Path
    disc_number: Optional[int] = None
    disc_label: Optional[str] = None


@dataclass
class GameGroup:
    """One game, with the one or more disc files that make it up."""

    title: str
    files: list[DiscFile] = field(default_factory=list)

    @property
    def is_multi_disc(self) -> bool:
        return len(self.files) > 1

    @property
    def primary_file(self) -> Path:
        """Disc 1, or the only file — what to launch when there is no playlist."""
        return self.sorted_files[0].path

    @property
    def sorted_files(self) -> list[DiscFile]:
        return sorted(self.files, key=lambda f: (f.disc_number or 0, f.path.name))


# ── Title parsing ─────────────────────────────────────────────────

def parse_disc_number(name: str) -> tuple[str, Optional[int], Optional[str]]:
    """Split a filename stem into (base title, disc number, disc label).

    Returns (name, None, None) when the file carries no disc marker.
    Lettered discs (Disc A/B/C) are normalised to 1/2/3.
    """
    for pattern in _DISC_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue

        raw = match.group("num")
        if raw.isdigit():
            number = int(raw)
        else:
            # 'a' -> 1, 'b' -> 2, ...
            number = ord(raw.lower()) - ord("a") + 1

        base = (name[: match.start()] + name[match.end():]).strip()
        # Collapse whitespace and strip separators left behind by the removal.
        base = re.sub(r"\s{2,}", " ", base).strip(" -_.")
        return base, number, match.group(0).strip("()[] ")

    return name, None, None


def normalise_title(name: str) -> str:
    """Reduce a filename to a comparable game title.

    Strips region/dump tags and trailing separators. Used to decide whether two
    files are discs of the same game, so it must be stable but not so
    aggressive that genuinely different games collide.
    """
    title = _TAG_RE.sub("", name)
    title = re.sub(r"\s{2,}", " ", title)
    return title.strip(" -_.")


def sort_title(title: str) -> str:
    """Title as it should sort: lowercased, leading article moved off the front.

    'The Legend of Zelda' and 'Legend of Zelda, The' both sort under L.
    """
    lowered = title.lower().strip()
    lowered = _TRAILING_ARTICLE_RE.sub("", lowered)
    lowered = _LEADING_ARTICLE_RE.sub("", lowered)
    return lowered.strip()


# ── Grouping ──────────────────────────────────────────────────────

def filter_redundant_tracks(paths: Iterable[Path]) -> list[Path]:
    """Drop .bin/.img track files that a cuesheet beside them already covers.

    A single-disc PlayStation rip is often `Game.cue` plus `Game (Track 1).bin`
    and friends. Indexing the tracks would show one game as five entries.
    """
    paths = list(paths)
    cue_stems: dict[Path, set[str]] = {}

    for path in paths:
        if path.suffix.lower() in CUESHEET_SUFFIXES:
            cue_stems.setdefault(path.parent, set()).add(path.stem.lower())

    kept: list[Path] = []
    for path in paths:
        if path.suffix.lower() in TRACK_SUFFIXES:
            stems = cue_stems.get(path.parent, set())
            stem = path.stem.lower()
            # Covered if a cuesheet shares its stem, or if the stem looks like
            # "<cue stem> (Track N)".
            covered = stem in stems or any(
                stem.startswith(s) and "track" in stem[len(s):] for s in stems
            )
            if covered:
                continue
        kept.append(path)

    return kept


def group_discs(paths: Iterable[Path]) -> list[GameGroup]:
    """Group files into games, merging multi-disc sets into single entries.

    Files are grouped when they share a normalised title AND a directory tree
    position, so two unrelated games that happen to both be called "Disc 1"
    in different folders never merge.
    """
    groups: dict[tuple[Path, str], GameGroup] = {}

    for path in filter_redundant_tracks(paths):
        base, disc_number, disc_label = parse_disc_number(path.stem)
        title = normalise_title(base)
        if not title:
            title = path.stem

        key = (path.parent, sort_title(title))

        group = groups.get(key)
        if group is None:
            group = GameGroup(title=title)
            groups[key] = group

        group.files.append(
            DiscFile(path=path, disc_number=disc_number, disc_label=disc_label)
        )

    # A "multi-disc" group whose files have no disc numbers is really several
    # distinct games that normalised to the same title — split them back apart
    # rather than silently merging unrelated games.
    result: list[GameGroup] = []
    for group in groups.values():
        if group.is_multi_disc and all(f.disc_number is None for f in group.files):
            result.extend(
                GameGroup(title=f.path.stem, files=[f]) for f in group.files
            )
        else:
            result.append(group)

    return sorted(result, key=lambda g: sort_title(g.title))


# ── Playlists ─────────────────────────────────────────────────────

def write_m3u(group: GameGroup, directory: Path) -> Optional[Path]:
    """Write an .m3u playlist for a multi-disc game. Returns its path.

    Returns None for single-disc games, which need no playlist.

    Paths are written relative to the playlist when the discs sit alongside it,
    so the playlist survives the library being moved or copied to another
    machine — absolute paths would break on the first move.
    """
    if not group.is_multi_disc:
        return None

    directory.mkdir(parents=True, exist_ok=True)
    # Strip characters that are awkward in filenames across filesystems.
    safe = re.sub(r'[/\\:*?"<>|]', "_", group.title).strip() or "playlist"
    playlist = directory / f"{safe}.m3u"

    lines = []
    for disc in group.sorted_files:
        try:
            lines.append(str(disc.path.relative_to(directory)))
        except ValueError:
            lines.append(str(disc.path))

    # Trailing newline: some emulators drop the final entry without it.
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return playlist
