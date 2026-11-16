"""Gapless FLAC playback without mpv (docs/native-flac-plan.md).

``fetcher``  downloads a track to a cache file and lets it be read while it
             is still arriving;
``decoder``  turns that file into PCM blocks;
``output``   is the sound card: one stream, opened once per sample format;
``engine``   joins them: it decodes one track after another into the same
             stream, so nothing is closed or reopened at a track boundary.

Needs numpy, soundfile and sounddevice (the ``sink`` extra), none of which the
rest of the app imports. ``python -m jellyfin_mpv_shim.native_audio URL...``
plays a list of tracks from the command line.
"""
