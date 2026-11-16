"""Run the spike end to end on this machine and measure the join.

    python run_spike.py [--naive] [--keep DIR]

1. generates two quiet test-tone FLAC tracks (gen_tones.py),
2. serves them over HTTP on localhost, standing in for the Jellyfin server,
3. records the default sink's monitor with ``parec`` while
4. play_gapless.py plays them through the default output,
5. compares the recording with the original sample by sample and reports
   whether it matches, and if not, whether the fault is at the track join.

Needs PulseAudio or PipeWire-pulse for ``pactl`` / ``parec``.
"""
import argparse
import functools
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
RATE = 48000


def serve(directory):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=directory)
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def find_offset(capture, ref):
    """Where ``ref`` starts inside ``capture`` (samples), by cross-correlation."""
    n = 1 << (len(capture) + len(ref)).bit_length()
    corr = np.fft.irfft(np.fft.rfft(capture, n) * np.conj(np.fft.rfft(ref, n)), n)
    lag = int(np.argmax(corr))
    return lag - n if lag > len(capture) else lag


def analyse(capture_path, track_paths):
    """Compare the recording with the original, sample by sample.

    Returns ``(verdict, slip)``: verdict is "gapless", "fault at the join" or
    "fault elsewhere"; slip is the samples inserted (+) or lost (-) overall.
    """
    cap, rate = sf.read(capture_path, dtype="float64", always_2d=True)
    cap = cap.mean(axis=1)
    t1, t2 = (sf.read(p, dtype="float64", always_2d=True)[0].mean(axis=1)
              for p in track_paths)
    if rate != RATE:
        raise SystemExit("capture is %d Hz, expected %d" % (rate, RATE))
    peak = np.abs(cap).max()
    if peak < 1e-5:
        raise SystemExit("capture is silent (peak %.2g): is the sink muted, "
                         "or is the monitor the wrong device?" % peak)
    ref = np.concatenate([t1, t2])
    join = len(t1)

    # Anchor on one second early in track 1, then on the last second of
    # track 2: any difference between the two is audio inserted or lost.
    a, b = RATE // 2, RATE // 2 + RATE
    base = find_offset(cap, ref[a:b]) - a
    slip = find_offset(cap, ref[-b:-a]) - (len(ref) - b) - base
    chunk = cap[base + a:base + b]
    gain = float(np.dot(chunk, ref[a:b]) / np.dot(ref[a:b], ref[a:b]))

    seg = cap[max(base, 0):base + len(ref)]
    if base < 0 or len(seg) < len(ref):
        raise SystemExit("the recording does not cover the whole playback")
    diff = np.abs(seg - gain * ref)
    bad = np.nonzero(diff > 0.05 * gain * np.abs(ref).max())[0]
    exact = diff[:join + RATE // 10].max() < 1e-7

    print("capture: %.2fs, playback gain %.3f, track join at %.3fs"
          % (len(cap) / rate, gain, join / rate))
    print("samples inserted (+) or lost (-) over the whole playback: %+d" % slip)
    if not len(bad):
        verdict = "gapless"
        print("recording matches the original throughout%s"
              % (" (bit-exact across the join)" if exact else ""))
    else:
        t = bad[0] / rate
        at_join = abs(bad[0] - join) < RATE // 10
        verdict = "fault at the join" if at_join else "fault elsewhere"
        print("first mismatch at %.3fs (%s), %.1f ms of audio affected up to %.3fs"
              % (t, "the track join" if at_join else "not the track join",
                 1000.0 * len(bad) / rate, bad[-1] / rate))
    print("RESULT:", verdict.upper())
    return verdict, slip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--naive", action="store_true",
                    help="control run: a new stream per track")
    ap.add_argument("--keep", help="keep the tones and capture in this directory")
    ap.add_argument("--rig-check", action="store_true",
                    help="play the two tracks as ONE file with paplay instead "
                         "of the spike player: a fault here is the sound "
                         "server or the recording, not the player")
    ap.add_argument("player_args", nargs="*",
                    help="extra arguments for play_gapless.py (after --)")
    args = ap.parse_args()

    work = args.keep or tempfile.mkdtemp(prefix="gapless-spike-run-")
    os.makedirs(work, exist_ok=True)
    subprocess.run([sys.executable, os.path.join(HERE, "gen_tones.py"), work,
                    "--rate", str(RATE)], check=True)
    srv = serve(work)
    base = "http://127.0.0.1:%d/" % srv.server_address[1]

    sink = subprocess.run(["pactl", "get-default-sink"], check=True,
                          capture_output=True, text=True).stdout.strip()
    capture = os.path.join(work, "capture.wav")
    rec = subprocess.Popen(
        ["parec", "-d", sink + ".monitor", "--format=float32le",
         "--rate=%d" % RATE, "--channels=2", "--file-format=wav", capture])
    time.sleep(1.0)
    try:
        if args.rig_check:
            whole = os.path.join(work, "whole.wav")
            sf.write(whole, np.concatenate(
                [sf.read(os.path.join(work, n), dtype="float32")[0]
                 for n in ("track1.flac", "track2.flac")]), RATE,
                subtype="PCM_16")
            subprocess.run(["paplay", whole], check=True)
        else:
            cmd = [sys.executable, os.path.join(HERE, "play_gapless.py"),
                   base + "track1.flac", base + "track2.flac"]
            subprocess.run(cmd + (["--naive"] if args.naive else [])
                           + args.player_args, check=True)
        time.sleep(3.0)                     # the recorder trails playback
    finally:
        rec.terminate()
        rec.wait()
        srv.shutdown()

    verdict, _slip = analyse(capture, [os.path.join(work, "track1.flac"),
                                       os.path.join(work, "track2.flac")])
    sys.exit(0 if (verdict == "gapless") != args.naive else 1)


if __name__ == "__main__":
    main()
