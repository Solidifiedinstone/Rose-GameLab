"""Tests for the artwork cache, metadata providers and the scraper.

No test here touches the network. Providers are driven through a fake session
so the suite is fast, deterministic and works offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_folder_games import build_sfo

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
from rose_gamelab.metadata.steamgriddb import SteamGridDBProvider
from rose_gamelab.metadata.wikidata import WikidataProvider

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

    def get(self, url, params=None, timeout=None, headers=None):
        if params:
            url = f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
        self.requested.append(url)
        self.last_headers = headers or {}
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


# ── Steam asset resolution ────────────────────────────────────────
#
# Steam used to serve art from a guessable path. Apps published in the last few
# years keep each asset in a content-hashed directory instead, so guessing
# returns 404 for every recent release — which is why they scraped to no art at
# all while older games were fine.

def browse_payload(appid: int, assets: dict) -> dict:
    return {"response": {"store_items": [{"appid": appid, "assets": assets}]}}


HASHED_ASSETS = {
    "asset_url_format": "steam/apps/2244210/${FILENAME}?t=1784167139",
    "library_capsule": "cffacba/library_capsule.jpg",
    "library_capsule_2x": "cffacba/library_capsule_2x.jpg",
    "header": "25fb635/header.jpg",
    "community_icon": "58663d7338f3b6ac513d5fe4cc28c418076109a7",
}


def test_hashed_asset_paths_are_resolved():
    """The fix for every recently published game."""
    session = FakeSession()
    session.route("IStoreBrowseService", FakeResponse(payload=browse_payload(2244210, HASHED_ASSETS)))

    urls = SteamStoreProvider(session=session, rate_limit=0).resolved_artwork_urls(2244210, "cover")

    assert urls[0].endswith("steam/apps/2244210/cffacba/library_capsule_2x.jpg?t=1784167139")
    assert urls[0].startswith("https://shared.akamai.steamstatic.com/store_item_assets/")


def test_steam_reported_urls_are_preferred_over_guesses():
    session = FakeSession()
    session.route("IStoreBrowseService", FakeResponse(payload=browse_payload(1, HASHED_ASSETS)))

    urls = SteamStoreProvider(session=session, rate_limit=0).resolved_artwork_urls(1, "cover")

    # The legacy guesses stay as a fallback, but never come first.
    assert "library_capsule_2x" in urls[0]
    assert any("library_600x900" in u for u in urls)


def test_assets_without_an_extension_are_not_artwork():
    """community_icon is a bare hash; treating it as a file yields a 404."""
    session = FakeSession()
    session.route("IStoreBrowseService", FakeResponse(payload=browse_payload(1, HASHED_ASSETS)))

    assets = SteamStoreProvider(session=session, rate_limit=0).assets(1)

    assert "community_icon" not in assets
    assert "library_capsule" in assets


def test_legacy_paths_are_used_when_the_store_service_is_unreachable():
    """Older games still scrape when Steam's newer endpoint cannot be reached."""
    session = FakeSession()          # every route 404s, including the service
    urls = SteamStoreProvider(session=session, rate_limit=0).resolved_artwork_urls(440, "cover")

    assert urls and all("/steam/apps/440/" in u for u in urls)


def test_asset_lookup_is_cached_per_app():
    session = FakeSession()
    session.route("IStoreBrowseService", FakeResponse(payload=browse_payload(1, HASHED_ASSETS)))

    provider = SteamStoreProvider(session=session, rate_limit=0)
    provider.assets(1)
    provider.assets(1)

    assert sum("IStoreBrowseService" in u for u in session.requested) == 1


# ── Steam search ──────────────────────────────────────────────────

def search_payload(items: list[tuple[int, str]]) -> dict:
    return {"items": [{"id": appid, "name": name} for appid, name in items]}


def test_search_finds_an_appid_by_name():
    session = FakeSession()
    session.route("storesearch", FakeResponse(payload=search_payload([(2244210, "Echoes of Aincrad")])))

    assert SteamStoreProvider(session=session, rate_limit=0).search("Echoes of Aincrad") == 2244210


def test_search_ignores_trademark_and_punctuation_differences():
    session = FakeSession()
    session.route("storesearch", FakeResponse(payload=search_payload([(1, "Forza Horizon 6™")])))

    assert SteamStoreProvider(session=session, rate_limit=0).search("Forza Horizon 6") == 1


def test_search_refuses_a_near_miss():
    """Wrong art is worse than none: the user cannot tell it is wrong."""
    session = FakeSession()
    session.route("storesearch", FakeResponse(payload=search_payload([
        (1551360, "Forza Horizon 5"),
        (1, "Forza Horizon 6 VIP Membership"),
    ])))

    assert SteamStoreProvider(session=session, rate_limit=0).search("Forza Horizon 6") is None


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


# ── Systems the archive carries ───────────────────────────────────

@pytest.mark.parametrize("system", ["ps3", "ps4", "psvita", "xbox", "xbox360", "wiiu"])
def test_hd_era_systems_are_supported(system):
    """These were missing from the map, so PS3 libraries scraped to nothing.

    The scraper asks `supports_system` before trying at all — an absent entry
    meant the archive was never even asked.
    """
    provider = LibretroArtProvider(session=FakeSession(), rate_limit=0)
    assert provider.supports_system(system)
    assert provider.url_for(system, "Demon's Souls (USA)")


def test_arcade_searches_both_archives():
    """MAME and FBNeo disagree about which sets they hold."""
    provider = LibretroArtProvider(session=FakeSession(), rate_limit=0)
    urls = provider.urls_for("arcade", "sfiii3")

    assert len(urls) == 2
    assert any("MAME" in u for u in urls)
    assert any("FBNeo" in u for u in urls)


# ── Names from the game itself ────────────────────────────────────

def test_extra_names_are_tried_before_the_library_title():
    """A PS3 folder is often named BLES01143; PARAM.SFO knows the real name."""
    names = candidate_names("BLES01143", filename_stem="BLES01143",
                            extra=["Ni no Kuni: Wrath of the White Witch"])

    assert names[0] == "BLES01143"          # the stem is still tried first
    assert names.index("Ni no Kuni: Wrath of the White Witch") < names.index("BLES01143 (USA)")


def test_extra_names_get_dat_style_variants():
    """The archive writes subtitles with ' - ', never a colon."""
    names = candidate_names("BLUS30605", extra=["Castlevania: Lords of Shadow"])

    assert "Castlevania - Lords of Shadow" in names
    assert "Castlevania - Lords of Shadow (USA)" in names


def test_article_variants_still_come_from_the_library_title():
    names = candidate_names("The Legend of Zelda")
    assert "Legend of Zelda, The" in names


def test_libretro_uses_an_extra_name_to_find_art():
    session = FakeSession()
    session.route("Castlevania%20-%20Lords%20of%20Shadow", FakeResponse(content=PNG))

    provider = LibretroArtProvider(session=session, rate_limit=0)
    data = provider.download_artwork(
        "ps3", "BLUS30605", filename_stem="BLUS30605",
        extra_names=["Castlevania: Lords of Shadow"],
    )

    assert data == PNG


# ── SteamGridDB ───────────────────────────────────────────────────
#
# The catch-all for what the keyless sources cannot answer: launchers, fan
# games, storefront exclusives, and dumps the archive has not got.

def griddb(session, key="k"):
    return SteamGridDBProvider(api_key=key, session=session, rate_limit=0)


def test_griddb_is_unavailable_without_a_key(monkeypatch):
    monkeypatch.delenv("STEAMGRIDDB_API_KEY", raising=False)
    assert not SteamGridDBProvider(api_key=None, session=FakeSession()).available()


def test_griddb_reads_a_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("STEAMGRIDDB_API_KEY", "from-env")
    assert SteamGridDBProvider(session=FakeSession()).available()


def test_griddb_sends_the_key_as_a_bearer_token():
    session = FakeSession()
    session.route("autocomplete", FakeResponse(payload={"success": True, "data": []}))

    griddb(session).find_game(["Sober"])

    assert session.last_headers.get("Authorization") == "Bearer k"


def test_griddb_finds_a_game_by_exact_name():
    session = FakeSession()
    session.route("autocomplete", FakeResponse(payload={
        "success": True, "data": [{"id": 42, "name": "Sober"}]}))

    assert griddb(session).find_game(["Sober"]) == 42


def test_griddb_refuses_a_near_miss():
    session = FakeSession()
    session.route("autocomplete", FakeResponse(payload={
        "success": True, "data": [{"id": 1, "name": "Sober Simulator 2"}]}))

    assert griddb(session).find_game(["Sober"]) is None


def test_griddb_prefers_an_exact_appid_over_a_name():
    session = FakeSession()
    session.route("games/steam/440", FakeResponse(payload={
        "success": True, "data": {"id": 99, "name": "Anything At All"}}))

    assert griddb(session).find_game(["ignored"], steam_appid=440) == 99


def test_griddb_downloads_the_top_voted_art():
    session = FakeSession()
    session.route("autocomplete", FakeResponse(payload={
        "success": True, "data": [{"id": 42, "name": "Sober"}]}))
    session.route("grids/game/42", FakeResponse(payload={"success": True, "data": [
        {"url": "https://cdn.example/best.png"},
        {"url": "https://cdn.example/worse.png"},
    ]}))
    session.route("best.png", FakeResponse(content=PNG))

    assert griddb(session).download_artwork(["Sober"]) == PNG


def test_griddb_asks_for_portrait_covers():
    """Landscape grids in a portrait grid look broken."""
    session = FakeSession()
    session.route("autocomplete", FakeResponse(payload={
        "success": True, "data": [{"id": 42, "name": "Sober"}]}))
    session.route("grids/game/42", FakeResponse(payload={"success": True, "data": []}))

    griddb(session).download_artwork(["Sober"])

    assert any("600x900" in url for url in session.requested)


def test_griddb_reports_an_unknown_game_as_no_art():
    session = FakeSession()
    session.route("autocomplete", FakeResponse(status=404))

    assert griddb(session).download_artwork(["Nothing At All"]) is None


def test_griddb_rejects_a_bad_key_loudly():
    """A wrong key must not look like 'this game has no art'."""
    from rose_gamelab.metadata.base import ProviderError

    session = FakeSession()
    session.route("autocomplete", FakeResponse(status=401))

    with pytest.raises(ProviderError):
        griddb(session).find_game(["Sober"])


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
        griddb=SteamGridDBProvider(api_key=None, session=FakeSession(), rate_limit=0),
        wikidata=WikidataProvider(session=FakeSession(), rate_limit=0),
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
                      griddb=SteamGridDBProvider(api_key=None, session=FakeSession(), rate_limit=0),
                      wikidata=WikidataProvider(session=FakeSession(), rate_limit=0),
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
                      griddb=SteamGridDBProvider(api_key=None, session=FakeSession(), rate_limit=0),
                      wikidata=WikidataProvider(session=FakeSession(), rate_limit=0),
                      openvgdb=OpenVGDBProvider(tmp_path / "absent.sqlite"))

    library.add_game(title="A", system="pc", steam_appid=1)
    library.add_game(title="B", system="pc", steam_appid=2)

    state = scraper.scrape_library()

    assert state.processed == 2
    assert len(state.errors) == 2


# ── The provider chain ────────────────────────────────────────────
#
# The scraper used to pick ONE art source per game — Steam if it had an appid,
# otherwise the archive — and give up if that source missed. So a Steam game
# Steam had no portrait art for ended up blank even when SteamGridDB had one.

def chain_scraper(library, cache, tmp_path, *, steam=None, libretro=None, griddb=None):
    from rose_gamelab.metadata.openvgdb import OpenVGDBProvider
    return Scraper(
        library, cache=cache,
        steam=steam or SteamStoreProvider(session=FakeSession(), rate_limit=0),
        libretro=libretro or LibretroArtProvider(session=FakeSession(), rate_limit=0),
        griddb=griddb or SteamGridDBProvider(api_key=None, session=FakeSession(), rate_limit=0),
        wikidata=WikidataProvider(session=FakeSession(), rate_limit=0),
        openvgdb=OpenVGDBProvider(tmp_path / "absent.sqlite"),
    )


def test_art_falls_through_to_the_next_source(library, cache, tmp_path):
    """Steam having no art must not stop the archive from being asked."""
    libretro_session = FakeSession()
    libretro_session.route("Named_Boxarts", FakeResponse(content=PNG))

    scraper = chain_scraper(
        library, cache, tmp_path,
        libretro=LibretroArtProvider(session=libretro_session, rate_limit=0),
    )

    # A PS3 game: Steam knows nothing about it, the archive does.
    game_id = library.add_game(title="Demon's Souls (USA)", system="ps3")
    scraper.scrape_game(game_id)

    assert library.get(game_id).cover_path


def test_a_broken_provider_does_not_block_the_rest(library, cache, tmp_path):
    class ExplodingLibretro(LibretroArtProvider):
        def download_artwork(self, *a, **kw):
            raise RuntimeError("boom")

    griddb_session = FakeSession()
    griddb_session.route("autocomplete", FakeResponse(payload={
        "success": True, "data": [{"id": 7, "name": "Demon's Souls (USA)"}]}))
    griddb_session.route("grids/game/7", FakeResponse(payload={
        "success": True, "data": [{"url": "https://cdn.example/cover.png"}]}))
    griddb_session.route("cdn.example", FakeResponse(content=PNG))

    scraper = chain_scraper(
        library, cache, tmp_path,
        libretro=ExplodingLibretro(session=FakeSession(), rate_limit=0),
        griddb=SteamGridDBProvider(api_key="k", session=griddb_session, rate_limit=0),
    )

    game_id = library.add_game(title="Demon's Souls (USA)", system="ps3")
    scraper.scrape_game(game_id)

    assert library.get(game_id).cover_path


def test_a_folder_game_is_looked_up_by_its_folder_not_its_eboot(
    library, cache, tmp_path
):
    """Every PS3 game's file is called EBOOT.BIN; the folder carries the name."""
    game = tmp_path / "Demon's Souls (USA)"
    (game / "PS3_GAME" / "USRDIR").mkdir(parents=True)
    (game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").write_bytes(b"\0" * 16)

    game_id = library.add_game(title="Demon's Souls (USA)", system="ps3")
    library.add_file(game_id, game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")

    scraper = chain_scraper(library, cache, tmp_path)
    stem, _extra = scraper._lookup_names(library.get(game_id))

    assert stem == "Demon's Souls (USA)"


def test_a_folder_games_own_title_is_offered(library, cache, tmp_path):
    """A folder named by title id is useless; PARAM.SFO holds the real name."""
    game = tmp_path / "BLUS30443"
    (game / "PS3_GAME" / "USRDIR").mkdir(parents=True)
    (game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").write_bytes(b"\0" * 16)
    (game / "PS3_GAME" / "PARAM.SFO").write_bytes(build_sfo({"TITLE": "Demon's Souls"}))

    game_id = library.add_game(title="BLUS30443", system="ps3")
    library.add_file(game_id, game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")

    scraper = chain_scraper(library, cache, tmp_path)
    stem, extra = scraper._lookup_names(library.get(game_id))

    assert stem == "BLUS30443"
    assert extra == ["Demon's Souls"]


def test_a_pc_game_without_an_appid_is_found_by_name(library, cache, tmp_path):
    """Heroic, GOG and hand-added games get Steam art too."""
    session = FakeSession()
    session.route("storesearch", FakeResponse(payload=search_payload([(2483190, "Forza Horizon 6")])))
    session.route("IStoreBrowseService", FakeResponse(payload=browse_payload(2483190, HASHED_ASSETS)))
    session.route("library_capsule_2x", FakeResponse(content=JPEG))

    scraper = chain_scraper(
        library, cache, tmp_path,
        steam=SteamStoreProvider(session=session, rate_limit=0),
    )

    game_id = library.add_game(title="Forza Horizon 6", system="pc")
    scraper.scrape_game(game_id)

    assert library.get(game_id).cover_path


def test_a_searched_appid_is_never_written_to_the_library(library, cache, tmp_path):
    """It drives duplicate-merging and the 'not on Steam' filters; a guess
    is good enough to choose a picture and not to merge two entries."""
    session = FakeSession()
    session.route("storesearch", FakeResponse(payload=search_payload([(2483190, "Forza Horizon 6")])))

    scraper = chain_scraper(
        library, cache, tmp_path,
        steam=SteamStoreProvider(session=session, rate_limit=0),
    )

    game_id = library.add_game(title="Forza Horizon 6", system="pc")
    scraper.scrape_game(game_id)

    assert library.get(game_id).steam_appid is None


def test_only_missing_skips_complete_games(library, scraper):
    complete = library.add_game(title="Done", system="pc", steam_appid=1145360)
    library.update_game(
        complete, summary="x", release_date="2020-01-01", cover_path="/tmp/x.jpg"
    )

    state = scraper.scrape_library(only_missing=True)
    assert state.total == 0


# ── Matching by name alone ────────────────────────────────────────
#
# Guessing filenames only works when a dump is named the way the archive's dat
# files are. Real folders are called `[PS3] Demon's Souls [BLES00932]`, and no
# amount of variant-generation turns that into `Demon's Souls (USA).png`.

LISTING = (
    '<a href="Demon%27s%20Souls%20%28USA%29.png">x</a>'
    '<a href="Castlevania%20-%20Lords%20of%20Shadow%20%28USA%29.png">y</a>'
)


def indexed_session():
    session = FakeSession()
    session.route("Named_Boxarts/", FakeResponse(status=200, content=b"", payload=None))
    return session


class ListingResponse(FakeResponse):
    """A directory index, which the provider reads as text rather than bytes."""

    def __init__(self, text: str):
        super().__init__(status=200, content=text.encode())
        self.text = text


@pytest.mark.parametrize("folder", [
    "Demon's Souls",
    "[PS3] Demon's Souls [BLES00932]",
    "Demons Souls",
    "demon's souls (usa)",
    "DEMONS SOULS",
])
def test_a_messy_folder_name_still_finds_the_cover(folder):
    """The names real dumps actually have on disk."""
    session = FakeSession()
    session.route("Named_Boxarts/", ListingResponse(LISTING))

    provider = LibretroArtProvider(session=session, rate_limit=0)

    assert provider.find_by_name("ps3", [folder]) is not None


def test_a_different_game_is_not_matched():
    session = FakeSession()
    session.route("Named_Boxarts/", ListingResponse(LISTING))

    provider = LibretroArtProvider(session=session, rate_limit=0)

    assert provider.find_by_name("ps3", ["Dark Souls"]) is None


def test_the_listing_is_fetched_once_per_system():
    session = FakeSession()
    session.route("Named_Boxarts/", ListingResponse(LISTING))

    provider = LibretroArtProvider(session=session, rate_limit=0)
    provider.find_by_name("ps3", ["a"])
    provider.find_by_name("ps3", ["b"])

    assert sum(u.endswith("Named_Boxarts/") for u in session.requested) == 1


def test_index_key_ignores_what_does_not_identify_a_game():
    from rose_gamelab.metadata.libretro_art import index_key

    assert index_key("Demon's Souls (USA)") == index_key("[PS3] Demons Souls")
    assert index_key("Legend of Zelda, The") == index_key("The Legend of Zelda")
    assert index_key("Dark Souls") != index_key("Dark Souls II")


# ── Editions, for artwork only ────────────────────────────────────

def test_a_remaster_supplies_art_for_the_original():
    """Ni no Kuni is on Steam only as '… Remastered'; the cover is the same."""
    session = FakeSession()
    session.route("storesearch", FakeResponse(payload=search_payload([
        (798460, "Ni no Kuni Wrath of the White Witch Remastered"),
    ])))
    provider = SteamStoreProvider(session=session, rate_limit=0)

    assert provider.search("Ni no Kuni: Wrath of the White Witch") is None
    assert provider.search(
        "Ni no Kuni: Wrath of the White Witch", allow_editions=True
    ) == 798460


def test_a_sequel_is_never_treated_as_an_edition():
    """A bare prefix match would put Dark Souls' cover on Dark Souls II."""
    session = FakeSession()
    session.route("storesearch", FakeResponse(payload=search_payload([
        (2, "Dark Souls II"), (3, "Dark Souls III"),
    ])))
    provider = SteamStoreProvider(session=session, rate_limit=0)

    assert provider.search("Dark Souls", allow_editions=True) is None


def test_metadata_never_accepts_an_edition():
    """A remaster's cover is right; its release date is not."""
    session = FakeSession()
    session.route("storesearch", FakeResponse(payload=search_payload([
        (1, "Some Game Definitive Edition"),
    ])))

    assert SteamStoreProvider(session=session, rate_limit=0).search("Some Game") is None


# ── Artwork from the dump itself ──────────────────────────────────

def test_a_dump_supplies_its_own_cover_when_nothing_else_can(
    library, cache, tmp_path
):
    """The archives hold about sixty PS3 covers. Dumps hold their own."""
    game_dir = tmp_path / "Uncharted 3"
    (game_dir / "PS3_GAME" / "USRDIR").mkdir(parents=True)
    (game_dir / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").write_bytes(b"\0" * 16)
    (game_dir / "PS3_GAME" / "ICON0.PNG").write_bytes(PNG)

    game_id = library.add_game(title="Uncharted 3", system="ps3")
    library.add_file(game_id, game_dir / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")

    # Every network source misses; the dump still has art.
    scraper = chain_scraper(library, cache, tmp_path)
    scraper.scrape_game(game_id)

    assert library.get(game_id).cover_path


def test_a_real_cover_still_wins_over_the_dumps_icon(library, cache, tmp_path):
    """ICON0 is a dashboard icon; a box art is the better picture."""
    game_dir = tmp_path / "Demon's Souls"
    (game_dir / "PS3_GAME" / "USRDIR").mkdir(parents=True)
    (game_dir / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").write_bytes(b"\0" * 16)
    (game_dir / "PS3_GAME" / "ICON0.PNG").write_bytes(PNG)

    game_id = library.add_game(title="Demon's Souls", system="ps3")
    library.add_file(game_id, game_dir / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")

    libretro_session = FakeSession()
    libretro_session.route("Named_Boxarts", FakeResponse(content=JPEG))
    scraper = chain_scraper(
        library, cache, tmp_path,
        libretro=LibretroArtProvider(session=libretro_session, rate_limit=0),
    )

    sources = [name for name, _ in scraper._cover_sources(library.get(game_id))]

    assert sources.index("libretro") < sources.index("the dump itself")
