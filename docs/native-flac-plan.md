# Plan: native gapless FLAC playback without mpv

Written 2026-11-16 against `master` at `9d3b0fb6`. Follows
[gapless-playback.md](gapless-playback.md). Only the step 1 spike exists so far; the app
itself is unchanged.

## Is the premise correct?

Yes. The app holds the whole queue and can fetch the next track early, wherever the server is.

- **The full queue is in Python.** A cast `PlayNow` hands over every `ItemIds` entry
  (`event_handler.py:152`), and `Media.queue` keeps all of them (`media.py:1175`).
  `PlayNext` / `PlayLast` insert into the same list.
- **The next track is one call away.** `Media.get_next()` (`media.py:1221`) builds the next
  item's `Video` without touching the player.
- **Its URL is plain HTTP.** For a direct-played file `_get_url_from_source` returns
  `<server>/Videos/<id>/stream?static=true&MediaSourceId=...` (`media.py:770`), authorised
  by an `Authorization: MediaBrowser Token=...` header. Any HTTP client can download it;
  `requests` is already a dependency.

So prefetch is not blocked by the data model. It is blocked only because mpv is handed one
file at a time, after the previous one ends.

One caveat: `get_playback_url` begins with `terminate_transcode()` and carries play-session
state, so calling it early for the next track needs care (see step 3).

## Approach

FLAC stores an exact sample count and has no encoder padding. If two tracks are decoded to
PCM and written back to back into **one output stream that never closes**, the join is
sample-exact. That is the whole trick: gapless stops being a player feature and becomes
"don't stop writing".

```
queue (Media) ──> TrackFetcher ──> memory ──> FLAC decoder ──> PCM ring ──> ALSA
                  current + next   or stream     per track        one stream
```

- **TrackFetcher** downloads the current track into memory and, as soon as that finishes,
  the next one. Tracks over a size limit are streamed instead (see "Memory, and no disk
  cache").
- **Decoder** reads those bytes, blocking when it catches up with a download in progress,
  so the first track starts before it has fully arrived.
- **Output** is one stream opened at the first track's sample rate, bit depth and channel
  count. When a track's decoder runs dry, the next track's decoder takes over in the same
  callback, with no silence written between them.
- If the next track's format differs, drain, reopen the device and accept a gap. Tracks of
  one album normally match.

Holding a whole track in memory makes seeking a local operation and removes the network
from the track boundary entirely. A streamed track gives both of those up.

## Libraries

Recommended: **`soundfile`** (libsndfile, decodes FLAC) + **`sounddevice`** (PortAudio, plays
through ALSA). Both are on PyPI and packaged in Debian (`python3-soundfile`,
`python3-sounddevice`), which matters on Raspberry Pi OS. `soundfile` brings in numpy.

Alternative: **`miniaudio`** (one package, decoder and output together, no numpy), built
from source with cffi where no wheel exists.

`soundfile` + `sounddevice` passed the spike on the desktop and the Pi (see below).
`miniaudio` was not tried. On the Pi, `sounddevice` also needs the system package
`libportaudio2`.

## Where it plugs in

`PlayerManager` (`player.py`, 5,864 lines) is mpv through and through, and
`event_handler.py`, `action_thread.py` and `mpv_shim.py` import the `playerManager`
singleton directly. Adding a second backend inside that class would put a branch in most
of its methods.

Recommended instead: a separate, small **`AudioSinkManager`** that implements only what the
remote-control path calls, and a startup switch that decides which object `playerManager`
is. The surface used outside the UI is about thirty methods: `play`, `stop`, `seek`,
`toggle_pause`, `pause_if_playing`, `play_if_paused`, `play_next`, `play_prev`, `skip_to`,
`set_volume`, `get_volume`, `set_mute`, `is_playing`, `is_paused`, `has_video`,
`get_video`, `send_timeline`, `timeline_trigger`, `terminate`, and no-op stubs for the
video-only ones (`set_streams`, `toggle_fullscreen`, `screenshot`, `menu`, `idle_quit`).

New setting, config-file only: `audio_sink: true`. It implies `enable_gui: false` and does
not import `python-mpv` at all, so the Pi needs no mpv installed.

## Steps

1. **Spike (standalone script, no app code).** Done on the desktop and on the Pi with real
   tracks, see "Spike results". Still to do: a whole-album soak. Original description: download two adjacent tracks of a
   live album from the server with `requests`, decode and play them through one output
   stream. Confirms the libraries, the ALSA device, CPU load, and that the join is
   inaudible. Everything after this depends on the result.
2. **`native_audio/` package.** Done, see "Step 2 results". Original description: `fetcher.py` (download to cache dir, progress, cancel),
   `decoder.py` (blocking read over a growing file), `output.py` (stream, volume, mute,
   pause, position counted in frames written). Unit-testable without a server: feed it local
   FLAC files and capture output into a buffer; assert the PCM equals the two files
   concatenated.
3. **URL resolution without side effects.** Done, see "Step 3 results". Original
   description: Split what the sink needs out of
   `Video.get_playback_url` so the next track's URL and headers can be resolved while the
   current one plays, without `terminate_transcode()` or disturbing the current play session.
4. **`AudioSinkManager`.** Done, see "Step 4 results". Original description: Owns the queue position, drives fetcher/decoder/output, and
   advances `Media` when the output crosses a track boundary (not when a download ends).
5. **Server reporting.** Done, see "Step 5 results". Original description: At each boundary: stopped report for the old item, played mark,
   start report for the new one; progress on the existing timeline tick. `ReportingMixin`
   (`player_reporting.py`) reads `_player` for position and pause state, so either give it a
   thin object exposing those properties from the output stage or lift the report-building
   code out of the mixin. Decide after reading it in full.
6. **Startup switch.** `mpv_shim.py` picks the manager from `audio_sink`; make
   `event_handler.py` and `action_thread.py` resolve the singleton through one accessor
   instead of importing it from `player.py`.
7. **Queue edits and transport.** Next/previous, skip-to, `PlayNext`/`PlayLast`, repeat and
   shuffle each invalidate the prefetched track; cancel and refetch.
8. **Docs and tests.** `docs/configuration.md` entry, update the README known issue and
   `gapless-playback.md`, tests alongside the existing `tests/test_*.py`.

## Scope (decided 2026-11-16)

- **FLAC only.** Other formats are out of scope for now and can be added later.
- **Personal use.** Not aimed at upstream, so reporting code may be copied rather than
  refactored for sharing (steps 5 and 6).
- **No SyncPlay** in sink mode.
- **Test locally first**, then on the Pi.

## Spike results (2026-11-16)

The spike is in `tools/gapless_spike/`:

| File | Purpose |
|---|---|
| `play_gapless.py` | The player. Fetches tracks over HTTP, each next one while the current plays, and writes them into one output stream. Takes real URLs. |
| `gen_tones.py` | Two FLAC tracks that are one quiet sweep (-34 dBFS) cut mid-waveform. |
| `run_spike.py` | Desktop test with the tones: records the default sink's monitor and compares it with the original sample by sample. |
| `run_real.py` | Desktop test with two real tracks: compares timing against the same audio played as one continuous file. |
| `run_alsa.py` | Pi test with two real tracks: copies everything sent to the DAC into a file and compares it bit for bit with the FLAC data. |

The real-track tests play only the 10 seconds either side of the join, at -20 dB.

### Desktop (Debian 13, PipeWire 1.6.9, 48 kHz sink)

| Test | Result |
|---|---|
| Test tones | 25 of 25 runs bit-identical to the original across the join. |
| Tones, `--naive` control (a new stream per track) | 64 ms and 106 ms of silence inserted at the join in two runs. |
| Tones, `--rig-check` (one file via `paplay`) | 10 of 10 clean, so the recording rig is sound. |
| Two real tracks from the Jellyfin server (16-bit 44.1 kHz FLAC, 12.6 MB and 26.4 MB) | Join clean in 6 of 6 runs. One run had an 11 ms dropout 7 seconds *after* the join. |

### Raspberry Pi (`plexamp`: Pi 3 A+, Debian 13, plain ALSA, Schiit Modi+ USB DAC at 44.1 kHz)

| Test | Result |
|---|---|
| Two real tracks, default device buffer | 7 of 8 runs gapless and bit-perfect: every sample sent to the DAC equals the FLAC data, including across the join. One run had the DAC run dry once, about 2 s in, while the next track was downloading. |
| Same, `--latency 1.0` (one-second device buffer) | 10 of 10 gapless and bit-perfect. |

Ten clean runs against a fault seen once in eight is encouraging, not proof; it needs a
longer soak (whole albums) before it is trusted.

### What the spike turned up

- **Buffer size matters, on both machines.** On the desktop, letting PortAudio pick the
  buffer (`blocksize=0`) dropped audio in about one run in three through PipeWire's ALSA
  bridge; a fixed 4096 frames fixed the tones, though one real-track run still dropped. On
  the Pi the default buffer underran once during a download. The audio callback is Python,
  and the download and decode threads compete with it. A large device buffer is the cheap
  fix for a music-only sink; moving the download to a separate process is the thorough one.
- **Stopping the stream cuts off the end.** The last 38 ms of the final track was discarded
  on stop. Half a second of trailing silence before stopping avoids it.
- **PortAudio starts with silence** (8,192 frames by default, 61,440 with the 1 s buffer).
  Harmless, but it delays the first track by that much.
- **Prefetch needs time.** With only 2 s of track 1 left, the 26 MB next track did not
  arrive in time (it takes 3 to 5 s) and the player correctly wrote silence. In real use
  the fetch starts minutes ahead, but a skip to the last seconds of a track will gap.
- **The DAC is shared on the Pi.** `announce-watch.service` speaks through the DAC when a
  source stops, holding it for a few seconds; an immediate reopen fails with "Device
  unavailable". The sink must retry when the device is busy, and each test run triggers an
  announcement. Plexamp, shairport-sync and bluealsa-aplay also use the same DAC.
- **16-bit path is exact.** Decoding to `int16` and opening the stream as `int16` with
  dither off reaches the DAC unmodified. 24-bit files need `int32` and were not tried.

Not covered yet: whole tracks or albums end to end, 24-bit or high-rate files, format
changes between tracks, seek, pause, volume, full-scale (gain 1.0) playback, and anything
inside the app itself.

### Left on the Pi

- `libportaudio2` installed with apt.
- `~/gapless_spike/`: the scripts, a virtualenv (numpy, soundfile, sounddevice, requests),
  and `work/` with the two downloaded FLAC files and the last capture.

### Repeating it

Desktop:

```
uv venv .venv && uv pip install -p .venv/bin/python numpy soundfile sounddevice requests
.venv/bin/python tools/gapless_spike/run_spike.py            # expect RESULT: GAPLESS
.venv/bin/python tools/gapless_spike/run_spike.py --naive    # control, expect a fault
.venv/bin/python tools/gapless_spike/run_real.py URL1 URL2
```

Pi:

```
cd ~/gapless_spike
venv/bin/python run_alsa.py URL1 URL2 --pcm hw:CARD=Modi,DEV=0 --gain 0.1 --latency 1.0
```

To listen to two whole tracks: `venv/bin/python play_gapless.py URL1 URL2 --device
"Schiit Modi+" --dtype int16 --latency 1.0`.

## Step 2 results (2026-11-16)

`jellyfin_mpv_shim/native_audio/` exists and is not yet used by the app:

| Module | What it does |
|---|---|
| `fetcher.py` | `HttpSource` holds a track in memory or streams it, by size (see below); either way it can be read while still arriving. `LocalSource` for files on disk. |
| `decoder.py` | libsndfile to PCM blocks. 16-bit stays `int16`, 24-bit goes out as `int32`. |
| `output.py` | One PortAudio stream: 1 s device buffer, 4096-frame blocks, dither off, retries while the device is busy. |
| `engine.py` | `GaplessPlayer`: decodes track after track into the same stream. Play, pause, seek, stop, volume, mute, position; asks a `next_track` callback what follows; reopens the stream only when the sample format changes. |
| `__main__.py` | `python -m jellyfin_mpv_shim.native_audio URL...` for listening and soak runs. |

The libraries are the optional `sink` extra in `pyproject.toml`.

Tests: `tests/test_native_audio.py`, 13 tests, real code throughout with only the sound
card faked. They cover the sample-exact join (local and over HTTP, 16 and 24 bit), a
format change, start offsets, seek, volume, mute, a queue change before the join, a
failed download, reading ahead of the download, and streaming (including a seek). They pass on the desktop and on the
Pi, and skip where numpy/soundfile are missing (as with the system Python here).

On the Pi through the Modi+, using the command-line player on the two real tracks (last
10 s of track 1 into the first 10 s of track 2, -20 dB): **6 of 6 runs bit-perfect across
the join**, 0 underruns, 0 device status flags.

### Memory, and no disk cache (changed 2026-11-16)

Nothing is written to disk. A track from the server is handled by its size:

| Track size | What happens | Join into it |
|---|---|---|
| Up to the limit (default 100 MB, about 15 minutes of CD-quality FLAC) | Downloaded whole into memory, ahead of time, while the previous track plays. | Gapless. |
| Over the limit | Streamed: read from the server as it is decoded, through a window of about 2 MB; seeks are HTTP range requests. Not fetched ahead. | May have a gap if the server is slow to answer. |

The limit is `ram_limit` on `HttpSource` and `--ram-limit-mb` on the command-line player.
It becomes a `conf.json` setting when sink mode is wired into the app (step 4). Two tracks
can be in memory at once (playing and next), so the worst case is twice the limit; on the
415 MB Pi that argues against raising it much.

Measured on the Pi through the Modi+, the same two real tracks (12.6 MB and 26.4 MB), three
runs at each setting, all nine bit-perfect across the join with no underruns:

| `--ram-limit-mb` | Mode | Peak process memory |
|---|---|---|
| 100 | both in memory | 87 MB |
| 20 | track 2 streamed | 63 MB |
| 5 | both streamed | 52 MB |

So on this LAN a streamed track also joined without a gap; the gap is a risk on a slow or
busy server, not something observed. The decoder opens the next track about four seconds
before it is heard (three seconds of decoded audio plus the one-second device buffer), which
is the time a streamed track has to answer.

An earlier version cached every track to a disk file. With two generated 40-minute tracks
(311 MB each) that held memory flat but wrote 623 MB to disk, which on the Pi means SD-card
wear, or failure if the cache is the RAM-backed `/tmp`. It was replaced by the above.

Known limits of the package as it stands:

- **Timing of "now playing".** A track change is reported when its first sample is handed
  to the device, about one second (the device buffer) before it is heard. `position`
  subtracts that delay; the change event does not.
- **Start-up delay.** About 1.4 s of PortAudio lead-in silence plus the preroll before the
  first sound.
- **Pause keeps the DAC open**, so other players on the Pi cannot use it meanwhile.
- **One download at a time.** The next track starts downloading when the current one has
  fully arrived. Skipping to a track that was not prefetched waits for its header.
- **A streamed track depends on the network throughout.** A stall longer than about four
  seconds is heard as silence; one dropped connection is resumed, a second fails the track.
- **Not exercised on real hardware:** 24-bit and high-rate files, a format change, the
  busy-device retry, pause/seek/volume by ear, and whole albums.

## Step 3 results (2026-11-16)

`jellyfin_mpv_shim/sink_source.py`: `native_track(video, ram_limit)` runs the app's normal
negotiation (`Video.get_playback_url`) for a queue item and returns a `native_audio` Track,
or raises `NotNative` with the reason.

- **No split of `get_playback_url` was needed.** The plan feared it would disturb the
  current play session. It does not: `terminate_transcode()` and everything else in it act
  on the Video it is called on, and each call gets its own `PlaySessionId`. Resolving the
  next track early leaves the playing one untouched.
- **What it accepts:** audio items the server hands over unmodified as FLAC. A transcode,
  another container, a non-audio item or an item with no source raises `NotNative`.
- **The token goes in a header**, per request, and only to the server's own origin. The URL
  carries no key, so it cannot end up in a log. A file on another host (an http source, a
  path substitution) is sent no credential.
- **A direct path** on the local machine becomes a `LocalSource`.

Tests: `tests/test_sink_source.py`, 7 tests against a stand-in Video.

Checked on the Pi against the real server with the real `Media` and `Video` classes and
the app's stored login (an HTTP-only client; the running service was not touched). For the
two test tracks: `DirectPlay`, not a transcode, container `flac`, URL
`/Videos/<id>/stream` with no token in it, `Authorization` header present, and each opened
and decoded over HTTP (44.1 kHz, 16-bit). Resolving the second left the first's play
session, source and URL unchanged. That endpoint also answers range requests, which the
streaming mode needs.

The audio libraries (numpy, soundfile, sounddevice) are now installed in the app's
virtualenv on the Pi (`~/jellyfin/.venv`), ready for sink mode.

Left for step 4: `Media.get_next()` builds a new Video (one `get_item` request) every time
it is called, and the engine asks for the next track twice and expects the same object, so
the sink manager has to cache what it resolved.

## Step 4 results (2026-11-16)

`jellyfin_mpv_shim/sink_player.py`: `AudioSinkManager`, with the method names the event
handler and timeline thread already call on the mpv player, driving the native engine. It
does not import mpv. Nothing selects it yet (step 6). Server reporting was added in step 5.

- **Cast commands covered:** play (with a start offset), stop, pause/unpause/toggle, seek
  (absolute and relative), next, previous, skip-to, volume, mute, and "play next / play
  last" queue inserts.
- **Gapless advance:** the engine moves to the next track on its own; when that track is
  handed to the device, the manager's current item moves with it. No `play()` call happens
  at a track boundary.
- **The next item is cached.** `Media.get_next()` is called once per boundary, not once
  per question from the engine. Pressing next reuses the track already fetched ahead.
- **Queue edits** clear the cache and tell the engine, so an item queued "next" is what
  plays next.
- **What cannot be played is refused.** A cast of a non-FLAC item is declined and logged
  and the current track carries on. A queue stops after the last playable track before one
  that cannot be played. Skipping to an unplayable item ends in silence, not the old track.
- **A stop is only reported for an item that was reported started.** An item paused or
  skipped before any of it was heard is never announced.
- **Not supported:** SyncPlay (declined), repeat, shuffle, and everything that needs a
  screen (menus, fullscreen, screenshots, track selection), which are accepted and ignored.

Two settings, in `conf.py` and `docs/configuration.md`: `sink_device` (the output device as
PortAudio names it) and `sink_ram_limit_mb` (default 100).

Tests: `tests/test_sink_player.py`, 17 tests with the real engine, a fake sound card and a
stand-in queue that, like the real one, builds a new Video on every `get_next()`.

Checked on the Pi with the real `Media`/`Video` classes, the stored login, the real server
and the Modi+: a two-track queue started 10 s before the end of track 1. The current item
moved to track 2 at the join, with no underruns or device status flags, and what reached the
DAC was bit-exact across the join. A pause, unpause and position read during track 2 behaved.
The running service was not touched.

Seen in that run and relevant to step 5: "started" is reported about 1.2 s before the track
is heard (the device buffer plus PortAudio's lead-in).

## Step 5 results (2026-11-16)

`AudioSinkManager` now tells the server what is playing, with the same three reports and
the same body as the mpv player (`session_playing`, `session_progress`, `session_stop`).
The code is its own, in `sink_player.py`, not `ReportingMixin`: that mixin reads mpv
properties throughout. It reuses `SessionReporter`, the app's ordered report queue, so a
stop cannot overtake the start that follows it and no report sits between two tracks.

| When | Report | Position sent |
|---|---|---|
| A cast starts and is first handed to the device | playing | the offset the cast asked for |
| Gapless advance | stop for the old track, then playing for the new | old: its full length; new: 0 |
| Every timeline tick, and on pause / seek / volume | progress | as heard (device delay removed) |
| Stop, or replaced by another cast | stop | where it was |
| Next / previous / skip-to | stop | where it was, not the full length |
| The queue runs out | stop | full length |

- **Played marks:** a track that plays out is reported at its full length, which is what
  makes the server count it. With `force_set_played` on, an explicit mark follows the stop
  on the same queue. A track stopped or skipped part-way is never marked.
- **Progress waits for the start.** No progress report is sent until the server has been
  told the track started.
- **A failing server does not stop the music.** Start and stop reports run on the report
  queue, which swallows failures. Progress is sent on the caller's thread, as the mpv
  player's is, and the timeline thread absorbs a failure there.
- **Quitting delivers the last stop**: `terminate()` drains the queue.
- **Not carried over from the mpv player:** the offline catalog mirror, Discord presence,
  and the browser's "stopped at" callback. None applies to a headless sink.

Tests: `tests/test_sink_player.py` is now 27 tests; the ten new ones run against a stand-in
server that records every report. 25 consecutive runs passed.

Checked on the Pi against the real server, reading the session back from `/Sessions`:

| Moment | Server showed |
|---|---|
| During track 1 | track 1, 126.3 s, not paused, volume 10 |
| Just after the join | track 2, 0.3 s |
| 4 s later | track 2, 5.0 s |
| After a pause | paused |
| After stop | nothing playing |

Track 1's play count went from 3 to 4. Track 2's also went up by one although it was
stopped after about 7 s; why the server counted that was not looked into.

Still open: "now playing" changes about 1.2 s before the new track is heard (it is sent
when the track is handed to the device). The position reported is corrected for the device
delay; the moment of the change is not.

## Risks

- **Memory.** Up to two tracks are held in RAM, each up to the configured limit. Larger
  tracks are streamed and may gap at the join. A 20-minute 24-bit/96 kHz track is several
  hundred MB and will always be streamed on the Pi.
- **Slow links.** If the next track has not started downloading in time there is a gap
  anyway; the output must write silence rather than stall or close.
- **Server-side transcodes.** If the server decides to transcode a FLAC (bitrate limit,
  remote connection), the response is no longer the original file. Sink mode should request
  direct play and log loudly when it does not get it.
- **Reporting drift.** The server's idea of the current track must follow what is audible,
  not what is downloaded.
- **Scope.** Volume, mute, seek, pause and MPRIS all have to be reimplemented for the sink;
  mpv provided them for free.
