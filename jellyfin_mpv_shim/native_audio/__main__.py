"""Play a list of tracks gaplessly from the command line.

    python -m jellyfin_mpv_shim.native_audio URL_OR_FILE... [options]

For listening tests and soak runs on the sink machine, without the app.
"""
import argparse
import functools
import logging
import os
import tempfile
import threading
import time

from .engine import GaplessPlayer, Track
from .fetcher import HttpSource, LocalSource
from .output import Output


def main():
    ap = argparse.ArgumentParser(prog="python -m jellyfin_mpv_shim.native_audio")
    ap.add_argument("tracks", nargs="+", help="http(s) URLs or local files")
    ap.add_argument("--header", action="append", default=[],
                    help="extra HTTP header, 'Name: value' (repeatable)")
    ap.add_argument("--device", default=None, help="sounddevice output device")
    ap.add_argument("--latency", type=float, default=1.0,
                    help="device buffer in seconds")
    ap.add_argument("--volume", type=float, default=1.0,
                    help="digital volume, 1.0 is bit-perfect")
    ap.add_argument("--start", type=float, default=0.0,
                    help="where to start the first track, in seconds; "
                         "negative counts back from its end")
    ap.add_argument("--stop-after", type=float, metavar="SECONDS",
                    help="stop this long into the last track")
    ap.add_argument("--cache-dir", help="where downloads go while they play "
                    "(default: a temporary directory under ~/.cache)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    headers = dict(h.split(": ", 1) for h in args.header)

    base = args.cache_dir or os.path.expanduser("~/.cache")
    os.makedirs(base, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-audio-", dir=base) as cache:
        tracks = []
        for n, item in enumerate(args.tracks, 1):
            label = "track %d" % n          # never the URL: it may hold a key
            source = HttpSource(item, headers, cache, label=label) \
                if item.startswith(("http://", "https://")) \
                else LocalSource(item)
            tracks.append(Track(source, key=label))
        done = threading.Event()
        t0 = time.monotonic()

        def stamp():
            return "[%6.1fs]" % (time.monotonic() - t0)

        def following(track):
            i = tracks.index(track) + 1
            return tracks[i] if i < len(tracks) else None

        def started(track, previous, gapless):
            print("%s now playing %s: %d Hz, %d ch, %s, %.1fs%s" % (
                stamp(), track.key, track.rate, track.channels, track.dtype,
                track.duration, " (gapless from %s)" % previous.key
                if gapless else ""))
            if args.stop_after is not None and track is tracks[-1]:
                threading.Timer(args.stop_after, done.set).start()

        def failed(track, error):
            print("%s FAILED %s: %s" % (stamp(), track and track.key, error))
            done.set()

        player = GaplessPlayer(
            next_track=following,
            output_factory=functools.partial(
                Output, device=args.device, latency=args.latency),
            on_track_started=started,
            on_finished=lambda track: done.set(),
            on_error=failed)
        player.volume = args.volume
        player.play(tracks[0], args.start)
        try:
            done.wait()
        except KeyboardInterrupt:
            pass
        print("%s done: %d underrun frames, %d device status flags" % (
            stamp(), player.underrun_frames, player.status_flags))
        player.close()


if __name__ == "__main__":
    main()
