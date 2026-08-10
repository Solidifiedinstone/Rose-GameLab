"""Tests for the library database layer."""

from __future__ import annotations

import sqlite3

import pytest

from rose_gamelab.db.database import Database, utc_now
from rose_gamelab.db.migrations import MIGRATIONS, SCHEMA_VERSION


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "library.db")
    yield database
    database.close()


def _add_game(db: Database, title: str = "Chrono Trigger", system: str = "snes") -> int:
    cur = db.execute(
        "INSERT INTO games (title, sort_title, system, added_at) VALUES (?, ?, ?, ?)",
        (title, title.lower(), system, utc_now()),
    )
    return cur.lastrowid


# ── Migrations ────────────────────────────────────────────────────

def test_migrates_to_current_version(db):
    assert db.version == SCHEMA_VERSION


def test_migrate_is_idempotent(db):
    assert db.migrate() == 0, "re-running migrations should apply nothing"


def test_reopening_existing_db_does_not_remigrate(tmp_path):
    path = tmp_path / "library.db"
    Database(path).close()
    reopened = Database(path)
    assert reopened.migrate() == 0
    assert reopened.version == SCHEMA_VERSION
    reopened.close()


def test_expected_tables_exist(db):
    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "games", "game_files", "sources", "launch_options", "launch_profiles",
        "play_sessions", "collections", "collection_games", "tags", "game_tags",
        "saves", "achievements",
    } <= names


def test_migrations_are_append_only_and_contiguous(db):
    """A renumbered or edited migration silently skips upgrades for anyone
    whose database already recorded that version."""
    versions = [version for version, _, _ in MIGRATIONS]
    assert versions == sorted(versions)
    assert versions == list(range(1, len(versions) + 1))


def test_upgrading_from_version_1_applies_every_later_migration(tmp_path):
    """Fresh databases never exercise the upgrade path real users take."""
    path = tmp_path / "old.db"
    _, _, first_sql = MIGRATIONS[0]

    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.executescript(f"BEGIN;\n{first_sql}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute(
        "INSERT INTO games (title, sort_title, system, added_at)"
        " VALUES ('Chrono Trigger', 'chrono trigger', 'snes', ?)",
        (utc_now(),),
    )
    conn.close()

    upgraded = Database(path)
    try:
        assert upgraded.version == SCHEMA_VERSION
        assert upgraded.migrate() == 0
        # Pre-existing data survives the upgrade untouched.
        assert upgraded.query_one("SELECT title FROM games")["title"] == "Chrono Trigger"
    finally:
        upgraded.close()


def _with_broken_migration(path, sql: str):
    """Open `path` with one extra, failing migration appended. Returns the error."""
    # database.py binds MIGRATIONS at import time, so the patch must target
    # that name rather than the migrations module's.
    from rose_gamelab.db import database as database_module

    broken = MIGRATIONS + [(SCHEMA_VERSION + 1, "broken", sql)]
    original = database_module.MIGRATIONS
    try:
        database_module.MIGRATIONS = broken
        with pytest.raises(Exception) as excinfo:
            Database(path)
        return excinfo.value
    finally:
        database_module.MIGRATIONS = original


def test_a_failing_migration_does_not_advance_the_version(tmp_path):
    path = tmp_path / "library.db"
    Database(path).close()

    _with_broken_migration(path, "CREATE TABLE oops (")

    reopened = Database(path)
    try:
        assert reopened.version == SCHEMA_VERSION
    finally:
        reopened.close()


def test_a_failing_migration_leaves_no_partial_schema_behind(tmp_path):
    """An interrupted upgrade must roll back, not half-apply.

    Regression: the rollback path used executescript('ROLLBACK;'), but
    executescript issues an implicit COMMIT first — so a failed migration
    committed its partial schema, the ROLLBACK then raised "no transaction
    is active", and the next open failed on "table already exists"
    forever. The fix is execute(), which has no implicit commit.
    """
    path = tmp_path / "library.db"
    Database(path).close()

    error = _with_broken_migration(
        path, "CREATE TABLE half_applied (x);\nCREATE TABLE oops ("
    )
    assert isinstance(error, RuntimeError), "should report which migration failed"

    reopened = Database(path)
    try:
        assert reopened.query_one(
            "SELECT name FROM sqlite_master WHERE name = 'half_applied'"
        ) is None
    finally:
        reopened.close()


def test_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deeper" / "library.db"
    Database(path).close()
    assert path.exists()


# ── Referential integrity ─────────────────────────────────────────

def test_foreign_keys_are_enforced(db):
    """PRAGMA foreign_keys is off by default in SQLite; we must enable it."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO game_files (game_id, path, added_at) VALUES (?, ?, ?)",
            (9999, "/nonexistent/rom.sfc", utc_now()),
        )


def test_deleting_game_cascades_to_files_and_saves(db):
    game_id = _add_game(db)
    db.execute(
        "INSERT INTO game_files (game_id, path, added_at) VALUES (?, ?, ?)",
        (game_id, "/roms/ct.sfc", utc_now()),
    )
    db.execute(
        "INSERT INTO saves (game_id, kind, path, modified_at) VALUES (?, ?, ?, ?)",
        (game_id, "state", "/saves/ct.state0", utc_now()),
    )

    db.execute("DELETE FROM games WHERE id = ?", (game_id,))

    assert db.query("SELECT 1 FROM game_files WHERE game_id = ?", (game_id,)) == []
    assert db.query("SELECT 1 FROM saves WHERE game_id = ?", (game_id,)) == []


def test_file_paths_are_unique(db):
    game_id = _add_game(db)
    db.execute(
        "INSERT INTO game_files (game_id, path, added_at) VALUES (?, ?, ?)",
        (game_id, "/roms/ct.sfc", utc_now()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO game_files (game_id, path, added_at) VALUES (?, ?, ?)",
            (game_id, "/roms/ct.sfc", utc_now()),
        )


def test_multi_disc_game_is_one_game_with_many_files(db):
    """The whole point of splitting games from game_files."""
    game_id = _add_game(db, "Final Fantasy VII", "ps1")
    for disc in (1, 2, 3):
        db.execute(
            "INSERT INTO game_files (game_id, path, disc_number, disc_label, added_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (game_id, f"/roms/ff7-d{disc}.bin", disc, f"Disc {disc}", utc_now()),
        )

    assert len(db.query("SELECT 1 FROM games")) == 1
    files = db.query(
        "SELECT disc_number FROM game_files WHERE game_id = ? ORDER BY disc_number",
        (game_id,),
    )
    assert [f["disc_number"] for f in files] == [1, 2, 3]


def test_deleting_source_keeps_its_games(db):
    """Removing a source must not destroy the user's library entries."""
    db.execute(
        "INSERT INTO sources (id, name, type, added_at) VALUES (?, ?, ?, ?)",
        ("roms-1", "ROM Folder", "rom_folder", utc_now()),
    )
    db.execute(
        "INSERT INTO games (title, sort_title, system, source_id, added_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("Super Metroid", "super metroid", "snes", "roms-1", utc_now()),
    )

    db.execute("DELETE FROM sources WHERE id = ?", ("roms-1",))

    row = db.query_one("SELECT source_id FROM games WHERE title = ?", ("Super Metroid",))
    assert row is not None, "game should survive its source being removed"
    assert row["source_id"] is None


# ── Transactions ──────────────────────────────────────────────────

def test_transaction_commits_on_success(db):
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO games (title, sort_title, system, added_at) VALUES (?, ?, ?, ?)",
            ("Terranigma", "terranigma", "snes", utc_now()),
        )
    assert db.query_one("SELECT 1 FROM games WHERE title = 'Terranigma'") is not None


def test_transaction_rolls_back_on_error(db):
    with pytest.raises(ValueError):
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO games (title, sort_title, system, added_at) VALUES (?, ?, ?, ?)",
                ("Ghost", "ghost", "snes", utc_now()),
            )
            raise ValueError("boom")

    assert db.query_one("SELECT 1 FROM games WHERE title = 'Ghost'") is None


# ── Full-text search ──────────────────────────────────────────────

def test_fts_finds_inserted_game(db):
    _add_game(db, "The Legend of Zelda: A Link to the Past")
    hits = db.query("SELECT rowid FROM games_fts WHERE games_fts MATCH ?", ("zelda",))
    assert len(hits) == 1


def test_fts_follows_title_updates(db):
    game_id = _add_game(db, "Old Title")
    db.execute("UPDATE games SET title = ? WHERE id = ?", ("Castlevania", game_id))

    assert db.query("SELECT rowid FROM games_fts WHERE games_fts MATCH ?", ("castlevania",))
    assert not db.query("SELECT rowid FROM games_fts WHERE games_fts MATCH ?", ("Old",))


def test_fts_drops_deleted_games(db):
    game_id = _add_game(db, "Earthbound")
    db.execute("DELETE FROM games WHERE id = ?", (game_id,))
    assert not db.query("SELECT rowid FROM games_fts WHERE games_fts MATCH ?", ("earthbound",))


# ── Maintenance ───────────────────────────────────────────────────

def test_backup_produces_a_readable_copy(db, tmp_path):
    _add_game(db, "Secret of Mana")
    backup = db.backup_to(tmp_path / "backup" / "library.db")

    restored = Database(backup)
    assert restored.query_one("SELECT title FROM games")["title"] == "Secret of Mana"
    restored.close()
