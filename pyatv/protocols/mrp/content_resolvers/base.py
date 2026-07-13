"""Base classes for the content resolver framework."""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_LOGGER = logging.getLogger(__name__)


@dataclass
class ContentResult:
    """Result of a content lookup."""

    series_name: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    artwork_url: Optional[str] = None
    show_slug: Optional[str] = None
    extra: Dict = field(default_factory=dict)


class ContentResolver:
    """Base class for app-specific content resolvers.

    Subclasses implement `can_resolve` to declare which app bundle IDs
    they handle, and `resolve` to look up metadata for a given
    contentIdentifier.
    """

    def can_resolve(self, bundle_identifier: str) -> bool:
        """Return True if this resolver handles the given app."""
        raise NotImplementedError

    async def resolve(self, content_identifier: str) -> Optional[ContentResult]:
        """Look up metadata for a contentIdentifier.

        Returns a ContentResult with enriched metadata, or None if the
        identifier could not be resolved. Implementations should cache
        results to avoid repeated network lookups.
        """
        raise NotImplementedError


class ResolverRegistry:
    """Registry of content resolvers, looked up by bundle identifier."""

    def __init__(self) -> None:
        self._resolvers: List[ContentResolver] = []

    def register(self, resolver: ContentResolver) -> None:
        """Register a content resolver."""
        self._resolvers.append(resolver)

    def get_resolver(self, bundle_identifier: str) -> Optional[ContentResolver]:
        """Find a resolver that handles the given bundle identifier."""
        for resolver in self._resolvers:
            if resolver.can_resolve(bundle_identifier):
                return resolver
        return None

    async def resolve(
        self, bundle_identifier: str, content_identifier: str
    ) -> Optional[ContentResult]:
        """Resolve content metadata for the given app and identifier."""
        resolver = self.get_resolver(bundle_identifier)
        if resolver is None:
            return None
        try:
            return await resolver.resolve(content_identifier)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("Content resolver failed: %s", ex)
            return None
