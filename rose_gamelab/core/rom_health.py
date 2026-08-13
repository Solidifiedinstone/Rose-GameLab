"""Checking ROMs against the No-Intro and Redump catalogues.

"I have 400 ROMs" and "I have 398 good ROMs and 2 that will crash three hours
into a playthrough" are different facts, and only one of them is useful. A bad
dump is invisible until the exact moment it matters, which is usually long after
the point where re-acquiring the file was easy.

The preservation projects publish DAT files: catalogues of every known good dump
with its size and checksums. `core/hashing.py` already computes exactly the
hashes those catalogues index — CRC32 for No-Intro, MD5 and SHA-1 for Redump —
with copier headers skipped, which is the form the catalogues use. So verifying
a library is a lookup, not a re-read of every file.

DAT files are not vendored. No-Intro and Redump distribute them from their own
sites under their own terms, they change weekly, and bundling a stale copy would
mean confidently reporting a good dump as unknown. The user points GameLab at a
folder of the ones they care about.

Verdicts are deliberately not stored in the database. A verdict is only true
relative to the catalogue that produced it, and catalogues gain entries every
week — a stored "unknown" would harden into a permanent wrong answer for a ROM
that a later DAT recognises perfectly.

Both DAT dialects are read: the Logiqx XML that No-Intro and Redump ship today,
and the older clrmamepro text format that a lot of archived sets still use.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

DAT_SUFFIXES = (".dat", ".xml")

#: Statuses the catalogues use to mark an entry that is itself known to be a
#: bad dump. Matching one of these is a match, but not good news.
_BAD_STATUSES = {"baddump", "bad"}


class Health(str, Enum):
    """What the catalogue says about a file."""

    VERIFIED = "verified"
    #: Matches an entry the catalogue itself marks as a known bad dump.
    KNOWN_BAD = "known_bad"
    #: The catalogue has this ROM, and this file is not it — the usual sign of
    #: a bad dump, a hack, a translation, or a trimmed file.
    MODIFIED = "modified"
    #: No catalogue loaded covers this system, so nothing can be said.
    NOT_CATALOGUED = "not_catalogued"
    #: Catalogues were searched and nothing matched at all.
    UNKNOWN = "unknown"

    @property
    def is_good(self) -> bool:
        return self is Health.VERIFIED

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    Health.VERIFIED: "Verified good dump",
    Health.KNOWN_BAD: "Known bad dump",
    Health.MODIFIED: "Does not match the catalogued dump",
    Health.NOT_CATALOGUED: "No catalogue for this system",
    Health.UNKNOWN: "Not in any loaded catalogue",
}


@dataclass(frozen=True)
class DatEntry:
    """One catalogued ROM."""

    game_name: str
    rom_name: str
    size: Optional[int] = None
    crc32: str = ""
    md5: str = ""
    sha1: str = ""
    status: str = ""
    #: The catalogue this came from, so a result can say which one matched.
    catalogue: str = ""

    @property
    def known_bad(self) -> bool:
        return self.status.lower() in _BAD_STATUSES


@dataclass
class DatIndex:
    """Catalogued ROMs, indexed by every hash they publish.

    Indexed by all three because the catalogues do not agree on which they
    publish: No-Intro leads with CRC32, Redump with SHA-1, and older sets carry
    only MD5. Matching on whichever is present is what makes one index work
    across all of them.
    """

    by_crc32: dict[str, DatEntry] = field(default_factory=dict)
    by_md5: dict[str, DatEntry] = field(default_factory=dict)
    by_sha1: dict[str, DatEntry] = field(default_factory=dict)
    #: Normalised ROM name -> entries, for spotting a file that claims to be a
    #: catalogued game but does not match it.
    by_name: dict[str, list[DatEntry]] = field(default_factory=dict)
    catalogues: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.by_crc32) + len(self.by_sha1) + len(self.by_md5)

    @property
    def empty(self) -> bool:
        return not (self.by_crc32 or self.by_md5 or self.by_sha1)

    def add(self, entry: DatEntry) -> None:
        if entry.crc32:
            self.by_crc32.setdefault(entry.crc32, entry)
        if entry.md5:
            self.by_md5.setdefault(entry.md5, entry)
        if entry.sha1:
            self.by_sha1.setdefault(entry.sha1, entry)

        name = normalise_name(entry.rom_name)
        if name:
            self.by_name.setdefault(name, []).append(entry)

    def extend(self, entries: Iterable[DatEntry]) -> None:
        for entry in entries:
            self.add(entry)


def normalise_name(name: str) -> str:
    """A ROM name reduced to something comparable.

    Extension, region and dump tags all differ between a user's file and the
    catalogue entry without meaning the contents differ, so they are dropped —
    this is only used to notice "you have a file claiming to be this game".
    """
    stem = Path(name.strip()).stem.lower()
    stem = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", stem)
    return re.sub(r"[^a-z0-9]+", "", stem)


def _clean(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _size(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def parse_logiqx(text: str, *, catalogue: str = "") -> list[DatEntry]:
    """Parse the Logiqx XML DAT that No-Intro and Redump publish."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        logger.warning("could not parse %s as XML: %s", catalogue or "a DAT", exc)
        return []

    name = catalogue
    header = root.find("header")
    if header is not None and header.findtext("name"):
        name = header.findtext("name", "").strip() or catalogue

    entries: list[DatEntry] = []
    # `machine` is the MAME spelling of the same element.
    for game in list(root.iter("game")) + list(root.iter("machine")):
        game_name = game.get("name", "")
        for rom in game.iter("rom"):
            entries.append(DatEntry(
                game_name=game_name,
                rom_name=rom.get("name", ""),
                size=_size(rom.get("size")),
                crc32=_clean(rom.get("crc")),
                md5=_clean(rom.get("md5")),
                sha1=_clean(rom.get("sha1")),
                status=rom.get("status", ""),
                catalogue=name,
            ))

    return entries


_CMP_GAME = re.compile(r"\bgame\s*\((.*?)\n\)", re.S)
# Parentheses appear *inside* quoted ROM names — "Sonic The Hedgehog (USA,
# Europe).md" — so a lazy match to the first ')' truncates the entry halfway
# through its name and loses every checksum after it. Quoted runs are consumed
# whole here so only a real closing parenthesis ends the block.
_CMP_ROM = re.compile(r'\brom\s*\((?:[^()"]|"[^"]*")*\)', re.S)
_CMP_FIELD = re.compile(r'(\w+)\s+(?:"([^"]*)"|(\S+))')


def _cmp_fields(block: str) -> dict[str, str]:
    return {
        key: quoted or bare
        for key, quoted, bare in _CMP_FIELD.findall(block)
    }


def parse_clrmamepro(text: str, *, catalogue: str = "") -> list[DatEntry]:
    """Parse the older clrmamepro text DAT that archived sets still use."""
    entries: list[DatEntry] = []

    for block in _CMP_GAME.findall(text):
        # The rom sections are removed before reading the game's own fields,
        # rather than splitting on the word "rom" — which would truncate
        # "Romance of the Three Kingdoms" at its second letter.
        game_name = _cmp_fields(_CMP_ROM.sub(" ", block)).get("name", "")

        for rom_block in _CMP_ROM.findall(block):
            rom = _cmp_fields(rom_block)
            # 'rom' itself is matched as a field by the pattern; drop it so an
            # entry consisting of nothing else is still seen as empty.
            rom.pop("rom", None)
            if not rom:
                continue
            entries.append(DatEntry(
                game_name=game_name,
                rom_name=rom.get("name", ""),
                size=_size(rom.get("size")),
                crc32=_clean(rom.get("crc")),
                md5=_clean(rom.get("md5")),
                sha1=_clean(rom.get("sha1")),
                status=rom.get("flags", ""),
                catalogue=catalogue,
            ))

    return entries


def parse_dat(text: str, *, catalogue: str = "") -> list[DatEntry]:
    """Parse either DAT dialect, choosing by what the text actually is."""
    if "<" in text[:2048] and "datafile" in text[:2048].lower():
        return parse_logiqx(text, catalogue=catalogue)

    entries = parse_clrmamepro(text, catalogue=catalogue)
    if entries:
        return entries

    # A file that looked like neither is still worth one XML attempt: some
    # publishers omit the doctype this checks for.
    return parse_logiqx(text, catalogue=catalogue)


def load_dats(folder: str | Path) -> DatIndex:
    """Load every DAT in a folder into one index.

    Unreadable and malformed files are logged and skipped. Someone verifying a
    library against fifteen catalogues should not lose all fifteen because one
    was truncated mid-download.
    """
    index = DatIndex()
    directory = Path(folder).expanduser()

    try:
        candidates = sorted(
            path for path in directory.iterdir()
            if path.suffix.lower() in DAT_SUFFIXES
        )
    except OSError as exc:
        logger.warning("could not read the DAT folder %s: %s", directory, exc)
        return index

    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("could not read %s: %s", path.name, exc)
            continue

        entries = parse_dat(text, catalogue=path.stem)
        if not entries:
            logger.warning("no ROM entries found in %s", path.name)
            continue

        index.extend(entries)
        index.catalogues.append(path.stem)
        logger.info("loaded %d entries from %s", len(entries), path.name)

    return index


@dataclass(frozen=True)
class HealthResult:
    """What the catalogues say about one file."""

    health: Health
    entry: Optional[DatEntry] = None
    #: Filled in when the verdict is MODIFIED: what it should have matched.
    expected: Optional[DatEntry] = None

    @property
    def catalogue(self) -> str:
        source = self.entry or self.expected
        return source.catalogue if source else ""

    @property
    def summary(self) -> str:
        if self.health is Health.VERIFIED and self.entry:
            return f"{self.entry.game_name} — verified against {self.catalogue}"
        if self.health is Health.MODIFIED and self.expected:
            return (
                f"Does not match {self.expected.game_name} in {self.catalogue}. "
                "This is a hack, a translation, a trimmed file, or a bad dump."
            )
        if self.health is Health.KNOWN_BAD and self.entry:
            return f"{self.entry.game_name} is catalogued as a known bad dump."
        return self.health.label


def verify(
    index: DatIndex,
    *,
    crc32: str = "",
    md5: str = "",
    sha1: str = "",
    name: str = "",
) -> HealthResult:
    """Check one file's hashes against the catalogues.

    SHA-1 is tried first and CRC32 last, in order of how much a match is worth:
    CRC32 is 32 bits and collides often enough that a catalogue of a hundred
    thousand ROMs will contain pairs, so it decides only when nothing stronger
    is available.
    """
    if index.empty:
        return HealthResult(Health.NOT_CATALOGUED)

    for value, table in (
        (_clean(sha1), index.by_sha1),
        (_clean(md5), index.by_md5),
        (_clean(crc32), index.by_crc32),
    ):
        if not value:
            continue
        entry = table.get(value)
        if entry is not None:
            health = Health.KNOWN_BAD if entry.known_bad else Health.VERIFIED
            return HealthResult(health, entry=entry)

    # Nothing matched by content. If the catalogue holds a ROM by this name,
    # the file is a variant of it rather than something unheard of — which is
    # a much more useful thing to tell someone.
    candidates = index.by_name.get(normalise_name(name), [])
    if candidates:
        return HealthResult(Health.MODIFIED, expected=candidates[0])

    return HealthResult(Health.UNKNOWN)


@dataclass(frozen=True)
class GameHealth:
    """A library game, and what the catalogues say about its file."""

    game_id: int
    title: str
    system: str
    path: str
    result: HealthResult

    @property
    def health(self) -> Health:
        return self.result.health


def check_library(library, index: DatIndex, *, system: Optional[str] = None) -> list[GameHealth]:
    """Verify every hashed file in the library.

    Files that have never been hashed are skipped rather than reported as
    unknown: "we have not looked" and "we looked and found nothing" are
    different answers, and only the second is the user's problem.
    """
    if index.empty:
        return []

    sql = (
        "SELECT g.id, g.title, g.system, f.path, f.crc32, f.md5, f.sha1"
        "  FROM games g JOIN game_files f ON f.game_id = g.id"
        " WHERE (f.sha1 IS NOT NULL OR f.md5 IS NOT NULL OR f.crc32 IS NOT NULL)"
    )
    params: tuple = ()
    if system:
        sql += " AND g.system = ?"
        params = (system,)

    results = []
    for row in library.db.query(sql + " ORDER BY g.sort_title, f.path", params):
        results.append(GameHealth(
            game_id=row["id"],
            title=row["title"],
            system=row["system"],
            path=row["path"],
            result=verify(
                index,
                crc32=row["crc32"] or "",
                md5=row["md5"] or "",
                sha1=row["sha1"] or "",
                name=Path(row["path"]).name,
            ),
        ))

    return results


def summarise(results: Iterable[GameHealth]) -> dict[Health, int]:
    """How many files landed in each verdict."""
    counts: dict[Health, int] = {health: 0 for health in Health}
    for item in results:
        counts[item.health] += 1
    return counts


def problems(results: Iterable[GameHealth]) -> Iterator[GameHealth]:
    """Only the files worth acting on."""
    for item in results:
        if item.health in (Health.KNOWN_BAD, Health.MODIFIED):
            yield item
