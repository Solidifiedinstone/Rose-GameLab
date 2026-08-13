"""Tidying a library by hand: merging duplicates, and editing games in bulk.

Both of these are things `maintenance.py` deliberately refuses to do on its own.
Merging two entries is a judgement about which copy is the real one, and setting
the system on forty games at once is a claim only the person who owns them can
make. So they live here, as operations somebody asks for explicitly, rather than
as repairs a cleanup pass performs quietly.

Merging keeps one entry and moves everything the other had onto it — files,
launch options, playtime, achievements, collections. The surviving game ends up
with two ways to start, which is the same shape import-time duplicate detection
produces when it finds the same game on Steam and as a ROM. Nothing on disk is
touched, ever: the loser's files are re-pointed at the winner, not deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


def _label_for(game) -> str:
    """What to call one way of playing, when an entry has several.

    The system, because that is the difference a person is choosing between:
    "PlayStation 2" and "PlayStation 3", not two identical game titles.
    """
    from rose_gamelab.core.emulator import get_system

    system = get_system(game.system)
    return system.name if system else (game.system or "Other")


@dataclass
class MergeResult:
    """What a merge moved."""

    kept: int
    removed: list[int] = field(default_factory=list)
    files: int = 0
    launch_options: int = 0
    play_seconds: int = 0
    achievements: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.errors:
            return "; ".join(self.errors)
        return (
            f"Merged {len(self.removed)} entr"
            f"{'y' if len(self.removed) == 1 else 'ies'}: "
            f"{self.files} file(s) and {self.launch_options} way(s) to play "
            "moved across"
        )


def merge_games(library, keep_id: int, merge_ids: Iterable[int]) -> MergeResult:
    """Fold one or more entries into another. Returns what moved.

    Playtime is summed rather than taken from the survivor: the hours are real
    either way, and quietly dropping half of somebody's recorded time because
    they tidied their library would be the worst possible outcome of a tidy-up.

    A file that is already on the surviving game is dropped rather than
    duplicated — merging the same game twice must not leave it with two
    identical paths.
    """
    keeper = library.get(keep_id)
    if keeper is None:
        return MergeResult(kept=keep_id, errors=["The game to keep no longer exists."])

    losers = [game_id for game_id in merge_ids if game_id != keep_id]
    if not losers:
        return MergeResult(kept=keep_id, errors=["Nothing to merge."])

    result = MergeResult(kept=keep_id)
    existing_paths = {
        row["path"] for row in
        library.db.query("SELECT path FROM game_files WHERE game_id = ?", (keep_id,))
    }

    # What the survivor is missing and a loser might have. Collected as the
    # losers are visited, because after the merge they are gone and there is
    # nothing left to read it from.
    # Read off the Game object, which is why `ra_game_id` is not in this list:
    # the dataclass does not carry it, so `getattr` would quietly answer None
    # and the link would be lost. It is fetched from the row instead, below.
    inheritable = (
        "cover_path", "summary", "release_date", "developer", "publisher",
        "notes",
    )
    inherited: dict[str, object] = {}
    play_count = int(getattr(keeper, "play_count", 0) or 0)

    for loser_id in losers:
        loser = library.get(loser_id)
        if loser is None:
            result.errors.append(f"Game {loser_id} no longer exists.")
            continue

        with library.db.transaction() as cur:
            for row in cur.execute(
                "SELECT id, path FROM game_files WHERE game_id = ?", (loser_id,)
            ).fetchall():
                if row["path"] in existing_paths:
                    continue
                cur.execute(
                    "UPDATE game_files SET game_id = ? WHERE id = ?",
                    (keep_id, row["id"]),
                )
                existing_paths.add(row["path"])
                result.files += 1

            # Stamped with the system they came from before they move, or a
            # PS3 disc folded into a PS2 entry would be handed to PCSX2. The
            # launcher reads this per option, not per game.
            moved = cur.execute(
                "UPDATE launch_options"
                "   SET game_id = ?, is_primary = 0,"
                "       system = COALESCE(system, ?),"
                "       label = COALESCE(label, ?)"
                " WHERE game_id = ?",
                (keep_id, loser.system, _label_for(loser), loser_id),
            ).rowcount
            result.launch_options += max(0, moved)

            cur.execute(
                "UPDATE play_sessions SET game_id = ? WHERE game_id = ?",
                (keep_id, loser_id),
            )

            # Achievements are keyed (game_id, ra_id) and the two entries may
            # both have some, so a plain update can collide. The survivor's own
            # rows win; a collision means the same achievement twice.
            for row in cur.execute(
                "SELECT ra_id FROM achievements WHERE game_id = ?", (loser_id,)
            ).fetchall():
                clash = cur.execute(
                    "SELECT 1 FROM achievements WHERE game_id = ? AND ra_id = ?",
                    (keep_id, row["ra_id"]),
                ).fetchone()
                if clash:
                    continue
                cur.execute(
                    "UPDATE achievements SET game_id = ? WHERE game_id = ? AND ra_id = ?",
                    (keep_id, loser_id, row["ra_id"]),
                )
                result.achievements += 1

            for row in cur.execute(
                "SELECT collection_id FROM collection_games WHERE game_id = ?",
                (loser_id,),
            ).fetchall():
                cur.execute(
                    "INSERT OR IGNORE INTO collection_games (collection_id, game_id)"
                    " VALUES (?, ?)",
                    (row["collection_id"], keep_id),
                )

            result.play_seconds += int(loser.play_seconds or 0)

        play_count += int(getattr(loser, "play_count", 0) or 0)

        # Read while the loser still exists. A merge must never lose art or a
        # description that only the entry being folded in had.
        for column in inheritable:
            if column in inherited:
                continue
            if getattr(keeper, column, None) not in (None, ""):
                continue
            value = getattr(loser, column, None)
            if value not in (None, ""):
                inherited[column] = value

        if "ra_game_id" not in inherited:
            row = library.db.query_one(
                "SELECT ra_game_id FROM games WHERE id = ?", (loser_id,)
            )
            keeper_link = library.db.query_one(
                "SELECT ra_game_id FROM games WHERE id = ?", (keep_id,)
            )
            if row and row["ra_game_id"] and not (keeper_link and keeper_link["ra_game_id"]):
                inherited["ra_game_id"] = row["ra_game_id"]

        # Outside the transaction: this is the destructive half, and it should
        # not be able to take the moves with it if it fails.
        library.remove_game(loser_id)
        result.removed.append(loser_id)

    if result.removed:
        # Playtime and the RetroAchievements link are written directly, because
        # `update_game` whitelists the columns it will touch and silently drops
        # anything else — which is correct for metadata edits, and is what
        # `bulk_update` below relies on, but it meant a merge quietly discarded
        # the hours it had just finished adding up.
        library.db.execute(
            "UPDATE games SET play_seconds = ?, play_count = ? WHERE id = ?",
            (
                int(keeper.play_seconds or 0) + result.play_seconds,
                play_count,
                keep_id,
            ),
        )

        ra_game_id = inherited.pop("ra_game_id", None)
        if ra_game_id is not None:
            library.db.execute(
                "UPDATE games SET ra_game_id = ? WHERE id = ?", (ra_game_id, keep_id)
            )

        if inherited:
            library.update_game(keep_id, **inherited)

    return result


# ── Bulk editing ──────────────────────────────────────────────────

@dataclass
class BulkResult:
    """What a bulk edit changed."""

    changed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.errors:
            return "; ".join(self.errors)
        return f"{self.changed} game(s) updated"


#: Fields a bulk edit may set. Whitelisted rather than passed through, so a
#: caller cannot rewrite an id, a hash, or a playtime by accident.
BULK_FIELDS = ("system", "source_id", "favorite", "hidden")


def bulk_update(library, game_ids: Iterable[int], **fields) -> BulkResult:
    """Apply the same change to several games.

    Anyone who imports a messy folder wants this within five minutes: forty
    games filed under the wrong system, or a batch that should all be hidden.
    """
    wanted = {
        name: value for name, value in fields.items()
        if name in BULK_FIELDS and value is not None
    }
    ids = [int(game_id) for game_id in game_ids]

    if not wanted:
        return BulkResult(errors=["Nothing to change."])
    if not ids:
        return BulkResult(errors=["No games selected."])

    result = BulkResult()
    with library.db.transaction():
        for game_id in ids:
            library.update_game(game_id, **wanted)
            result.changed += 1

    return result


def add_to_collection(library, game_ids: Iterable[int], collection_id: int) -> BulkResult:
    """Put several games in a collection at once."""
    result = BulkResult()
    with library.db.transaction():
        for game_id in game_ids:
            try:
                library.add_to_collection(collection_id, int(game_id))
            except Exception as exc:
                result.errors.append(f"{game_id}: {exc}")
                continue
            result.changed += 1
    return result
