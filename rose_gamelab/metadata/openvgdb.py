"""Identifying ROMs by content hash, offline, using OpenVGDB.

OpenVGDB is a freely-licensed SQLite database mapping ROM checksums to game
identities: title, release date, developer, publisher, genre and cover art. It
is what OpenEmu uses. Once downloaded it needs no network at all, which makes
it the right backbone for a launcher that promises to work offline.

This is what closes the gap left by filename matching. A ROM called `smb3.nes`
tells the libretro thumbnail archive nothing, because that archive is keyed on
No-Intro titles. Its SHA-1, however, identifies it exactly — and our
`core/hashing.py` already computes exactly the right hash, because it skips the
same copier headers the datasets exclude.

Lookup order is deliberate: SHA-1 first (effectively no collisions), then MD5,
then CRC32 (fast to compute and what most dat files index, but short enough
that collisions are possible), and only then filename. Anything matched by
filename is flagged as such, so the interface can distinguish "identified" from
"guessed".
"""

from __future__ import annotations

import logging
import sqlite3
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests

from rose_gamelab.metadata.base import GameMetadata, MetadataProvider, ProviderError

logger = logging.getLogger(__name__)

# The database is published as a zipped SQLite file on GitHub releases.
RELEASE_API = "https://api.github.com/repos/OpenVGDB/OpenVGDB/releases/latest"
DOWNLOAD_TIMEOUT = 300

# Refuse anything implausible as the database, rather than writing a 404 page
# to disk and failing confusingly on every later query.
MIN_DB_BYTES = 1024 * 1024


@dataclass
class Identification:
    """A ROM identified against the database."""

    title: str
    metadata: GameMetadata
    cover_url: Optional[str] = None
    #: How the match was made — 'sha1', 'md5', 'crc32' or 'filename'.
    matched_by: str = "sha1"
    #: Hash matches are exact; a filename match is a guess and is labelled so.
    exact: bool = True


def _data_dir() -> Path:
    import os

    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "rose-gamelab"


DEFAULT_DB_PATH = _data_dir() / "openvgdb.sqlite"


class OpenVGDBProvider(MetadataProvider):
    """Offline ROM identification by content hash."""

    name = "OpenVGDB"
    requires_key = False

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self._conn: Optional[sqlite3.Connection] = None

    # ── Availability ──────────────────────────────────────────────

    def available(self) -> bool:
        """Whether the database has been downloaded and looks usable."""
        try:
            return self.path.is_file() and self.path.stat().st_size >= MIN_DB_BYTES
        except OSError:
            return False

    def connection(self) -> sqlite3.Connection:
        """Open the database read-only.

        Read-only is not a nicety: this is a downloaded artefact shared between
        processes, and nothing in GameLab has any business writing to it.
        """
        if self._conn is None:
            if not self.available():
                raise ProviderError(
                    "The offline game database has not been downloaded yet."
                )
            self._conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row

        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Download ──────────────────────────────────────────────────

    def download(
        self,
        *,
        session: Optional[requests.Session] = None,
        progress=None,
    ) -> Path:
        """Fetch the latest OpenVGDB release and unpack it.

        Written to a temporary file and moved into place, so an interrupted
        download never leaves a truncated database that looks valid.
        """
        session = session or requests.Session()

        try:
            response = session.get(RELEASE_API, timeout=30)
            response.raise_for_status()
            release = response.json()
        except requests.RequestException as exc:
            raise ProviderError(f"could not reach GitHub: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"GitHub returned malformed JSON: {exc}") from exc

        asset_url = None
        for asset in release.get("assets", []):
            if str(asset.get("name", "")).lower().endswith(".zip"):
                asset_url = asset.get("browser_download_url")
                break

        if not asset_url:
            raise ProviderError("The OpenVGDB release contains no database archive.")

        if progress:
            progress("Downloading game database…", 0, 0)

        try:
            archive = session.get(asset_url, timeout=DOWNLOAD_TIMEOUT, stream=True)
            archive.raise_for_status()
            payload = archive.content
        except requests.RequestException as exc:
            raise ProviderError(f"could not download the database: {exc}") from exc

        try:
            with zipfile.ZipFile(BytesIO(payload)) as bundle:
                names = [n for n in bundle.namelist() if n.lower().endswith(".sqlite")]
                if not names:
                    raise ProviderError("The downloaded archive contains no database.")
                data = bundle.read(names[0])
        except zipfile.BadZipFile as exc:
            raise ProviderError(f"the downloaded archive is corrupt: {exc}") from exc

        if len(data) < MIN_DB_BYTES:
            raise ProviderError("The downloaded database is implausibly small.")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".part")
        temporary.write_bytes(data)
        temporary.replace(self.path)

        self.close()  # force a reconnect against the new file

        if progress:
            progress("Game database ready", 1, 1)

        return self.path

    # ── Lookup ────────────────────────────────────────────────────

    def identify(
        self,
        *,
        sha1: Optional[str] = None,
        md5: Optional[str] = None,
        crc32: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Optional[Identification]:
        """Identify a ROM, strongest evidence first. None when nothing matches."""
        for value, column, label in (
            (sha1, "romHashSHA1", "sha1"),
            (md5, "romHashMD5", "md5"),
            (crc32, "romHashCRC", "crc32"),
        ):
            if not value:
                continue
            row = self._lookup(column, value.upper())
            if row:
                return self._to_identification(row, matched_by=label, exact=True)

        if filename:
            row = self._lookup("romExtensionlessFileName", Path(filename).stem)
            if row:
                # A filename match is a guess, and is labelled as one so the
                # interface can show it differently from a hash match.
                return self._to_identification(row, matched_by="filename", exact=False)

        return None

    def _lookup(self, column: str, value: str) -> Optional[sqlite3.Row]:
        # Column names are from our own fixed tuple above, never user input.
        sql = f"""
            SELECT r.releaseTitleName, r.releaseCoverFront, r.releaseDescription,
                   r.releaseDate, r.releaseDeveloper, r.releasePublisher,
                   r.releaseGenre
            FROM ROMs AS m
            JOIN RELEASES AS r ON r.romID = m.romID
            WHERE m.{column} = ?
            LIMIT 1
        """
        try:
            return self.connection().execute(sql, (value,)).fetchone()
        except sqlite3.Error as exc:
            # A schema change upstream must not take the whole scan down.
            logger.warning("OpenVGDB lookup failed on %s: %s", column, exc)
            return None

    @staticmethod
    def _to_identification(
        row: sqlite3.Row, *, matched_by: str, exact: bool
    ) -> Identification:
        genres = [
            part.strip()
            for part in (row["releaseGenre"] or "").split(",")
            if part.strip()
        ]

        return Identification(
            title=row["releaseTitleName"],
            cover_url=row["releaseCoverFront"] or None,
            matched_by=matched_by,
            exact=exact,
            metadata=GameMetadata(
                title=row["releaseTitleName"],
                summary=row["releaseDescription"] or None,
                release_date=_normalise_date(row["releaseDate"]),
                developer=row["releaseDeveloper"] or None,
                publisher=row["releasePublisher"] or None,
                genres=genres,
                source="openvgdb",
            ),
        )


def _normalise_date(text: Optional[str]) -> Optional[str]:
    """OpenVGDB stores dates as MM/DD/YYYY. Convert to ISO, or None if odd."""
    if not text:
        return None

    from datetime import datetime

    for pattern in ("%m/%d/%Y", "%Y-%m-%d", "%Y"):
        try:
            parsed = datetime.strptime(text.strip(), pattern)
        except ValueError:
            continue
        return f"{parsed.year:04d}" if pattern == "%Y" else parsed.date().isoformat()

    return None
