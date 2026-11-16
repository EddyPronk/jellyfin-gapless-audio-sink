"""From a queue item to something the native audio engine can play.

``native_track(video)`` asks the server how to play ``video`` -- the same
negotiation the mpv path uses, ``Video.get_playback_url`` -- and wraps the
answer for ``native_audio``: the bytes' location, and the credential that
goes with it.

It is safe to call for a track that is not playing yet, which is the point:
the next track is resolved, and its download started, while the current one
is still playing. The negotiation only writes to the Video it is called on,
so the one playing is not touched.

The engine plays FLAC that the server hands over unmodified, and nothing
else. Anything else raises ``NotNative`` with the reason.
"""
import logging

from .native_audio.engine import Track
from .native_audio.fetcher import DEFAULT_RAM_LIMIT, HttpSource, LocalSource
from .utils import item_is_audio, same_origin

log = logging.getLogger("sink_source")


class NotNative(Exception):
    """This item cannot be played by the native engine; str() says why."""


def _auth_header(video):
    """This server's Authorization header value, or None without a token."""
    try:
        header = video.client.http._get_authenication_header()
    except Exception:
        log.debug("could not build an auth header", exc_info=True)
        return None
    return header if header and "Token=" in header else None


def native_track(video, ram_limit=DEFAULT_RAM_LIMIT):
    """A ``native_audio`` Track for ``video``, keyed by it.

    One PlaybackInfo request to the server; nothing is downloaded until the
    engine starts the track's source.
    """
    item = getattr(video, "item", None) or {}
    name = item.get("Name") or "track"
    if not item_is_audio(item):
        raise NotNative("%s is not audio" % name)

    # The token travels in a header, so the URL does not carry it into logs.
    # Unlike mpv's, this header is per request: it cannot leak to another
    # host, and the origin check below is all the care it needs.
    header = _auth_header(video)
    video.auth_via_header = header is not None
    url = video.get_playback_url()
    if url is None:
        raise NotNative("%s has nothing to play" % name)
    if video.is_transcode:
        raise NotNative("the server would transcode %s (%s)" % (
            name, ", ".join(video.transcode_reasons) or "no reason given"))
    container = ((video.media_source or {}).get("Container") or "").lower()
    if container != "flac":
        raise NotNative("%s is %s, not FLAC" % (name, container or "unknown"))

    if not url.startswith(("http://", "https://")):
        source = LocalSource(url)           # a direct path on this machine
    else:
        headers = {}
        server = video.client.config.data.get("auth.server")
        if header is not None and same_origin(url, server):
            headers["Authorization"] = header
        # Otherwise the file is on somebody else's host (an http source, a
        # path substitution): it gets the URL as the server gave it and no
        # credential of ours.
        source = HttpSource(url, headers, ram_limit, label=name)
    return Track(source, key=video)
