"""Decode nowPlayingInfoData NSKeyedArchive bplist for media metadata.

Some apps (e.g. Peacock, HBO Max) put media metadata in the
nowPlayingInfoData NSKeyedArchive bplist instead of structured protobuf
fields. This module decodes the bplist and fills in missing fields.
"""

import plistlib
import logging

_LOGGER = logging.getLogger(__name__)

_KEY_MAP = {
    "kMRMediaRemoteNowPlayingInfoTitle": "title",
    "kMRMediaRemoteNowPlayingInfoSeriesName": "seriesName",
    "kMRMediaRemoteNowPlayingInfoSeasonNumber": "seasonNumber",
    "kMRMediaRemoteNowPlayingInfoEpisodeNumber": "episodeNumber",
    "kMRMediaRemoteNowPlayingInfoPlaybackRate": "playbackRate",
    "kMRMediaRemoteNowPlayingInfoDuration": "duration",
    "kMRMediaRemoteNowPlayingInfoElapsedPlaybackTime": "elapsedTime",
    "kMRMediaRemoteNowPlayingInfoGenre": "genre",
    "kMRMediaRemoteNowPlayingInfoMediaType": "mediaType",
    "kMRMediaRemoteNowPlayingInfoContentIdentifier": "contentIdentifier",
}


def _decode_nskeyedarchiver(data):
    """Decode an NSKeyedArchiver bplist into a Python dict."""
    try:
        plist = plistlib.loads(data)
    except Exception:
        return None

    if not isinstance(plist, dict) or plist.get("$archiver") != "NSKeyedArchiver":
        return None

    objects = plist.get("$objects", [])

    def resolve(obj, depth=0):
        if depth > 50:
            return None
        if isinstance(obj, plistlib.UID):
            idx = obj.data
            if 0 < idx < len(objects):
                return resolve(objects[idx], depth + 1)
            return None
        if isinstance(obj, dict):
            if "NS.keys" in obj and "NS.objects" in obj:
                keys = [resolve(k, depth + 1) for k in obj["NS.keys"]]
                values = [resolve(v, depth + 1) for v in obj["NS.objects"]]
                return dict(zip(keys, values))
            if "NS.string" in obj:
                return obj["NS.string"]
            if "NS.number" in obj:
                return obj["NS.number"]
            if "NS.date" in obj:
                return obj["NS.date"]
            if "NS.data" in obj:
                return obj["NS.data"]
            return {k: resolve(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [resolve(i, depth + 1) for i in obj]
        return obj

    root_ref = plist.get("$top", {}).get("root")
    if root_ref is not None:
        return resolve(root_ref)
    return None


def enrich_metadata(metadata):
    """Fill in missing protobuf fields from nowPlayingInfoData bplist."""
    if metadata is None or not metadata.HasField("nowPlayingInfoData"):
        return
    decoded = _decode_nskeyedarchiver(metadata.nowPlayingInfoData)
    if not isinstance(decoded, dict):
        return
    for npid_key, field_name in _KEY_MAP.items():
        if npid_key in decoded and decoded[npid_key] is not None:
            if not metadata.HasField(field_name):
                try:
                    setattr(metadata, field_name, decoded[npid_key])
                except (TypeError, ValueError):
                    pass
