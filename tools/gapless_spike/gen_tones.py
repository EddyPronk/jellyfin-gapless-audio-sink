"""Generate two FLAC "tracks" that are one continuous quiet sweep cut in half.

The cut is mid-waveform, like the join between two tracks of a live album:
any silence, repeat or dropped sample at the join is a discontinuity that the
capture check (run_spike.py) can measure to the sample.

    python gen_tones.py OUTDIR [--rate 48000] [--seconds 6] [--level 0.02]
"""
import argparse
import os

import numpy as np
import soundfile as sf


def sweep(rate, seconds, level, f0=300.0, f1=600.0):
    """A linear sine sweep: unlike a steady tone it lines up in one place only."""
    t = np.arange(int(rate * seconds)) / rate
    phase = 2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2 * seconds))
    x = level * np.sin(phase)
    fade = int(rate * 0.05)                 # no click at the very start and end
    ramp = np.linspace(0.0, 1.0, fade)
    x[:fade] *= ramp
    x[-fade:] *= ramp[::-1]
    return np.column_stack([x, x]).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--level", type=float, default=0.02,
                    help="peak amplitude, 1.0 is full scale (0.02 is -34 dBFS)")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    x = sweep(args.rate, args.seconds, args.level)
    cut = len(x) // 2 + 137                 # deliberately not on a zero crossing
    for name, part in (("track1.flac", x[:cut]), ("track2.flac", x[cut:])):
        sf.write(os.path.join(args.outdir, name), part, args.rate,
                 subtype="PCM_16")
    print("wrote track1.flac (%d frames) and track2.flac (%d frames) to %s"
          % (cut, len(x) - cut, args.outdir))


if __name__ == "__main__":
    main()
