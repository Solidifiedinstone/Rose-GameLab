"""Tests for the Browse tab's chart sources.

The central concern here is honesty: a list must never claim to be a global
ranking when it is derived from the user's own library.
"""

from __future__ import annotations

import pytest

from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database
from rose_gamelab.metadata.base import ProviderError
from rose_gamelab.metadata.charts import (
    Chart,
    ChartEntry,
    LibraryCharts,
    SteamCharts,
    chart_for_system,
)


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "library.db")
    yield Library(db)
    db.close()


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("boom")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}

    def get(self, url, timeout=None):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


# Shape taken from the real endpoint.
REAL_PAYLOAD = {
    "response": {
        "ranks": [
            {"rank": 1, "appid": 730, "last_week_rank": 1, "peak_in_game": 1275982},
            {"rank": 2, "appid": 570, "last_week_rank": 4, "peak_in_game": 600000},
            {"rank": 3, "appid": 578080, "last_week_rank": 2, "peak_in_game": 400000},
        ]
    }
}


# ── Steam charts ──────────────────────────────────────────────────

def test_parses_the_real_payload_shape():
    charts = SteamCharts(session=FakeSession(FakeResponse(REAL_PAYLOAD)), rate_limit=0)
    chart = charts.most_played()

    assert len(chart.entries) == 3
    assert chart.entries[0].appid == 730
    assert chart.entries[0].peak_players == 1275982


def test_steam_chart_is_a_real_ranking():
    charts = SteamCharts(session=FakeSession(FakeResponse(REAL_PAYLOAD)), rate_limit=0)
    assert charts.most_played().is_real_ranking


def test_movement_is_computed_from_last_week():
    charts = SteamCharts(session=FakeSession(FakeResponse(REAL_PAYLOAD)), rate_limit=0)
    entries = charts.most_played().entries

    assert entries[1].movement == 2     # 4th -> 2nd is a rise of two
    assert entries[2].movement == -1    # 2nd -> 3rd is a fall of one


def test_limit_is_respected():
    charts = SteamCharts(session=FakeSession(FakeResponse(REAL_PAYLOAD)), rate_limit=0)
    assert len(charts.most_played(limit=2).entries) == 2


def test_cover_urls_are_generated():
    charts = SteamCharts(session=FakeSession(FakeResponse(REAL_PAYLOAD)), rate_limit=0)
    assert "730" in charts.most_played().entries[0].cover_url


def test_malformed_entries_are_skipped():
    payload = {"response": {"ranks": [
        {"rank": 1, "appid": "not-an-int"},
        {"rank": 2, "appid": 730},
    ]}}
    charts = SteamCharts(session=FakeSession(FakeResponse(payload)), rate_limit=0)

    assert [e.appid for e in charts.most_played().entries] == [730]


def test_empty_response_is_an_empty_chart():
    charts = SteamCharts(session=FakeSession(FakeResponse({"response": {}})), rate_limit=0)
    assert charts.most_played().entries == []


def test_network_failure_raises_rather_than_showing_an_empty_chart():
    """An empty list would read as 'nothing is popular' instead of 'offline'."""
    import requests

    charts = SteamCharts(
        session=FakeSession(requests.ConnectionError("offline")), rate_limit=0
    )
    with pytest.raises(ProviderError):
        charts.most_played()


def test_malformed_json_raises():
    charts = SteamCharts(session=FakeSession(FakeResponse(None)), rate_limit=0)
    with pytest.raises(ProviderError):
        charts.most_played()


# ── Library-derived charts ────────────────────────────────────────

def test_library_chart_is_labelled_as_not_a_global_ranking(library):
    chart = LibraryCharts(library).most_played()

    assert not chart.is_real_ranking
    assert chart.caveat


def test_library_most_played_ranks_by_playtime(library):
    quiet = library.add_game(title="Barely Touched", system="pc")
    loved = library.add_game(title="Played A Lot", system="pc")

    library.db.execute("UPDATE games SET play_seconds = ? WHERE id = ?", (100, quiet))
    library.db.execute("UPDATE games SET play_seconds = ? WHERE id = ?", (9999, loved))

    chart = LibraryCharts(library).most_played()
    assert [e.title for e in chart.entries] == ["Played A Lot", "Barely Touched"]


def test_never_played_games_are_excluded(library):
    library.add_game(title="Untouched", system="pc")
    assert LibraryCharts(library).most_played().entries == []


def test_library_chart_marks_everything_as_owned(library):
    game_id = library.add_game(title="Mine", system="pc")
    library.db.execute("UPDATE games SET play_seconds = 60 WHERE id = ?", (game_id,))

    assert all(e.owned for e in LibraryCharts(library).most_played().entries)


def test_recently_added_chart(library):
    library.add_game(title="First", system="pc")
    library.add_game(title="Second", system="pc")

    chart = LibraryCharts(library).recently_added()
    assert len(chart.entries) == 2
    assert chart.caveat


# ── Dispatch ──────────────────────────────────────────────────────

def test_pc_uses_the_real_steam_ranking(library):
    charts = SteamCharts(session=FakeSession(FakeResponse(REAL_PAYLOAD)), rate_limit=0)
    chart = chart_for_system("pc", library, steam=charts)

    assert chart.source == "steam"
    assert chart.is_real_ranking


def test_retro_systems_admit_they_have_no_ranking_source(library):
    """No free API publishes 'the top NES games'; inventing one is worse than
    saying so."""
    game_id = library.add_game(title="Mario", system="nes")
    library.db.execute("UPDATE games SET play_seconds = 60 WHERE id = ?", (game_id,))

    chart = chart_for_system("nes", library)

    assert not chart.is_real_ranking
    assert "No free ranking source" in chart.caveat


def test_retro_chart_still_returns_useful_data(library):
    game_id = library.add_game(title="Mario", system="nes")
    library.db.execute("UPDATE games SET play_seconds = 60 WHERE id = ?", (game_id,))

    assert [e.title for e in chart_for_system("nes", library).entries] == ["Mario"]
