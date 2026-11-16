"""Measure the join between two real tracks from a server.

    python run_real.py URL1 URL2 [--header 'Name: value'] [--seconds 10]
                       [--gain 0.1] [--runs 3] [--keep DIR]

Plays the last SECONDS of track 1 and the first SECONDS of track 2, twice:

* as ONE continuous file through ``paplay`` -- the reference, with no join in
  it at all;
* through play_gapless.py, which fetches the two URLs itself (track 2 while
  track 1 is already playing) and joins them in one output stream.

Both are recorded from the default sink's monitor and their timing is
compared window by window (see ``compare``). Recording is compared with
recording, not with the files, because the sound server resamples (44.1 kHz
tracks on a 48 kHz sink, say).
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
import requests
import soundfile as sf

from run_spike import HERE, RATE, find_offset


def record(path, play):
    sink = subprocess.run(["pactl", "get-default-sink"], check=True,
                          capture_output=True, text=True).stdout.strip()
    rec = subprocess.Popen(
        ["parec", "-d", sink + ".monitor", "--format=float32le",
         "--rate=%d" % RATE, "--channels=2", "--file-format=wav", path])
    time.sleep(1.0)
    try:
        play()
        time.sleep(3.0)                     # the recorder trails playback
    finally:
        rec.terminate()
        rec.wait()
    return sf.read(path, dtype="float64", always_2d=True)[0]


def compare(ref, got, seconds, window=0.25):
    """Both are stereo recordings; returns True when ``got`` matches ``ref``.

    The two recordings are not sample-identical even when playback is perfect
    (each stream is resampled by the sound server on its own), so this tracks
    *timing*: the reference is cut into short windows and each one is located
    in ``got``. Continuous playback puts every window at the same offset; a
    gap or dropout shows as a step in the offset, at the time it happened.
    """
    level = np.abs(ref).max(axis=1)
    loud = np.nonzero(level > 1e-6)[0]
    if not len(loud):
        raise SystemExit("the reference recording is silent")
    start, end = loud[0], loud[-1] + 1      # the played audio inside ``ref``
    r = ref[start:end].mean(axis=1)
    gm = got.mean(axis=1)
    join = int(seconds * RATE)

    base = find_offset(gm, r[RATE:3 * RATE]) - RATE
    w, reach = int(window * RATE), RATE // 10
    lags, worst = [], 1.0
    for k in range(0, len(r) - w + 1, w):
        lo = base + k - reach
        if lo < 0 or lo + w + 2 * reach > len(gm):
            raise SystemExit("the recording does not cover the whole playback")
        seg, win = gm[lo:lo + w + 2 * reach], r[k:k + w]
        lag = find_offset(seg, win) - reach
        hit = seg[lag + reach:lag + reach + w]
        if len(hit) == w:
            worst = min(worst, float(np.dot(hit, win) / (
                np.linalg.norm(hit) * np.linalg.norm(win) + 1e-30)))
        lags.append(lag)
    # A real gap moves every later window. One window landing elsewhere and
    # the next one back again is the music repeating itself (a beat), which
    # the continuous reference shows against itself too.
    steps, last = [], 0
    for n, lag in enumerate(lags):
        after = lags[n + 1:n + 4]
        if abs(lag - last) > 1 and len(after) == 3 and \
                sum(abs(x - lag) <= 1 for x in after) >= 2:
            steps.append((n * w / RATE, lag - last))
            last = lag
    print("  %.2fs of audio, join at %.2fs, %d windows of %d ms, "
          "worst match %.3f (1.000 is identical)"
          % (len(r) / RATE, join / RATE, len(r) // w, 1000 * window, worst))
    for t, d in steps:
        where = "at the track join" if abs(t - join / RATE) <= 2 * window \
            else "NOT at the track join"
        print("  timing step of %+d samples (%.1f ms) in the window at %.2fs, %s"
              % (d, 1000.0 * d / RATE, t, where))
    print("  RESULT:", "NOT gapless" if steps else
          "GAPLESS, timing identical to continuous playback")
    return not steps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs=2)
    ap.add_argument("--header", action="append", default=[])
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--gain", type=float, default=0.1)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--keep")
    args = ap.parse_args()
    headers = dict(h.split(": ", 1) for h in args.header)
    work = args.keep or tempfile.mkdtemp(prefix="gapless-real-")
    os.makedirs(work, exist_ok=True)

    # The reference: the same excerpt, cut and joined on disk.
    parts = []
    for i, url in enumerate(args.urls):
        path = os.path.join(work, "src%d.flac" % i)
        r = requests.get(url, headers=headers, timeout=60)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        with sf.SoundFile(path) as f:
            rate, n = f.samplerate, int(args.seconds * f.samplerate)
            if i == 0:
                f.seek(max(0, f.frames - n))
            parts.append(f.read(n, dtype="float32", always_2d=True))
            print("track %d: %s %s, %d Hz, %.1fs" % (
                i + 1, f.format, f.subtype, rate, f.frames / rate))
    whole = os.path.join(work, "reference.wav")
    sf.write(whole, np.concatenate(parts) * np.float32(args.gain), rate,
             subtype="FLOAT")

    ref = record(os.path.join(work, "capture-reference.wav"),
                 lambda: subprocess.run(["paplay", whole], check=True))
    print("rig check (the continuous file, played a second time):")
    compare(ref, record(os.path.join(work, "capture-reference-2.wav"),
                        lambda: subprocess.run(["paplay", whole], check=True)),
            args.seconds)
    cmd = [sys.executable, os.path.join(HERE, "play_gapless.py"), *args.urls,
           "--join-only", str(args.seconds), "--gain", str(args.gain)]
    for h in args.header:
        cmd += ["--header", h]
    good = 0
    for n in range(args.runs):
        print("run %d:" % (n + 1))
        got = record(os.path.join(work, "capture-spike-%d.wav" % (n + 1)),
                     lambda: subprocess.run(cmd, check=True))
        good += compare(ref, got, args.seconds)
    print("%d of %d runs gapless" % (good, args.runs))
    sys.exit(0 if good == args.runs else 1)


if __name__ == "__main__":
    main()
