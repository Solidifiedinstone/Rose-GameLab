"""Tests for the Lutris source provider.

Each test builds a real SQLite database in tmp_path with the columns Lutris's
`games` table has, so the provider is exercised against actual SQL rather than
a mocked connection — the schema handling is most of what this module does.
"""

from __future__ import annotations

import sqlite3

import pytest

from rose_gamelab.sources.lutris import LutrisProvider

# The columns Lutris's games table carries. Tests that care about schema drift
# build a narrower table instead.
FULL_COLUMNS = (
    "id INTEGER PRIMARY KEY",
    "name TEXT",
    "slug TEXT",
    "runner TEXT",
    "directory TEXT",
    "installed INTEGER",
    "hidden INTEGER",
    "platform TEXT",
    "playtime REAL",
    "lastplayed INTEGER",
    "steamid INTEGER",
    "service TEXT",
    "service_id TEXT",
    "year INTEGER",
)


def make_db(path, rows, columns=FULL_COLUMNS, table="games"):
    """Build a pga.db-shaped SQLite database and insert `rows` (dicts)."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
        names = [column.split()[0] for column in columns]
        for row in rows:
            present = [n for n in names if n in row]
            connection.execute(
                f"INSERT INTO {table} ({', '.join(present)}) "
                f"VALUES ({', '.join('?' for _ in present)})",
                [row[n] for n in present],
            )
        connection.commit()
    finally:
        connection.close()
    return path


def game(**overrides):
    row = {
        "id": 1,
        "name": "Deus Ex",
        "slug": "deus-ex",
        "runner": "wine",
        "directory": "",
        "installed": 1,
        "hidden": 0,
    }
    row.update(overrides)
    return row


@pytest.fixture
def db(tmp_path):
    return tmp_path / "pga.db"


# ── Discovery ─────────────────────────────────────────────────────

def test_discovers_installed_game(db):
    make_db(db, [game()])
    games = LutrisProvider(db_path=str(db)).discover()

    assert len(games) == 1
    assert games[0].name == "Deus Ex"
    assert games[0].id == "lutris:1"
    assert games[0].source == "lutris"
    assert games[0].metadata["runner"] == "wine"


def test_launches_via_lutris_uri(db):
    """Running the executable in `directory` skips the Wine prefix, the
    environment and the pre-launch scripts Lutris configures."""
    make_db(db, [game()])
    assert LutrisProvider(db_path=str(db)).discover()[0].path == "lutris:rungameid/1"


def test_uninstalled_games_are_excluded(db):
    make_db(db, [game(), game(id=2, name="Uninstalled", installed=0)])
    assert [g.name for g in LutrisProvider(db_path=str(db)).discover()] == ["Deus Ex"]


def test_hidden_games_are_excluded(db):
    """The user hid it from their own library; importing it undoes that."""
    make_db(db, [game(), game(id=2, name="Hidden", hidden=1)])
    assert [g.name for g in LutrisProvider(db_path=str(db)).discover()] == ["Deus Ex"]


def test_games_without_a_runner_are_excluded(db):
    """Regression: `lutris:rungameid/N` on a runner-less row opens the
    installer, not a game. Those rows are owned-but-never-installed."""
    make_db(db, [game(), game(id=2, name="Never Installed", runner="", installed=1)])
    assert [g.name for g in LutrisProvider(db_path=str(db)).discover()] == ["Deus Ex"]


def test_nameless_games_are_excluded(db):
    make_db(db, [game(), game(id=2, name="")])
    assert [g.name for g in LutrisProvider(db_path=str(db)).discover()] == ["Deus Ex"]


def test_missing_install_directory_does_not_drop_the_game(db):
    """An unmounted drive is worth a log line, not a disappearing game —
    Lutris is what decides whether it can launch."""
    make_db(db, [game(directory="/mnt/nope/deusex")])
    games = LutrisProvider(db_path=str(db)).discover()

    assert len(games) == 1
    assert games[0].metadata["install_path"] == "/mnt/nope/deusex"


def test_optional_metadata_is_passed_through(db):
    make_db(db, [game(platform="Windows", playtime=12.5, year=2000)])
    metadata = LutrisProvider(db_path=str(db)).discover()[0].metadata

    assert metadata["platform"] == "Windows"
    assert metadata["playtime_hours"] == 12.5
    assert metadata["year"] == 2000


def test_absent_optional_values_are_omitted_not_faked(db):
    make_db(db, [game()])
    metadata = LutrisProvider(db_path=str(db)).discover()[0].metadata
    assert "platform" not in metadata
    assert "year" not in metadata


# ── Steam cross-referencing ───────────────────────────────────────

def test_steam_appid_is_passed_through_for_deduplication(db):
    """Without the appid the same game imports twice — once from Steam, once
    from Lutris."""
    make_db(db, [game(runner="steam", steamid=6910)])
    assert LutrisProvider(db_path=str(db)).discover()[0].metadata["steam_appid"] == 6910


def test_steam_appid_read_from_service_id_when_stored_as_text(db):
    make_db(db, [game(runner="steam", service="steam", service_id="6910")])
    assert LutrisProvider(db_path=str(db)).discover()[0].metadata["steam_appid"] == 6910


def test_service_id_of_another_service_is_not_a_steam_appid(db):
    make_db(db, [game(runner="wine", service="gog", service_id="1207658924")])
    assert "steam_appid" not in LutrisProvider(db_path=str(db)).discover()[0].metadata


def test_zero_steamid_is_not_an_appid(db):
    make_db(db, [game(steamid=0)])
    assert "steam_appid" not in LutrisProvider(db_path=str(db)).discover()[0].metadata


# ── Schema drift ──────────────────────────────────────────────────

def test_survives_a_table_without_the_optional_columns(db):
    make_db(db, [{"id": 1, "name": "Deus Ex", "runner": "wine"}],
            columns=("id INTEGER PRIMARY KEY", "name TEXT", "runner TEXT"))

    games = LutrisProvider(db_path=str(db)).discover()
    assert [g.name for g in games] == ["Deus Ex"]


def test_missing_required_column_reports_rather_than_guesses(db, caplog):
    """No `name` column means Lutris changed its schema. Say so; do not return
    an empty library that looks like 'you have no games'."""
    make_db(db, [{"id": 1}], columns=("id INTEGER PRIMARY KEY", "runner TEXT"))

    with caplog.at_level("ERROR"):
        assert LutrisProvider(db_path=str(db)).discover() == []
    assert "schema" in caplog.text.lower()


def test_wrong_database_is_reported(db, caplog):
    make_db(db, [], columns=("id INTEGER",), table="something_else")

    with caplog.at_level("WARNING"):
        assert LutrisProvider(db_path=str(db)).discover() == []
    assert "games" in caplog.text


def test_corrupt_database_is_not_fatal(db):
    db.write_bytes(b"this is not a sqlite database" * 10)
    provider = LutrisProvider(db_path=str(db))

    assert provider.discover() == []
    assert provider.validate() is False


# ── Read-only access ──────────────────────────────────────────────

def test_opens_read_only(db):
    """Lutris may be running; GameLab must not take a write lock or leave a
    -wal file in someone else's data directory."""
    make_db(db, [game()])
    connection = LutrisProvider(db_path=str(db)).connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM games")
    finally:
        connection.close()


def test_missing_database_is_not_created(tmp_path):
    """Regression: a plain sqlite3.connect() on a missing path creates an empty
    database, turning 'Lutris not installed' into 'Lutris has no games'."""
    missing = tmp_path / "lutris" / "pga.db"
    provider = LutrisProvider(db_path=str(missing))

    assert provider.discover() == []
    assert provider.validate() is False
    assert not missing.exists()


# ── Provider interface ────────────────────────────────────────────

def test_validates_with_a_real_database(db):
    make_db(db, [])
    assert LutrisProvider(db_path=str(db)).validate() is True


def test_launch_command_is_never_empty():
    command = LutrisProvider.launch_command("lutris:rungameid/1")
    assert command
    assert command[-1] == "lutris:rungameid/1"


def test_source_def(db):
    make_db(db, [])
    definition = LutrisProvider(db_path=str(db)).get_def()
    assert (definition.id, definition.type, definition.system) == ("lutris", "lutris", "pc")
    assert definition.path == str(db)
