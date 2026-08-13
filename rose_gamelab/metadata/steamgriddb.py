"""Artwork from SteamGridDB — the fallback for everything else.

The keyless sources each cover one world and nothing outside it. Steam knows
Steam games. The libretro archive knows console games that were dumped and
named the way its dat files expect. Between them sits a large and growing set
of things a real library contains and neither can answer for:

  - launchers and clients: Sober, Prism, an Anime Game Launcher
  - storefront exclusives that never shipped on Steam
  - fan games, ROM hacks, translations and homebrew
  - console dumps the archive simply has not got

SteamGridDB is a community art database that covers all of it, keyed by name
rather than by hash or filename. It needs a free API key, so this provider is
frequently unavailable — everything above it in the chain works without one,
and this is what turns "no art" into art when they miss.

The key is read from config, never from the library database, matching how
RetroAchievements credentials are handled: a credential does not belong in a
file users are encouraged to copy around and share.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

import requests

from rose_gamelab.metadata.base import (
    USER_AGENT,
    MetadataProvider,
    ProviderError,
    normalise_for_match,
)
from rose_gamelab.metadata.steam_store import RateLimiter

logger = logging.getLogger(__name__)

BASE = "https://www.steamgriddb.com/api/v2"

REQUEST_TIMEOUT = 15
RATE_LIMIT = 0.34

#: What SteamGridDB calls each kind of art, and the shape we ask for.
#:
#: `grid` with a 600x900 dimension is the portrait cover; asking without the
#: dimension returns landscape grids too, which would fill a portrait cover
#: grid with letterboxed images.
ENDPOINTS = {
    "cover": ("grids", {"dimensions": "600x900,342x482,660x930"}),
    "hero": ("heroes", {}),
    "logo": ("logos", {}),
}


def api_key_from_config(config) -> Optional[str]:
    """Read the SteamGridDB API key out of the GameLab config.

    Falls back to the environment, which is how a user who would rather not
    write a credential to disk at all can still use the provider.
    """
    key = None
    if config is not None:
        try:
            key = config.get("steamgriddb.api_key")
        except Exception:                            # a config of any shape
            key = None

    if isinstance(key, str) and key.strip():
        return key.strip()

    from_env = (os.environ.get("STEAMGRIDDB_API_KEY") or "").strip()
    if from_env:
        return from_env

    # Where the Settings screen puts it. Imported late: the metadata layer must
    # not depend on the interface, and this is the one place they touch.
    try:
        from rose_gamelab.ui.preferences import artwork_key
        return artwork_key()
    except Exception:
        return None


class SteamGridDBProvider(MetadataProvider):
    """Community artwork for anything, by name. Needs a free API key."""

    name = "SteamGridDB"
    requires_key = True

    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
        *,
        rate_limit: float = RATE_LIMIT,
    ) -> None:
        # Falling back to the environment means a key set there works without
        # any config plumbing, which is how the scraper's default instance
        # becomes useful rather than permanently unavailable.
        self._api_key = (api_key or "").strip() or api_key_from_config(None)
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.limiter = RateLimiter(rate_limit)

    @classmethod
    def from_config(cls, config, **kwargs) -> "SteamGridDBProvider":
        return cls(api_key_from_config(config), **kwargs)

    def available(self) -> bool:
        return self._api_key is not None

    # ── Requests ──────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """Call the API. Returns the payload's `data`, or None when it has none.

        A missing game is a 404 here and is not an error: most of what this
        provider is asked about it will not have.
        """
        if not self.available():
            raise ProviderError("no SteamGridDB API key configured")

        self.limiter.wait()
        try:
            response = self.session.get(
                f"{BASE}/{path}",
                params=params or {},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"could not reach SteamGridDB: {exc}") from exc

        if response.status_code == 401:
            raise ProviderError("SteamGridDB rejected the API key")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProviderError(f"SteamGridDB returned {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"SteamGridDB returned malformed JSON: {exc}") from exc

        if not payload.get("success"):
            return None
        return payload

    # ── Lookup ────────────────────────────────────────────────────

    def find_game(self, names: Iterable[str], *, steam_appid: Optional[int] = None) -> Optional[int]:
        """SteamGridDB's own id for a game, or None.

        An appid is exact, so it is tried first. Falling back to search, only
        an exact name match is accepted — the search endpoint happily returns
        sequels and spin-offs, and putting the wrong cover on a game is worse
        than leaving it blank.
        """
        if steam_appid:
            payload = self._get(f"games/steam/{int(steam_appid)}")
            data = (payload or {}).get("data") or {}
            if isinstance(data, dict) and isinstance(data.get("id"), int):
                return data["id"]

        for name in names:
            if not name or not name.strip():
                continue

            payload = self._get(f"search/autocomplete/{requests.utils.quote(name.strip())}")
            results = (payload or {}).get("data") or []
            wanted = normalise_for_match(name)

            for entry in results:
                if not isinstance(entry, dict):
                    continue
                if (
                    normalise_for_match(entry.get("name") or "") == wanted
                    and isinstance(entry.get("id"), int)
                ):
                    return entry["id"]

        return None

    def artwork_urls(self, game_id: int, kind: str = "cover") -> list[str]:
        """Candidate artwork URLs for a SteamGridDB game id, best first."""
        endpoint, params = ENDPOINTS.get(kind, (None, None))
        if endpoint is None:
            return []

        payload = self._get(f"{endpoint}/game/{int(game_id)}", params)
        entries = (payload or {}).get("data") or []

        # The API returns most-upvoted first, which is the community's answer
        # to "which of these is the good one".
        return [
            entry["url"] for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("url"), str)
        ]

    def download_artwork(
        self,
        names: Iterable[str],
        *,
        kind: str = "cover",
        steam_appid: Optional[int] = None,
    ) -> Optional[bytes]:
        """Find and download artwork. None when there is none, which is normal."""
        names = list(names)

        game_id = self.find_game(names, steam_appid=steam_appid)
        if game_id is None:
            return None

        for url in self.artwork_urls(game_id, kind):
            self.limiter.wait()
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.debug("SteamGridDB download failed for %s: %s", url, exc)
                continue

            if response.status_code == 200 and response.content:
                return response.content

        return None
