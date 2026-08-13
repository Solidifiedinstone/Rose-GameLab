"""Discovering popular games, for the Browse tab.

An honest note on coverage, because it shaped this module.

For PC there is a real, key-free source: Steam publishes its most-played chart
with actual concurrent-player counts. That is genuine ranking data and this
module uses it.

For retro consoles there is no free, open ranking API. Nothing publishes "the
top NES games" in a form we can query without a commercial key. The previous
implementation solved this by hardcoding a list of games and presenting it as
live data, which is exactly the kind of thing this project does not do.

So: PC gets real charts. Retro systems report honestly that no ranking source
is configured, and offer what CAN be derived truthfully — the most-played games
in the user's own library, clearly labelled as such. If the user supplies an
IGDB key later, that becomes a real cross-platform source and can slot in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

from rose_gamelab.metadata.base import USER_AGENT, ProviderError
from rose_gamelab.metadata.steam_store import RateLimiter

logger = logging.getLogger(__name__)

MOST_PLAYED = "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"
REQUEST_TIMEOUT = 20


@dataclass
class ChartEntry:
    """One game in a chart."""

    rank: int
    title: str
    appid: Optional[int] = None
    peak_players: Optional[int] = None
    last_week_rank: Optional[int] = None
    cover_url: Optional[str] = None
    #: True when the user already has this game in their library.
    owned: bool = False

    @property
    def movement(self) -> Optional[int]:
        """Places gained since last week. Positive is a rise."""
        if self.last_week_rank is None:
            return None
        return self.last_week_rank - self.rank


@dataclass
class Chart:
    """A ranked list, with an honest account of where it came from."""

    title: str
    entries: list[ChartEntry] = field(default_factory=list)
    source: str = ""
    #: Explains what this list actually is, shown in the interface. Required
    #: when the list is not a real external ranking.
    caveat: Optional[str] = None

    @property
    def is_real_ranking(self) -> bool:
        return self.caveat is None


class SteamCharts:
    """Steam's most-played chart. No API key required."""

    name = "Steam"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        rate_limit: float = 1.0,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.limiter = RateLimiter(rate_limit)

    def most_played(self, *, limit: int = 50) -> Chart:
        """The current most-played games on Steam, by concurrent players.

        Raises ProviderError if Steam cannot be reached, so the interface can
        say "could not load" rather than showing an empty list that reads as
        "nothing is popular".
        """
        self.limiter.wait()

        try:
            response = self.session.get(MOST_PLAYED, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise ProviderError(f"could not reach Steam: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"Steam returned malformed JSON: {exc}") from exc

        ranks = (payload.get("response") or {}).get("ranks") or []

        entries: list[ChartEntry] = []
        for item in ranks[:limit]:
            appid = item.get("appid")
            if not isinstance(appid, int):
                continue

            entries.append(ChartEntry(
                rank=item.get("rank", len(entries) + 1),
                # The chart endpoint returns appids only; titles are resolved
                # separately so one slow lookup cannot block the whole list.
                title=f"App {appid}",
                appid=appid,
                peak_players=item.get("peak_in_game"),
                last_week_rank=item.get("last_week_rank"),
                cover_url=(
                    "https://cdn.cloudflare.steamstatic.com/steam/apps/"
                    f"{appid}/library_600x900_2x.jpg"
                ),
            ))

        return Chart(title="Most Played on Steam", entries=entries, source="steam")

    def resolve_titles(self, chart: Chart, store) -> Chart:
        """Fill in real titles for a chart, using the store provider.

        Done as a separate step because it costs one request per game at a
        polite rate; the interface shows the chart immediately and fills names
        in as they arrive.
        """
        for entry in chart.entries:
            if entry.appid is None:
                continue
            try:
                metadata = store.fetch(entry.appid)
            except ProviderError as exc:
                logger.debug("could not resolve %s: %s", entry.appid, exc)
                continue

            if metadata and metadata.title:
                entry.title = metadata.title

        return chart


class LibraryCharts:
    """Rankings derived from the user's own library.

    Everything here is truthful by construction — it is the user's own data —
    and is always labelled so it is never mistaken for a global ranking.
    """

    def __init__(self, library) -> None:
        self.library = library

    def most_played(self, *, system: Optional[str] = None, limit: int = 50) -> Chart:
        games = [
            game for game in self.library.list_games(
                system=system, sort="playtime", descending=True
            )
            if game.play_seconds
        ][:limit]

        return Chart(
            title="Your Most Played",
            source="library",
            caveat="Based on your own playtime, not a global ranking.",
            entries=[
                ChartEntry(
                    rank=index,
                    title=game.title,
                    appid=game.steam_appid,
                    owned=True,
                )
                for index, game in enumerate(games, start=1)
            ],
        )

    def recently_added(self, *, system: Optional[str] = None, limit: int = 50) -> Chart:
        games = self.library.list_games(
            system=system, sort="added", descending=True
        )[:limit]

        return Chart(
            title="Recently Added",
            source="library",
            caveat="From your own library.",
            entries=[
                ChartEntry(rank=index, title=game.title, owned=True)
                for index, game in enumerate(games, start=1)
            ],
        )


def chart_for_system(system_id: str, library, *, steam: Optional[SteamCharts] = None) -> Chart:
    """The best available chart for a system.

    PC gets Steam's real ranking. Every other system gets a labelled
    library-derived list, because no free ranking source exists for retro
    consoles and inventing one would be worse than admitting it.
    """
    if system_id == "pc":
        provider = steam or SteamCharts()
        return provider.most_played()

    chart = LibraryCharts(library).most_played(system=system_id)
    chart.caveat = (
        "No free ranking source exists for this system, so this shows your own "
        "most-played games. Adding an IGDB key in Settings would enable real "
        "cross-platform rankings."
    )
    return chart
