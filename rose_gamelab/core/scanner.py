"""Scanning: turn folders of files into library entries.

The pipeline is deliberately linear and each stage is independently testable:

    walk directory -> filter by extension -> group discs -> write playlists
                   -> import into library -> (later) hash and identify

Hashing is separated from scanning because it is orders of magnitude slower: a
scan of ten thousand ROMs is a directory walk, while hashing them is reading
every byte. Scanning gets the library populated immediately; hashing runs
afterwards to sharpen identification.

Nothing here touches the network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from rose_gamelab.core import folder_games
from rose_gamelab.core.discs import (
    DiscFile,
    GameGroup,
    group_discs,
    normalise_title,
    write_m3u,
)
from rose_gamelab.core.emulator import SYSTEMS, get_system, systems_for_extension
from rose_gamelab.core.folder_games import FolderGame
from rose_gamelab.core.hashing import hash_file, should_hash
from rose_gamelab.core.library import ImportResult, Library
from rose_gamelab.db.database import utc_now

logger = logging.getLogger(__name__)

# Directories that never contain games and are expensive or noisy to walk.
SKIP_DIRECTORIES = {
    ".git", ".svn", "__pycache__", "node_modules",
    "System Volume Information", "$RECYCLE.BIN", ".Trash-1000",
    # Emulator working directories that sit alongside ROMs.
    "saves", "savestates", "states", "screenshots", "cheats", "bios",
    "media", "covers", "artwork", "manuals", "themes",
}

# Files that look like ROMs by extension but are support files.
SKIP_FILENAMES = {"neogeo.zip", "pgm.zip", "decrypted.zip"}


@dataclass
class ScanResult:
    """What a scan found. Reported to the user verbatim — never estimated."""

    files_seen: int = 0
    games_found: int = 0
    playlists_written: int = 0
    #: Entries deleted because they were files from inside a folder game.
    debris_removed: int = 0
    imported: ImportResult = field(default_factory=ImportResult)
    errors: list[str] = field(default_factory=list)


# ── Walking ───────────────────────────────────────────────────────

def walk_library(
    root: str | Path,
    *,
    extensions: Optional[set[str]] = None,
    recursive: bool = True,
    follow_symlinks: bool = False,
) -> Iterator[Path | FolderGame]:
    """Yield everything under `root` that is a game: files, and game folders.

    A directory is checked against the known folder layouts BEFORE being
    descended into. When it matches it is yielded as one `FolderGame` and its
    contents are never walked — a PS3 title holds thousands of files with ROM
    extensions, and every one of them used to arrive as a separate game.

    Symlinks are not followed by default: ROM collections frequently contain
    symlinks back into themselves, and following them can loop forever.
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return

    # The root itself may be a single game the user pointed at directly.
    game = folder_games.detect(root)
    if game is not None:
        yield game
        return

    stack = [root]
    visited: set[Path] = set()

    while stack:
        directory = stack.pop()

        try:
            resolved = directory.resolve()
        except OSError:
            continue
        if resolved in visited:
            continue
        visited.add(resolved)

        try:
            children = list(directory.iterdir())
        except (PermissionError, OSError) as exc:
            logger.debug("skipping %s: %s", directory, exc)
            continue

        for child in children:
            try:
                if child.is_dir():
                    if child.name in SKIP_DIRECTORIES or child.name.startswith("."):
                        continue
                    if child.is_symlink() and not follow_symlinks:
                        continue

                    # Checked even when not recursing: a folder of PS3 games is
                    # a flat list of games as far as the user is concerned.
                    game = folder_games.detect(child)
                    if game is not None:
                        yield game
                        continue

                    if not recursive:
                        continue
                    stack.append(child)
                    continue

                if child.name.lower() in SKIP_FILENAMES:
                    continue
                if extensions and child.suffix.lower() not in extensions:
                    continue

                yield child
            except OSError:
                continue


def walk_roms(
    root: str | Path,
    *,
    extensions: Optional[set[str]] = None,
    recursive: bool = True,
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    """Yield candidate ROM files under `root`, ignoring folder games.

    The file-only view of `walk_library`, for callers that deal in files.
    Folder games are still pruned rather than descended into, so their contents
    never leak out as hundreds of imaginary games.
    """
    for found in walk_library(
        root,
        extensions=extensions,
        recursive=recursive,
        follow_symlinks=follow_symlinks,
    ):
        if isinstance(found, Path):
            yield found


def folder_group(game: FolderGame) -> GameGroup:
    """Present a folder game as a one-'disc' group the library can import.

    The recorded path is the file the emulator is pointed at, not the folder,
    so launching works without every launcher needing to know about layouts.
    """
    return GameGroup(
        title=normalise_title(game.title),
        files=[DiscFile(path=game.entry)],
    )


# ── System inference ──────────────────────────────────────────────

def infer_system(path: Path, *, hint: Optional[str] = None) -> Optional[str]:
    """Best guess at which system a file belongs to.

    `hint` (the source's configured system) always wins — the user told us.
    Otherwise: an unambiguous extension decides, and for ambiguous ones such as
    `.iso` and `.bin` we look for a system id or name in the folder path.

    Returns None when genuinely undecidable, rather than guessing. A wrong
    system means a wrong emulator and a failed launch, so 'unknown' is the
    honest answer and the interface can ask.
    """
    if hint and hint in SYSTEMS:
        return hint

    candidates = systems_for_extension(path.suffix)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].id

    # Ambiguous: look for a system id or name in the parent directories.
    parts = [part.lower() for part in path.parts[:-1]]
    for system in candidates:
        aliases = {system.id, system.id.replace("_", "-"), system.name.lower()}
        if any(alias in part for part in parts for alias in aliases):
            return system.id

    return None


# ── Scanning ──────────────────────────────────────────────────────

class RomScanner:
    """Scans directories and imports what it finds into the library."""

    def __init__(self, library: Library, *, playlist_dir: Optional[Path] = None) -> None:
        self.library = library
        # Playlists are written beside the discs by default, so a copied ROM
        # folder stays self-contained.
        self.playlist_dir = playlist_dir

    def scan_folder(
        self,
        root: str | Path,
        *,
        system: Optional[str] = None,
        source_id: Optional[str] = None,
        recursive: bool = True,
        progress: Optional[Callable[[str], None]] = None,
    ) -> ScanResult:
        """Scan one folder and import its games.

        `system` is the user's declared system for this folder, and overrides
        inference. Without it, each file's system is inferred and files that
        cannot be resolved are reported rather than silently dropped.
        """
        result = ScanResult()
        root = Path(root).expanduser()

        if not root.is_dir():
            result.errors.append(f"not a directory: {root}")
            return result

        extensions = (
            set(get_system(system).rom_extensions) if system and get_system(system)
            else None
        )

        files: list[Path] = []
        folders: list[FolderGame] = []
        for found in walk_library(root, extensions=extensions, recursive=recursive):
            if isinstance(found, FolderGame):
                folders.append(found)
            else:
                files.append(found)

        result.files_seen = len(files) + len(folders)

        if progress:
            progress(f"Found {len(files) + len(folders)} games in {root.name}")

        # Folder games first: they are already one game each, so they skip
        # extension filtering and disc grouping entirely.
        for game in folders:
            if system and game.system_id != system:
                # The user declared this folder to be one system and this game
                # says otherwise. Believe the game — the marker files are the
                # game's own, not a guess from a filename.
                logger.debug(
                    "%s is %s, not the declared %s", game.root, game.system_id, system
                )
            result.games_found += 1
            try:
                _, outcome = self.library.import_group(
                    folder_group(game),
                    system=game.system_id,
                    source_id=source_id,
                    emulator=(
                        get_system(game.system_id).default_core
                        if get_system(game.system_id) else None
                    ),
                )
                setattr(result.imported, outcome, getattr(result.imported, outcome) + 1)
                if progress:
                    progress(f"{outcome}: {game.title}")
            except Exception as exc:
                result.imported.errors.append(f"{game.title}: {exc}")

        # Group by inferred system first, so disc grouping never merges a PS1
        # rip with a Saturn rip that happens to share a title.
        by_system: dict[Optional[str], list[Path]] = {}
        for path in files:
            by_system.setdefault(infer_system(path, hint=system), []).append(path)

        unknown = by_system.pop(None, [])
        if unknown:
            result.errors.append(
                f"{len(unknown)} file(s) could not be matched to a system"
            )

        for system_id, paths in by_system.items():
            groups = group_discs(paths)
            result.games_found += len(groups)

            emulator = get_system(system_id).default_core if get_system(system_id) else None

            for group in groups:
                try:
                    playlist = None
                    if group.is_multi_disc:
                        target_dir = self.playlist_dir or group.primary_file.parent
                        playlist = write_m3u(group, target_dir)
                        if playlist:
                            result.playlists_written += 1

                    _, outcome = self.library.import_group(
                        group,
                        system=system_id,
                        source_id=source_id,
                        emulator=emulator,
                        playlist=playlist,
                    )
                    setattr(
                        result.imported, outcome,
                        getattr(result.imported, outcome) + 1,
                    )

                    if progress:
                        progress(f"{outcome}: {group.title}")

                except Exception as exc:
                    result.imported.errors.append(f"{group.title}: {exc}")

        if source_id:
            self.library.mark_source_scanned(source_id)

        result.debris_removed = self.remove_folder_game_debris()

        return result

    # ── Repairing earlier scans ───────────────────────────────────

    def remove_folder_game_debris(self) -> int:
        """Delete entries that are really files from inside a folder game.

        Before folder layouts were understood, scanning a PS3 collection
        imported every shader cache and localisation blob inside each game as
        its own title — forty games arriving as three hundred entries. Those
        are removed here rather than left for the user to delete by hand.

        Only entries whose file sits inside a folder game AND is not that
        game's launch file are touched, so a correctly imported game is never
        mistaken for debris.
        """
        seen_roots: dict[str, Optional[FolderGame]] = {}
        debris: set[int] = set()

        for game_id, title, path in self.library.all_game_files():
            file_path = Path(path)
            # Keyed by the containing directory, since every file inside one
            # game shares it. A game launched FROM a directory is keyed by
            # itself instead — sharing its parent's answer with its siblings
            # would report each of them as debris from the first one.
            key = str(file_path if file_path.is_dir() else file_path.parent)

            if key not in seen_roots:
                seen_roots[key] = folder_games.game_root_for(file_path)
            found = seen_roots[key]

            if found is None:
                continue

            # An entry pointing at the right file can still be wrong: one
            # imported as "EBOOT" would sit beside the properly named game
            # forever, since nothing would ever match the two up. Rescanning
            # re-adds it correctly, so removing it here loses nothing.
            if file_path == found.entry and title == normalise_title(found.title):
                continue

            debris.add(game_id)

        # Counted as games, not files: one bogus entry can hold several of a
        # game's inner files, and the number reported to the user is how many
        # things vanish from their library.
        for game_id in debris:
            self.library.remove_game(game_id)

        if debris:
            logger.info("removed %d entries from inside folder games", len(debris))

        return len(debris)

    # ── Hashing pass ──────────────────────────────────────────────

    def hash_pending(
        self,
        *,
        limit: Optional[int] = None,
        progress: Optional[Callable[[str, int, int], None]] = None,
    ) -> int:
        """Hash library files that have not been hashed yet. Returns the count.

        Runs separately from scanning because it reads every byte of every ROM.
        Safe to interrupt: each file is committed as it completes, so a
        cancelled run keeps the work it already did.
        """
        sql = (
            "SELECT id, path FROM game_files WHERE sha1 IS NULL AND missing = 0"
            " ORDER BY id"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"

        rows = self.library.db.query(sql)
        hashed = 0

        for index, row in enumerate(rows, start=1):
            path = Path(row["path"])

            if not path.exists():
                self.library.db.execute(
                    "UPDATE game_files SET missing = 1 WHERE id = ?", (row["id"],)
                )
                continue

            # A folder game is launched by pointing an emulator at a directory;
            # there is no single file to hash, and hashing one file from inside
            # it would identify that file, not the game.
            if path.is_dir() or not should_hash(path):
                continue

            if progress:
                progress(path.name, index, len(rows))

            try:
                hashes = hash_file(path)
            except OSError as exc:
                logger.warning("could not hash %s: %s", path, exc)
                continue

            self.library.db.execute(
                "UPDATE game_files SET crc32 = ?, md5 = ?, sha1 = ?, hashed_at = ?,"
                " size_bytes = COALESCE(size_bytes, ?) WHERE id = ?",
                (
                    hashes.crc32, hashes.md5, hashes.sha1, utc_now(),
                    hashes.size, row["id"],
                ),
            )
            hashed += 1

        return hashed

    def mark_missing_files(self) -> int:
        """Flag library files that are no longer on disk. Returns the count.

        Files are flagged, never deleted: an unplugged external drive should
        grey a game out, not erase its playtime and artwork.

        Existence, not file-ness: some games are launched from a directory, and
        testing for a file marked every one of them missing.
        """
        missing = 0
        for row in self.library.db.query("SELECT id, path, missing FROM game_files"):
            gone = not Path(row["path"]).exists()
            if gone != bool(row["missing"]):
                self.library.db.execute(
                    "UPDATE game_files SET missing = ? WHERE id = ?",
                    (int(gone), row["id"]),
                )
            if gone:
                missing += 1
        return missing
