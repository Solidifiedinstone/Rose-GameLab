"""On-disk cache for downloaded artwork.

Art is stored under the user's cache directory, keyed by a stable identifier so
the same game never downloads twice. Once a library has been scraped it keeps
its artwork with no network at all, which is what makes the offline promise
real rather than aspirational.

The cache is plain files in plain directories — the user can browse it, back it
up, or delete it, and nothing breaks except that art re-downloads.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil

from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Artwork kinds and their subdirectories.
KINDS = ("cover", "hero", "logo", "screenshot")

# Anything larger than this is not artwork; refuse it rather than filling the
# user's disk with a mis-served file.
MAX_ARTWORK_BYTES = 32 * 1024 * 1024

# Magic bytes for the formats we accept, so a server returning an HTML error
# page with a 200 status does not get saved as "cover.jpg".
_SIGNATURES = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"RIFF": ".webp",   # RIFF....WEBP
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
}


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "rose-gamelab" / "artwork"


def detect_image_type(data: bytes) -> Optional[str]:
    """Return the file extension for image data, or None if it is not an image.

    Servers return HTML error pages with a 200 status often enough that
    trusting the content-type header is not sufficient.
    """
    for signature, extension in _SIGNATURES.items():
        if data.startswith(signature):
            if signature == b"RIFF":
                # RIFF is also WAV/AVI; WebP has "WEBP" at offset 8.
                if len(data) < 12 or data[8:12] != b"WEBP":
                    continue
            return extension
    return None


class ArtCache:
    """Stores and retrieves artwork files."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _cache_dir()

    # ── Paths ─────────────────────────────────────────────────────

    @staticmethod
    def key_for(identifier: str) -> str:
        """A stable, filesystem-safe key for a game identifier.

        Hashed rather than sanitised so that titles with slashes, colons or
        non-ASCII characters cannot escape the cache directory or collide
        after sanitisation.
        """
        return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]

    def path_for(self, identifier: str, kind: str, extension: str = ".jpg") -> Path:
        if kind not in KINDS:
            raise ValueError(f"unknown artwork kind: {kind}")
        return self.root / kind / f"{self.key_for(identifier)}{extension}"

    def find(self, identifier: str, kind: str) -> Optional[Path]:
        """Return cached artwork for this game, in any format, or None."""
        key = self.key_for(identifier)
        directory = self.root / kind

        if not directory.is_dir():
            return None

        for extension in (".jpg", ".png", ".webp", ".gif"):
            candidate = directory / f"{key}{extension}"
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate

        return None

    def has(self, identifier: str, kind: str) -> bool:
        return self.find(identifier, kind) is not None

    # ── Storing ───────────────────────────────────────────────────

    def store(self, identifier: str, kind: str, data: bytes) -> Optional[Path]:
        """Save artwork bytes. Returns the path, or None if the data is not an image.

        Written to a temporary file and moved into place, so an interrupted
        write never leaves a truncated image that later reads as valid cache.
        """
        if not data:
            return None

        if len(data) > MAX_ARTWORK_BYTES:
            logger.warning("refusing oversized artwork for %s (%d bytes)", identifier, len(data))
            return None

        extension = detect_image_type(data)
        if extension is None:
            logger.debug("response for %s was not an image; discarding", identifier)
            return None

        target = self.path_for(identifier, kind, extension)
        target.parent.mkdir(parents=True, exist_ok=True)

        temporary = target.with_suffix(target.suffix + ".part")
        try:
            temporary.write_bytes(data)
            temporary.replace(target)
        except OSError as exc:
            logger.warning("could not write artwork for %s: %s", identifier, exc)
            temporary.unlink(missing_ok=True)
            return None

        return target

    def store_file(self, identifier: str, kind: str, source: Path) -> Optional[Path]:
        """Copy an existing image file into the cache — for user-supplied art."""
        try:
            return self.store(identifier, kind, Path(source).read_bytes())
        except OSError as exc:
            logger.warning("could not read %s: %s", source, exc)
            return None

    # ── Maintenance ───────────────────────────────────────────────

    def remove(self, identifier: str, kind: Optional[str] = None) -> int:
        """Delete cached artwork for a game. Returns how many files were removed."""
        kinds = [kind] if kind else list(KINDS)
        removed = 0

        for k in kinds:
            found = self.find(identifier, k)
            if found:
                found.unlink(missing_ok=True)
                removed += 1

        return removed

    def size_bytes(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(
            path.stat().st_size
            for path in self.root.rglob("*")
            if path.is_file()
        )

    def clear(self) -> None:
        """Delete the whole cache. Art re-downloads; nothing else is lost."""
        if self.root.is_dir():
            shutil.rmtree(self.root, ignore_errors=True)
