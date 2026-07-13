"""Unit tests for YouTube tvOS playback support."""

import asyncio

from aiohttp import ClientSession, web
import pytest

from pyatv.protocols.companion import youtube

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://youtube.com/watch?v=jNQXAC9IVRw&feature=share",
        "https://youtu.be/jNQXAC9IVRw",
        "https://www.youtube.com/shorts/jNQXAC9IVRw",
        "youtube://watch/jNQXAC9IVRw",
        "youtube://watch?v=jNQXAC9IVRw",
        "youtube://www.youtube.com/watch?v=jNQXAC9IVRw",
    ],
)
async def test_extract_video_id(url):
    assert youtube.video_id_from_url(url) == "jNQXAC9IVRw"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=jNQXAC9IVRw",
        "https://www.youtube.com/playlist?list=123",
        "youtube://",
        "youtube://watch/not-a-video-id",
    ],
)
async def test_ignore_non_video_url(url):
    assert youtube.video_id_from_url(url) is None


async def test_discover_youtube_app_url(aiohttp_server, monkeypatch):
    discovery_calls = []
    server = None

    async def device_description_handler(_request):
        return web.Response(headers={"Application-URL": str(server.make_url("/apps/"))})

    async def app_handler(_request):
        return web.Response(text="running")

    app = web.Application()
    app.router.add_get("/dd.xml", device_description_handler)
    app.router.add_get("/apps/YouTube", app_handler)
    server = await aiohttp_server(app)

    async def discover_dial_location(_loop, address):
        discovery_calls.append(address)
        return str(server.make_url("/dd.xml"))

    monkeypatch.setattr(youtube, "_discover_dial_location", discover_dial_location)
    monkeypatch.setattr(youtube, "_DIAL_APP_URLS", {})

    async with ClientSession() as session:
        app_url = await youtube._discover_youtube_app_url(
            asyncio.get_running_loop(), session, "127.0.0.1"
        )
        cached_app_url = await youtube._discover_youtube_app_url(
            asyncio.get_running_loop(), session, "127.0.0.1"
        )

    assert app_url == str(server.make_url("/apps/YouTube"))
    assert cached_app_url == app_url
    assert discovery_calls == ["127.0.0.1"]


async def test_play_video(aiohttp_server, monkeypatch):
    calls = []

    async def app_handler(_request):
        return web.Response(
            text=(
                '<?xml version="1.0"?>'
                '<service xmlns="urn:dial-multiscreen-org:schemas:dial">'
                "<additionalData><screenId>test-screen</screenId></additionalData>"
                "</service>"
            ),
            content_type="text/xml",
        )

    async def token_handler(request):
        calls.append(("token", dict(await request.post())))
        return web.json_response(
            {"screens": [{"screenId": "test-screen", "loungeToken": "token"}]}
        )

    async def bind_handler(request):
        calls.append(
            (
                "bind",
                dict(request.query),
                dict(await request.post()),
                request.headers.get("X-YouTube-LoungeId-Token"),
            )
        )
        if len([call for call in calls if call[0] == "bind"]) == 1:
            return web.Response(
                text='[[0,["c","test-sid","",8]],[1,["S","test-gsession"]]]'
            )
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/apps/YouTube", app_handler)
    app.router.add_post("/token", token_handler)
    app.router.add_post("/bind", bind_handler)
    server = await aiohttp_server(app)

    async def discover_youtube_app_url(_loop, _session, _address):
        return str(server.make_url("/apps/YouTube"))

    monkeypatch.setattr(youtube, "_discover_youtube_app_url", discover_youtube_app_url)
    monkeypatch.setattr(youtube, "_LOUNGE_TOKEN_URL", str(server.make_url("/token")))
    monkeypatch.setattr(youtube, "_BIND_URL", str(server.make_url("/bind")))

    async with ClientSession() as session:
        await youtube.play_video(
            asyncio.get_running_loop(), session, "127.0.0.1", "jNQXAC9IVRw"
        )

    assert calls[0] == ("token", {"screen_ids": "test-screen"})
    bind = calls[1]
    assert bind[1] == {"RID": "0", "VER": "8", "CVER": "1"}
    assert bind[2]["device"] == "REMOTE_CONTROL"
    assert bind[3] == "token"
    playlist = calls[2]
    assert playlist[1] == {
        "SID": "test-sid",
        "gsessionid": "test-gsession",
        "RID": "1",
        "VER": "8",
        "CVER": "1",
    }
    assert playlist[2] == {
        "req0_listId": "",
        "req0__sc": "setPlaylist",
        "req0_currentTime": "0",
        "req0_currentIndex": "-1",
        "req0_audioOnly": "false",
        "req0_videoId": "jNQXAC9IVRw",
        "count": "1",
    }
    assert playlist[3] == "token"
