"""Shared types for metadata and artwork providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


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
