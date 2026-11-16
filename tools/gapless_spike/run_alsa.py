"""Measure the join between two real tracks on a plain-ALSA box (the Pi).

    python run_alsa.py URL1 URL2 --pcm hw:CARD=Modi,DEV=0
                       [--header 'Name: value'] [--seconds 10] [--gain 1.0]
                       [--runs 3] [--work DIR]

Plays the last SECONDS of track 1 and the first SECONDS of track 2 through
play_gapless.py, on the real DAC. There is no sound server to record from
here, so the DAC is wrapped in an ALSA ``file`` device that plays to it AND
writes every sample handed to it into a file. That file is compared, bit for
bit, with the two excerpts cut straight out of the FLAC files.

The file proves what was *sent* to the DAC. Whether the DAC ever ran dry is
the other half: play_gapless.py counts the device's status flags, and a run
only passes with none.

Nothing outside --work is touched: the extra ALSA device lives in a config
file passed through ALSA_CONFIG_PATH, not in ~/.asoundrc.
"""
import argparse
import os
import re
import subprocess
import sys
import time

import numpy as np
import requests
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
TEE = "gaplesstee"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs=2)
    ap.add_argument("--pcm", required=True,
                    help="the DAC as an ALSA pcm, e.g. hw:CARD=Modi,DEV=0")
    ap.add_argument("--header", action="append", default=[])
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--gain", type=float, default=1.0,
                    help="digital volume; 1.0 is bit-perfect")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--work", default=os.path.join(HERE, "work"))
    ap.add_argument("--latency", default="high",
                    help="passed to play_gapless.py: 'high' or seconds of "
                         "device buffer")
    args = ap.parse_args()
    headers = dict(h.split(": ", 1) for h in args.header)
    os.makedirs(args.work, exist_ok=True)
    sent = os.path.join(args.work, "sent-to-dac.raw")
    conf = os.path.join(args.work, "asound.conf")
    with open(conf, "w") as f:
        f.write('</usr/share/alsa/alsa.conf>\n'
                'pcm.%s {\n    type file\n    slave.pcm "%s"\n'
                '    file "%s"\n    format "raw"\n'
                '    hint.description "DAC plus a copy to a file"\n}\n'
                % (TEE, args.pcm, sent))

    # What should reach the DAC: the two excerpts, straight from the files.
    parts = []
    for i, url in enumerate(args.urls):
        path = os.path.join(args.work, "src%d.flac" % i)
        r = requests.get(url, headers=headers, timeout=120)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        with sf.SoundFile(path) as f:
            if f.subtype != "PCM_16":
                raise SystemExit("this check is written for 16-bit FLAC; "
                                 "track %d is %s" % (i + 1, f.subtype))
            rate, n = f.samplerate, int(args.seconds * f.samplerate)
            if i == 0:
                f.seek(max(0, f.frames - n))
            parts.append(f.read(n, dtype="int16", always_2d=True))
            print("track %d: %s %s, %d Hz, %d ch, %.1fs" % (
                i + 1, f.format, f.subtype, rate, f.channels,
                f.frames / rate))
    join = len(parts[0])
    want = np.concatenate(parts)
    if args.gain != 1.0:                    # as play_gapless.apply_gain does
        want = np.round(want.astype(np.float64) * args.gain).astype(np.int16)

    env = dict(os.environ, ALSA_CONFIG_PATH=conf, TMPDIR=args.work)
    cmd = [sys.executable, os.path.join(HERE, "play_gapless.py"), *args.urls,
           "--device", TEE, "--dtype", "int16",
           "--join-only", str(args.seconds), "--gain", str(args.gain),
           "--latency", args.latency]
    for h in args.header:
        cmd += ["--header", h]

    good = 0
    for run in range(args.runs):
        if os.path.exists(sent):
            os.remove(sent)
        t0 = time.monotonic()
        for _attempt in range(15):
            out = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if "Device unavailable" not in out.stderr:
                break
            # Something else has the DAC, e.g. a service that speaks an
            # announcement when the previous stream stops. Wait it out.
            print("DAC busy, waiting...")
            time.sleep(2.0)
        took = time.monotonic() - t0
        print("run %d:" % (run + 1))
        for line in out.stdout.splitlines():
            print("   ", line)
        if out.returncode:
            print(out.stderr[-2000:])
            raise SystemExit("the player failed")
        flags = int(re.search(r"(\d+) device status flags", out.stdout).group(1))
        under = int(re.search(r"(\d+) underrun frames", out.stdout).group(1))

        got = np.fromfile(sent, dtype="<i2").reshape(-1, want.shape[1])
        # PortAudio starts a stream with a couple of buffers of silence
        # (8192 frames when measured), so line the two up on their first
        # non-silent frame before comparing.
        heard = np.nonzero(got.any(axis=1))[0]
        if not len(heard):
            raise SystemExit("nothing but silence was sent to the DAC")
        lead = int(heard[0] - np.nonzero(want.any(axis=1))[0][0])
        body = got[max(lead, 0):max(lead, 0) + len(want)]
        same = lead >= 0 and len(body) == len(want) and \
            np.array_equal(body, want)
        tail_silent = not got[max(lead, 0) + len(want):].any()
        print("  %d frames sent to the DAC in %.1fs: %d of start-up silence, "
              "%d of music, join at music frame %d"
              % (len(got), took, lead, len(want), join))
        if same:
            print("  every sample equals the FLAC data, including across the "
                  "join; trailing silence clean: %s" % tail_silent)
        else:
            n = min(len(body), len(want))
            bad = np.nonzero((body[:n] != want[:n]).any(axis=1))[0]
            print("  MISMATCH: %d frames differ, first at frame %d (%.3fs); "
                  "length %d vs %d" % (len(bad), bad[0] if len(bad) else n,
                                       (bad[0] if len(bad) else n) / rate,
                                       len(body), len(want)))
        ok = same and tail_silent and not flags and not under
        print("  device status flags: %d, underrun frames: %d" % (flags, under))
        print("  RESULT:", "GAPLESS and bit-perfect" if ok else "FAILED")
        good += ok
    print("%d of %d runs passed" % (good, args.runs))
    sys.exit(0 if good == args.runs else 1)


if __name__ == "__main__":
    main()
