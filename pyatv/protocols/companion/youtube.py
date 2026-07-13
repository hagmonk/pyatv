"""Launch YouTube videos on tvOS via the YouTube Lounge protocol."""

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlsplit
from xml.etree import ElementTree

from aiohttp import ClientError, ClientSession

from pyatv import exceptions

_LOGGER = logging.getLogger(__name__)

YOUTUBE_BUNDLE_ID = "com.google.ios.youtube"

_DIAL_SERVICE = "urn:dial-multiscreen-org:service:dial:1"
_DIAL_REQUEST = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"
    f"ST: {_DIAL_SERVICE}\r\n"
    "\r\n"
).encode()
_DIAL_DISCOVERY_ATTEMPTS = 20
_DIAL_DISCOVERY_TIMEOUT = 0.5
_DIAL_APP_URLS: Dict[str, str] = {}

_YOUTUBE_BASE_URL = "https://www.youtube.com/"
_LOUNGE_TOKEN_URL = urljoin(
    _YOUTUBE_BASE_URL, "api/lounge/pairing/get_lounge_token_batch"
)
_BIND_URL = urljoin(_YOUTUBE_BASE_URL, "api/lounge/bc/bind")
_LOUNGE_ID_HEADER = "X-YouTube-LoungeId-Token"

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SID = re.compile(r'\["c","([^"]+)","')
_GSESSION_ID = re.compile(r'\["S","([^"]+)"\]')
_EVENT_ID = re.compile(r"\[(\d+),\[")


def video_id_from_url(value: str) -> Optional[str]:
    """Extract a video ID from a YouTube URL understood by tvOS clients."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    path = parsed.path.strip("/")
    video_id: Optional[str] = None

    if parsed.scheme in ("http", "https"):
        if host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            if path == "watch":
                video_id = parse_qs(parsed.query).get("v", [None])[0]
            elif path.startswith(("shorts/", "embed/", "live/")):
                video_id = path.split("/", 1)[1].split("/", 1)[0]
        elif host in ("youtu.be", "www.youtu.be"):
            video_id = path.split("/", 1)[0]
    elif parsed.scheme == "youtube":
        if host == "watch":
            video_id = path.split("/", 1)[0] if path else None
            video_id = video_id or parse_qs(parsed.query).get("v", [None])[0]
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            if path == "watch":
                video_id = parse_qs(parsed.query).get("v", [None])[0]

    return video_id if video_id and _VIDEO_ID.fullmatch(video_id) else None


async def play_video(
    loop: asyncio.AbstractEventLoop,
    session: ClientSession,
    address: str,
    video_id: str,
) -> None:
    """Play a YouTube video on the active YouTube tvOS app."""
    try:
        app_url = await _discover_youtube_app_url(loop, session, address)
        screen_id = await _get_screen_id(session, app_url)
        lounge_token = await _get_lounge_token(session, screen_id)
        sid, gsession_id, last_event_id = await _bind(session, lounge_token, screen_id)
        await _set_playlist(
            session,
            lounge_token,
            sid,
            gsession_id,
            last_event_id,
            video_id,
        )
    except exceptions.ProtocolError:
        raise
    except Exception as ex:
        raise exceptions.ProtocolError(
            f"Failed to launch YouTube video {video_id}"
        ) from ex


async def _discover_youtube_app_url(
    loop: asyncio.AbstractEventLoop,
    session: ClientSession,
    address: str,
) -> str:
    """Wait for the YouTube app's DIAL receiver and return its app URL."""
    try:
        ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError as ex:
        raise exceptions.ProtocolError(
            "YouTube playback via DIAL currently requires an IPv4 address"
        ) from ex

    cached_app_url = _DIAL_APP_URLS.get(address)
    if cached_app_url:
        try:
            async with session.get(cached_app_url) as response:
                response.raise_for_status()
                await response.read()
            return cached_app_url
        except (ClientError, asyncio.TimeoutError):
            _DIAL_APP_URLS.pop(address, None)

    for _ in range(_DIAL_DISCOVERY_ATTEMPTS):
        location = await _discover_dial_location(loop, address)
        if location:
            parsed = urlsplit(location)
            if parsed.scheme != "http" or parsed.hostname != address:
                raise exceptions.ProtocolError("Invalid DIAL location from Apple TV")

            async with session.get(location) as response:
                response.raise_for_status()
                application_url = response.headers.get("Application-URL")
            if not application_url:
                raise exceptions.ProtocolError("DIAL response has no Application-URL")

            _LOGGER.debug("Discovered YouTube DIAL receiver at %s", application_url)
            app_url = urljoin(application_url.rstrip("/") + "/", "YouTube")
            _DIAL_APP_URLS[address] = app_url
            return app_url

        await asyncio.sleep(0.25)

    raise exceptions.ProtocolError("YouTube DIAL receiver did not become available")


async def _discover_dial_location(
    loop: asyncio.AbstractEventLoop, address: str
) -> Optional[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_sendto(sock, _DIAL_REQUEST, (address, 1900))
        try:
            payload, sender = await asyncio.wait_for(
                loop.sock_recvfrom(sock, 65535), _DIAL_DISCOVERY_TIMEOUT
            )
        except asyncio.TimeoutError:
            return None
    finally:
        sock.close()

    if sender[0] != address:
        return None

    for line in payload.decode("iso-8859-1", "replace").split("\r\n"):
        key, separator, value = line.partition(":")
        if separator and key.lower() == "location":
            return value.strip()
    return None


async def _get_screen_id(session: ClientSession, app_url: str) -> str:
    async with session.get(app_url) as response:
        response.raise_for_status()
        payload = await response.text()

    root = ElementTree.fromstring(payload)
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "screenId" and element.text:
            return element.text
    raise exceptions.ProtocolError("YouTube DIAL response has no screenId")


async def _get_lounge_token(session: ClientSession, screen_id: str) -> str:
    async with session.post(
        _LOUNGE_TOKEN_URL,
        headers={"Origin": _YOUTUBE_BASE_URL},
        data={"screen_ids": screen_id},
    ) as response:
        response.raise_for_status()
        payload = await response.json()

    screens = payload.get("screens", [])
    if not screens or not screens[0].get("loungeToken"):
        raise exceptions.ProtocolError("YouTube did not return a lounge token")
    return screens[0]["loungeToken"]


def _lounge_headers(lounge_token: str) -> Mapping[str, str]:
    return {
        "Origin": _YOUTUBE_BASE_URL,
        _LOUNGE_ID_HEADER: lounge_token,
    }


async def _bind(
    session: ClientSession, lounge_token: str, screen_id: str
) -> Tuple[str, str, int]:
    data = {
        "app": "web",
        "capabilities": "que,dsdtr,atp,vsp",
        "device": "REMOTE_CONTROL",
        "deviceContext": (
            "user_agent=pyatv&window_width_points=&window_height_points="
            "&os_name=python&ms="
        ),
        "id": screen_id,
        "loungeIdToken": lounge_token,
        "magnaKey": "cloudPairedDevice",
        "name": "pyatv",
        "mdx-version": "3",
        "theme": "cl",
        "ui": "false",
    }
    async with session.post(
        _BIND_URL,
        params={
            "RID": "1",
            "VER": "8",
            "CVER": "1",
            "auth_failure_option": "send_error",
        },
        headers=_lounge_headers(lounge_token),
        data=data,
    ) as response:
        response.raise_for_status()
        payload = await response.text()

    sid = _SID.search(payload)
    gsession_id = _GSESSION_ID.search(payload)
    event_ids = [int(event_id) for event_id in _EVENT_ID.findall(payload)]
    if not sid or not gsession_id or not event_ids:
        raise exceptions.ProtocolError("Could not bind to YouTube lounge session")
    return sid.group(1), gsession_id.group(1), max(event_ids)


async def _set_playlist(
    session: ClientSession,
    lounge_token: str,
    sid: str,
    gsession_id: str,
    last_event_id: int,
    video_id: str,
) -> None:
    data = {
        "count": "1",
        "ofs": "1",
        "req0__sc": "setPlaylist",
        "req0_videoId": video_id,
    }
    params = {
        "name": "pyatv",
        "loungeIdToken": lounge_token,
        "SID": sid,
        "AID": str(last_event_id),
        "gsessionid": gsession_id,
        "device": "REMOTE_CONTROL",
        "app": "youtube-desktop",
        "RID": "2",
        "VER": "8",
        "v": "2",
    }
    async with session.post(
        _BIND_URL,
        params=params,
        headers=_lounge_headers(lounge_token),
        data=data,
    ) as response:
        response.raise_for_status()
        await response.read()

    _LOGGER.debug("Started YouTube video %s via Lounge", video_id)
