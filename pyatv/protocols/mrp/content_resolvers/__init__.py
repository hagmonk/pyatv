"""Content resolver framework for enriching MRP metadata.

Some streaming apps (Peacock, HBO Max, etc.) do not populate structured
protobuf fields like seriesName, seasonNumber, or episodeNumber in their
MRP SET_STATE messages. Instead they only provide a contentIdentifier
(their internal episode UUID) and a title.

This package provides a pluggable resolver system that can look up
missing metadata — including series name, season, episode number, and
artwork URLs — from external sources (e.g. public sitemaps, catalog
APIs) keyed by the app's bundle identifier and the contentIdentifier.
"""

from .base import ContentResolver, ContentResult, ResolverRegistry
from .peacock import PeacockResolver

__all__ = [
    "ContentResolver",
    "ContentResult",
    "ResolverRegistry",
    "PeacockResolver",
]
