"""Peacock content resolver using public sitemaps.

Peacock's public sitemaps at peacocktv.com list every episode URL with
the format:

    /watch-online/tv/{show-slug}/{series-id}/seasons/{season}/episodes/{episode-slug}/{episode-uuid}

The episode-uuid in the URL is the same contentIdentifier that Peacock
reports via MRP. By downloading and indexing the sitemaps, we can map
a contentIdentifier to its show name, season, and episode number.

The sitemaps are:
    https://www.peacocktv.com/sitemap-content_page_entertainment_us-{0..6}.xml
    https://www.peacocktv.com/sitemap-content_page_movies_us-0.xml
    https://www.peacocktv.com/sitemap-content_page_sports_us-0.xml

Artwork URLs are available from Peacock's image service at:
    https://imageservice.disco.peacocktv.com/uuid/{uuid}/{format}
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, Optional

from .base import ContentResolver, ContentResult

_LOGGER = logging.getLogger(__name__)

_PEACOCK_BUNDLE_IDS = {
    "com.peacocktv.peacock",
    "com.peacocktv.peacocktv",
}

_SITEMAP_INDEX_URL = "https://www.peacocktv.com/sitemap.xml"
_SITEMAP_ENTERTAINMENT_PATTERN = (
    "https://www.peacocktv.com/sitemap-content_page_entertainment_us-{i}.xml"
)
_SITEMAP_MOVIES_URL = "https://www.peacocktv.com/sitemap-content_page_movies_us-0.xml"
_NUM_ENTERTAINMENT_SITEMAPS = 7

# URL pattern: /watch-online/tv/{slug}/{id}/seasons/{season}/episodes/{ep-slug}/{uuid}
_EPISODE_RE = re.compile(
    r"/watch-online/tv/([^/]+)/(\d+)/seasons/(\d+)/episodes/([^/]+)/([0-9a-f-]+)"
)

# URL pattern: /watch-online/movie/{slug}/{id}/{uuid}
_MOVIE_RE = re.compile(
    r"/watch-online/movie/([^/]+)/(\d+)/([0-9a-f-]+)"
)


class _SitemapIndex:
    """In-memory index of contentIdentifier -> ContentResult."""

    def __init__(self) -> None:
        self._cache: Dict[str, ContentResult] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self, client_session) -> None:
        """Download and parse all Peacock sitemaps."""
        async with self._lock:
            if self._loaded:
                return

            urls = [
                _SITEMAP_ENTERTAINMENT_PATTERN.format(i=i)
                for i in range(_NUM_ENTERTAINMENT_SITEMAPS)
            ]
            urls.append(_SITEMAP_MOVIES_URL)

            for url in urls:
                try:
                    async with client_session.get(url) as resp:
                        if resp.status != 200:
                            _LOGGER.debug("Sitemap %s returned %d", url, resp.status)
                            continue
                        data = await resp.read()
                    self._parse_sitemap(data)
                    _LOGGER.debug("Loaded sitemap %s (%d entries)", url, len(self._cache))
                except Exception as ex:  # pylint: disable=broad-except
                    _LOGGER.debug("Failed to load sitemap %s: %s", url, ex)

            self._loaded = True
            _LOGGER.debug("Peacock sitemap index loaded: %d total entries", len(self._cache))

    def _parse_sitemap(self, xml_data: bytes) -> None:
        """Parse a sitemap XML and add entries to the cache."""
        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError:
            return

        # Namespace for sitemaps
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        for url_elem in root.findall("sm:url", ns):
            loc = url_elem.find("sm:loc", ns)
            if loc is None or not loc.text:
                continue

            url = loc.text.strip()

            # Try episode pattern
            m = _EPISODE_RE.search(url)
            if m:
                show_slug = m.group(1)
                season = int(m.group(3))
                episode_slug = m.group(4)
                episode_uuid = m.group(5)

                # Extract episode number from slug (e.g. "episode-30-episode-30" -> 30)
                ep_num = self._extract_episode_number(episode_slug)

                # Build artwork URL from the show's image service
                # We don't have the image UUID from the sitemap, but the
                # show slug can be used for show-level artwork
                self._cache[episode_uuid] = ContentResult(
                    series_name=self._slug_to_title(show_slug),
                    season_number=season,
                    episode_number=ep_num,
                    show_slug=show_slug,
                )
                continue

            # Try movie pattern
            m = _MOVIE_RE.search(url)
            if m:
                movie_slug = m.group(1)
                movie_uuid = m.group(3)
                self._cache[movie_uuid] = ContentResult(
                    series_name=self._slug_to_title(movie_slug),
                    show_slug=movie_slug,
                )

    @staticmethod
    def _extract_episode_number(slug: str) -> Optional[int]:
        """Extract episode number from slug like 'episode-30-episode-30'."""
        # Try to find a number in the slug
        m = re.search(r"episode-(\d+)", slug)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def _slug_to_title(slug: str) -> str:
        """Convert a URL slug to a human-readable title.

        e.g. 'love-island' -> 'Love Island'
        """
        return slug.replace("-", " ").title()

    def lookup(self, content_identifier: str) -> Optional[ContentResult]:
        """Look up a contentIdentifier in the cache."""
        return self._cache.get(content_identifier)


class PeacockResolver(ContentResolver):
    """Content resolver for Peacock using public sitemaps."""

    def __init__(self, client_session=None) -> None:
        self._index = _SitemapIndex()
        self._client_session = client_session

    def set_client_session(self, client_session) -> None:
        """Set the aiohttp client session for sitemap downloads."""
        self._client_session = client_session

    def can_resolve(self, bundle_identifier: str) -> bool:
        """Return True for Peacock app bundle IDs."""
        return bundle_identifier in _PEACOCK_BUNDLE_IDS

    async def resolve(self, content_identifier: str) -> Optional[ContentResult]:
        """Look up metadata for a Peacock contentIdentifier."""
        if not self._index.is_loaded:
            if self._client_session is None:
                _LOGGER.debug("Peacock resolver has no client session")
                return None
            await self._index.load(self._client_session)

        return self._index.lookup(content_identifier)
