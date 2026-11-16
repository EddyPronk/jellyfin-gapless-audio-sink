# Gapless playback: current status

Checked 2026-11-16 against `master` at `9d3b0fb6` (client version 3.3.0).

**Question:** can jellyfin-mpv-shim run on a Raspberry Pi as a headless audio sink and play
live albums gaplessly, the way Plexamp does?

**Answer: no.** The current code does not do gapless playback, and upstream says so itself.
Running it as a display-less cast target is supported; gapless track transitions are not.

How this was established: reading the code and docs in this checkout, plus the online
sources listed at the bottom. Nothing here was measured by ear or on a Pi.

## What upstream says

- `README.md`, Known issues: "Music has no gapless playback."
- The PyPI project page says repeat, shuffle and gapless playback "would require major
  changes to the application to properly support, as it was built for video". Repeat and
  shuffle have since been added for music queues (v3.2.0 release notes); gapless has not.
- The winget package description says gapless playback is not planned.
- A search of the GitHub issue tracker for "gapless" returns three issues, none of them a
  gapless feature request by title (#452 repeat mode, #136, #15). The issue bodies were not
  read.

## Why the code cannot be gapless today

The queue lives in Python, and mpv is only ever given one file at a time.

1. `docs/mpv-backends.md` ("The queue"): "`Media` owns the queue in Python; mpv's playlist
   holds the one item that is playing." This is a deliberate design choice, to avoid keeping
   two copies of the playback state in step across transcode restarts, queue edits and
   SyncPlay.
2. The next track is started only after the current one has ended:
   - `jellyfin_mpv_shim/player.py:1947` `_on_eof_reached` sees mpv's `eof-reached` and
     queues `finished_callback`.
   - `jellyfin_mpv_shim/player.py:4260` `finished_callback` reports the stop to the server,
     takes the next item from the Python queue and calls `self.play(new_video)`.
   - `jellyfin_mpv_shim/player.py:3340` `_play_media` loads the new URL with
     `self._player.play(self.url)`, then waits for `duration` to become available.
3. So every track boundary includes an event round trip through Python, a stop report, and
   opening a new HTTP stream from cold. Nothing preloads or pre-buffers the next track.
4. Nothing in the package sets `gapless-audio` or `prefetch-playlist`, and neither would
   help as things stand: both act on mpv's own playlist, which here has one entry.

mpv's manual describes the limit directly. `--gapless-audio` (default `weak`) "relies on
audio output device buffering to continue playback while moving from one file to another",
and if the new file starts slowly, for example from a network location, the buffered audio
can run out first and leave a gap. That is the situation at every track change here.

Not determined: whether mpv keeps the audio device open across these transitions, and how
long the gap is in practice. It will vary with the network and the server.

## Running it without a display

Two different settings, easily confused (`docs/configuration.md`, "Startup and background"):

| Setting | What it does |
|---|---|
| `enable_gui: false` | Command-line mode: no window, no tray, no Settings. This is the one for a Pi with no screen. The only way to change settings afterwards is editing `conf.json`. |
| `headless: true` | Cast-target mode: still opens a window, showing "Ready to cast" instead of the library. Meant for a shared TV, not a display-less box. |

For music the app already sets `audio-display=no` (`jellyfin_mpv_shim/mpv_options.py:663`),
so cover art does not open a video window. Audio device and exclusive mode are under
**Audio** in `docs/configuration.md`; anything else goes in `mpv.conf` beside `conf.json`.

This setup was not tried on a Raspberry Pi as part of this check.

## What gapless would take

Roughly: hand mpv the next track before the current one ends (`loadfile <url> append`), turn
on `prefetch-playlist`, and let mpv make the transition itself, with Python following the
`playlist-pos` change instead of driving it. Server reporting, track memory, SyncPlay and
transcode restarts all currently assume Python starts each file, which is the "major
changes" upstream refers to.

Two limits would remain even then:

- `gapless-audio=weak` only holds across files with the same audio format. Tracks of one
  album normally qualify.
- The tracks must be direct-played or losslessly remuxed. A lossy transcode of each track
  adds encoder padding at the boundaries.

## Alternatives seen while searching

None of these were tested, and none is confirmed as a headless cast target:

- **jfmp**: a Python "minimalist cross-platform gapless music player for Jellyfin" on PyPI,
  at version 0.0.1.
- **Feishin**: desktop music client; a Jellyfin forum thread reports gapless works when it
  uses mpv as its playback engine.
- **Gelly**: Linux desktop music player for Jellyfin that lists gapless playback.

## Sources

- [jellyfin-mpv-shim on PyPI](https://pypi.org/project/jellyfin-mpv-shim)
- [winget package listing](https://winget.ragerworks.com/package/IanWalton.JellyfinMPVShim)
- [v3.2.0 release notes](https://newreleases.io/project/github/jellyfin/jellyfin-mpv-shim/release/v3.2.0)
- [GitHub issue search for "gapless"](https://github.com/jellyfin/jellyfin-mpv-shim/issues?q=gapless)
- [mpv manual](https://mpv.io/manual/master/) (`--gapless-audio`, `--prefetch-playlist`, `--audio-display`)
- [Jellyfin forum: Jellyfin Media Player gapless support](https://forum.jellyfin.org/t-jellyfin-media-player-gapless-support)
- [jfmp on PyPI](https://pypi.org/project/jfmp/0.0.1/)
- [Gelly write-up, OMG! Ubuntu](https://www.omgubuntu.co.uk/2026/10/gelly-jellyfin-desktop-music-player-linux)
