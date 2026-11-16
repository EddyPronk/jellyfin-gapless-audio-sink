# Jellyfin Gapless Audio Sink (client)

A headless Jellyfin client that turns a Raspberry Pi with a DAC into an audio output you
cast to. Pick it as the player in the Jellyfin web or mobile app and the music plays on the
Pi, with **no gap between tracks**: live albums, classical works and mixes play through as
one piece.

- **Gapless**: consecutive tracks are decoded into one continuous audio stream. Nothing is
  closed or reopened at a track boundary.
- **Bit-perfect**: 16-bit FLAC reaches a 16-bit DAC unmodified when the volume is at 100.
- **Small**: about 52 MB of memory when idle and about 6% of one core while playing, on a
  Raspberry Pi 3 Model A+.
- **Controlled from Jellyfin**: play, pause, seek, next, previous, volume, mute, and "play
  next" / "add to queue", from any Jellyfin app. What is playing, and how far in, is
  reported back to the server.
- **No disk writes for audio**: tracks are held in memory or streamed, never cached to the
  SD card.

## About this fork

This project is a fork of [Jellyfin MPV Shim](https://github.com/jellyfin/jellyfin-mpv-shim),
the full-featured Jellyfin desktop client by Izzie Walton and its contributors. Nearly
everything here is their work: the server connection, login, casting, the play queue and
session reporting are all theirs, and this fork would not exist without it. Thank you.

Jellyfin MPV Shim plays media with MPV, which is given one file at a time, so music has a
short gap at every track change. Upstream says so itself: its README lists "Music has no
gapless playback" under
[Known issues](https://github.com/jellyfin/jellyfin-mpv-shim#known-issues). Upstream
describes gapless playback as needing major changes to an application that was built for
video, which is a fair assessment. We wanted
one thing, gapless music on a small headless box, so we added a second playback path for
audio that decodes FLAC itself and never loads MPV. That work was done on the branch
[`native-flac-sink`](https://github.com/EddyPronk/jellyfin-gapless-audio-sink/tree/native-flac-sink), which keeps its full history; `main`
continues from it.

We would like to work with upstream, not apart from it. If the maintainers find any of
this useful, in whole or in part, we would be glad to adapt it and contribute it back, and
we will keep this fork close enough to upstream to make that practical. Until then, please
report problems with *this* fork here, not to the upstream project.

Two things follow from being a fork, and you will notice both:

- **Names are inherited.** The command, the Python package, the service and the
  configuration folder are still called `jellyfin-mpv-shim`. Renaming them would make it
  much harder to follow upstream.
- **There are leftovers.** The upstream code for the desktop interface, video and other
  platforms is still in the repository, and so is its documentation under `docs/`. This
  README describes only what this fork is for.

The license is unchanged: GPL-3.0-or-later, see [LICENSE.md](LICENSE.md).

## What it supports

| | |
|---|---|
| Platform | Linux. Tested **only** on a Raspberry Pi 3 Model A+ (512 MB), Debian 13, with a USB DAC (Schiit Modi+) on plain ALSA. |
| Server | Jellyfin. Tested with Jellyfin 12.4.0 on the same network. |
| Audio | **FLAC only**, and only when the server hands the file over unmodified (direct play). |
| Tested by measurement | 16-bit 44.1 kHz: the samples sent to the DAC were compared with the FLAC files and were identical across track changes. |
| Written but not tried on hardware | 24-bit and high sample rates; a change of sample rate between tracks (the output is reopened, with a gap). |

Not supported:

- Any format other than FLAC. Such a track is refused and logged, and a queue stops before
  the first track it cannot play.
- Transcoding. If the server would transcode a track, it is refused.
- Video, photos, books, Live TV, a window, a library browser or a settings screen.
- SyncPlay, repeat and shuffle.
- Windows and macOS.

Other Linux machines and other Pi models may well work. We have not tried them.

## Installation

On Raspberry Pi OS or Debian:

```bash
sudo apt install libportaudio2 git

git clone https://github.com/EddyPronk/jellyfin-gapless-audio-sink.git
cd jellyfin-gapless-audio-sink
python3 -m venv .venv
.venv/bin/pip install -e '.[sink]'
```

We installed ours with `uv`; the `venv` and `pip` commands above are the equivalent and
have not been run exactly as written.

Before the first start, create `~/.config/jellyfin-mpv-shim/conf.json`:

```json
{
    "audio_sink": true,
    "sink_device": "Schiit Modi+",
    "sink_ram_limit_mb": 40,
    "player_name": "living-room"
}
```

- `audio_sink` turns this fork's playback path on. Without it the application starts as
  the upstream desktop client.
- `sink_device` is your DAC, as `.venv/bin/python -m sounddevice` lists it. Leave it out
  to use the system's default output.
- `sink_ram_limit_mb` is explained under [Memory](#memory).
- `player_name` is what the Jellyfin apps show in their cast menu.

The application fills in the rest of the file. **Edit it only while the application is
stopped**: it writes its settings back and would overwrite your change. Every setting is
described in [docs/configuration.md](docs/configuration.md), most of which concerns the
upstream desktop client.

## Logging in

Once, in a terminal on the Pi:

```bash
.venv/bin/jellyfin-mpv-shim add --server http://YOUR-SERVER:8096 --quick-connect
```

It prints a code. In Jellyfin's web interface, open your user menu, choose **Quick
Connect** and enter it. When it reports that the server was added it is already running as
a player, and it also asks for another server. You do not need one: stop it from another
terminal with `.venv/bin/jellyfin-mpv-shim stop`, then start it as described below.

Without Quick Connect, run `.venv/bin/jellyfin-mpv-shim add` and answer the prompts.

## Running it

By hand:

```bash
.venv/bin/jellyfin-mpv-shim
```

As a service, in `/etc/systemd/system/jellyfin-gapless-audio-sink.service` (adjust the
user and paths):

```ini
[Unit]
Description=Jellyfin Gapless Audio Sink
Wants=network-online.target
After=network-online.target sound.target

[Service]
User=pi
ExecStart=/home/pi/jellyfin-gapless-audio-sink/.venv/bin/jellyfin-mpv-shim
ExecStop=/home/pi/jellyfin-gapless-audio-sink/.venv/bin/jellyfin-mpv-shim stop
StandardInput=null
TimeoutStopSec=15
Restart=on-failure
RestartSec=30
# Lets the player keep its memory out of swap. See "Dropouts" below.
LimitMEMLOCK=infinity

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jellyfin-gapless-audio-sink
journalctl -u jellyfin-gapless-audio-sink -f
```

Then, in any Jellyfin app, choose the player by the name you gave it and play an album.

## How it behaves

### Memory

A track is handled by its size:

| Track size | What happens | The join into it |
|---|---|---|
| Up to `sink_ram_limit_mb` | Downloaded whole into memory, ahead of time, while the previous track plays. | Gapless. |
| Larger | Streamed from the server as it plays, through a window of about 2 MB. | May have a gap if the server is slow to answer. |

Two tracks can be in memory at once, the one playing and the next, so allow for twice the
limit on top of about 60 MB for the application. On a 512 MB Pi we use 40. A 40 MB limit
holds roughly six minutes of CD-quality FLAC per track; longer tracks are streamed.

### Dropouts

On a small machine, another program wanting memory can push the player out to swap, and on
a Pi swap is usually the SD card. We saw one dropout this way, while the system's daily
package job ran in the middle of an album. With `LimitMEMLOCK=infinity` in the service
unit the player locks its memory and cannot be swapped out. It says which at startup:

```
Memory locked: this process will not be swapped out.
```

If audio does drop out, the log says so and names the track, and separately says when a
streamed track stalled on the network, so the two causes can be told apart.

### Sharing the DAC

The DAC is opened when playback starts and released when it stops. If another program is
using it, the player waits up to 30 seconds for it. While paused, the player keeps the
DAC, so other programs cannot use it until playback is stopped.

## Known issues

- "Now playing" changes in the Jellyfin apps about a second before the new track is heard.
- There is about a second and a half of silence before the first track of a cast starts.
- A track that is skipped or stopped within its first seconds may still count as a play on
  the server.
- Locking memory removes the cause we believe was behind the dropout we saw. It has not
  been tested against that situation again.
- Play counts and positions are reported to the server; nothing is kept locally, and there
  is no offline playback.

## How it works, and what was measured

[docs/native-flac-plan.md](docs/native-flac-plan.md) is the working log of this fork: the
design, each step as it was built, and the measurements behind the claims above, including
the ones that went wrong. [docs/gapless-playback.md](docs/gapless-playback.md) records why
the upstream playback path cannot be gapless.

The code this fork adds is in `jellyfin_mpv_shim/native_audio/` (fetching, decoding and
output), `sink_source.py`, `sink_player.py` and `player_select.py`, with tests in
`tests/test_native_audio.py`, `tests/test_sink_source.py`, `tests/test_sink_player.py` and
`tests/test_player_select.py`. Those tests need the `sink` extra installed and do not need
a sound card.
