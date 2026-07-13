"""Peacock content resolver using public sitemaps and show pages.

Peacock's public sitemaps at peacocktv.com list every episode URL with
the format:

    /watch-online/tv/{show-slug}/{series-id}/seasons/{season}/episodes/{episode-slug}/{episode-uuid}

The episode-uuid in the URL is the same contentIdentifier that Peacock
reports via MRP. By downloading and indexing the sitemaps, we can map
a contentIdentifier to its show name, season, and episode number.

Artwork URLs require an additional fetch of the show page, which
contains JSON data mapping each episode UUID to an image UUID. The
image URL format is:

    https://imageservice.disco.peacocktv.com/uuid/{image-uuid}/{format}?language=eng&proposition=NBCUOTT

The sitemap lookup is fast (in-memory after initial load). Artwork
fetching is lazy — only when a show page hasn't been cached yet.

The sitemaps are:
    https://www.peacocktv.com/sitemap-content_page_entertainment_us-{0..6}.xml
    https://www.peacocktv.com/sitemap-content_page_movies_us-0.xml
    https://www.peacocktv.com/sitemap-content_page_sports_us-0.xml
"""

import asyncio
import json
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

_SITEMAP_ENTERTAINMENT_PATTERN = (
    "https://www.peacocktv.com/sitemap-content_page_entertainment_us-{i}.xml"
)
_SITEMAP_MOVIES_URL = "https://www.peacocktv.com/sitemap-content_page_movies_us-0.xml"
_NUM_ENTERTAINMENT_SITEMAPS = 7

_SHOW_PAGE_URL = "https://www.peacocktv.com/stream-tv/{slug}"

_IMAGE_SERVICE_BASE = "https://imageservice.disco.peacocktv.com/uuid/{image_uuid}/{format}?language=eng&proposition=NBCUOTT"

# URL pattern: /watch-online/tv/{slug}/{id}/seasons/{season}/episodes/{ep-slug}/{uuid}
_EPISODE_RE = re.compile(
    r"/watch-online/tv/([^/]+)/(\d+)/seasons/(\d+)/episodes/([^/]+)/([0-9a-f-]+)"
)

# URL pattern: /watch-online/movie/{slug}/{id}/{uuid}
_MOVIE_RE = re.compile(
    r"/watch-online/movie/([^/]+)/(\d+)/([0-9a-f-]+)"
)

# Pattern to extract image UUID from Peacock page JSON near an episode UUID
# The page has JSON like: {"slug":".../{episode_uuid}","images":{"landscape":"https://imageservice.../uuid/{image_uuid}/..."}}
_IMAGE_UUID_RE = re.compile(
    r'"uuid/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/'
)


class _SitemapIndex:
    """In-memory index of contentIdentifier -> ContentResult.

    Also tracks show_slug -> set of episode UUIDs for artwork fetching.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, ContentResult] = {}
        self._show_episodes: Dict[str, set] = {}  # show_slug -> {episode_uuids}
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

        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        for url_elem in root.findall("sm:url", ns):
            loc = url_elem.find("sm:loc", ns)
            if loc is None or not loc.text:
                continue

            url = loc.text.strip()

            m = _EPISODE_RE.search(url)
            if m:
                show_slug = m.group(1)
                season = int(m.group(3))
                episode_slug = m.group(4)
                episode_uuid = m.group(5)

                ep_num = self._extract_episode_number(episode_slug)

                self._cache[episode_uuid] = ContentResult(
                    series_name=self._slug_to_title(show_slug),
                    season_number=season,
                    episode_number=ep_num,
                    show_slug=show_slug,
                )

                # Track which episodes belong to which show for artwork fetching
                if show_slug not in self._show_episodes:
                    self._show_episodes[show_slug] = set()
                self._show_episodes[show_slug].add(episode_uuid)
                continue

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
        m = re.search(r"episode-(\d+)", slug)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def _slug_to_title(slug: str) -> str:
        """Convert a URL slug to a human-readable title."""
        return slug.replace("-", " ").title()

    def lookup(self, content_identifier: str) -> Optional[ContentResult]:
        """Look up a contentIdentifier in the cache."""
        return self._cache.get(content_identifier)

    def get_show_slug(self, content_identifier: str) -> Optional[str]:
        """Get the show slug for a contentIdentifier."""
        result = self._cache.get(content_identifier)
        if result and result.show_slug:
            return result.show_slug
        return None


class _ArtworkCache:
    """Cache of episode UUID -> artwork URL, populated from show pages."""

    def __init__(self) -> None:
        self._artwork: Dict[str, str] = {}
        self._loaded_shows: set = set()
        self._lock = asyncio.Lock()

    async def load_show(
        self, show_slug: str, client_session, episode_uuids: Optional[set] = None
    ) -> None:
        """Fetch a show page and extract artwork URLs for its episodes."""
        async with self._lock:
            if show_slug in self._loaded_shows:
                return

            url = _SHOW_PAGE_URL.format(slug=show_slug)
            try:
                async with client_session.get(url) as resp:
                    if resp.status != 200:
                        _LOGGER.debug("Show page %s returned %d", url, resp.status)
                        return
                    html = await resp.text()
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("Failed to fetch show page %s: %s", url, ex)
                return

            self._parse_show_page(html, show_slug, episode_uuids)
            self._loaded_shows.add(show_slug)
            _LOGGER.debug(
                "Loaded show page %s: %d artwork entries",
                show_slug,
                len(self._artwork),
            )

    def _parse_show_page(
        self, html: str, show_slug: str, episode_uuids: Optional[set]
    ) -> None:
        """Parse a show page HTML and extract episode UUID -> artwork URL mappings.

        The page contains JSON data in <script> tags that maps each episode
        UUID to image UUIDs. The image URL format is:
        https://imageservice.disco.peacocktv.com/uuid/{image_uuid}/{format}
        """
        # Find all script tags
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)

        for script in scripts:
            if not episode_uuids:
                # If no specific episodes to find, look for any with this show
                continue

            # Check if this script has any of our episode UUIDs
            has_episodes = any(ep in script for ep in episode_uuids)
            if not has_episodes:
                continue

            # For each episode UUID, find the nearest image UUID
            for ep_uuid in episode_uuids:
                if ep_uuid in self._artwork:
                    continue  # Already cached

                idx = script.find(ep_uuid)
                if idx < 0:
                    continue

                # Search within a window around the episode UUID for image URLs
                # The image data is typically within a few thousand chars
                window = script[max(0, idx - 3000):idx + 3000]

                # Find "landscape" image URL which is the main show image
                # Pattern: "landscape":"https://imageservice.disco.peacocktv.com/uuid/{uuid}/LAND_16_9?..."
                landscape_match = re.search(
                    r'"landscape":"(https://imageservice\.disco\.peacocktv\.com/uuid/[0-9a-f-]+/LAND_16_9[^"]*)"',
                    window,
                )
                if landscape_match:
                    self._artwork[ep_uuid] = landscape_match.group(1)
                    continue

                # Fallback: find any image service URL near the episode UUID
                img_matches = _IMAGE_UUID_RE.findall(window)
                if img_matches:
                    # Use the first image UUID found, LAND_16_9 format
                    img_uuid = img_matches[0]
                    self._artwork[ep_uuid] = _IMAGE_SERVICE_BASE.format(
                        image_uuid=img_uuid, format="LAND_16_9"
                    )

    def get_artwork_url(self, episode_uuid: str) -> Optional[str]:
        """Get the artwork URL for an episode UUID."""
        return self._artwork.get(episode_uuid)


class PeacockResolver(ContentResolver):
    """Content resolver for Peacock using public sitemaps and show pages.

    Metadata (show name, season, episode) comes from the sitemap index.
    Artwork URLs come from fetching the show page on first access, then
    cached in memory.
    """

    def __init__(self, client_session=None) -> None:
        self._index = _SitemapIndex()
        self._artwork = _ArtworkCache()
        self._client_session = client_session

    def set_client_session(self, client_session) -> None:
        """Set the aiohttp client session for network requests."""
        self._client_session = client_session

    def can_resolve(self, bundle_identifier: str) -> bool:
        """Return True for Peacock app bundle IDs."""
        return bundle_identifier in _PEACOCK_BUNDLE_IDS

    async def resolve(self, content_identifier: str) -> Optional[ContentResult]:
        """Look up metadata and artwork for a Peacock contentIdentifier."""
        if not self._index.is_loaded:
            if self._client_session is None:
                _LOGGER.debug("Peacock resolver has no client session")
                return None
            await self._index.load(self._client_session)

        result = self._index.lookup(content_identifier)
        if result is None:
            return None

        # Enrich with artwork URL if not already cached
        if result.artwork_url is None and result.show_slug and self._client_session:
            show_slug = result.show_slug
            episode_uuids = self._index._show_episodes.get(show_slug, set())
            await self._artwork.load_show(
                show_slug, self._client_session, episode_uuids
            )
            artwork_url = self._artwork.get_artwork_url(content_identifier)
            if artwork_url:
                result.artwork_url = artwork_url

        return result
