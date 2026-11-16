"""Spike: play FLAC tracks from HTTP URLs through ONE output stream, no mpv.

    python play_gapless.py URL [URL ...] [--header 'Authorization: ...']
                           [--device NAME] [--naive]

The first track is downloaded, playback starts, and each following track is
downloaded while the one before it plays. Decoded PCM from every track goes
into the same output stream, so nothing is closed or reopened at a track
boundary. ``--naive`` plays each track through its own stream instead, as a
control that is expected to leave a gap.
"""
import argparse
import os
import queue
import tempfile
import threading
import time

import numpy as np
import requests
import sounddevice as sd
import soundfile as sf

BLOCK = 4096          # frames decoded per read
QUEUE_BLOCKS = 32     # decoded audio held ahead of the sound card


def fetch(url, headers, cache_dir, index):
    path = os.path.join(cache_dir, "track%d.flac" % index)
    t0 = time.monotonic()
    with requests.get(url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    print("fetched  track %d  %.0f kB in %.2fs"
          % (index, os.path.getsize(path) / 1e3, time.monotonic() - t0))
    return path


def apply_gain(data, gain):
    """Digital volume. Integer samples are rounded, so the result is exact
    and repeatable (run_alsa.py predicts it)."""
    if gain == 1.0:
        return data
    if data.dtype.kind == "f":
        return data * np.float32(gain)
    return np.round(data.astype(np.float64) * gain).astype(data.dtype)


class Prefetcher:
    """Downloads tracks in order, one ahead of the decoder."""

    def __init__(self, urls, headers, cache_dir):
        self._urls, self._headers, self._dir = urls, headers, cache_dir
        self._paths = {}
        self._cond = threading.Condition()
        self._error = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            for i, url in enumerate(self._urls):
                with self._cond:            # stay at most one track ahead
                    self._cond.wait_for(lambda: self._wanted >= i - 1)
                path = fetch(url, self._headers, self._dir, i)
                with self._cond:
                    self._paths[i] = path
                    self._cond.notify_all()
        except Exception as e:              # surface it to the caller
            with self._cond:
                self._error = e
                self._cond.notify_all()

    _wanted = 0

    def get(self, i):
        with self._cond:
            self._wanted = i
            self._cond.notify_all()
            self._cond.wait_for(lambda: i in self._paths or self._error)
            if i not in self._paths:
                raise self._error
            return self._paths[i]


def play_gapless(urls, headers, device, cache_dir, latency="high",
                 blocksize=4096, join_only=None, gain=1.0, dtype="float32"):
    pre = Prefetcher(urls, headers, cache_dir)
    first = sf.SoundFile(pre.get(0))
    rate, channels = first.samplerate, first.channels
    blocks = queue.Queue(maxsize=QUEUE_BLOCKS)

    def decode():
        f = first
        for i in range(len(urls)):
            if i:
                f = sf.SoundFile(pre.get(i))
                if (f.samplerate, f.channels) != (rate, channels):
                    raise SystemExit("track %d changes format; the spike "
                                     "does not handle that" % i)
            print("decoding track %d  %d frames" % (i, f.frames))
            todo = f.frames
            if join_only:                   # just the audio around the joins
                span = int(join_only * rate)
                if i == 0:
                    f.seek(max(0, f.frames - span))
                    todo = min(span, f.frames)
                if i == len(urls) - 1:
                    todo = min(span, todo)
            while todo > 0:
                data = f.read(min(BLOCK, todo), dtype=dtype, always_2d=True)
                if not len(data):
                    break
                todo -= len(data)
                blocks.put(apply_gain(data, gain))
            f.close()
        # Silence after the last track: stopping a stream discards what the
        # sound server has not played yet (38 ms of the tone, when measured).
        blocks.put(np.zeros((rate // 2, channels), dtype=dtype))
        blocks.put(None)

    threading.Thread(target=decode, daemon=True).start()

    state = {"left": None, "underrun": 0, "played": 0, "flags": 0}
    done = threading.Event()

    def callback(out, frames, _time, status):
        if status:                          # the device ran dry or overflowed
            state["flags"] += 1
            print("stream status:", status)
        filled = 0
        while filled < frames:
            buf = state["left"]
            if buf is None:
                try:
                    buf = blocks.get_nowait()
                except queue.Empty:         # decoder fell behind: audible gap
                    state["underrun"] += frames - filled
                    out[filled:] = 0
                    return
                if buf is None:             # end of the last track
                    out[filled:] = 0
                    raise sd.CallbackStop
            n = min(len(buf), frames - filled)
            out[filled:filled + n] = buf[:n]
            filled += n
            state["played"] += n
            state["left"] = buf[n:] if n < len(buf) else None

    while blocks.qsize() < 4 and threading.active_count() > 1:
        time.sleep(0.01)                    # a little audio in hand first
    with sd.OutputStream(blocksize=blocksize, samplerate=rate, channels=channels, dtype=dtype,
                         dither_off=True, device=device, callback=callback, latency=latency,
                         finished_callback=done.set):
        done.wait()
    print("played %d frames in one stream (%d of them trailing silence), "
          "%d underrun frames, %d device status flags"
          % (state["played"], rate // 2, state["underrun"], state["flags"]))


def play_naive(urls, headers, device, cache_dir):
    """Control: one stream per track, next track fetched after the last ends."""
    for i, url in enumerate(urls):
        data, rate = sf.read(fetch(url, headers, cache_dir, i),
                             dtype="float32", always_2d=True)
        sd.play(data, rate, device=device)
        sd.wait()
    print("played %d tracks, one stream each" % len(urls))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--header", action="append", default=[],
                    help="extra HTTP header, 'Name: value' (repeatable)")
    ap.add_argument("--device", default=None, help="sounddevice output device")
    ap.add_argument("--naive", action="store_true")
    ap.add_argument("--blocksize", type=int, default=4096,
                    help="frames per device buffer. 0 lets PortAudio choose, "
                         "which dropped audio in about 1 run in 3 through "
                         "PipeWire's ALSA bridge when measured")
    ap.add_argument("--join-only", type=float, metavar="SECONDS",
                    help="play only the end of the first track and the start "
                         "of the last, this many seconds each")
    ap.add_argument("--gain", type=float, default=1.0,
                    help="digital volume, 1.0 is unchanged (0.1 is -20 dB)")
    ap.add_argument("--dtype", default="float32",
                    choices=["float32", "int16", "int32"],
                    help="sample type decoded and sent to the device; int16 "
                         "keeps 16-bit FLAC bit-perfect on a 16-bit DAC")
    ap.add_argument("--latency", default="high",
                    help="'low', 'high' or seconds; low risks dropouts")
    args = ap.parse_args()
    headers = dict(h.split(": ", 1) for h in args.header)
    try:
        args.latency = float(args.latency)
    except ValueError:
        pass
    with tempfile.TemporaryDirectory(prefix="gapless-spike-") as cache_dir:
        if args.naive:
            play_naive(args.urls, headers, args.device, cache_dir)
        else:
            play_gapless(args.urls, headers, args.device, cache_dir,
                         args.latency, args.blocksize, args.join_only,
                         args.gain, args.dtype)


if __name__ == "__main__":
    main()
