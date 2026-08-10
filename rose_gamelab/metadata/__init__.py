"""Metadata and artwork providers.

Every provider here works without an API key or user account, which is what
lets GameLab keep its promise of no accounts and offline operation. Providers
that need credentials (IGDB, SteamGridDB) are optional enhancements a user can
enable with their own key, never a requirement.

Results are cached on disk, so a library that has been scraped once keeps its
art and metadata with no network at all.
"""

from rose_gamelab.metadata.base import ArtworkResult, GameMetadata, MetadataProvider
from rose_gamelab.metadata.cache import ArtCache

__all__ = ["ArtCache", "ArtworkResult", "GameMetadata", "MetadataProvider"]
