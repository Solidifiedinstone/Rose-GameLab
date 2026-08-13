"""Importing loose ROMs into an organised library folder.

The everyday case this exists for: a ROM lands in ~/Downloads, and filing it by
hand means knowing which system it belongs to, finding the right folder, and
remembering to keep multi-disc sets together. GameLab already knows how to
answer all three questions, so it can just do it.

Identification runs strongest-evidence-first, the same order used everywhere
else: content hash against the offline database, then unambiguous file
extension, then the folder the file came from. A file that cannot be identified
is NOT moved — a ROM filed under the wrong system is worse than one left in
Downloads, because the user will look for it where they expect it to be.

Moving a user's files is the most destructive thing GameLab does, so:
  - nothing is ever overwritten; a name clash is reported, not resolved
  - multi-disc sets move together or not at all
  - copy is available for anyone who would rather not move originals
  - every action is reported individually, including the ones that failed
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from rose_gamelab.core import folder_games
from rose_gamelab.core.discs import GameGroup, group_discs
from rose_gamelab.core.emulator import SYSTEMS, get_system
from rose_gamelab.core.folder_games import FolderGame
from rose_gamelab.core.hashing import hash_file, should_hash
from rose_gamelab.core.media import MediaKind, classify
from rose_gamelab.core.scanner import folder_group, infer_system, walk_library

logger = logging.getLogger(__name__)


def default_library_root() -> Path:
    """Where organised ROMs live by default.

    Under the user's home rather than a hidden directory, because this is
    content they own and will want to browse, back up and copy to other
    machines — not application state.
    """
    return Path(os.environ.get("ROSE_ROM_ROOT") or Path.home() / "Games" / "ROMs")


@dataclass
class ImportPlan:
    """What would happen to one game, decided before anything moves."""

    group: GameGroup
    system_id: Optional[str]
    destination: Optional[Path] = None
    #: 'hash' | 'extension' | 'folder' | 'layout' | 'user' — the evidence.
    identified_by: str = ""
    #: Populated when this cannot be imported, and says why in plain words.
    problem: str = ""
    #: Set when this game IS a directory (a PS3 title, an extracted disc).
    #: The whole tree moves as one; the file inside it never moves alone.
    folder: Optional[FolderGame] = None

    @property
    def ok(self) -> bool:
        return self.problem == "" and self.system_id is not None and self.destination is not None

    @property
    def title(self) -> str:
        return self.group.title

    @property
    def media_kind(self) -> MediaKind:
        """What shape this game is on disk, decided when it was planned."""
        if self.folder is not None:
            return MediaKind.FOLDER
        if self.group.is_multi_disc:
            return MediaKind.PLAYLIST
        return classify(self.group.primary_file, system_id=self.system_id)

    @property
    def source_paths(self) -> list[Path]:
        """What is actually on disk for this game, as the user would see it.

        A folder game is its directory — NOT the EBOOT.BIN buried inside it,
        which is what an emulator is pointed at but is meaningless on its own.
        """
        if self.folder is not None:
            return [self.folder.root]
        return [disc.path for disc in self.group.sorted_files]

    @property
    def system_name(self) -> str:
        system = get_system(self.system_id) if self.system_id else None
        return system.name if system else (self.system_id or "unknown")


@dataclass
class ImportOutcome:
    """What actually happened. Every file is accounted for."""

    moved: list[tuple[Path, Path]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (title, why)
    errors: list[str] = field(default_factory=list)

    @property
    def files_moved(self) -> int:
        return len(self.moved)


class RomImporter:
    """Files loose ROMs into per-system folders."""

    def __init__(
        self,
        *,
        root: Optional[Path] = None,
        openvgdb=None,
    ) -> None:
        self.root = Path(root) if root else default_library_root()

        if openvgdb is None:
            from rose_gamelab.metadata.openvgdb import OpenVGDBProvider
            openvgdb = OpenVGDBProvider()
        self.openvgdb = openvgdb

    # ── Planning ──────────────────────────────────────────────────

    def folder_for(self, system_id: str) -> Path:
        """The directory a system's games belong in.

        Named after the system's human name rather than its internal id, so the
        folder tree makes sense to someone browsing it in a file manager who
        has never heard of GameLab.
        """
        system = get_system(system_id)
        name = system.name if system else system_id
        # Keep it filesystem-safe without mangling it beyond recognition.
        safe = "".join("-" if c in '/\\:*?"<>|' else c for c in name).strip()
        return self.root / safe

    def identify(self, group: GameGroup, *, hint: Optional[str] = None) -> tuple[Optional[str], str]:
        """Work out which system a game belongs to, and on what evidence.

        Returns (system id or None, how it was decided).
        """
        if hint and hint in SYSTEMS:
            return hint, "user"

        primary = group.primary_file

        # Strongest evidence: the file's content, looked up in the offline
        # database. This is the only method that survives a bad filename.
        if self.openvgdb.available() and should_hash(primary):
            try:
                hashes = hash_file(primary)
                found = self.openvgdb.identify(
                    sha1=hashes.sha1, md5=hashes.md5, crc32=hashes.crc32
                )
                if found is not None and found.exact:
                    system_id = self._system_from_extension(primary)
                    if system_id:
                        return system_id, "hash"
            except OSError as exc:
                logger.debug("could not hash %s: %s", primary, exc)

        system_id = infer_system(primary, hint=None)
        if system_id:
            # infer_system already prefers an unambiguous extension and only
            # then falls back to reading the folder path.
            from rose_gamelab.core.emulator import systems_for_extension
            decided_by = "extension" if len(systems_for_extension(primary.suffix)) == 1 else "folder"
            return system_id, decided_by

        return None, ""

    @staticmethod
    def _system_from_extension(path: Path) -> Optional[str]:
        from rose_gamelab.core.emulator import systems_for_extension

        candidates = systems_for_extension(path.suffix)
        return candidates[0].id if len(candidates) == 1 else None

    def _plan_folder(self, game: FolderGame) -> ImportPlan:
        """Plan a game that is a directory. The whole tree moves, or none of it.

        The system is never inferred here and never overridden by the user's
        hint: it comes from the game's own marker files, which are the dump
        saying what it is rather than anyone guessing from a name.
        """
        destination = self.folder_for(game.system_id)
        plan = ImportPlan(
            group=folder_group(game),
            system_id=game.system_id,
            destination=destination,
            identified_by="layout",
            folder=game,
        )

        target = destination / game.root.name
        if target.exists() and target != game.root:
            plan.problem = f"Already in your library: {game.root.name}"
        elif game.root.parent == destination:
            plan.problem = "Already filed here"

        return plan

    def plan(
        self,
        paths: Iterable[Path],
        *,
        hint: Optional[str] = None,
    ) -> list[ImportPlan]:
        """Decide what would happen to each game, without touching anything.

        Always run and shown to the user before importing, so nothing moves
        without them having seen where it is going.
        """
        files: list[Path] = []
        #: Keyed by root so dropping a folder game twice — or dropping several
        #: files from inside one — still plans it exactly once.
        folders: dict[Path, FolderGame] = {}

        for path in paths:
            path = Path(path)

            if path.is_dir():
                for found in walk_library(path):
                    if isinstance(found, FolderGame):
                        folders.setdefault(found.root, found)
                    else:
                        files.append(found)
                continue

            if not path.is_file():
                continue

            # A file the user dropped may be part of a folder game, in which
            # case the game is what moves. Moving the EBOOT.BIN out of a PS3
            # folder on its own destroys the dump.
            game = folder_games.game_root_for(path)
            if game is not None:
                folders.setdefault(game.root, game)
            else:
                files.append(path)

        plans: list[ImportPlan] = [
            self._plan_folder(game) for game in folders.values()
        ]

        for group in group_discs(files):
            system_id, evidence = self.identify(group, hint=hint)

            if system_id is None:
                plans.append(ImportPlan(
                    group=group, system_id=None, identified_by="",
                    problem="Could not tell which system this is for. "
                            "Choose one to import it anyway.",
                ))
                continue

            destination = self.folder_for(system_id)
            plan = ImportPlan(
                group=group, system_id=system_id,
                destination=destination, identified_by=evidence,
            )

            # A clash is reported rather than resolved: silently renaming or
            # overwriting someone's ROM is not ours to decide.
            clashes = [
                disc.path.name for disc in group.sorted_files
                if (destination / disc.path.name).exists()
                and (destination / disc.path.name) != disc.path
            ]
            if clashes:
                plan.problem = (
                    f"Already in your library: {', '.join(clashes[:3])}"
                    + ("…" if len(clashes) > 3 else "")
                )

            # Already filed correctly — nothing to do, and worth saying so.
            if all(disc.path.parent == destination for disc in group.sorted_files):
                plan.problem = "Already filed here"

            plans.append(plan)

        return plans

    # ── Execution ─────────────────────────────────────────────────

    def apply(
        self,
        plans: Iterable[ImportPlan],
        *,
        move: bool = True,
        progress: Optional[Callable[[str, int, int], None]] = None,
    ) -> ImportOutcome:
        """Carry out a plan. Returns exactly what happened.

        A multi-disc set moves as a unit: if any disc cannot be placed, none of
        them are, because half a game in the library is worse than none.
        """
        outcome = ImportOutcome()
        plans = [p for p in plans]
        total = len(plans)

        for index, plan in enumerate(plans, start=1):
            if progress:
                progress(plan.title, index, total)

            if not plan.ok:
                outcome.skipped.append((plan.title, plan.problem or "not identified"))
                continue

            destination = plan.destination
            try:
                destination.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                outcome.errors.append(f"{plan.title}: cannot create {destination}: {exc}")
                continue

            if plan.folder is not None:
                self._place_folder(plan, destination, move=move, outcome=outcome)
                continue

            # Verify the whole set can be placed before moving any of it.
            targets = []
            blocked = None
            for disc in plan.group.sorted_files:
                target = destination / disc.path.name
                if target.exists() and target != disc.path:
                    blocked = disc.path.name
                    break
                targets.append((disc.path, target))

            if blocked:
                outcome.skipped.append((plan.title, f"{blocked} already exists"))
                continue

            placed: list[tuple[Path, Path]] = []
            try:
                for source, target in targets:
                    if source == target:
                        continue
                    if move:
                        shutil.move(str(source), str(target))
                    else:
                        shutil.copy2(source, target)
                    placed.append((source, target))
            except OSError as exc:
                outcome.errors.append(f"{plan.title}: {exc}")
                # Put back whatever already moved, so a failure part-way
                # through does not leave a game split across two folders.
                if move:
                    for source, target in placed:
                        try:
                            shutil.move(str(target), str(source))
                        except OSError:
                            outcome.errors.append(
                                f"{plan.title}: could not restore {target} to {source}"
                            )
                continue

            outcome.moved.extend(placed)

        return outcome

    @staticmethod
    def _place_folder(
        plan: ImportPlan,
        destination: Path,
        *,
        move: bool,
        outcome: ImportOutcome,
    ) -> None:
        """Move or copy a whole game directory into `destination`.

        Never merges into an existing directory: a half-overwritten PS3 dump
        launches, runs for an hour and then fails on a file from the wrong
        release, which is far worse than being told the name is taken.
        """
        source = plan.folder.root
        target = destination / source.name

        if target.exists() and target != source:
            outcome.skipped.append((plan.title, f"{source.name} already exists"))
            return
        if target == source:
            outcome.skipped.append((plan.title, "already filed here"))
            return

        try:
            if move:
                shutil.move(str(source), str(target))
            else:
                # dirs_exist_ok stays False: the guard above already decided
                # this name is free, and merging is exactly what must not happen.
                shutil.copytree(source, target, symlinks=True)
        except (OSError, shutil.Error) as exc:
            outcome.errors.append(f"{plan.title}: {exc}")
            return

        outcome.moved.append((source, target))

    # ── Convenience ───────────────────────────────────────────────

    def existing_systems(self) -> list[tuple[str, int]]:
        """(system name, game count) for what is already in the ROM folder."""
        if not self.root.is_dir():
            return []

        rows = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            # Directories count too: a PS3 folder is a game, and counting only
            # files reported a shelf of forty titles as empty.
            count = sum(1 for p in directory.iterdir() if not p.name.startswith("."))
            if count:
                rows.append((directory.name, count))
        return rows
