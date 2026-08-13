"""Schema migrations, and the upgrade path real libraries actually take.

The dangerous case is not a fresh database — it is somebody's existing library
gaining a migration. Every user upgrading has games, playtime, notes and
artwork in a database at an older version, and a migration that only works
starting from empty destroys all of it while passing every other test.

So these apply migrations one version at a time, put real data in between, and
check it survives.
"""

from __future__ import annotations

import sqlite3

import pytest

from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database
from rose_gamelab.db.migrations import MIGRATIONS, SCHEMA_VERSION


def build_to_version(path, version: int) -> sqlite3.Connection:
    """A database migrated only as far as `version`, as an older release left it."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    for number, _description, sql in sorted(MIGRATIONS, key=lambda m: m[0]):
        if number > version:
            break
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")

    return conn


def table_names(conn) -> set[str]:
    return {
        row["name"] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


# ── The migration list itself ─────────────────────────────────────

def test_versions_are_unique_and_sequential():
    """A repeated or skipped version silently never applies to somebody."""
    versions = [version for version, _, _ in MIGRATIONS]

    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions == list(range(1, len(versions) + 1))


def test_every_migration_says_what_it_does():
    for version, description, sql in MIGRATIONS:
        assert description.strip(), version
        assert sql.strip(), version


def test_schema_version_is_the_highest_migration():
    assert max(version for version, _, _ in MIGRATIONS) == SCHEMA_VERSION


# ── Fresh databases ───────────────────────────────────────────────

def test_a_new_database_lands_on_the_current_version(tmp_path):
    database = Database(tmp_path / "new.db")

    assert database.version == SCHEMA_VERSION
    database.close()


def test_migrating_twice_does_nothing_the_second_time(tmp_path):
    database = Database(tmp_path / "new.db")

    assert database.migrate() == 0

    database.close()


def test_reopening_does_not_re_run_migrations(tmp_path):
    path = tmp_path / "reopen.db"
    first = Database(path)
    first.close()

    second = Database(path)
    assert second.version == SCHEMA_VERSION
    second.close()


# ── Upgrading an existing library ─────────────────────────────────

@pytest.mark.parametrize("start", [version for version, _, _ in MIGRATIONS][:-1])
def test_upgrading_from_any_older_version_works(tmp_path, start):
    """Every version somebody could be sitting on must reach the current one."""
    path = tmp_path / f"from{start}.db"
    build_to_version(path, start).close()

    database = Database(path)

    assert database.version == SCHEMA_VERSION
    database.close()


def test_a_real_library_survives_the_upgrade(tmp_path):
    """The case that matters: games, playtime and notes already in the file."""
    path = tmp_path / "existing.db"
    conn = build_to_version(path, SCHEMA_VERSION - 1)
    conn.execute(
        "INSERT INTO games (title, sort_title, system, added_at)"
        " VALUES ('Chrono Trigger', 'chrono trigger', 'snes', '2026-01-01')"
    )
    conn.execute(
        "UPDATE games SET play_seconds = 7200, notes = 'slot 3 is the good one'"
    )
    conn.close()

    database = Database(path)
    row = database.query_one("SELECT * FROM games")

    assert row["title"] == "Chrono Trigger"
    assert row["play_seconds"] == 7200
    assert row["notes"] == "slot 3 is the good one"
    database.close()


def test_the_controller_tables_arrive_in_the_upgrade(tmp_path):
    path = tmp_path / "old.db"
    conn = build_to_version(path, 3)
    assert "controller_profiles" not in table_names(conn)
    conn.close()

    database = Database(path)

    assert "controller_profiles" in table_names(database.conn)
    assert "game_controller_profiles" in table_names(database.conn)
    database.close()


def test_an_upgraded_library_can_still_be_used(tmp_path):
    """Schema present is not the same as schema usable."""
    path = tmp_path / "old.db"
    build_to_version(path, 3).close()

    database = Database(path)
    library = Library(database)
    game_id = library.add_game(title="New Game", system="snes", path="/roms/n.sfc")

    assert library.get(game_id).title == "New Game"
    database.close()


# ── Constraints the new tables rely on ────────────────────────────

def test_one_profile_per_pad(tmp_path):
    database = Database(tmp_path / "c.db")
    values = ("Pad", "g" * 32, "", "mapping", "user", None, "now", "now")
    database.execute(
        "INSERT INTO controller_profiles"
        " (name, guid, device_name, mapping, source, player, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values
    )

    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO controller_profiles"
            " (name, guid, device_name, mapping, source, player, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values
        )

    database.close()


def test_two_pads_may_both_be_unassigned(tmp_path):
    """The player index is unique, but NULL is not a player — without a partial
    index every unassigned pad would collide with every other."""
    database = Database(tmp_path / "c.db")

    for index in range(3):
        database.execute(
            "INSERT INTO controller_profiles"
            " (name, guid, device_name, mapping, source, player, created_at, updated_at)"
            " VALUES (?, ?, '', 'm', 'user', NULL, 'now', 'now')",
            (f"Pad {index}", f"{index}" * 32),
        )

    assert len(database.query("SELECT * FROM controller_profiles")) == 3
    database.close()


def test_two_pads_cannot_share_a_player_slot(tmp_path):
    database = Database(tmp_path / "c.db")
    database.execute(
        "INSERT INTO controller_profiles"
        " (name, guid, device_name, mapping, source, player, created_at, updated_at)"
        " VALUES ('A', 'a', '', 'm', 'user', 1, 'now', 'now')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO controller_profiles"
            " (name, guid, device_name, mapping, source, player, created_at, updated_at)"
            " VALUES ('B', 'b', '', 'm', 'user', 1, 'now', 'now')"
        )

    database.close()
