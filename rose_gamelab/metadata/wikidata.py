"""Metadata for any game on any platform, from Wikidata and Wikipedia.

The other metadata sources each have a hard edge:

  - Steam knows Steam games, and nothing else.
  - OpenVGDB identifies ROMs by content hash, which is exact and wonderful and
    completely unavailable for a PS3 or Wii U title — those are folders, there
    is no single file to hash, and the database does not carry the HD era
    anyway.

That left every disc-era and HD-era console with artwork but no description,
no release date and no developer. This closes it: Wikidata is keyless, covers
every platform GameLab supports, and its structured claims are exactly the
fields the detail panel wants. The prose summary comes from the linked
Wikipedia article, which is a real paragraph rather than a one-line label.

Matching is by title AND platform, never title alone. Searching for "Demon's
Souls" returns both the 2009 PS3 original and the 2020 PS5 remake, and putting
the remake's date on a PS3 dump is the kind of quiet wrongness that is worse
than a blank field. The platform claim disambiguates them exactly.

Both endpoints ask for a descriptive User-Agent and polite request rates, and
this obeys both — they are donated infrastructure.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional
from urllib.parse import unquote

import requests

from rose_gamelab.metadata.base import (
    USER_AGENT,
    GameMetadata,
    MetadataProvider,
    ProviderError,
    normalise_for_match,
)
from rose_gamelab.metadata.steam_store import RateLimiter

logger = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"

REQUEST_TIMEOUT = 30
#: Both endpoints are donated infrastructure. One request a second.
RATE_LIMIT = 1.0

#: GameLab system id -> Wikidata platform item (the P400 "platform" claim).
#:
#: Every id here was resolved from Wikidata itself rather than recalled, and
#: verified against the English Wikipedia article each item links to. A wrong
#: id does not fail loudly — it silently matches nothing, or worse, matches the
#: wrong console's release of the same game.
PLATFORMS: dict[str, str] = {
    # Nintendo
    "nes": "Q172742",
    "fds": "Q135321",
    "snes": "Q183259",
    "n64": "Q184839",
    "gc": "Q182172",
    "wii": "Q8079",
    "wiiu": "Q56942",
    "switch": "Q19610114",
    "gb": "Q186437",
    "gbc": "Q203992",
    "gba": "Q188642",
    "nds": "Q170323",
    "3ds": "Q203597",
    "virtualboy": "Q164651",
    # Sony
    "ps1": "Q10677",
    "ps2": "Q10680",
    "ps3": "Q10683",
    "ps4": "Q5014725",
    "psp": "Q170325",
    "psvita": "Q188808",
    # Microsoft
    "xbox": "Q132020",
    "xbox360": "Q48263",
    "pc": "Q1406",
    "dos": "Q170434",
    # Sega
    "master_system": "Q209868",
    "megadrive": "Q10676",
    "segacd": "Q1047516",
    "sega32x": "Q1063978",
    "saturn": "Q200912",
    "dreamcast": "Q184198",
    "gamegear": "Q751719",
    # Atari
    "atari2600": "Q206261",
    "atari7800": "Q753600",
    "lynx": "Q753657",
    "jaguar": "Q650601",
    # Everyone else
    "pc_engine": "Q1057377",
    # Wikidata files the CD add-on's games under the base system.
    "pc_engine_cd": "Q1057377",
    "neogeo": "Q1054350",
    "ngp": "Q1977455",
    "wonderswan": "Q1065792",
    "3do": "Q229429",
    "msx": "Q853547",
    "c64": "Q99775",
    "amiga": "Q100047",
    "arcade": "Q192851",
    # Deliberately absent: scummvm is an engine, not a platform — its games are
    # filed under whatever they originally shipped on, so a lookup by "ScummVM"
    # would match nothing.
}

# `wd:Q7889` is "video game"; the property path also admits subclasses, so
# expansions and compilations resolve too.
_QUERY = """
SELECT ?item ?date ?devLabel ?pubLabel ?genreLabel ?article WHERE {
  ?item wdt:P31/wdt:P279* wd:Q7889 ;
        wdt:P400 wd:%(platform)s .
  ?item rdfs:label|skos:altLabel ?label .
  FILTER(LANG(?label) = "en" && LCASE(STR(?label)) = %(title)s)
  OPTIONAL { ?item wdt:P577 ?date }
  OPTIONAL { ?item wdt:P178 ?dev }
  OPTIONAL { ?item wdt:P123 ?pub }
  OPTIONAL { ?item wdt:P136 ?genre }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT 50
"""

# The same fields for an item we already know the id of. Used when the label
# search finds nothing because the item has no English label at all — Halo:
# Combat Evolved is one such, named only by its Wikipedia sitelink. The
# platform is still required, so this is a second way in, not a looser one.
_QUERY_BY_ITEM = """
SELECT ?item ?date ?devLabel ?pubLabel ?genreLabel ?article WHERE {
  VALUES ?item { wd:%(item)s }
  ?item wdt:P400 wd:%(platform)s .
  OPTIONAL { ?item wdt:P577 ?date }
  OPTIONAL { ?item wdt:P178 ?dev }
  OPTIONAL { ?item wdt:P123 ?pub }
  OPTIONAL { ?item wdt:P136 ?genre }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT 50
"""

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

# Region, language and dump tags: "(USA)", "[!]", "(En,Fr,De)". Universal in
# ROM and disc-dump naming and never part of the game's name.
_TAGS = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
# Disc and revision suffixes left over once the tags are gone.
_TRAILING = re.compile(r"\s*[-–]\s*(disc|disk|cd)\s*\d+\s*$", re.I)


def search_titles(*names: Optional[str]) -> list[str]:
    """Names worth asking Wikidata about, best first, de-duplicated.

    A library title is whatever the dump was called — `Demon's Souls (USA)`,
    `Dark Souls II (Europe) (En,Fr,De,Es,It)`. Wikidata knows the game's actual
    name, so the tags have to come off before asking.
    """
    out: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in out:
            out.append(value)

    for name in names:
        if not name:
            continue
        add(name)
        stripped = _TRAILING.sub("", _TAGS.sub("", name)).strip()
        add(stripped)
        # Dat naming moves a leading article to the end; Wikidata never does.
        match = re.match(r"^(.*),\s+(The|A|An)$", stripped, re.I)
        if match:
            add(f"{match.group(2)} {match.group(1)}")

    return out


class WikidataProvider(MetadataProvider):
    """Game metadata for every platform. No key required."""

    name = "Wikidata"
    requires_key = False

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        rate_limit: float = RATE_LIMIT,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.limiter = RateLimiter(rate_limit)
        #: (title, system) -> result, so the metadata and any later pass agree
        #: and a rescan of an unchanged library is nearly free.
        self._cache: dict[tuple[str, str], Optional[GameMetadata]] = {}

    def available(self) -> bool:
        return True

    def supports_system(self, system_id: str) -> bool:
        return system_id in PLATFORMS

    # ── Querying ──────────────────────────────────────────────────

    def _sparql(self, platform: str, title: str) -> list[dict]:
        """Run the lookup. Returns raw bindings, or [] when there is no match."""
        # SPARQL string literal: the only characters that can break out are the
        # quote and the backslash, and both are escaped here rather than the
        # value being interpolated raw.
        escaped = title.lower().replace("\\", "\\\\").replace('"', '\\"')
        return self._run(_QUERY % {"platform": platform, "title": f'"{escaped}"'})

    def _run(self, query: str) -> list[dict]:
        """Execute a SPARQL query and return its bindings."""
        self.limiter.wait()
        try:
            response = self.session.get(
                SPARQL_ENDPOINT,
                params={"query": query, "format": "json"},
                headers={"Accept": "application/sparql-results+json"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"could not reach Wikidata: {exc}") from exc

        if response.status_code != 200:
            raise ProviderError(f"Wikidata returned {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"Wikidata returned malformed JSON: {exc}") from exc

        return (payload.get("results") or {}).get("bindings") or []

    def _sparql_item(self, platform: str, item: str) -> list[dict]:
        """The same lookup for an item id we already have."""
        if not re.fullmatch(r"Q\d+", item):
            return []
        return self._run(_QUERY_BY_ITEM % {"item": item, "platform": platform})

    def _item_for_article(self, title: str) -> Optional[str]:
        """The Wikidata item id for an English Wikipedia article title.

        The way in for games whose item carries no English label. Wikipedia
        resolves redirects on the way, so a title the user's dump uses often
        lands on the right article even when Wikidata's own labels do not.
        """
        self.limiter.wait()
        try:
            response = self.session.get(
                WIKIPEDIA_API,
                params={
                    "action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                    "redirects": "1", "format": "json", "titles": title,
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            pages = (response.json().get("query") or {}).get("pages") or {}
        except (requests.RequestException, ValueError) as exc:
            logger.debug("Wikipedia lookup failed for %r: %s", title, exc)
            return None

        for page in pages.values():
            item = (page.get("pageprops") or {}).get("wikibase_item")
            if isinstance(item, str) and re.fullmatch(r"Q\d+", item):
                return item

        return None

    def _summary(self, article_url: str) -> Optional[str]:
        """The opening paragraph of the linked Wikipedia article."""
        slug = article_url.rstrip("/").rsplit("/", 1)[-1]
        if not slug:
            return None

        self.limiter.wait()
        try:
            response = self.session.get(
                WIKIPEDIA_SUMMARY + slug, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            logger.debug("Wikipedia summary failed for %s: %s", slug, exc)
            return None

        if response.status_code != 200:
            return None

        try:
            extract = response.json().get("extract")
        except ValueError:
            return None

        return extract.strip() if isinstance(extract, str) and extract.strip() else None

    # ── Public ────────────────────────────────────────────────────

    def fetch(self, titles: Iterable[str], system_id: str) -> Optional[GameMetadata]:
        """Metadata for the first of `titles` this platform has a game for.

        None when nothing matches, which is normal and is not an error — plenty
        of homebrew, prototypes and regional releases are not in Wikidata.
        """
        platform = PLATFORMS.get(system_id)
        if platform is None:
            return None

        for title in search_titles(*titles):
            key = (normalise_for_match(title), system_id)
            if key in self._cache:
                found = self._cache[key]
                if found is not None:
                    return found
                continue

            try:
                bindings = self._sparql(platform, title)

                # Some items carry no English label and are named only by their
                # Wikipedia article. Asking Wikipedia for the item id gets them
                # — still checked against the platform, so it stays exact.
                if not bindings:
                    item = self._item_for_article(title)
                    if item:
                        bindings = self._sparql_item(platform, item)
            except ProviderError as exc:
                logger.debug("Wikidata lookup failed for %r: %s", title, exc)
                raise

            result = self._parse(bindings) if bindings else None
            self._cache[key] = result
            if result is not None:
                return result

        return None

    def _parse(self, bindings: list[dict]) -> Optional[GameMetadata]:
        """Fold the rows for one game into a single record.

        A SPARQL result is one row per combination of optional values, so a game
        with two genres and two developers arrives as four near-identical rows.
        """
        def value(row: dict, key: str) -> Optional[str]:
            found = row.get(key)
            return found.get("value") if isinstance(found, dict) else None

        # More than one item can share a name on a platform (a game and its
        # remaster). The first is Wikidata's own best answer; sticking to it
        # keeps every field describing the SAME game rather than a blend.
        item = value(bindings[0], "item")
        rows = [r for r in bindings if value(r, "item") == item]

        developers: list[str] = []
        publishers: list[str] = []
        genres: list[str] = []
        dates: list[str] = []
        article: Optional[str] = None

        for row in rows:
            for key, target in (
                ("devLabel", developers),
                ("pubLabel", publishers),
                ("genreLabel", genres),
            ):
                found = value(row, key)
                # An unresolved label comes back as the bare item id, which is
                # not something to show a user.
                if found and found not in target and not re.fullmatch(r"Q\d+", found):
                    target.append(found)

            date = value(row, "date")
            if date:
                dates.append(date)

            article = article or value(row, "article")

        summary = self._summary(unquote(article)) if article else None

        return GameMetadata(
            summary=summary,
            # A game has one release date per region; the earliest is the one
            # every other source means by "released".
            release_date=(min(dates)[:10] if dates else None),
            developer=", ".join(developers) or None,
            publisher=", ".join(publishers) or None,
            genres=genres,
            source="wikidata",
        )
