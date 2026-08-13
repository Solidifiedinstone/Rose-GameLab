"""Finding what has quietly gone wrong in a library, and offering to fix it.

Libraries rot in small ways that nothing announces. A drive gets unplugged and
forty games point at files that are not there. Artwork is downloaded for a game
that is later removed and the picture stays in the cache forever. An emulator is
uninstalled and the games that needed it become unlaunchable without saying so.
Two copies of the same game arrive from two sources. None of these is an error
at the moment it happens, so nothing raises and nothing is logged; they are only
visible when someone goes looking.

Every check here already existed somewhere — the scanner knows about missing
files, the library knows about orphans, detection knows which emulators are
installed. What was missing was one place that asks all of them at once and
reports in a form a person can act on.

Two rules shape this module. Nothing is deleted without being asked for first:
`inspect()` only looks, and `repair()` acts, so a caller can always show the
findings before touching anything. And a game's files are never deleted, only
ever the library's own records and its own cache — an unplugged drive must
grey a game out, not erase it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from rose_gamelab.core import emulator_detect
from rose_gamelab.core.emulator import get_system

logger = logging.getLogger(__name__)

#: Kinds of problem, in the order they are worth showing.
KINDS = (
    "missing_files",
    "no_emulator",
    "duplicates",
    "orphaned_games",
    "orphaned_art",
    "empty_collections",
)

_DESCRIPTIONS = {
    "missing_files": "games whose file is not on disk",
    "no_emulator": "games whose system has no emulator installed",
    "duplicates": "games that look like the same game twice",
    "orphaned_games": "games left behind by a removed source",
    "orphaned_art": "artwork files no game refers to",
    "empty_collections": "collections with nothing in them",
}


@dataclass
class Finding:
    """One problem, and what it concerns."""

    kind: str
    #: Human-readable, already phrased for display.
    summary: str
    #: Library game ids involved, where the problem is about games.
    game_ids: list[int] = field(default_factory=list)
    #: Files on the filesystem involved, where it is about files.
    paths: list[str] = field(default_factory=list)
    #: Whether `repair()` can act on this without the user deciding anything.
    repairable: bool = False

    @property
    def count(self) -> int:
        return len(self.game_ids) or len(self.paths)

    @property
    def description(self) -> str:
        return _DESCRIPTIONS.get(self.kind, self.kind)


@dataclass
class Report:
    """Everything one inspection found."""

    findings: list[Finding] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.findings)

    @property
    def total(self) -> int:
        return sum(finding.count for finding in self.findings)

    def of_kind(self, kind: str) -> Optional[Finding]:
        return next((f for f in self.findings if f.kind == kind), None)

    @property
    def repairable(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.repairable]


def _art_directory() -> Path:
    from rose_gamelab.metadata.cache import _cache_dir

    return _cache_dir()


def inspect(library, *, art_directory: Optional[Path] = None) -> Report:
    """Look for everything that has gone quietly wrong. Changes nothing."""
    report = Report()

    for check in (
        _missing_files,
        _games_without_an_emulator,
        _duplicates,
        _orphaned_games,
        _empty_collections,
    ):
        finding = check(library)
        if finding is not None:
            report.findings.append(finding)

    art = _orphaned_art(library, art_directory or _art_directory())
    if art is not None:
        report.findings.append(art)

    # Reported in the order KINDS lists, which is roughly worst first.
    report.findings.sort(key=lambda f: KINDS.index(f.kind) if f.kind in KINDS else 99)
    return report


# ── The checks ────────────────────────────────────────────────────

def _missing_files(library) -> Optional[Finding]:
    """Games whose file is gone. Flagged, never removed: it may be a drive."""
    rows = library.db.query(
        "SELECT DISTINCT g.id, g.title, f.path"
        "  FROM games g JOIN game_files f ON f.game_id = g.id"
        " WHERE f.missing = 1"
        " ORDER BY g.sort_title"
    )
    if not rows:
        return None

    return Finding(
        kind="missing_files",
        summary=(
            f"{len(rows)} game(s) point at a file that is not there. "
            "If a drive is unplugged, plug it in and rescan rather than removing them."
        ),
        game_ids=[row["id"] for row in rows],
        paths=[row["path"] for row in rows],
        # Deliberately not repairable: deleting these is a decision about
        # somebody's collection, not a tidy-up.
        repairable=False,
    )


def _games_without_an_emulator(library) -> Optional[Finding]:
    """Games that cannot be launched because nothing is installed to run them."""
    rows = library.db.query(
        "SELECT system, COUNT(*) AS n FROM games"
        " WHERE hidden = 0 GROUP BY system ORDER BY n DESC"
    )

    stranded: list[str] = []
    total = 0
    for row in rows:
        system_id = row["system"]
        if not system_id or system_id == "pc":
            continue
        if emulator_detect.best_for(system_id) is not None:
            continue

        system = get_system(system_id)
        options = [o for o in emulator_detect.options_for(system_id) if not o.installed]
        hint = f" — try `{options[0].install_hint}`" if options else ""
        stranded.append(
            f"{row['n']} × {system.name if system else system_id}{hint}"
        )
        total += int(row["n"])

    if not stranded:
        return None

    return Finding(
        kind="no_emulator",
        summary="Nothing installed can run these:\n  " + "\n  ".join(stranded),
        game_ids=[],
        repairable=False,
    )


def _duplicates(library) -> Optional[Finding]:
    """Entries that look like the same game twice.

    Matched on normalised title *and* system, which is what duplicate detection
    uses when importing. Reported rather than merged: which copy to keep depends
    on which one launches better, and that is not ours to decide.
    """
    from rose_gamelab.metadata.base import normalise_for_match

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in library.db.query("SELECT id, title, system FROM games WHERE hidden = 0"):
        key = (normalise_for_match(row["title"] or ""), row["system"] or "")
        if key[0]:
            groups[key].append(row["id"])

    duplicated = {key: ids for key, ids in groups.items() if len(ids) > 1}
    if not duplicated:
        return None

    extra = sum(len(ids) - 1 for ids in duplicated.values())
    return Finding(
        kind="duplicates",
        summary=(
            f"{len(duplicated)} title(s) appear more than once "
            f"({extra} extra entr{'y' if extra == 1 else 'ies'}). "
            "Which copy to keep depends on which one launches better."
        ),
        game_ids=[game_id for ids in duplicated.values() for game_id in ids],
        repairable=False,
    )


def _orphaned_games(library) -> Optional[Finding]:
    """Games left behind when their source was removed.

    Only reported when the library actually has sources. A game added by hand —
    a custom entry, a ROM dropped on the window — has no source and never had
    one, and calling that a problem would flag an entirely hand-built library as
    broken. A maintenance tool that cries wolf is one people learn to ignore.
    """
    sources = library.db.query_one("SELECT COUNT(*) AS n FROM sources")
    if not sources or not int(sources["n"]):
        return None

    row = library.db.query_one(
        "SELECT COUNT(*) AS n FROM games WHERE source_id IS NULL"
    )
    count = int(row["n"]) if row else 0
    if not count:
        return None

    return Finding(
        kind="orphaned_games",
        summary=(
            f"{count} game(s) belong to no source. Usually a source was removed "
            "and its games were kept, which is fine — but they only appear under "
            "\"No source\" in the sidebar."
        ),
        repairable=False,
    )


def _empty_collections(library) -> Optional[Finding]:
    rows = library.db.query(
        "SELECT c.id, c.name FROM collections c"
        " LEFT JOIN collection_games g ON g.collection_id = c.id"
        " GROUP BY c.id HAVING COUNT(g.game_id) = 0"
    )
    if not rows:
        return None

    return Finding(
        kind="empty_collections",
        summary=f"{len(rows)} collection(s) contain nothing: "
                + ", ".join(row["name"] for row in rows),
        game_ids=[row["id"] for row in rows],
        repairable=True,
    )


def _orphaned_art(library, directory: Path) -> Optional[Finding]:
    """Cached artwork no game refers to any more.

    Only files inside GameLab's own cache are ever considered. Art a user
    pointed at from their own folder is theirs, and deleting it because a game
    was removed would be destroying something we did not create.
    """
    try:
        if not directory.is_dir():
            return None
        cached = [path for path in directory.rglob("*") if path.is_file()]
    except OSError as exc:
        logger.warning("could not read the artwork cache: %s", exc)
        return None

    if not cached:
        return None

    referenced = set()
    for column in ("cover_path", "hero_path", "logo_path"):
        for row in library.db.query(
            f"SELECT {column} AS p FROM games WHERE {column} IS NOT NULL"
        ):
            referenced.add(str(Path(row["p"]).resolve()))

    orphaned = [
        path for path in cached
        if str(path.resolve()) not in referenced
    ]
    if not orphaned:
        return None

    size = sum(path.stat().st_size for path in orphaned if path.exists())
    return Finding(
        kind="orphaned_art",
        summary=(
            f"{len(orphaned)} cached image(s) no game refers to, "
            f"{size // 1024} KB. Safe to delete; they re-download if needed."
        ),
        paths=[str(path) for path in orphaned],
        repairable=True,
    )


# ── Repair ────────────────────────────────────────────────────────

@dataclass
class RepairResult:
    """What a repair actually did."""

    removed_art: int = 0
    freed_bytes: int = 0
    removed_collections: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def anything_done(self) -> bool:
        return bool(self.removed_art or self.removed_collections)

    @property
    def summary(self) -> str:
        if not self.anything_done:
            return "Nothing needed fixing."
        parts = []
        if self.removed_art:
            parts.append(
                f"deleted {self.removed_art} unused image(s), "
                f"freeing {self.freed_bytes // 1024} KB"
            )
        if self.removed_collections:
            parts.append(f"removed {self.removed_collections} empty collection(s)")
        return "; ".join(parts)


def repair(library, report: Report, *, kinds: Optional[Iterable[str]] = None) -> RepairResult:
    """Fix what can be fixed without a judgement call.

    Only the findings marked `repairable` are ever acted on, and only those the
    caller asks for. Missing files, duplicates and orphaned games are all
    deliberately excluded: each is a decision about somebody's collection, and
    a maintenance command that quietly deletes games is a maintenance command
    nobody should run.
    """
    wanted = set(kinds) if kinds is not None else {f.kind for f in report.repairable}
    result = RepairResult()

    art = report.of_kind("orphaned_art")
    if art is not None and "orphaned_art" in wanted:
        for raw in art.paths:
            path = Path(raw)
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:
                result.errors.append(f"{path.name}: {exc}")
                continue
            result.removed_art += 1
            result.freed_bytes += size

    empty = report.of_kind("empty_collections")
    if empty is not None and "empty_collections" in wanted:
        for collection_id in empty.game_ids:
            try:
                library.delete_collection(collection_id)
            except Exception as exc:
                result.errors.append(f"collection {collection_id}: {exc}")
                continue
            result.removed_collections += 1

    return result
