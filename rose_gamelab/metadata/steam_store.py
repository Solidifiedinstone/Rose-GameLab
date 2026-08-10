"""Steam metadata and artwork, without an API key.

Steam's store endpoint and its artwork CDN are both publicly readable with no
key, no account and no OAuth. For the Steam half of a library that covers
everything GameLab needs: description, genres, release date, developer,
publisher, Metacritic score, and real cover art.

Rate limiting is self-imposed. The endpoint is undocumented and unmetered, and
hammering it gets IP addresses blocked, which would break the feature for every
GameLab user rather than just the impatient one.
"""

from __future__ import annotations

import logging
import threading
import time

from typing import Optional

import requests

from rose_gamelab.metadata.base import GameMetadata, MetadataProvider, ProviderError

logger = logging.getLogger(__name__)

STORE_API = "https://store.steampowered.com/api/appdetails"

# Steam's artwork CDN. `library_600x900` is the portrait cover used in the
# Steam library grid, which is the shape GameLab's cover grid wants.
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"
ART_URLS = {
    "cover": (f"{CDN}/{{appid}}/library_600x900_2x.jpg", f"{CDN}/{{appid}}/library_600x900.jpg"),
    "hero": (f"{CDN}/{{appid}}/library_hero.jpg",),
    "logo": (f"{CDN}/{{appid}}/logo.png",),
}

# Requests per second. Deliberately conservative.
RATE_LIMIT = 1.5
REQUEST_TIMEOUT = 15


class RateLimiter:
    """Spaces requests out across threads."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last = time.monotonic()


class SteamStoreProvider(MetadataProvider):
    """Metadata and artwork for Steam games. No key required."""

    name = "Steam"
    requires_key = False

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        rate_limit: float = RATE_LIMIT,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Rose-GameLab/0.1 (+https://github.com/Solidifiedinstone/Rose-GameLab)")
        # Tests pass rate_limit=0 to run against fakes without sleeping.
        self.limiter = RateLimiter(rate_limit)

    def available(self) -> bool:
        return True

    # ── Metadata ──────────────────────────────────────────────────

    def fetch(self, appid: int, *, country: str = "us", language: str = "english") -> Optional[GameMetadata]:
        """Fetch metadata for a Steam appid. Returns None if Steam has nothing.

        Raises ProviderError on network failure, so the caller can distinguish
        "no such game" from "could not reach Steam" and retry appropriately.
        """
        self.limiter.wait()

        try:
            response = self.session.get(
                STORE_API,
                params={"appids": str(appid), "cc": country, "l": language},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise ProviderError(f"could not reach Steam: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"Steam returned malformed JSON: {exc}") from exc

        entry = payload.get(str(appid))
        if not isinstance(entry, dict) or not entry.get("success"):
            # Steam reports delisted and region-locked apps this way.
            return None

        data = entry.get("data")
        if not isinstance(data, dict):
            return None

        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> GameMetadata:
        release = data.get("release_date") or {}
        release_date = _parse_release_date(release.get("date", ""))

        metacritic = data.get("metacritic") or {}
        score = metacritic.get("score")

        return GameMetadata(
            title=data.get("name"),
            # short_description is a clean one-paragraph blurb; detailed
            # descriptions are full of marketing HTML.
            summary=data.get("short_description") or None,
            release_date=release_date,
            developer=", ".join(data.get("developers") or []) or None,
            publisher=", ".join(data.get("publishers") or []) or None,
            genres=[g["description"] for g in data.get("genres") or [] if "description" in g],
            rating=float(score) if isinstance(score, (int, float)) else None,
            rating_source="metacritic" if score else None,
            source="steam",
        )

    # ── Artwork ───────────────────────────────────────────────────

    def artwork_urls(self, appid: int, kind: str) -> tuple[str, ...]:
        """Candidate URLs for a kind of artwork, best quality first."""
        templates = ART_URLS.get(kind, ())
        return tuple(template.format(appid=appid) for template in templates)

    def download_artwork(self, appid: int, kind: str) -> Optional[bytes]:
        """Download artwork, trying each candidate URL in turn.

        Returns None when Steam has no art of that kind for the game, which is
        common for older titles and is not an error.
        """
        for url in self.artwork_urls(appid, kind):
            self.limiter.wait()
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.debug("artwork request failed for %s: %s", url, exc)
                continue

            if response.status_code == 200 and response.content:
                return response.content

        return None


def _parse_release_date(text: str) -> Optional[str]:
    """Convert Steam's human-readable release date to ISO 8601.

    Steam returns things like '13 Aug, 2020', 'Aug 13, 2020', '2020', or
    'Coming soon'. Anything unparseable yields None rather than a wrong date.
    """
    text = (text or "").strip()
    if not text:
        return None

    from datetime import datetime

    for pattern in ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%b %Y", "%B %Y", "%Y"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue

        if pattern == "%Y":
            return f"{parsed.year:04d}"
        if pattern in ("%b %Y", "%B %Y"):
            return f"{parsed.year:04d}-{parsed.month:02d}"
        return parsed.date().isoformat()

    return None
