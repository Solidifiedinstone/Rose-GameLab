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

import json
import logging
import threading
import time
from typing import Optional

import requests

from rose_gamelab.metadata.base import (
    USER_AGENT,
    GameMetadata,
    MetadataProvider,
    ProviderError,
    normalise_for_match,
)

logger = logging.getLogger(__name__)

STORE_API = "https://store.steampowered.com/api/appdetails"
SEARCH_API = "https://store.steampowered.com/api/storesearch/"

# Where Steam publishes the real filename of every asset it holds for an app.
# This is the only way to get art for anything released recently — see
# `assets()` for why guessing the URL stopped working.
STORE_BROWSE_API = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"

# Steam's artwork CDN. `library_600x900` is the portrait cover used in the
# Steam library grid, which is the shape GameLab's cover grid wants.
#
# These are the LEGACY paths, and they only exist for apps published before
# Steam moved to content-hashed asset directories. They are kept as a fallback
# for the case where the store service cannot be reached at all.
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"
ART_URLS = {
    "cover": (
        f"{CDN}/{{appid}}/library_600x900_2x.jpg",
        f"{CDN}/{{appid}}/library_600x900.jpg",
        # Older titles predate the portrait capsule. A landscape header is not
        # the right shape, but it beats an empty tile.
        f"{CDN}/{{appid}}/capsule_616x353.jpg",
        f"{CDN}/{{appid}}/header.jpg",
    ),
    "hero": (f"{CDN}/{{appid}}/library_hero.jpg", f"{CDN}/{{appid}}/page_bg_generated_v6b.jpg"),
    "logo": (f"{CDN}/{{appid}}/logo.png", f"{CDN}/{{appid}}/logo_2x.png"),
}

# What Steam's store service calls each kind of art we want, best first.
# `library_capsule` is the portrait cover; the landscape forms are fallbacks
# for apps that genuinely have no portrait art rather than shape preferences.
ASSET_NAMES = {
    "cover": ("library_capsule_2x", "library_capsule", "main_capsule", "header"),
    "hero": ("library_hero_2x", "library_hero", "page_background"),
    "logo": ("library_logo", "logo"),
}

# Words that mark a re-release rather than a different game. Used only when
# looking for ARTWORK, where the cover of a remaster is the right picture; a
# remaster's release date and reviews are a different matter, so metadata never
# accepts these.
EDITION_WORDS = {
    "remastered", "remaster", "remake", "hd", "definitive", "complete",
    "enhanced", "deluxe", "ultimate", "goty", "anniversary", "redux",
    "edition", "collection", "the", "of", "year", "game", "classic",
}

# Requests per second. Deliberately conservative.
RATE_LIMIT = 1.0
CDN_RATE_LIMIT = 0.05
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
        self.session.headers["User-Agent"] = USER_AGENT
        # Tests pass rate_limit=0 to run against fakes without sleeping.
        self.limiter = RateLimiter(rate_limit)
        # Artwork comes from a static CDN, not the API, so it gets its own
        # much lighter limiter.
        self.cdn_limiter = RateLimiter(0.0 if rate_limit == 0 else CDN_RATE_LIMIT)
        # One asset lookup serves every kind of art for a game.
        self._asset_cache: dict[int, dict[str, str]] = {}

    def available(self) -> bool:
        return True

    # ── Search ────────────────────────────────────────────────────

    def search(self, title: str, *, allow_editions: bool = False) -> Optional[int]:
        """Find the appid for a game by name, or None if unsure.

        For PC games that did not arrive through Steam — a Heroic install, a
        GOG copy, a launcher added by hand — Steam is still much the best
        source of art, but only once we know the appid.

        Deliberately strict: only an exact match after normalisation is
        accepted. A fuzzy match here would put Forza Horizon 5's cover on
        Forza Horizon 6, and wrong art is worse than none — the user cannot
        tell it is wrong without opening the game.
        """
        wanted = normalise_for_match(title)
        if not wanted:
            return None

        self.limiter.wait()
        try:
            response = self.session.get(
                SEARCH_API,
                params={"term": title, "l": "english", "cc": "us"},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.debug("Steam search failed for %r: %s", title, exc)
            return None

        fallback = None

        for item in payload.get("items") or []:
            name = item.get("name")
            appid = item.get("id")
            if not isinstance(name, str) or not isinstance(appid, int):
                continue

            found = normalise_for_match(name)
            if found == wanted:
                return appid

            # A re-release is the same game wearing a different subtitle, and
            # its cover is the one the user is looking for. Only whole known
            # edition words are allowed: a bare prefix match would put Dark
            # Souls' cover on Dark Souls II.
            if allow_editions and found.startswith(wanted + " "):
                suffix = found[len(wanted) + 1:]
                if all(word in EDITION_WORDS for word in suffix.split()):
                    fallback = fallback or appid

        return fallback

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

    def assets(self, appid: int) -> dict[str, str]:
        """Every asset Steam holds for an app: asset name -> absolute URL.

        Steam used to serve art from a predictable path — `.../apps/<appid>/
        library_600x900.jpg` — and GameLab guessed it. That stopped working:
        apps published in the last few years keep each asset in a
        content-hashed directory, e.g. `.../apps/2244210/cffacba…bc7/
        library_capsule.jpg`, where the hash is not derivable from anything we
        know. Guessing returns 404 for every one of them, which is why recent
        releases came back with no art at all while older games were fine.

        This asks Steam for the real filenames instead. Results are cached per
        instance, so fetching a cover, a hero and a logo is one request.

        Returns {} when Steam has nothing or cannot be reached — the caller
        falls back to the legacy paths, which still work for older apps.
        """
        if appid in self._asset_cache:
            return self._asset_cache[appid]

        request = {
            "ids": [{"appid": int(appid)}],
            "context": {"language": "english", "country_code": "US"},
            "data_request": {"include_assets": True},
        }

        self.limiter.wait()
        try:
            response = self.session.get(
                STORE_BROWSE_API,
                params={"input_json": json.dumps(request)},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Deliberately not a ProviderError: art is optional, and the legacy
            # paths are still worth trying.
            logger.debug("Steam asset lookup failed for %s: %s", appid, exc)
            return {}

        items = (payload.get("response") or {}).get("store_items") or []
        assets = (items[0].get("assets") or {}) if items else {}

        # Every filename is relative to this template, which carries the
        # appid and a cache-busting timestamp.
        template = assets.get("asset_url_format")
        resolved: dict[str, str] = {}
        if isinstance(template, str) and template:
            for name, filename in assets.items():
                if name == "asset_url_format" or not isinstance(filename, str):
                    continue
                if "." not in filename:
                    # community_icon is a bare hash with no extension; it is an
                    # icon rather than artwork and we have no use for it.
                    continue
                resolved[name] = ASSET_BASE + template.replace("${FILENAME}", filename)

        self._asset_cache[appid] = resolved
        return resolved

    def artwork_urls(self, appid: int, kind: str) -> tuple[str, ...]:
        """Legacy candidate URLs for a kind of artwork, best quality first.

        Pure and offline. `resolved_artwork_urls` is what callers want.
        """
        templates = ART_URLS.get(kind, ())
        return tuple(template.format(appid=appid) for template in templates)

    def resolved_artwork_urls(self, appid: int, kind: str) -> tuple[str, ...]:
        """URLs to try for one kind of art, best first.

        Steam's own answer comes first; the legacy guesses follow, so a game
        still gets art when the store service is unreachable.
        """
        assets = self.assets(appid)
        live = [
            assets[name] for name in ASSET_NAMES.get(kind, ())
            if name in assets
        ]
        legacy = [url for url in self.artwork_urls(appid, kind) if url not in live]
        return tuple(live + legacy)

    def download_artwork(self, appid: int, kind: str) -> Optional[bytes]:
        """Download artwork, trying each candidate URL in turn.

        Returns None when Steam has no art of that kind for the game, which is
        common for older titles and is not an error.
        """
        for url in self.resolved_artwork_urls(appid, kind):
            self.cdn_limiter.wait()
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
