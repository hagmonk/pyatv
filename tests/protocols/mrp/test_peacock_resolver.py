"""Unit tests for Peacock metadata and artwork resolution."""

from aiohttp import ClientSession, web
import pytest

from pyatv.protocols.mrp.content_resolvers import peacock

pytestmark = pytest.mark.asyncio


async def test_catalog_resolves_new_episode_and_artwork(aiohttp_server, monkeypatch):
    """Resolve a new episode directly and cache its preferred artwork."""
    calls = []

    async def catalog_handler(request):
        calls.append(request)
        return web.json_response(
            {
                "attributes": {
                    "seriesName": "Love Island",
                    "seasonNumber": 8,
                    "episodeNumber": 35,
                    "providerVariantId": "episode-id",
                    "slug": (
                        "/tv/love-island/123/seasons/8/episodes/"
                        "episode-35/episode-id"
                    ),
                    "images": [
                        {"type": "scene169", "url": "https://example.com/scene"},
                        {
                            "type": "landscape",
                            "url": "https://example.com/landscape",
                        },
                    ],
                }
            }
        )

    app = web.Application()
    app.router.add_get("/catalog/{content_identifier}", catalog_handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        peacock,
        "_CATALOG_VARIANT_URL",
        str(server.make_url("/catalog/")) + "{content_identifier}",
    )

    async with ClientSession() as session:
        resolver = peacock.PeacockResolver(session)
        result = await resolver.resolve("episode-id")
        cached_result = await resolver.resolve("episode-id")

    assert result == peacock.ContentResult(
        series_name="Love Island",
        season_number=8,
        episode_number=35,
        artwork_url="https://example.com/landscape",
        show_slug="love-island",
    )
    assert cached_result is result
    assert len(calls) == 1
    assert calls[0].headers["X-SkyOTT-Proposition"] == "NBCUOTT"
    assert calls[0].headers["X-SkyOTT-Territory"] == "US"


async def test_catalog_uses_scene_artwork_fallback(aiohttp_server, monkeypatch):
    """Use a clean scene image when landscape artwork is unavailable."""

    async def catalog_handler(_request):
        return web.json_response(
            {
                "attributes": {
                    "seriesName": "Show",
                    "images": [
                        {"type": "scene169", "url": "https://example.com/scene"}
                    ],
                }
            }
        )

    app = web.Application()
    app.router.add_get("/catalog/{content_identifier}", catalog_handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        peacock,
        "_CATALOG_VARIANT_URL",
        str(server.make_url("/catalog/")) + "{content_identifier}",
    )

    async with ClientSession() as session:
        result = await peacock.PeacockResolver(session).resolve("episode-id")

    assert result.artwork_url == "https://example.com/scene"
