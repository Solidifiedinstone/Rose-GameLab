"""Shared types for metadata and artwork providers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from rose_gamelab import __version__

#: Sent by every provider. Several of the services GameLab reads from are
#: donated infrastructure that blocks unidentified clients outright — Wikidata
#: returns 403 to the default `python-requests` agent — and all of them deserve
#: to know who is calling and where to complain.
#:
#: Note this must be ASSIGNED onto a session's headers, never `setdefault`:
#: requests populates its own User-Agent at construction, so setdefault always
#: loses and the descriptive name never leaves the machine.
#: The version is read rather than written: this said 0.1 for the whole of 0.1
#: and 0.2, so every server we identify ourselves to was told the wrong one.
USER_AGENT = (
    f"Rose-GameLab/{__version__} "
    "(+https://github.com/Solidifiedinstone/Rose-GameLab)"
)

#: Trademark noise that appears in a store's name for a game but never in a
#: launcher's, a filename's, or a user's.
_TRADEMARKS = ("™", "®", "©")


def normalise_for_match(title: str) -> str:
    """Reduce a title to what two names for the same game have in common.

    Case, punctuation, trademark symbols and spacing all vary between a desktop
    entry, a launcher manifest, a folder name and a store listing; none of them
    change which game is meant. What survives is compared for EQUALITY, never
    for similarity — see the callers for why a fuzzy match is not wanted.
    """
    text = (title or "").lower()
    for mark in _TRADEMARKS:
        text = text.replace(mark, "")

    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


@dataclass
class GameMetadata:
    """Metadata about a game, from any provider.

    Every field is optional. A provider fills in what it knows and leaves the
    rest as None, so partial results from several providers can be merged
    without one clobbering another's better data.
    """

    title: Optional[str] = None
    summary: Optional[str] = None
    release_date: Optional[str] = None      # ISO 8601
    developer: Optional[str] = None
    publisher: Optional[str] = None
    genres: list[str] = field(default_factory=list)
    rating: Optional[float] = None          # normalised 0-100
    rating_count: Optional[int] = None
    rating_source: Optional[str] = None
    source: str = ""                        # which provider produced this

    def merge(self, other: "GameMetadata") -> "GameMetadata":
        """Fill this record's empty fields from `other`. Existing values win.

        Providers are consulted best-first, so the first answer for a field is
        the one we trust most.
        """
        merged = GameMetadata(
            title=self.title or other.title,
            summary=self.summary or other.summary,
            release_date=self.release_date or other.release_date,
            developer=self.developer or other.developer,
            publisher=self.publisher or other.publisher,
            genres=self.genres or other.genres,
            rating=self.rating if self.rating is not None else other.rating,
            rating_count=(
                self.rating_count if self.rating_count is not None else other.rating_count
            ),
            rating_source=self.rating_source or other.rating_source,
            source=self.source or other.source,
        )
        return merged

    @property
    def is_empty(self) -> bool:
        return not any([
            self.summary, self.release_date, self.developer,
            self.publisher, self.genres, self.rating,
        ])


@dataclass
class ArtworkResult:
    """A downloaded piece of artwork."""

    kind: str          # cover | hero | logo | screenshot
    path: str          # where it landed in the cache
    source: str        # which provider it came from


class MetadataProvider(ABC):
    """A source of game metadata and/or artwork."""

    #: Human-readable name, shown in the interface.
    name: str = "provider"

    #: Whether this provider needs credentials the user must supply.
    requires_key: bool = False

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider can be used right now."""
        ...


class ProviderError(Exception):
    """A provider could not answer. Never fatal — the caller tries the next one."""
