"""Metadata for platforms no other source covers.

Steam knows Steam. OpenVGDB identifies ROMs by hash, which a PS3 title — a
folder, not a hashable file — can never have. This is what fills the detail
panel for everything in between, and the thing it must never do is answer with
the wrong console's release of the same game.

No test here touches the network.
"""

from __future__ import annotations

import pytest
from test_metadata import FakeResponse, FakeSession

from rose_gamelab.core.emulator import SYSTEMS
from rose_gamelab.metadata.base import ProviderError
from rose_gamelab.metadata.wikidata import (
    PLATFORMS,
    WikidataProvider,
    search_titles,
)


def provider(session):
    return WikidataProvider(session=session, rate_limit=0)


def binding(**fields) -> dict:
    return {k: {"value": v} for k, v in fields.items()}


def results(*rows) -> FakeResponse:
    return FakeResponse(payload={"results": {"bindings": list(rows)}})


DEMONS_SOULS = binding(
    item="http://www.wikidata.org/entity/Q1153708",
    date="2009-02-05T00:00:00Z",
    devLabel="FromSoftware",
    pubLabel="Sony Computer Entertainment",
    genreLabel="action role-playing game",
    article="https://en.wikipedia.org/wiki/Demon%27s_Souls",
)


# ── Platform coverage ─────────────────────────────────────────────

def test_every_system_has_a_metadata_path():
    """The requirement: no console left with artwork and a blank panel.

    ScummVM is the one deliberate exception — it is an engine, not a platform,
    and its games are filed under whatever they originally shipped on.
    """
    missing = sorted(set(SYSTEMS) - set(PLATFORMS) - {"scummvm"})
    assert missing == []


@pytest.mark.parametrize("system", ["ps3", "ps4", "psvita", "xbox360", "wiiu", "switch"])
def test_the_hd_era_is_covered(system):
    """Exactly the systems OpenVGDB cannot reach, because they have no hash."""
    assert provider(FakeSession()).supports_system(system)


def test_platform_ids_are_wikidata_item_ids():
    import re
    assert all(re.fullmatch(r"Q\d+", q) for q in PLATFORMS.values())


def test_an_unsupported_system_is_not_guessed_at():
    session = FakeSession()
    assert provider(session).fetch(["Broken Sword"], "scummvm") is None
    assert session.requested == [], "nothing should be asked"


# ── Matching ──────────────────────────────────────────────────────

def test_the_query_is_constrained_to_the_platform():
    """Without this, a PS3 dump gets the 2020 PS5 remake's release date."""
    session = FakeSession()
    session.route("sparql", results())

    provider(session).fetch(["Demon's Souls"], "ps3")

    assert any("Q10683" in url for url in session.requested)
    assert not any("Q63184502" in url for url in session.requested)


def test_dump_tags_are_stripped_before_asking():
    assert "Demon's Souls" in search_titles("Demon's Souls (USA)")
    assert "Dark Souls II" in search_titles("Dark Souls II (Europe) (En,Fr,De)")


def test_the_raw_title_is_still_tried_first():
    """Some games really do have brackets in their name."""
    assert search_titles("Ratchet & Clank (2016)")[0] == "Ratchet & Clank (2016)"


def test_dat_style_articles_are_restored():
    assert "The Legend of Zelda" in search_titles("Legend of Zelda, The")


def test_a_quote_in_a_title_cannot_break_the_query():
    session = FakeSession()
    session.route("sparql", results())

    provider(session).fetch(['A "Weird" Game'], "ps3")

    # The literal is escaped, not interpolated raw. Compared lowercase because
    # the match itself is case-insensitive.
    assert any('\\"weird\\"' in url for url in session.requested)


# ── Parsing ───────────────────────────────────────────────────────

def test_metadata_is_read_from_the_claims():
    session = FakeSession()
    session.route("sparql", results(DEMONS_SOULS))
    session.route("rest_v1/page/summary", FakeResponse(payload={"extract": "A 2009 game."}))

    found = provider(session).fetch(["Demon's Souls (USA)"], "ps3")

    assert found.release_date == "2009-02-05"
    assert found.developer == "FromSoftware"
    assert found.publisher == "Sony Computer Entertainment"
    assert found.genres == ["action role-playing game"]
    assert found.summary == "A 2009 game."
    assert found.source == "wikidata"


def test_repeated_rows_are_folded_into_one_record():
    """SPARQL returns one row per combination of optional values."""
    second = dict(DEMONS_SOULS, genreLabel={"value": "dark fantasy video game"})

    session = FakeSession()
    session.route("sparql", results(DEMONS_SOULS, second))
    session.route("rest_v1/page/summary", FakeResponse(payload={"extract": "x"}))

    found = provider(session).fetch(["Demon's Souls"], "ps3")

    assert found.genres == ["action role-playing game", "dark fantasy video game"]
    assert found.developer == "FromSoftware"


def test_the_earliest_release_date_wins():
    """A game has one date per region; 'released' means the first one."""
    japan = dict(DEMONS_SOULS, date={"value": "2009-02-05T00:00:00Z"})
    europe = dict(DEMONS_SOULS, date={"value": "2010-06-25T00:00:00Z"})

    session = FakeSession()
    session.route("sparql", results(europe, japan))
    session.route("rest_v1/page/summary", FakeResponse(payload={"extract": "x"}))

    assert provider(session).fetch(["Demon's Souls"], "ps3").release_date == "2009-02-05"


def test_only_the_first_item_is_described():
    """Two games can share a name on one platform; blending them helps nobody."""
    other = binding(
        item="http://www.wikidata.org/entity/Q999",
        devLabel="Somebody Else", date="2015-01-01T00:00:00Z",
    )

    session = FakeSession()
    session.route("sparql", results(DEMONS_SOULS, other))
    session.route("rest_v1/page/summary", FakeResponse(payload={"extract": "x"}))

    found = provider(session).fetch(["Demon's Souls"], "ps3")

    assert found.developer == "FromSoftware"
    assert found.release_date == "2009-02-05"


def test_unresolved_labels_are_not_shown_to_the_user():
    """Wikidata's label service returns the bare item id when it cannot resolve."""
    row = binding(item="http://www.wikidata.org/entity/Q1", devLabel="Q2708014")

    session = FakeSession()
    session.route("sparql", results(row))

    assert provider(session).fetch(["Whatever"], "ps3").developer is None


def test_a_game_that_is_not_there_returns_nothing():
    session = FakeSession()
    session.route("sparql", results())
    session.route("api.php", FakeResponse(payload={"query": {"pages": {}}}))

    assert provider(session).fetch(["Made-Up Homebrew"], "snes") is None


def test_a_missing_summary_does_not_lose_the_rest():
    session = FakeSession()
    session.route("sparql", results(DEMONS_SOULS))
    session.route("rest_v1/page/summary", FakeResponse(status=404))

    found = provider(session).fetch(["Demon's Souls"], "ps3")

    assert found.summary is None
    assert found.developer == "FromSoftware"


# ── Items with no English label ───────────────────────────────────

def test_a_game_named_only_by_its_article_is_found():
    """Halo: Combat Evolved's item carries no English label at all."""
    session = FakeSession()
    # Routes match in order, so the item query is claimed before the general
    # one that stands in for the label search finding nothing.
    session.route("api.php", FakeResponse(payload={
        "query": {"pages": {"1": {"pageprops": {"wikibase_item": "Q276217"}}}}}))
    session.route("Q276217", results(binding(
        item="http://www.wikidata.org/entity/Q276217",
        devLabel="Bungie", date="2001-11-15T00:00:00Z")))
    session.route("sparql", results())

    found = provider(session).fetch(["Halo: Combat Evolved"], "xbox")

    assert found is not None and found.developer == "Bungie"


def test_the_article_fallback_still_checks_the_platform():
    session = FakeSession()
    session.route("api.php", FakeResponse(payload={
        "query": {"pages": {"1": {"pageprops": {"wikibase_item": "Q276217"}}}}}))
    session.route("sparql", results())

    provider(session).fetch(["Halo: Combat Evolved"], "xbox")

    item_queries = [u for u in session.requested if "Q276217" in u and "sparql" in u]
    assert item_queries and all("Q132020" in u for u in item_queries)


# ── Failure handling ──────────────────────────────────────────────

def test_an_unreachable_endpoint_is_reported_not_swallowed():
    import requests

    class Broken(FakeSession):
        def get(self, *a, **kw):
            raise requests.ConnectionError("offline")

    with pytest.raises(ProviderError):
        provider(Broken()).fetch(["Demon's Souls"], "ps3")


def test_a_repeated_lookup_is_cached():
    session = FakeSession()
    session.route("sparql", results(DEMONS_SOULS))
    session.route("rest_v1/page/summary", FakeResponse(payload={"extract": "x"}))

    p = provider(session)
    p.fetch(["Demon's Souls"], "ps3")
    p.fetch(["Demon's Souls"], "ps3")

    assert sum("sparql" in u for u in session.requested) == 1
