"""Tests for the RetroAchievements provider, RA hashing and migration 2.

No test here touches the network. The provider is driven through a fake
session, in the same style as tests/test_metadata.py, so the suite is fast,
deterministic and works offline.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from rose_gamelab.db.database import Database, utc_now
from rose_gamelab.db.migrations import MIGRATIONS, SCHEMA_VERSION
from rose_gamelab.metadata.base import ProviderError
from rose_gamelab.metadata.retroachievements import (
    Achievement,
    RetroAchievementsProvider,
    UnverifiedHashAlgorithm,
    credentials_from_config,
    link_game,
    ra_hash,
    save_achievements,
    supports_hashing,
    _parse_released,
)


# ── Fakes ─────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, *, status: int = 200, content: bytes = b"", payload=None):
        self.status_code = status
        self.content = content
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records requests and returns queued responses by URL substring."""

    def __init__(self):
        self.routes: list[tuple[str, FakeResponse]] = []
        self.requested: list[str] = []
        self.headers: dict[str, str] = {}
        self.default = FakeResponse(status=404)

    def route(self, fragment: str, response: FakeResponse) -> None:
        self.routes.append((fragment, response))

    def get(self, url, params=None, timeout=None):
        if params:
            url = f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
        self.requested.append(url)
        for fragment, response in self.routes:
            if fragment in url:
                return response
        return self.default


def provider(session: FakeSession, **kwargs) -> RetroAchievementsProvider:
    kwargs.setdefault("username", "Gavin")
    kwargs.setdefault("api_key", "s3cret")
    return RetroAchievementsProvider(session=session, rate_limit=0, **kwargs)


def progress_payload(**overrides) -> dict:
    payload = {
        "ID": 1447,
        "Title": "Sonic the Hedgehog",
        "ConsoleID": 1,
        "ConsoleName": "Mega Drive",
        "Developer": "Sonic Team",
        "Publisher": "Sega",
        "Genre": "Platformer, Action",
        "Released": "1991-06-23",
        "NumAchievements": 2,
        "Achievements": {
            "9": {
                "ID": 9,
                "Title": "Green Hill Zone",
                "Description": "Clear act 1.",
                "Points": 5,
                "BadgeName": "12345",
                "DateEarned": "2024-03-01 12:00:00",
            },
            "10": {
                "ID": 10,
                "Title": "Ring King",
                "Description": "Collect 200 rings.",
                "Points": 25,
                "BadgeName": "12346",
            },
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "library.db")
    yield database
    database.close()


def _add_game(db: Database, title: str = "Sonic the Hedgehog") -> int:
    cur = db.execute(
        "INSERT INTO games (title, sort_title, system, added_at) VALUES (?, ?, ?, ?)",
        (title, title.lower(), "megadrive", utc_now()),
    )
    return cur.lastrowid


# ══ Availability and credentials ══════════════════════════════════

def test_provider_declares_that_it_needs_a_key():
    assert RetroAchievementsProvider.requires_key is True


def test_unavailable_without_a_key():
    assert not RetroAchievementsProvider(session=FakeSession(), rate_limit=0).available()
    assert not provider(FakeSession(), api_key=None).available()
    assert not provider(FakeSession(), username=None).available()


def test_available_with_both_credentials():
    assert provider(FakeSession()).available()


def test_unconfigured_provider_raises_rather_than_reporting_no_such_game():
    """'You have not set up RA' must not look like 'RA has no such game'."""
    unconfigured = RetroAchievementsProvider(session=FakeSession(), rate_limit=0)

    with pytest.raises(ProviderError):
        unconfigured.fetch(1447)


def test_credentials_are_read_from_config(tmp_path):
    from rose_gamelab.config import Config

    config = Config(config_dir=str(tmp_path / "conf"))
    assert credentials_from_config(config) == (None, None)

    config.set("retroachievements.username", "Gavin")
    config.set("retroachievements.api_key", "s3cret")

    assert credentials_from_config(config) == ("Gavin", "s3cret")

    built = RetroAchievementsProvider.from_config(config, session=FakeSession(), rate_limit=0)
    assert built.available()
    assert built.username == "Gavin"


def test_api_key_is_not_stored_in_the_database(db):
    """The key is a credential; the library database is a file users copy."""
    columns = {
        row["name"]
        for table in ("games", "achievements")
        for row in db.query(f"PRAGMA table_info({table})")
    }
    assert not any("key" in name.lower() for name in columns)


def test_network_error_message_does_not_leak_the_key():
    import requests

    class BrokenSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            # requests embeds the full URL, query string included, in its
            # exception text — which is exactly how a key ends up in a log.
            raise requests.ConnectionError(f"failed to connect to {url}?y=s3cret")

    with pytest.raises(ProviderError) as excinfo:
        provider(BrokenSession()).fetch(1447)

    assert "s3cret" not in str(excinfo.value)


# ══ Metadata ══════════════════════════════════════════════════════

def test_parses_game_metadata():
    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(payload=progress_payload()))

    result = provider(session).fetch(1447)

    assert result.title == "Sonic the Hedgehog"
    assert result.developer == "Sonic Team"
    assert result.publisher == "Sega"
    assert result.genres == ["Platformer", "Action"]
    assert result.release_date == "1991-06-23"
    assert result.source == "retroachievements"


def test_no_rating_is_invented():
    """RA has no rating of any kind; completion percentage is not a rating."""
    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(
        payload=progress_payload(UserCompletion="80.00%")))

    result = provider(session).fetch(1447)

    assert result.rating is None
    assert result.rating_source is None


def test_unknown_game_returns_none():
    """RA answers an unknown id with a null ID, not a 404."""
    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(payload={"ID": None}))

    assert provider(session).fetch(999999) is None


def test_network_failure_raises_rather_than_reporting_no_such_game():
    import requests

    class BrokenSession(FakeSession):
        def get(self, *a, **kw):
            raise requests.ConnectionError("offline")

    with pytest.raises(ProviderError):
        provider(BrokenSession()).fetch(1447)


def test_malformed_json_raises():
    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(payload=None))

    with pytest.raises(ProviderError):
        provider(session).fetch(1447)


def test_http_error_raises():
    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(status=401))

    with pytest.raises(ProviderError):
        provider(session).fetch(1447)


@pytest.mark.parametrize("text,expected", [
    ("1991-06-23", "1991-06-23"),
    ("1991-06-23 00:00:00", "1991-06-23"),
    ("June 23, 1991", "1991-06-23"),
    ("1991", "1991"),
])
def test_parses_release_date_formats(text, expected):
    assert _parse_released(text) == expected


@pytest.mark.parametrize("text", ["", "Unknown", "TBA", None, 1991])
def test_unparseable_release_dates_yield_none_not_a_wrong_date(text):
    assert _parse_released(text) is None


# ══ Achievements ══════════════════════════════════════════════════

def test_parses_achievements_with_progress():
    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(payload=progress_payload()))

    earned, unearned = sorted(provider(session).achievements(1447), key=lambda a: a.ra_id)

    assert earned.title == "Green Hill Zone"
    assert earned.points == 5
    assert earned.badge_url.endswith("/12345.png")
    assert earned.earned is True
    assert earned.hardcore is False

    assert unearned.earned is False
    assert unearned.earned_at is None


def test_hardcore_award_is_recorded():
    payload = progress_payload()
    payload["Achievements"]["9"]["DateEarnedHardcore"] = "2024-03-02 09:00:00"

    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(payload=payload))

    hardcore = next(a for a in provider(session).achievements(1447) if a.ra_id == 9)

    assert hardcore.hardcore is True
    assert hardcore.earned_at == "2024-03-02 09:00:00"


def test_game_without_an_achievement_set_yields_an_empty_list():
    """RA sends an empty JSON array, not an object, when there is no set."""
    session = FakeSession()
    session.route("GetGameInfoAndUserProgress",
                  FakeResponse(payload=progress_payload(Achievements=[])))

    assert provider(session).achievements(1447) == []


def test_unusable_achievement_entries_are_dropped_not_stored_blank():
    payload = progress_payload()
    payload["Achievements"]["bad"] = {"Points": 5}       # no id, no title

    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(payload=payload))

    assert len(provider(session).achievements(1447)) == 2


def test_numeric_fields_sent_as_strings_are_accepted():
    """RA returns ids and points as JSON strings on some endpoints."""
    payload = progress_payload(Achievements={
        "9": {"ID": "9", "Title": "Green Hill Zone", "Points": "5"},
    })

    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(payload=payload))

    achievement = provider(session).achievements(1447)[0]
    assert achievement.ra_id == 9
    assert achievement.points == 5


def test_achievements_for_unknown_game_are_empty():
    session = FakeSession()
    session.route("GetGameInfoAndUserProgress", FakeResponse(payload={"ID": None}))

    assert provider(session).achievements(999999) == []


# ══ Account and hash matching ═════════════════════════════════════

def test_completed_games_returns_the_list_verbatim():
    session = FakeSession()
    session.route("GetUserCompletedGames", FakeResponse(payload=[
        {"GameID": "1447", "Title": "Sonic the Hedgehog", "PctWon": "0.5000"},
    ]))

    games = provider(session).completed_games()
    assert games[0]["Title"] == "Sonic the Hedgehog"


def test_finds_a_game_by_rom_hash():
    session = FakeSession()
    session.route("GetGameList", FakeResponse(payload=[
        {"ID": 1, "Title": "Other", "Hashes": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]},
        {"ID": 1447, "Title": "Sonic", "Hashes": ["BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"]},
    ]))

    # Case-insensitive: RA has published hashes in both cases over the years.
    assert provider(session).find_game_by_hash(1, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb") == 1447


def test_unknown_hash_returns_none_when_hashes_were_actually_present():
    session = FakeSession()
    session.route("GetGameList", FakeResponse(payload=[
        {"ID": 1, "Title": "Other", "Hashes": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]},
    ]))

    assert provider(session).find_game_by_hash(1, "f" * 32) is None


def test_game_list_without_hashes_raises_instead_of_claiming_no_match():
    """No hashes in the response means we never checked — saying 'not found'
    there would be a lie the user cannot distinguish from a real miss."""
    session = FakeSession()
    session.route("GetGameList", FakeResponse(payload=[{"ID": 1, "Title": "Other"}]))

    with pytest.raises(ProviderError):
        provider(session).find_game_by_hash(1, "f" * 32)


# ══ RA hashing ════════════════════════════════════════════════════

def write_rom(tmp_path, name: str, body: bytes, header: bytes = b"") -> "object":
    path = tmp_path / name
    path.write_bytes(header + body)
    return path


def test_nes_hash_skips_the_ines_header(tmp_path):
    body = b"\x01" * 32768
    path = write_rom(tmp_path, "smb.nes", body, header=b"NES\x1a" + b"\x00" * 12)

    assert ra_hash(path, "nes") == hashlib.md5(body).hexdigest()


def test_headerless_nes_hash_is_the_whole_file(tmp_path):
    body = b"\x02" * 32768
    path = write_rom(tmp_path, "homebrew.nes", body)

    assert ra_hash(path, "nes") == hashlib.md5(body).hexdigest()


def test_snes_hash_skips_the_copier_header(tmp_path):
    body = b"\x03" * (32768 * 4)
    path = write_rom(tmp_path, "ct.sfc", body, header=b"\x00" * 512)

    assert ra_hash(path, "snes") == hashlib.md5(body).hexdigest()


def test_headerless_snes_hash_is_the_whole_file(tmp_path):
    body = b"\x04" * (32768 * 4)
    path = write_rom(tmp_path, "ct.sfc", body)

    assert ra_hash(path, "snes") == hashlib.md5(body).hexdigest()


@pytest.mark.parametrize("name,system", [
    ("tetris.gb", "gb"),
    ("zelda.gbc", "gbc"),
    ("sonic.md", "megadrive"),
    ("sonic.gen", "genesis"),
])
def test_cartridge_systems_without_headers_hash_the_whole_file(tmp_path, name, system):
    body = b"\x05" * 4096
    path = write_rom(tmp_path, name, body)

    assert ra_hash(path, system) == hashlib.md5(body).hexdigest()


def test_supported_systems_are_reported():
    assert supports_hashing("nes")
    assert supports_hashing("SNES")
    assert not supports_hashing("ps1")
    assert not supports_hashing("n64")


@pytest.mark.parametrize("system", ["ps1", "ps2", "psp", "n64", "nds", "arcade", "dreamcast"])
def test_unverified_systems_refuse_rather_than_return_a_wrong_hash(tmp_path, system):
    """A plausible-but-wrong hash matches nothing and looks like 'no
    achievements exist', which is worse than an explicit refusal."""
    path = write_rom(tmp_path, "game.bin", b"\x00" * 64)

    with pytest.raises(UnverifiedHashAlgorithm) as excinfo:
        ra_hash(path, system)

    assert system in str(excinfo.value)


def test_interleaved_megadrive_dumps_are_refused(tmp_path):
    path = write_rom(tmp_path, "sonic.smd", b"\x00" * 64)

    with pytest.raises(UnverifiedHashAlgorithm):
        ra_hash(path, "megadrive")


@pytest.mark.parametrize("name,system", [
    ("sonic.zip", "megadrive"),
    ("ct.7z", "snes"),
    ("smb.rom", "nes"),
])
def test_unexpected_extensions_are_refused(tmp_path, name, system):
    """Without a known extension we cannot tell whether a copier header is
    present, and archives need extraction rules that were not verified."""
    path = write_rom(tmp_path, name, b"\x00" * 64)

    with pytest.raises(ValueError):
        ra_hash(path, system)


def test_hash_of_a_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        ra_hash(tmp_path / "nope.nes", "nes")


# ══ Migration 2 ═══════════════════════════════════════════════════

def test_schema_version_is_at_least_two():
    assert SCHEMA_VERSION >= 2


def test_fresh_database_has_the_achievements_schema(db):
    names = {
        row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "achievements" in names

    columns = {row["name"] for row in db.query("PRAGMA table_info(games)")}
    assert {"ra_game_id", "ra_hash"} <= columns


def _build_version_1_database(path) -> None:
    """Create a database at exactly migration 1, as a pre-upgrade user has."""
    version, _description, sql = MIGRATIONS[0]
    assert version == 1

    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        conn.execute(
            "INSERT INTO games (title, sort_title, system, added_at)"
            " VALUES ('Sonic the Hedgehog', 'sonic the hedgehog', 'megadrive', ?)",
            (utc_now(),),
        )
    finally:
        conn.close()


def test_version_1_database_upgrades_cleanly(tmp_path):
    """The upgrade path is what real users hit; a fresh DB never exercises it."""
    path = tmp_path / "old.db"
    _build_version_1_database(path)

    check = sqlite3.connect(str(path))
    assert check.execute("PRAGMA user_version").fetchone()[0] == 1
    check.close()

    upgraded = Database(path)          # migrate() runs on open
    try:
        assert upgraded.version == SCHEMA_VERSION

        columns = {row["name"] for row in upgraded.query("PRAGMA table_info(games)")}
        assert {"ra_game_id", "ra_hash"} <= columns

        # The user's existing row survives, with the new columns empty.
        row = upgraded.query_one("SELECT title, ra_game_id, ra_hash FROM games")
        assert row["title"] == "Sonic the Hedgehog"
        assert row["ra_game_id"] is None
        assert row["ra_hash"] is None

        # And migration 2's tables are usable, not just present.
        game_id = upgraded.query_one("SELECT id FROM games")["id"]
        link_game(upgraded, game_id, 1447, "b" * 32)
        assert upgraded.query_one("SELECT ra_game_id FROM games")["ra_game_id"] == 1447

        assert upgraded.migrate() == 0, "upgrade should be complete"
    finally:
        upgraded.close()


def test_upgrade_preserves_full_text_search(tmp_path):
    """Migration 2 touches the games table; the FTS triggers must survive."""
    path = tmp_path / "old.db"
    _build_version_1_database(path)

    upgraded = Database(path)
    try:
        hits = upgraded.query(
            "SELECT rowid FROM games_fts WHERE games_fts MATCH ?", ("sonic",)
        )
        assert len(hits) == 1

        upgraded.execute(
            "INSERT INTO games (title, sort_title, system, added_at) VALUES (?, ?, ?, ?)",
            ("Streets of Rage", "streets of rage", "megadrive", utc_now()),
        )
        assert upgraded.query(
            "SELECT rowid FROM games_fts WHERE games_fts MATCH ?", ("rage",)
        )
    finally:
        upgraded.close()


# ══ Persistence ═══════════════════════════════════════════════════

def test_achievements_are_stored_and_refreshed(db):
    game_id = _add_game(db)

    assert save_achievements(db, game_id, [
        Achievement(ra_id=9, title="Green Hill Zone", description="Clear act 1.",
                    points=5, badge_url=None),
        Achievement(ra_id=10, title="Ring King", description=None, points=25,
                    badge_url=None),
    ]) == 2

    # A refresh that now reports one as earned updates in place rather than
    # duplicating the row.
    save_achievements(db, game_id, [
        Achievement(ra_id=9, title="Green Hill Zone", description="Clear act 1.",
                    points=5, badge_url=None, earned_at="2024-03-01 12:00:00",
                    hardcore=True),
    ])

    rows = db.query("SELECT ra_id, earned_at, hardcore FROM achievements ORDER BY ra_id")
    assert len(rows) == 2
    assert rows[0]["earned_at"] == "2024-03-01 12:00:00"
    assert rows[0]["hardcore"] == 1
    assert rows[1]["earned_at"] is None


def test_saving_nothing_leaves_existing_progress_alone(db):
    game_id = _add_game(db)
    save_achievements(db, game_id, [
        Achievement(ra_id=9, title="Green Hill Zone", description=None, points=5,
                    badge_url=None, earned_at="2024-03-01 12:00:00"),
    ])

    assert save_achievements(db, game_id, []) == 0
    assert len(db.query("SELECT 1 FROM achievements")) == 1


def test_deleting_a_game_removes_its_achievements(db):
    game_id = _add_game(db)
    save_achievements(db, game_id, [
        Achievement(ra_id=9, title="Green Hill Zone", description=None, points=5,
                    badge_url=None),
    ])

    db.execute("DELETE FROM games WHERE id = ?", (game_id,))

    assert db.query("SELECT 1 FROM achievements WHERE game_id = ?", (game_id,)) == []


def test_achievements_require_a_real_game(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO achievements (game_id, ra_id, title) VALUES (?, ?, ?)",
            (9999, 1, "Orphan"),
        )
