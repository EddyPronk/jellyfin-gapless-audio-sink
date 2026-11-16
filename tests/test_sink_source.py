"""Turning a queue item into a native-engine track, and who gets the token.

``native_track`` runs for the NEXT track while the current one plays, so the
two things that must hold are: it decides correctly what the engine may play
(FLAC, handed over unmodified), and the access token goes in a header, to our
own server only.

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

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

try:
    from jellyfin_mpv_shim.native_audio.fetcher import (
        HttpSource, LocalSource)
    from jellyfin_mpv_shim.sink_source import NotNative, native_track
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

SERVER = "http://jelly.example:8096"
HEADER = 'MediaBrowser Client="x", Token="secret"'


class FakeVideo:
    """What ``native_track`` reads off a Video, and nothing more."""

    def __init__(self, item=None, container="flac", transcode=False,
                 url=None, header=HEADER):
        self.item = {"Name": "Song", "Type": "Audio", "MediaType": "Audio"}
        self.item.update(item or {})
        self._container, self._transcode, self._url = container, transcode, url
        self.asked = 0
        self.auth_via_header = False
        self.is_transcode = False
        self.transcode_reasons = []
        self.media_source = None
        http = type("Http", (), {
            "_get_authenication_header": staticmethod(lambda: header)})
        config = type("Config", (), {"data": {"auth.server": SERVER,
                                              "auth.token": "secret"}})
        self.client = type("Client", (), {"http": http, "config": config})

    def get_playback_url(self):
        self.asked += 1
        self.media_source = {"Id": "src", "Container": self._container}
        self.is_transcode = self._transcode
        if self._transcode:
            self.transcode_reasons = ["AudioBitrateNotSupported"]
        if self._url == "none":
            return None
        if self._url:
            return self._url
        # As Video._get_url_from_source: the token is in the URL only when
        # the player could not be given the header.
        token = "" if self.auth_via_header else "&ApiKey=secret"
        return "%s/Videos/1/stream?static=true&MediaSourceId=src%s" % (
            SERVER, token)


@unittest.skipUnless(HAVE_DEPS, "needs numpy and soundfile (the sink extra)")
class NativeTrackTest(unittest.TestCase):
    def test_flac_from_our_server_gets_the_token_in_a_header(self):
        video = FakeVideo()
        track = native_track(video, ram_limit=1234)
        self.assertIs(track.key, video)
        source = track.source
        self.assertIsInstance(source, HttpSource)
        self.assertEqual(source._headers["Authorization"], HEADER)
        self.assertNotIn("secret", source._url)
        self.assertNotIn("ApiKey", source._url)
        self.assertEqual(source._ram_limit, 1234)
        self.assertEqual(source.label, "Song")
        self.assertIsNone(source._thread, "nothing is downloaded yet")

    def test_without_a_token_to_put_in_a_header_the_url_keeps_its_own(self):
        video = FakeVideo(header="MediaBrowser Client=\"x\"")
        source = native_track(video).source
        self.assertNotIn("Authorization", source._headers)
        self.assertIn("ApiKey=secret", source._url)

    def test_a_file_on_another_host_is_sent_no_credential(self):
        for url in ("http://elsewhere.example/a.flac",
                    "https://jelly.example:8096/a.flac",    # other scheme
                    "http://jelly.example:9000/a.flac"):     # other port
            with self.subTest(url=url):
                source = native_track(FakeVideo(url=url)).source
                self.assertNotIn("Authorization", source._headers)
                self.assertEqual(source._url, url)

    def test_a_direct_path_is_read_from_disk(self):
        source = native_track(FakeVideo(url="/music/a.flac")).source
        self.assertIsInstance(source, LocalSource)
        self.assertEqual(source.path, "/music/a.flac")

    def test_what_the_engine_cannot_play_is_refused_with_the_reason(self):
        cases = (
            (FakeVideo(transcode=True), "AudioBitrateNotSupported"),
            (FakeVideo(container="mp3"), "mp3, not FLAC"),
            (FakeVideo(container=None), "unknown, not FLAC"),
            (FakeVideo(url="none"), "nothing to play"),
        )
        for video, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(NotNative) as caught:
                    native_track(video)
                self.assertIn(reason, str(caught.exception))
                self.assertIn("Song", str(caught.exception))

    def test_a_film_is_refused_without_asking_the_server(self):
        video = FakeVideo(item={"Type": "Movie", "MediaType": "Video"})
        with self.assertRaises(NotNative):
            native_track(video)
        self.assertEqual(video.asked, 0)

    def test_an_audiobook_counts_as_audio(self):
        video = FakeVideo(item={"Type": "AudioBook", "MediaType": "Audio"})
        self.assertIsInstance(native_track(video).source, HttpSource)


if __name__ == "__main__":
    unittest.main()
