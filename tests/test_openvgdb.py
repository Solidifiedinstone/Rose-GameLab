"""Tests for offline ROM identification via OpenVGDB.

A miniature database with OpenVGDB's real schema is built in tmp_path, so no
test needs the real 40 MB download or any network access.
"""

from __future__ import annotations

import sqlite3
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from rose_gamelab.metadata.base import ProviderError
from rose_gamelab.metadata.openvgdb import (
    MIN_DB_BYTES,
    Identification,
    OpenVGDBProvider,
    _normalise_date,
)

SHA1 = "9E9F1F1A4B4B0E6C5A5B1E2D3C4B5A6978899AA0"
MD5 = "0123456789ABCDEF0123456789ABCDEF"
CRC = "A1B2C3D4"


@pytest.fixture
def database(tmp_path):
    """A miniature OpenVGDB with the real table and column names."""
    path = tmp_path / "openvgdb.sqlite"
    conn = sqlite3.connect(path)

    conn.executescript(
        """
        CREATE TABLE ROMs (
            romID INTEGER PRIMARY KEY,
            romHashCRC TEXT, romHashMD5 TEXT, romHashSHA1 TEXT,
            romFileName TEXT, romExtensionlessFileName TEXT
        );
        CREATE TABLE RELEASES (
            releaseID INTEGER PRIMARY KEY,
            romID INTEGER,
            releaseTitleName TEXT, releaseCoverFront TEXT,
            releaseDescription TEXT, releaseDate TEXT,
            releaseDeveloper TEXT, releasePublisher TEXT, releaseGenre TEXT
        );
        """
    )

    conn.execute(
        "INSERT INTO ROMs VALUES (1, ?, ?, ?, ?, ?)",
        (CRC, MD5, SHA1, "smb3.nes", "smb3"),
    )
    conn.execute(
        "INSERT INTO RELEASES VALUES (1, 1, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Super Mario Bros. 3",
            "https://example.invalid/smb3.jpg",
            "Mario returns.",
            "02/12/1990",
            "Nintendo EAD",
            "Nintendo",
            "Action, Platform",
        ),
    )
    conn.commit()
    conn.close()

    # Pad past the plausibility floor so available() accepts it.
    with path.open("ab") as handle:
        handle.write(b"\x00" * MIN_DB_BYTES)

    return path


@pytest.fixture
def provider(database):
    p = OpenVGDBProvider(database)
    yield p
    p.close()


# ── Availability ──────────────────────────────────────────────────

def test_available_when_downloaded(provider):
    assert provider.available()


def test_unavailable_when_missing(tmp_path):
    assert not OpenVGDBProvider(tmp_path / "nope.sqlite").available()


def test_unavailable_when_implausibly_small(tmp_path):
    """A saved 404 page must not be treated as the database."""
    path = tmp_path / "openvgdb.sqlite"
    path.write_bytes(b"<html>Not Found</html>")

    assert not OpenVGDBProvider(path).available()


def test_querying_without_the_database_explains_itself(tmp_path):
    provider = OpenVGDBProvider(tmp_path / "nope.sqlite")
    with pytest.raises(ProviderError) as exc:
        provider.identify(sha1=SHA1)

    assert "not been downloaded" in str(exc.value)


def test_database_is_opened_read_only(provider):
    """It is a shared downloaded artefact; nothing here should write to it."""
    with pytest.raises(sqlite3.OperationalError):
        provider.connection().execute("DELETE FROM ROMs")


# ── Identification ────────────────────────────────────────────────

def test_identifies_by_sha1(provider):
    result = provider.identify(sha1=SHA1)

    assert result is not None
    assert result.title == "Super Mario Bros. 3"
    assert result.matched_by == "sha1"
    assert result.exact is True


def test_hash_match_is_case_insensitive(provider):
    assert provider.identify(sha1=SHA1.lower()) is not None


def test_identifies_by_md5(provider):
    result = provider.identify(md5=MD5)
    assert result.matched_by == "md5"


def test_identifies_by_crc32(provider):
    result = provider.identify(crc32=CRC)
    assert result.matched_by == "crc32"


def test_sha1_wins_over_weaker_hashes(provider):
    """SHA-1 has effectively no collisions; CRC32 is short enough to."""
    result = provider.identify(sha1=SHA1, md5=MD5, crc32=CRC)
    assert result.matched_by == "sha1"


def test_filename_match_is_flagged_as_a_guess(provider):
    result = provider.identify(filename="smb3.nes")

    assert result.title == "Super Mario Bros. 3"
    assert result.matched_by == "filename"
    assert result.exact is False


def test_hash_wins_over_filename(provider):
    result = provider.identify(sha1=SHA1, filename="something-else.nes")
    assert result.matched_by == "sha1"


def test_unknown_rom_returns_none(provider):
    assert provider.identify(sha1="0" * 40) is None


def test_no_evidence_returns_none(provider):
    assert provider.identify() is None


def test_this_is_what_closes_the_filename_gap(provider):
    """A badly-named ROM is identified correctly from its hash alone.

    `smb3.nes` matches nothing in a title-keyed archive; its SHA-1 is exact.
    """
    result = provider.identify(sha1=SHA1)
    assert result.title == "Super Mario Bros. 3"
    assert result.metadata.developer == "Nintendo EAD"


# ── Metadata mapping ──────────────────────────────────────────────

def test_metadata_is_populated(provider):
    metadata = provider.identify(sha1=SHA1).metadata

    assert metadata.summary == "Mario returns."
    assert metadata.developer == "Nintendo EAD"
    assert metadata.publisher == "Nintendo"
    assert metadata.source == "openvgdb"


def test_genres_are_split(provider):
    assert provider.identify(sha1=SHA1).metadata.genres == ["Action", "Platform"]


def test_cover_url_is_exposed(provider):
    assert provider.identify(sha1=SHA1).cover_url.endswith("smb3.jpg")


@pytest.mark.parametrize("stored,expected", [
    ("02/12/1990", "1990-02-12"),
    ("1990-02-12", "1990-02-12"),
    ("1990", "1990"),
])
def test_date_normalisation(stored, expected):
    assert _normalise_date(stored) == expected


@pytest.mark.parametrize("stored", ["", None, "sometime in the 90s"])
def test_unparseable_dates_yield_none(stored):
    assert _normalise_date(stored) is None


# ── Schema resilience ─────────────────────────────────────────────

def test_schema_change_does_not_crash_the_scan(tmp_path):
    """An upstream schema change must lose one lookup, not the whole run."""
    path = tmp_path / "openvgdb.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ROMs (romID INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with path.open("ab") as handle:
        handle.write(b"\x00" * MIN_DB_BYTES)

    provider = OpenVGDBProvider(path)
    assert provider.identify(sha1=SHA1) is None
    provider.close()


# ── Download ──────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, *, payload=None, content=b"", status=200):
        self._payload = payload
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests_HTTPError(f"status {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def requests_HTTPError(message):
    import requests
    return requests.HTTPError(message)


class FakeSession:
    def __init__(self, release_payload, archive_bytes):
        self.release_payload = release_payload
        self.archive_bytes = archive_bytes
        # A real requests.Session has this, and GameLab writes its User-Agent
        # into it. A double without it hides that the code ever did so.
        self.headers: dict[str, str] = {}

    def get(self, url, timeout=None, stream=False):
        if "api.github.com" in url:
            return FakeResponse(payload=self.release_payload)
        return FakeResponse(content=self.archive_bytes)


def make_archive(inner: bytes) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("openvgdb.sqlite", inner)
    return buffer.getvalue()


def test_download_unpacks_the_database(tmp_path):
    payload = {"assets": [{"name": "openvgdb.zip", "browser_download_url": "https://x/o.zip"}]}
    session = FakeSession(payload, make_archive(b"\x00" * (MIN_DB_BYTES + 10)))

    target = tmp_path / "openvgdb.sqlite"
    provider = OpenVGDBProvider(target)
    provider.download(session=session)

    assert target.is_file()
    assert target.stat().st_size >= MIN_DB_BYTES


def test_no_partial_file_is_left_behind(tmp_path):
    payload = {"assets": [{"name": "openvgdb.zip", "browser_download_url": "https://x/o.zip"}]}
    session = FakeSession(payload, make_archive(b"\x00" * (MIN_DB_BYTES + 10)))

    provider = OpenVGDBProvider(tmp_path / "openvgdb.sqlite")
    provider.download(session=session)

    assert list(tmp_path.glob("*.part")) == []


def test_release_without_an_archive_is_reported(tmp_path):
    session = FakeSession({"assets": []}, b"")
    provider = OpenVGDBProvider(tmp_path / "db.sqlite")

    with pytest.raises(ProviderError):
        provider.download(session=session)


def test_corrupt_archive_is_reported(tmp_path):
    payload = {"assets": [{"name": "o.zip", "browser_download_url": "https://x/o.zip"}]}
    session = FakeSession(payload, b"this is not a zip file")

    provider = OpenVGDBProvider(tmp_path / "db.sqlite")
    with pytest.raises(ProviderError) as exc:
        provider.download(session=session)

    assert "corrupt" in str(exc.value)


def test_implausibly_small_download_is_rejected(tmp_path):
    payload = {"assets": [{"name": "o.zip", "browser_download_url": "https://x/o.zip"}]}
    session = FakeSession(payload, make_archive(b"tiny"))

    provider = OpenVGDBProvider(tmp_path / "db.sqlite")
    with pytest.raises(ProviderError):
        provider.download(session=session)


def test_the_download_identifies_itself_to_github(tmp_path):
    """GitHub rate-limits anonymous callers harder and asks who is calling."""
    from rose_gamelab.metadata.base import USER_AGENT

    payload = {"assets": [{"name": "openvgdb.zip", "browser_download_url": "https://x/o.zip"}]}
    session = FakeSession(payload, make_archive(b"\x00" * (MIN_DB_BYTES + 10)))
    provider = OpenVGDBProvider(path=tmp_path / "db.sqlite")

    provider.download(session=session)

    assert session.headers["User-Agent"] == USER_AGENT
