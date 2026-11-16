"""The audio sink's player: the app's queue and the engine kept in step.

The engine is real and so is the decoding; the sound card is a fake that
keeps what it is given, and the queue is a stand-in for ``Media`` that
behaves like it in the one way that matters here -- every ``get_next()``
builds a NEW Video, as the real one does (with a server request each time).

Skipped where numpy / soundfile are not installed (the ``sink`` extra).
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

try:
    import numpy as np
    import soundfile as sf

    from jellyfin_mpv_shim.conf import settings
    from jellyfin_mpv_shim.native_audio.engine import (
        TAIL_SECONDS, Track, apply_gain)
    from jellyfin_mpv_shim.native_audio.fetcher import LocalSource
    from jellyfin_mpv_shim.sink_player import AudioSinkManager
    from jellyfin_mpv_shim.sink_source import NotNative
    from tests._sink_fakes import FakeOutput
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

RATE = 44100


class FakeVideo:
    client = None                           # no server: nothing is reported
    playback_info = None

    def __init__(self, parent, entry):
        self.parent = parent
        self.item_id = entry["Id"]

    def get_duration(self):
        return None

    def __repr__(self):
        return "Video(%s)" % self.item_id


class Server:
    """``client.jellyfin``: keeps every report it is sent, in order."""

    def __init__(self):
        self.calls = []
        self.failing = set()

    def _record(self, kind, options):
        self.calls.append((kind, dict(options)))
        if kind in self.failing:
            raise RuntimeError("the server is unreachable")

    def session_playing(self, options):
        self._record("playing", options)

    def session_progress(self, options):
        self._record("progress", options)

    def session_stop(self, options):
        self._record("stop", options)

    def kinds(self):
        return [(kind, o["ItemId"]) for kind, o in self.calls]


class ServerVideo(FakeVideo):
    """A Video that went through PlaybackInfo on a server."""

    server = None
    durations = {}

    def __init__(self, parent, entry):
        super().__init__(parent, entry)
        self.client = type("Client", (), {"jellyfin": self.server})
        self.playback_info = {"PlaySessionId": "session-" + self.item_id}
        self.media_source = {"Id": "source-" + self.item_id}
        self.aid = None

    def get_duration(self):
        return self.durations.get(self.item_id)

    def get_playlist_id(self):
        return "playlist-" + self.item_id

    def set_played(self, watched=True):
        self.server.calls.append(("played", {"ItemId": self.item_id}))


class FakeMedia:
    """``Media``: a queue, a position in it, and the Video at that position."""

    built = 0                               # Videos built, i.e. requests made
    video_class = FakeVideo

    def __init__(self, queue, seq=0):
        self.queue, self.seq = queue, seq
        self.has_next = seq < len(queue) - 1
        self.has_prev = seq > 0
        FakeMedia.built += 1
        self.video = self.video_class(self, queue[seq])

    def get_next(self):
        return FakeMedia(self.queue, self.seq + 1) if self.has_next else None

    def get_prev(self):
        return FakeMedia(self.queue, self.seq - 1) if self.has_prev else None

    def get_from_key(self, key):
        for i, entry in enumerate(self.queue):
            if entry["Id"] == key:
                return FakeMedia(self.queue, i)

    def get_video(self, _index):
        return self.video

    def insert_items(self, ids, append=False):
        items = [{"Id": i} for i in ids]
        self.queue = (self.queue + items if append else
                      self.queue[:self.seq + 1] + items
                      + self.queue[self.seq + 1:])
        self.has_next = self.seq < len(self.queue) - 1


class Recording(AudioSinkManager):
    """The manager, with the reporting hooks writing to a list."""

    def __init__(self, *args, **kwargs):
        self.reports = []
        self.positions = []
        self.idle = threading.Event()
        super().__init__(*args, **kwargs)

    def _report_started(self, video, position):
        self.reports.append(("started", video.item_id))

    def _report_stopped(self, video, finished, position):
        self.positions.append(position)
        self.reports.append(("stopped", video.item_id, finished))
        if self._video is None:
            self.idle.set()


class SinkTestCase(unittest.TestCase):
    manager = None                          # set by each test class

    def setUp(self):
        FakeOutput.opened = []
        FakeMedia.built = 0
        FakeMedia.video_class = FakeVideo
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.rng = np.random.default_rng(2)
        self.pcm = {}
        self.not_flac = set()
        self.resolved = []
        self.limits = []
        self.pm = self.manager(resolve=self.resolve,
                               output_factory=FakeOutput)
        self.addCleanup(self.pm.terminate)

    def track_file(self, name, seconds=1.0):
        pcm = self.rng.integers(-2 ** 15, 2 ** 15,
                                (int(seconds * RATE), 2)).astype(np.int16)
        sf.write(os.path.join(self._tmp.name, name + ".flac"), pcm, RATE,
                 subtype="PCM_16")
        self.pcm[name] = pcm

    def queue(self, *names, seconds=1.0):
        for name in names:
            if name not in self.pcm:
                self.track_file(name, seconds)
        return FakeMedia([{"Id": n} for n in names])

    def resolve(self, video, ram_limit):
        self.resolved.append(video.item_id)
        self.limits.append(ram_limit)
        if video.item_id in self.not_flac:
            raise NotNative("%s is mp3, not FLAC" % video.item_id)
        return Track(LocalSource(os.path.join(
            self._tmp.name, video.item_id + ".flac")), key=video)

    def wait_started(self, name):
        deadline = time.monotonic() + 10
        while ("started", name) not in self.pm.reports:
            self.assertLess(time.monotonic(), deadline,
                            "%s never became audible" % name)
            time.sleep(0.005)

    def wait_idle(self):
        self.assertTrue(self.pm.idle.wait(20), "playback never ended")

    def heard(self):
        return np.concatenate([o.pcm for o in FakeOutput.opened])

    def expect(self, *parts):
        tail = np.zeros((int(RATE * TAIL_SECONDS), 2), np.int16)
        np.testing.assert_array_equal(
            self.heard(), np.concatenate(list(parts) + [tail]))



@unittest.skipUnless(HAVE_DEPS, "needs numpy and soundfile (the sink extra)")
class AudioSinkManagerTest(SinkTestCase):
    manager = None

    def setUp(self):
        AudioSinkManagerTest.manager = Recording
        super().setUp()

    # -- a cast album ---------------------------------------------------------

    def test_a_queue_plays_through_gaplessly_and_the_current_item_follows(self):
        media = self.queue("a", "b", "c")
        self.assertTrue(self.pm.play(media.video))
        self.assertIs(self.pm.get_video(), media.video)
        self.assertTrue(self.pm.is_active())
        self.wait_idle()
        self.expect(self.pcm["a"], self.pcm["b"], self.pcm["c"])
        self.assertEqual(len(FakeOutput.opened), 1, "one stream throughout")
        self.assertEqual(self.pm.reports, [
            ("started", "a"),
            ("stopped", "a", True), ("started", "b"),
            ("stopped", "b", True), ("started", "c"),
            ("stopped", "c", True)])
        self.assertFalse(self.pm.is_active())
        self.assertIsNone(self.pm.get_video())

    def test_the_next_item_is_asked_for_once_not_once_per_question(self):
        media = self.queue("a", "b", "c")
        self.pm.play(media.video)
        self.wait_idle()
        # The engine asks what follows each track twice (to fetch ahead, and
        # at its end). One Video per queue entry is one request each.
        self.assertEqual(FakeMedia.built, 3)
        self.assertEqual(self.resolved, ["a", "b", "c"])

    def test_the_current_item_moves_when_the_next_track_is_heard(self):
        media = self.queue("a", "b", seconds=2.0)
        seen = []
        started = self.pm._report_started
        self.pm._report_started = lambda video, position: (
            seen.append((video.item_id, self.pm.get_video().item_id)),
            started(video, position))
        self.pm.play(media.video)
        self.wait_idle()
        self.assertEqual(seen, [("a", "a"), ("b", "b")])

    def test_auto_play_off_stops_after_one_track(self):
        media = self.queue("a", "b")
        with mock.patch.object(settings, "auto_play", False):
            self.pm.play(media.video)
            self.wait_idle()
        self.expect(self.pcm["a"])
        self.assertEqual(self.resolved, ["a"])

    def test_the_ram_limit_setting_reaches_the_fetcher(self):
        media = self.queue("a")
        with mock.patch.object(settings, "sink_ram_limit_mb", 7):
            self.pm.play(media.video)
            self.wait_idle()
        self.assertEqual(self.limits, [7 << 20])

    # -- what cannot be played -------------------------------------------------

    def test_a_queue_stops_at_a_track_the_engine_cannot_play(self):
        media = self.queue("a", "b", "c")
        self.not_flac.add("b")
        self.pm.play(media.video)
        self.wait_idle()
        self.expect(self.pcm["a"])
        self.assertEqual(self.pm.reports, [("started", "a"),
                                           ("stopped", "a", True)])

    def test_an_unplayable_item_is_refused_and_the_current_one_carries_on(self):
        media = self.queue("a", seconds=3.0)
        self.track_file("x")
        self.not_flac.add("x")
        self.pm.pause_if_playing()
        self.pm.play(media.video)
        self.assertFalse(self.pm.play(FakeMedia([{"Id": "x"}]).video))
        self.assertIs(self.pm.get_video(), media.video)
        self.wait_idle()
        self.expect(self.pcm["a"])

    def test_skipping_to_an_unplayable_item_ends_in_silence_not_the_old_track(
            self):
        media = self.queue("a", "x", seconds=5.0)
        self.not_flac.add("x")
        self.pm.play(media.video)
        self.wait_started("a")
        self.pm.pause_if_playing()
        self.assertFalse(self.pm.play_next())
        self.assertFalse(self.pm.is_active())
        self.assertEqual(self.pm.reports[-1], ("stopped", "a", True))
        self.pm.play_if_paused()
        time.sleep(0.3)
        self.assertLess(len(self.heard()) if FakeOutput.opened else 0,
                        len(self.pcm["a"]), "the old track must not resume")

    # -- transport --------------------------------------------------------------

    def test_next_reuses_the_track_already_fetched_ahead(self):
        media = self.queue("a", "b", seconds=5.0)
        self.pm.play(media.video)
        self.wait_started("a")
        self.pm.pause_if_playing()
        deadline = time.monotonic() + 5
        while "b" not in self.resolved and time.monotonic() < deadline:
            time.sleep(0.01)                # the engine looks ahead
        self.assertTrue(self.pm.play_next())
        self.assertEqual(self.pm.get_video().item_id, "b")
        self.pm.play_if_paused()
        self.wait_idle()
        self.assertEqual(self.resolved, ["a", "b"], "b was not resolved twice")
        self.assertEqual(self.pm.reports[1:3], [("stopped", "a", True),
                                                ("started", "b")])
        heard = self.heard()
        tail = len(self.pcm["b"]) + int(RATE * TAIL_SECONDS)
        np.testing.assert_array_equal(heard[-tail:][:len(self.pcm["b"])],
                                      self.pcm["b"])

    def test_previous_and_skip_to_move_within_the_queue(self):
        media = self.queue("a", "b", "c", seconds=5.0)
        self.pm.play(media.get_next().video)        # start on b
        self.pm.pause_if_playing()
        self.assertTrue(self.pm.play_prev())
        self.assertEqual(self.pm.get_video().item_id, "a")
        self.assertFalse(self.pm.play_prev(), "nothing before the first")
        self.assertTrue(self.pm.skip_to("c"))
        self.assertEqual(self.pm.get_video().item_id, "c")
        self.assertFalse(self.pm.play_next(), "nothing after the last")
        self.assertFalse(self.pm.skip_to("nope"))

    def test_an_item_queued_next_is_what_plays_next(self):
        media = self.queue("a", "b", seconds=3.0)
        self.track_file("n")
        self.pm.play(media.video)
        self.pm.pause_if_playing()
        time.sleep(0.3)                     # b has been looked ahead to
        self.pm.get_video().parent.insert_items(["n"])      # "Play next"
        self.pm.upd_player_hide()
        self.pm.play_if_paused()
        self.wait_idle()
        self.expect(self.pcm["a"], self.pcm["n"], self.pcm["b"])

    def test_stop_reports_the_item_unfinished(self):
        media = self.queue("a", "b", seconds=5.0)
        self.pm.play(media.video)
        self.wait_started("a")
        self.pm.stop()
        self.assertEqual(self.pm.reports[-1], ("stopped", "a", False))
        self.assertFalse(self.pm.has_video())
        self.assertEqual(self.pm.get_state(), "stopped")
        self.pm.stop()                      # twice is harmless
        self.assertEqual(self.pm.reports.count(("stopped", "a", False)), 1)

    def test_an_item_never_heard_is_never_reported(self):
        media = self.queue("a", "b", seconds=5.0)
        self.pm.pause_if_playing()
        self.pm.play(media.video)
        self.pm.pause_if_playing()          # before a sample of it is out
        self.assertTrue(self.pm.play_next())
        self.pm.pause_if_playing()
        self.pm.stop()
        self.assertEqual([r for r in self.pm.reports if r[1] == "a"], [])
        started = [r for r in self.pm.reports if r[0] == "started"]
        stopped = [r for r in self.pm.reports if r[0] == "stopped"]
        self.assertEqual(len(started), len(stopped),
                         "every stop has a start before it")

    def test_pause_seek_and_state(self):
        media = self.queue("a", seconds=3.0)
        self.pm.pause_if_playing()          # nothing playing: no effect
        self.assertFalse(self.pm.is_paused())
        self.pm.play(media.video)
        self.assertTrue(self.pm.is_playing())
        self.pm.toggle_pause()
        self.assertTrue(self.pm.is_paused())
        self.assertEqual(self.pm.get_state(), "paused")
        self.pm.seek(2.0, absolute=True)
        self.pm.toggle_pause()
        self.assertEqual(self.pm.get_state(), "playing")
        self.wait_idle()
        np.testing.assert_array_equal(
            FakeOutput.opened[-1].pcm[:RATE], self.pcm["a"][2 * RATE:])

    def test_volume_is_a_percentage_and_mute_is_separate(self):
        self.assertEqual(self.pm.get_volume(percent=True), 100)
        self.pm.set_volume(25)
        self.assertEqual(self.pm.get_volume(percent=True), 25)
        self.assertEqual(self.pm.get_volume(), 0.25)
        media = self.queue("a")
        self.pm.play(media.video)
        self.wait_idle()
        self.expect(apply_gain(self.pcm["a"], 0.25))
        self.pm.set_mute(True)
        self.assertEqual(self.pm.get_volume(percent=True), 25)

    def test_the_timeline_is_woken_when_something_changes(self):
        self.pm.timeline_trigger = threading.Event()
        media = self.queue("a")
        self.pm.play(media.video)
        self.assertTrue(self.pm.timeline_trigger.is_set())
        self.pm.timeline_trigger.clear()
        self.wait_idle()
        self.assertTrue(self.pm.timeline_trigger.wait(5))

    def test_a_dropout_is_logged_once_with_its_length(self):
        media = self.queue("a", seconds=5.0)
        self.pm.play(media.video)
        self.wait_started("a")
        self.pm.pause_if_playing()
        time.sleep(0.05)
        # The fake sound card is not paced, so it can outrun the decoder
        # before the pause; take that as read.
        self.pm.send_timeline()
        with self.assertNoLogs("sink_player", level="WARNING"):
            self.pm.send_timeline()
        self.pm._engine.underrun_frames += RATE // 2     # half a second
        with self.assertLogs("sink_player", level="WARNING") as logs:
            self.pm.send_timeline()
        self.assertIn("500 ms of silence", logs.output[0])
        with self.assertNoLogs("sink_player", level="WARNING"):
            self.pm.send_timeline()         # not repeated

    def test_memory_is_only_locked_when_there_is_no_limit(self):
        import resource
        from jellyfin_mpv_shim import sink_player

        with mock.patch.object(resource, "getrlimit",
                               return_value=(8 << 20, 8 << 20)), \
                mock.patch("ctypes.CDLL") as cdll:
            self.assertFalse(sink_player.lock_memory())
            cdll.assert_not_called()
        with mock.patch.object(resource, "getrlimit", return_value=(
                resource.RLIM_INFINITY, resource.RLIM_INFINITY)), \
                mock.patch("ctypes.CDLL") as cdll:
            cdll.return_value.mlockall.return_value = 0
            self.assertTrue(sink_player.lock_memory())
            cdll.return_value.mlockall.assert_called_once_with(7)

    def test_syncplay_is_declined(self):
        self.assertFalse(self.pm.syncplay.in_group())
        self.assertFalse(self.pm.syncplay.is_enabled())
        self.pm.syncplay.process_group_update({})
        self.pm.syncplay.process_command({})


@unittest.skipUnless(HAVE_DEPS, "needs numpy and soundfile (the sink extra)")
class ReportingTest(SinkTestCase):
    """What the server is told, and in what order."""

    def setUp(self):
        ReportingTest.manager = AudioSinkManager
        super().setUp()
        self.server = Server()
        ServerVideo.server = self.server
        ServerVideo.durations = {}
        FakeMedia.video_class = ServerVideo

    def queue(self, *names, seconds=1.0):
        media = super().queue(*names, seconds=seconds)
        for name in names:
            ServerVideo.durations[name] = len(self.pcm[name]) / RATE
        return media

    def wait_for(self, kind, name, timeout=10):
        deadline = time.monotonic() + timeout
        while (kind, name) not in self.server.kinds():
            self.assertLess(time.monotonic(), deadline,
                            "no %s report for %s: %s"
                            % (kind, name, self.server.kinds()))
            time.sleep(0.005)

    def test_a_queue_is_reported_start_to_finish_in_order(self):
        media = self.queue("a", "b", seconds=1.5)
        self.pm.play(media.video)
        self.wait_for("stop", "b")
        self.assertEqual(self.server.kinds(), [
            ("playing", "a"), ("stop", "a"), ("playing", "b"), ("stop", "b")])
        playing, stop = self.server.calls[0][1], self.server.calls[1][1]
        self.assertEqual(playing["PositionTicks"], 0)
        self.assertEqual(stop["PositionTicks"], 15000000,
                         "played out: the whole length, so it counts as "
                         "played")
        for options in (playing, stop):
            self.assertEqual(options["ItemId"], "a")
            self.assertEqual(options["PlaySessionId"], "session-a")
            self.assertEqual(options["MediaSourceId"], "source-a")
            self.assertEqual(options["PlaylistItemId"], "playlist-a")
            self.assertEqual(options["PlayMethod"], "DirectPlay")
            self.assertEqual(options["NowPlayingQueue"],
                             [{"Id": "a"}, {"Id": "b"}])
        self.assertEqual(self.server.calls[2][1]["PlaySessionId"],
                         "session-b")

    def test_a_cast_with_a_start_position_reports_it(self):
        media = self.queue("a", seconds=5.0)
        self.pm.play(media.video, 3.5)
        self.wait_for("playing", "a")
        self.assertEqual(self.server.calls[0][1]["PositionTicks"], 35000000)

    def test_a_stop_part_way_reports_where_it_was(self):
        media = self.queue("a", seconds=5.0)
        self.pm.play(media.video)
        self.wait_for("playing", "a")
        self.pm.pause_if_playing()
        self.pm.seek(2.0, absolute=True)
        self.pm.stop()
        self.pm._reporter.drain()
        kind, options = self.server.calls[-1]
        self.assertEqual(kind, "stop")
        self.assertEqual(options["PositionTicks"], 20000000)
        self.assertTrue(options["IsPaused"])

    def test_skipping_reports_the_position_not_the_whole_length(self):
        media = self.queue("a", "b", seconds=5.0)
        self.pm.play(media.video)
        self.wait_for("playing", "a")
        self.pm.pause_if_playing()
        self.pm.seek(1.0, absolute=True)
        self.pm.play_next()
        self.pm._reporter.drain()
        stop = dict(self.server.calls)["stop"]
        self.assertEqual(stop["ItemId"], "a")
        self.assertEqual(stop["PositionTicks"], 10000000)

    def test_progress_carries_position_pause_volume_and_mute(self):
        media = self.queue("a", seconds=5.0)
        self.pm.play(media.video)
        self.wait_for("playing", "a")
        self.pm.pause_if_playing()
        self.pm.seek(3.0, absolute=True)
        self.pm.set_volume(40)
        self.pm.set_mute(True)
        self.pm.send_timeline()
        kind, options = self.server.calls[-1]
        self.assertEqual(kind, "progress")
        self.assertEqual(options["PositionTicks"], 30000000)
        self.assertTrue(options["IsPaused"])
        self.assertEqual(options["VolumeLevel"], 40)
        self.assertTrue(options["IsMuted"])

    def test_progress_reports_reach_the_server_in_the_order_they_were_built(
            self):
        # A pause is reported from two threads at once. If the reports could
        # overlap, one built before the pause could land after one built
        # after it, and the server would go on showing "playing".
        media = self.queue("a", seconds=5.0)
        self.pm.play(media.video)
        self.wait_for("playing", "a")
        inside, overlapped, arrived = [0], [], []
        first_in, release = threading.Event(), threading.Event()

        def slow_progress(options):
            inside[0] += 1
            overlapped.append(inside[0] > 1)
            if not first_in.is_set():
                first_in.set()
                release.wait(5)             # a slow round trip
            arrived.append(options["IsPaused"])
            inside[0] -= 1

        self.server.session_progress = slow_progress
        early = threading.Thread(target=self.pm.send_timeline)
        early.start()                       # built while still playing
        self.assertTrue(first_in.wait(5))
        self.pm.pause_if_playing()
        late = threading.Thread(target=self.pm.send_timeline)
        late.start()                        # built after the pause
        time.sleep(0.2)
        release.set()
        early.join(5)
        late.join(5)
        self.assertEqual(arrived, [False, True])
        self.assertNotIn(True, overlapped)

    def test_no_progress_before_the_start_has_been_reported(self):
        media = self.queue("a", seconds=5.0)
        self.pm.send_timeline()             # nothing playing
        self.pm.pause_if_playing()
        self.pm.play(media.video)
        self.pm.pause_if_playing()          # held before a sample is out
        self.pm.send_timeline()
        self.assertEqual(self.server.calls, [])

    def test_played_out_tracks_are_marked_played_only_when_asked(self):
        media = self.queue("a", "b")
        with mock.patch.object(settings, "force_set_played", True):
            self.pm.play(media.video)
            self.wait_for("stop", "b")
            self.pm._reporter.drain()
        self.assertEqual(self.server.kinds(), [
            ("playing", "a"), ("stop", "a"), ("played", "a"),
            ("playing", "b"), ("stop", "b"), ("played", "b")])

    def test_a_stopped_track_is_not_marked_played(self):
        media = self.queue("a", seconds=5.0)
        with mock.patch.object(settings, "force_set_played", True):
            self.pm.play(media.video)
            self.wait_for("playing", "a")
            self.pm.stop()
            self.pm._reporter.drain()
        self.assertNotIn(("played", "a"), self.server.kinds())

    def test_an_unreachable_server_does_not_stop_the_music(self):
        self.server.failing = {"playing", "stop", "progress"}
        media = self.queue("a", "b")
        self.pm.play(media.video)
        self.wait_for("stop", "b")
        self.expect(self.pcm["a"], self.pcm["b"])
        with self.assertRaises(RuntimeError):
            # Progress is sent on the caller's thread, as the mpv player's
            # is; the timeline thread is what absorbs the failure.
            self.server.session_progress({"ItemId": "x"})

    def test_quitting_delivers_the_last_stop(self):
        media = self.queue("a", seconds=5.0)
        self.pm.play(media.video)
        self.wait_for("playing", "a")
        self.pm.terminate()
        self.assertEqual(self.server.kinds()[-1], ("stop", "a"))


if __name__ == "__main__":
    unittest.main()
