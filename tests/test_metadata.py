"""Tests for the artwork cache, metadata providers and the scraper.

No test here touches the network. Providers are driven through a fake session
so the suite is fast, deterministic and works offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rose_gamelab.core.library import Library
from rose_gamelab.db.database import Database
from rose_gamelab.metadata.base import GameMetadata
from rose_gamelab.metadata.cache import ArtCache, detect_image_type
from rose_gamelab.metadata.libretro_art import (
    LibretroArtProvider,
    candidate_names,
)
from rose_gamelab.metadata.scraper import Scraper
from rose_gamelab.metadata.steam_store import SteamStoreProvider, _parse_release_date

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64


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


@pytest.fixture
def cache(tmp_path):
    return ArtCache(tmp_path / "art")


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "library.db")
    yield Library(db)
    db.close()


# ── Image detection ───────────────────────────────────────────────

@pytest.mark.parametrize("data,expected", [
    (PNG, ".png"),
    (JPEG, ".jpg"),
    (WEBP, ".webp"),
    (b"GIF89a" + b"\x00" * 16, ".gif"),
])
def test_detects_image_formats(data, expected):
    assert detect_image_type(data) == expected


def test_html_error_page_is_not_an_image():
    """Servers return HTML error pages with a 200 status often enough that
    the content-type header cannot be trusted."""
    assert detect_image_type(b"<!DOCTYPE html><html>404</html>") is None


def test_riff_that_is_not_webp_is_rejected():
    assert detect_image_type(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 16) is None


def test_empty_data_is_not_an_image():
    assert detect_image_type(b"") is None


# ── Art cache ─────────────────────────────────────────────────────

def test_stores_and_finds_artwork(cache):
    stored = cache.store("game:1", "cover", PNG)

    assert stored is not None
    assert cache.find("game:1", "cover") == stored
    assert cache.has("game:1", "cover")


def test_non_image_is_not_stored(cache):
    assert cache.store("game:1", "cover", b"<html>nope</html>") is None
    assert not cache.has("game:1", "cover")


def test_oversized_artwork_is_refused(cache):
    from rose_gamelab.metadata import cache as cache_module
    too_big = JPEG + b"\x00" * cache_module.MAX_ARTWORK_BYTES

    assert cache.store("game:1", "cover", too_big) is None


def test_keys_with_path_separators_cannot_escape_the_cache(cache):
    """A title containing slashes must not write outside the cache directory."""
    stored = cache.store("../../etc/passwd", "cover", PNG)

    assert stored is not None
    assert cache.root.resolve() in stored.resolve().parents


def test_different_identifiers_do_not_collide(cache):
    cache.store("game:1", "cover", PNG)
    cache.store("game:2", "cover", JPEG)

    assert cache.find("game:1", "cover") != cache.find("game:2", "cover")


def test_no_partial_files_are_left_behind(cache):
    cache.store("game:1", "cover", PNG)
    assert list(cache.root.rglob("*.part")) == []


def test_unknown_kind_is_rejected(cache):
    with pytest.raises(ValueError):
        cache.path_for("game:1", "not-a-kind")


def test_missing_artwork_returns_none(cache):
    assert cache.find("game:404", "cover") is None


def test_remove_and_clear(cache):
    cache.store("game:1", "cover", PNG)

    assert cache.remove("game:1", "cover") == 1
    assert not cache.has("game:1", "cover")

    cache.store("game:2", "cover", PNG)
    assert cache.size_bytes() > 0
    cache.clear()
    assert cache.size_bytes() == 0


# ── Steam release-date parsing ────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("17 Sep, 2020", "2020-09-17"),
    ("Sep 17, 2020", "2020-09-17"),
    ("Oct 2007", "2007-10"),
    ("2020", "2020"),
])
def test_parses_steam_date_formats(text, expected):
    assert _parse_release_date(text) == expected


@pytest.mark.parametrize("text", ["Coming soon", "To be announced", "", "Q4 2025"])
def test_unparseable_dates_yield_none_not_a_wrong_date(text):
    assert _parse_release_date(text) is None


# ── Steam provider ────────────────────────────────────────────────

def steam_payload(appid: int, **overrides) -> dict:
    data = {
        "name": "Hades",
        "short_description": "Defy the god of the dead.",
        "release_date": {"date": "17 Sep, 2020"},
        "developers": ["Supergiant Games"],
        "publishers": ["Supergiant Games"],
        "genres": [{"description": "Action"}, {"description": "Indie"}],
        "metacritic": {"score": 93},
    }
    data.update(overrides)
    return {str(appid): {"success": True, "data": data}}


def test_parses_steam_metadata():
    session = FakeSession()
    session.route("appdetails", FakeResponse(payload=steam_payload(1145360)))

    result = SteamStoreProvider(session=session, rate_limit=0).fetch(1145360)

    assert result.title == "Hades"
    assert result.release_date == "2020-09-17"
    assert result.developer == "Supergiant Games"
    assert result.genres == ["Action", "Indie"]
    assert result.rating == 93.0
    assert result.rating_source == "metacritic"


def test_unsuccessful_steam_response_returns_none():
    """Delisted and region-locked apps report success: false."""
    session = FakeSession()
    session.route("appdetails", FakeResponse(payload={"1": {"success": False}}))

    assert SteamStoreProvider(session=session, rate_limit=0).fetch(1) is None


def test_network_failure_raises_rather_than_reporting_no_such_game():
    """The caller must distinguish 'not found' from 'could not reach Steam'."""
    import requests

    from rose_gamelab.metadata.base import ProviderError

    class BrokenSession(FakeSession):
        def get(self, *a, **kw):
            raise requests.ConnectionError("offline")

    with pytest.raises(ProviderError):
        SteamStoreProvider(session=BrokenSession(), rate_limit=0).fetch(1)


def test_missing_optional_fields_are_tolerated():
    session = FakeSession()
    session.route("appdetails", FakeResponse(payload={
        "1": {"success": True, "data": {"name": "Bare"}}
    }))

    result = SteamStoreProvider(session=session, rate_limit=0).fetch(1)
    assert result.title == "Bare"
    assert result.rating is None


def test_steam_cover_art_is_downloaded():
    session = FakeSession()
    session.route("library_600x900_2x", FakeResponse(content=JPEG))

    data = SteamStoreProvider(session=session, rate_limit=0).download_artwork(1145360, "cover")
    assert data == JPEG


def test_falls_back_to_lower_resolution_cover():
    session = FakeSession()
    session.route("library_600x900_2x.jpg", FakeResponse(status=404))
    session.route("library_600x900.jpg", FakeResponse(content=JPEG))

    assert SteamStoreProvider(session=session, rate_limit=0).download_artwork(1, "cover") == JPEG


# ── libretro naming ───────────────────────────────────────────────

def test_filename_stem_is_tried_first():
    names = candidate_names("Chrono Trigger", filename_stem="Chrono Trigger (USA)")
    assert names[0] == "Chrono Trigger (USA)"


def test_generates_dat_style_article_variant():
    """The archive files 'The Legend of Zelda' as 'Legend of Zelda, The'."""
    names = candidate_names("The Legend of Zelda")
    assert any(n.startswith("Legend of Zelda, The") for n in names)


def test_generates_subtitle_variant():
    """Dat naming uses ' - ' for subtitles, not a colon."""
    names = candidate_names("The Legend of Zelda: A Link to the Past")
    assert "Legend of Zelda, The - A Link to the Past" in names


def test_region_variants_are_offered():
    names = candidate_names("Super Metroid")
    assert "Super Metroid (USA)" in names


def test_unsupported_system_has_no_url():
    provider = LibretroArtProvider(session=FakeSession(), rate_limit=0)
    assert provider.url_for("pc", "Hades") is None
    assert not provider.supports_system("pc")


def test_illegal_characters_are_substituted():
    provider = LibretroArtProvider(session=FakeSession(), rate_limit=0)
    url = provider.url_for("snes", "Ratchet & Clank")
    assert "%26" not in url  # '&' becomes '_' before quoting


def test_libretro_tries_candidates_until_one_hits():
    session = FakeSession()
    session.route("Legend%20of%20Zelda%2C%20The", FakeResponse(content=PNG))

    provider = LibretroArtProvider(session=session, rate_limit=0)
    assert provider.download_artwork("nes", "The Legend of Zelda") == PNG


def test_libretro_returns_none_when_nothing_matches():
    provider = LibretroArtProvider(session=FakeSession(), rate_limit=0)
    assert provider.download_artwork("nes", "Nonexistent Game") is None


# ── Metadata merging ──────────────────────────────────────────────

def test_merge_fills_gaps_without_overwriting():
    first = GameMetadata(title="A", summary="good summary")
    second = GameMetadata(title="B", summary="worse", developer="Someone")

    merged = first.merge(second)

    assert merged.title == "A"
    assert merged.summary == "good summary"
    assert merged.developer == "Someone"


def test_empty_metadata_is_detected():
    assert GameMetadata(title="Only a title").is_empty
    assert not GameMetadata(summary="something").is_empty


# ── Scraper ───────────────────────────────────────────────────────

@pytest.fixture
def scraper(library, cache, tmp_path):
    session = FakeSession()
    session.route("appdetails", FakeResponse(payload=steam_payload(1145360)))
    session.route("library_600x900", FakeResponse(content=JPEG))

    from rose_gamelab.metadata.openvgdb import OpenVGDBProvider

    return Scraper(
        library,
        cache=cache,
        steam=SteamStoreProvider(session=session, rate_limit=0),
        libretro=LibretroArtProvider(session=FakeSession(), rate_limit=0),
        # Pointed at a path that does not exist, so these tests never depend on
        # whether the real offline database has been downloaded on this machine.
        openvgdb=OpenVGDBProvider(tmp_path / "absent.sqlite"),
    )


def test_scrapes_metadata_into_the_library(library, scraper):
    game_id = library.add_game(title="Hades", system="pc", steam_appid=1145360)

    assert scraper.scrape_game(game_id) is True

    game = library.get(game_id)
    assert game.release_date == "2020-09-17"
    assert game.developer == "Supergiant Games"
    assert game.rating == 93.0


def test_genres_become_tags(library, scraper):
    game_id = library.add_game(title="Hades", system="pc", steam_appid=1145360)
    scraper.scrape_game(game_id)

    assert set(library.tags_for(game_id)) >= {"Action", "Indie"}


def test_cover_is_cached_and_linked(library, scraper, cache):
    game_id = library.add_game(title="Hades", system="pc", steam_appid=1145360)
    scraper.scrape_game(game_id)

    game = library.get(game_id)
    assert game.cover_path
    assert Path(game.cover_path).is_file()


def test_rescraping_does_not_overwrite_existing_values(library, scraper):
    game_id = library.add_game(title="Hades", system="pc", steam_appid=1145360)
    library.update_game(game_id, summary="my own words")

    scraper.scrape_game(game_id)

    assert library.get(game_id).summary == "my own words"


def test_overwrite_replaces_existing_values(library, scraper):
    game_id = library.add_game(title="Hades", system="pc", steam_appid=1145360)
    library.update_game(game_id, summary="my own words")

    scraper.scrape_game(game_id, overwrite=True)

    assert library.get(game_id).summary == "Defy the god of the dead."


def test_hand_edited_games_are_left_alone(library, scraper):
    """Hand-corrected metadata is more trustworthy than a scraper's guess."""
    game_id = library.add_game(title="Hades", system="pc", steam_appid=1145360)
    library.update_game(game_id, metadata_locked=1)

    assert scraper.scrape_game(game_id) is False
    assert library.get(game_id).release_date is None


def test_locked_games_can_be_forced(library, scraper):
    game_id = library.add_game(title="Hades", system="pc", steam_appid=1145360)
    library.update_game(game_id, metadata_locked=1)

    scraper.scrape_game(game_id, overwrite=True)
    assert library.get(game_id).release_date == "2020-09-17"


def test_scrape_library_reports_real_counts(library, scraper):
    library.add_game(title="Hades", system="pc", steam_appid=1145360)
    library.add_game(title="Unknown Game", system="pc")

    state = scraper.scrape_library()

    assert state.total == 2
    assert state.processed == 2
    assert state.metadata_found == 1
    assert state.not_found == 1


def test_scrape_can_be_cancelled_mid_run(library, scraper):
    """Cancel is called from the interface while a scrape is in flight."""
    for i in range(5):
        library.add_game(title=f"Game {i}", system="pc", steam_appid=1145360)

    def stop_after_two(state, _title):
        if state.processed >= 2:
            scraper.cancel()

    state = scraper.scrape_library(progress=stop_after_two)

    assert state.processed == 2
    assert state.remaining == 3


def test_cancelled_scrape_keeps_what_it_found(library, cache, tmp_path):
    """Interrupting must not discard completed work."""
    session = FakeSession()
    session.route("appdetails", FakeResponse(payload=steam_payload(1145360)))

    from rose_gamelab.metadata.openvgdb import OpenVGDBProvider
    scraper = Scraper(library, cache=cache, steam=SteamStoreProvider(session=session, rate_limit=0),
                      libretro=LibretroArtProvider(session=FakeSession(), rate_limit=0),
                      openvgdb=OpenVGDBProvider(tmp_path / "absent.sqlite"))

    game_id = library.add_game(title="Hades", system="pc", steam_appid=1145360)
    scraper.scrape_game(game_id)
    scraper.cancel()
    scraper.scrape_library()

    assert library.get(game_id).release_date == "2020-09-17"


def test_one_failure_does_not_abort_the_run(library, cache, tmp_path):
    class ExplodingSteam(SteamStoreProvider):
        def fetch(self, appid, **kw):
            raise RuntimeError("boom")

    from rose_gamelab.metadata.openvgdb import OpenVGDBProvider
    scraper = Scraper(library, cache=cache, steam=ExplodingSteam(session=FakeSession(), rate_limit=0),
                      libretro=LibretroArtProvider(session=FakeSession(), rate_limit=0),
                      openvgdb=OpenVGDBProvider(tmp_path / "absent.sqlite"))

    library.add_game(title="A", system="pc", steam_appid=1)
    library.add_game(title="B", system="pc", steam_appid=2)

    state = scraper.scrape_library()

    assert state.processed == 2
    assert len(state.errors) == 2


def test_only_missing_skips_complete_games(library, scraper):
    complete = library.add_game(title="Done", system="pc", steam_appid=1145360)
    library.update_game(
        complete, summary="x", release_date="2020-01-01", cover_path="/tmp/x.jpg"
    )

    state = scraper.scrape_library(only_missing=True)
    assert state.total == 0
