"""The mpv-less FLAC player: what reaches the sound card is the files, joined.

The claim under test is the one the whole package exists for
(docs/native-flac-plan.md): two tracks decoded one after the other into one
output stream come out as exactly their samples, back to back, with nothing
inserted, dropped or altered at the join. The sound card is replaced by a
fake that records what it is given; everything else is the real code,
including the HTTP download.

Skipped where numpy / soundfile are not installed: they are the optional
``sink`` extra, not dependencies of the app.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import functools
import http.server
import os
import tempfile
import threading
import time
import unittest

try:
    import numpy as np
    import soundfile as sf

    from jellyfin_mpv_shim.native_audio.engine import (
        TAIL_SECONDS, GaplessPlayer, Track, apply_gain)
    from jellyfin_mpv_shim.native_audio.fetcher import (
        FetchError, HttpSource, LocalSource)
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False


class FakeOutput:
    """A sound card that pulls as fast as it can and keeps what it is given.

    Only frames the player says came from its queue are kept, so the silence
    written while the decoder is still warming up does not count.
    """

    opened = []

    def __init__(self, rate, channels, dtype, fill):
        self.format = (rate, channels, dtype)
        self.latency = 0.0
        self.status_flags = 0
        self._fill = fill
        self._channels, self._dtype = channels, dtype
        self._chunks = []
        self._running = False
        self._thread = None
        FakeOutput.opened.append(self)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._pull, daemon=True)
        self._thread.start()

    def _pull(self):
        buf = np.zeros((1024, self._channels), dtype=self._dtype)
        while self._running:
            n = self._fill(buf)
            if n:
                self._chunks.append(buf[:n].copy())
            else:
                time.sleep(0.001)

    def close(self, drain=True):
        self._running = False
        if self._thread is not None:
            self._thread.join(5)

    @property
    def pcm(self):
        if not self._chunks:
            return np.zeros((0, self._channels), dtype=self._dtype)
        return np.concatenate(self._chunks)


class _SlowHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files a little at a time, like a server further away."""

    def log_message(self, *args):
        pass

    def copyfile(self, source, outputfile):
        while True:
            data = source.read(16384)
            if not data:
                break
            outputfile.write(data)
            outputfile.flush()
            time.sleep(0.002)


@unittest.skipUnless(HAVE_DEPS, "needs numpy and soundfile (the sink extra)")
class NativeAudioTest(unittest.TestCase):
    def setUp(self):
        FakeOutput.opened = []
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.rng = np.random.default_rng(1)
        self.started, self.errors = [], []
        self.finished = threading.Event()
        self.failed = threading.Event()
        self.order = []
        self.player = GaplessPlayer(
            next_track=self._following,
            output_factory=FakeOutput,
            on_track_started=lambda t, prev, gapless: self.started.append(
                (t.key, prev.key if prev else None, gapless)),
            on_finished=lambda t: self.finished.set(),
            on_error=self._on_error)
        self.addCleanup(self.player.close)

    def _on_error(self, track, error):
        self.errors.append(error)
        self.failed.set()

    def _following(self, track):
        i = next(n for n, t in enumerate(self.order) if t is track) + 1
        return self.order[i] if i < len(self.order) else None

    def flac(self, name, seconds=1.5, rate=44100, subtype="PCM_16"):
        """A FLAC file of noise, and the samples that are in it."""
        top = 2 ** 15 if subtype == "PCM_16" else 2 ** 23
        pcm = self.rng.integers(-top, top, (int(seconds * rate), 2))
        if subtype == "PCM_16":
            pcm = pcm.astype(np.int16)
        else:                               # 24 bits, left-justified in 32
            pcm = (pcm << 8).astype(np.int32)
        path = os.path.join(self.dir, name)
        sf.write(path, pcm, rate, subtype=subtype)
        return path, pcm

    def serve(self):
        srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            functools.partial(_SlowHandler, directory=self.dir))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d/" % srv.server_address[1]

    def play_all(self, tracks, **kwargs):
        self.order = tracks
        self.player.play(tracks[0], **kwargs)
        self.assertTrue(self.finished.wait(20), "playback never finished")

    @staticmethod
    def tail(pcm, rate=44100):
        return np.zeros((int(rate * TAIL_SECONDS), pcm.shape[1]), pcm.dtype)

    # -- the point of it all -------------------------------------------------

    def test_two_tracks_come_out_joined_sample_for_sample(self):
        (p1, a), (p2, b) = self.flac("1.flac"), self.flac("2.flac", 2.0)
        self.play_all([Track(LocalSource(p1), "one"),
                       Track(LocalSource(p2), "two")])
        self.assertEqual(len(FakeOutput.opened), 1, "one stream for both")
        out = FakeOutput.opened[0]
        self.assertEqual(out.format, (44100, 2, "int16"))
        np.testing.assert_array_equal(
            out.pcm, np.concatenate([a, b, self.tail(a)]))
        self.assertEqual(self.started, [("one", None, False),
                                        ("two", "one", True)])

    def test_over_http_the_next_track_is_fetched_and_joined(self):
        (_, a), (_, b), (_, c) = (self.flac("1.flac"), self.flac("2.flac"),
                                  self.flac("3.flac"))
        base = self.serve()
        cache = os.path.join(self.dir, "cache")
        os.mkdir(cache)
        self.play_all([Track(HttpSource(base + "%d.flac" % n,
                                        cache_dir=cache), n)
                       for n in (1, 2, 3)])
        self.assertEqual(len(FakeOutput.opened), 1)
        np.testing.assert_array_equal(
            FakeOutput.opened[0].pcm,
            np.concatenate([a, b, c, self.tail(a)]))
        self.assertEqual([s[2] for s in self.started], [False, True, True])
        self.assertEqual(os.listdir(cache), [], "downloads are cleaned up")

    def test_24_bit_stays_integer(self):
        (p1, a), (p2, b) = (self.flac("1.flac", subtype="PCM_24"),
                            self.flac("2.flac", subtype="PCM_24"))
        self.play_all([Track(LocalSource(p1)), Track(LocalSource(p2))])
        out = FakeOutput.opened[0]
        self.assertEqual(out.format, (44100, 2, "int32"))
        np.testing.assert_array_equal(
            out.pcm, np.concatenate([a, b, self.tail(a)]))

    # -- the cases around it -------------------------------------------------

    def test_a_format_change_reopens_the_stream_and_loses_nothing(self):
        (p1, a), (p2, b) = self.flac("1.flac"), self.flac("2.flac", rate=48000)
        self.play_all([Track(LocalSource(p1)), Track(LocalSource(p2))])
        first, second = FakeOutput.opened
        self.assertEqual(first.format[0], 44100)
        self.assertEqual(second.format[0], 48000)
        np.testing.assert_array_equal(
            first.pcm, np.concatenate([a, self.tail(a)]))
        np.testing.assert_array_equal(
            second.pcm, np.concatenate([b, self.tail(b, 48000)]))

    def test_start_offsets_count_from_either_end(self):
        path, a = self.flac("1.flac", 2.0)
        self.play_all([Track(LocalSource(path))], start=-0.5)
        np.testing.assert_array_equal(
            FakeOutput.opened[0].pcm,
            np.concatenate([a[-22050:], self.tail(a)]))

    def test_seek_replays_from_the_target(self):
        path, a = self.flac("1.flac", 3.0)
        self.player.pause()                 # nothing is consumed while paused
        self.order = [Track(LocalSource(path))]
        self.player.play(self.order[0])
        self.player.pause()
        self.player.seek(1.0)
        self.player.resume()
        self.assertTrue(self.finished.wait(20))
        np.testing.assert_array_equal(
            FakeOutput.opened[-1].pcm,
            np.concatenate([a[44100:], self.tail(a)]))

    def test_volume_is_exact_and_full_volume_is_untouched(self):
        path, a = self.flac("1.flac")
        self.player.volume = 0.25
        self.play_all([Track(LocalSource(path))])
        np.testing.assert_array_equal(
            FakeOutput.opened[0].pcm,
            np.concatenate([apply_gain(a, 0.25), self.tail(a)]))
        self.assertIs(apply_gain(a, 1.0), a)

    def test_mute_plays_silence_at_the_same_pace(self):
        path, a = self.flac("1.flac")
        self.player.muted = True
        self.play_all([Track(LocalSource(path))])
        pcm = FakeOutput.opened[0].pcm
        self.assertEqual(len(pcm), len(a) + len(self.tail(a)))
        self.assertFalse(pcm.any())

    def test_a_changed_queue_plays_the_new_next_track(self):
        (p1, a), (p2, _b), (p3, c) = (self.flac("1.flac", 3.0),
                                      self.flac("2.flac"), self.flac("3.flac"))
        one, two, three = (Track(LocalSource(p), n)
                           for p, n in ((p1, 1), (p2, 2), (p3, 3)))
        self.order = [one, two]
        self.player.pause()
        self.player.play(one)
        self.player.pause()
        time.sleep(0.3)                     # let it look ahead to track 2
        self.order = [one, three]
        self.player.invalidate_next()
        self.player.resume()
        self.assertTrue(self.finished.wait(20))
        np.testing.assert_array_equal(
            np.concatenate([o.pcm for o in FakeOutput.opened]),
            np.concatenate([a, c, self.tail(a)]))
        self.assertEqual([s[0] for s in self.started], [1, 3])

    def test_a_failed_download_is_reported_not_played(self):
        base = self.serve()
        self.order = [Track(HttpSource(base + "missing.flac",
                                       cache_dir=self.dir), "gone")]
        self.player.play(self.order[0])
        self.assertTrue(self.failed.wait(20), "no error reported")
        self.assertIsInstance(self.errors[0], FetchError)
        self.assertIsNone(self.player.current)
        self.assertFalse(self.finished.is_set())

    def test_reading_waits_for_bytes_that_have_not_arrived(self):
        path, _ = self.flac("1.flac", 3.0)
        base = self.serve()
        src = HttpSource(base + "1.flac", cache_dir=self.dir)
        with src.open() as f:
            size = f.seek(0, os.SEEK_END)
            self.assertEqual(size, os.path.getsize(path))
            f.seek(size - 1000)             # far beyond what has arrived
            data = f.read(5000)
        with open(path, "rb") as real:
            self.assertEqual(data, real.read()[-1000:])
        src.discard()


if __name__ == "__main__":
    unittest.main()
