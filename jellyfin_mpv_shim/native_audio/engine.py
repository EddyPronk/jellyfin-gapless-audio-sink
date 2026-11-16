"""Decodes one track after another into a single output stream.

Three threads:

* the **decode** thread reads PCM and queues it, a few seconds ahead of the
  sound card, moving straight on to the next track when one ends;
* the **audio** thread (the device's callback, ``_fill``) copies queued PCM to
  the device and notices when it crosses into a new track;
* the **event** thread turns what the audio thread noticed into the caller's
  callbacks, so those never run on the audio thread.

Every seek, skip or stop bumps a generation number. Queued audio and events
carry the generation they were made under and are dropped once it is stale,
which is what lets the control methods return at once.
"""
import logging
import queue
import threading

import numpy as np

from .decoder import Decoder

log = logging.getLogger("native_audio.engine")

BLOCK = 4096            # frames per decoded block
QUEUE_BLOCKS = 32       # decoded audio held ahead of the device
PREROLL_BLOCKS = 8      # queued before the device is opened
TAIL_SECONDS = 0.5      # silence before a stream is stopped; see _pad


class Track:
    """One item to play. ``key`` is the caller's own handle for it."""

    def __init__(self, source, key=None):
        self.source = source
        self.key = key
        self.rate = self.channels = self.frames = self.dtype = None

    @property
    def duration(self):
        return self.frames / self.rate if self.rate else None

    def __repr__(self):
        return "Track(%r)" % (self.key if self.key is not None
                              else self.source,)


def apply_gain(data, gain):
    """Digital volume. Integer samples are rounded, so it is repeatable."""
    if gain == 1.0:
        return data
    if data.dtype.kind == "f":
        return data * data.dtype.type(gain)
    return np.round(data.astype(np.float64) * gain).astype(data.dtype)


class _Stale(Exception):
    """The generation moved on; whatever was being done no longer matters."""


class _Block:
    __slots__ = ("gen", "kind", "track", "start", "data")

    def __init__(self, gen, kind, track=None, start=0, data=None):
        self.gen, self.kind, self.track = gen, kind, track
        self.start, self.data = start, data


class GaplessPlayer:
    """Plays Tracks back to back with no gap between them.

    ``next_track(track)`` is asked what follows ``track``: once early, to
    start downloading it, and again when ``track`` ends, which is the answer
    that counts. Return the same Track object both times if nothing changed,
    and None at the end of the queue. Call ``invalidate_next()`` when the
    answer changes.

    Callbacks, all on the event thread:

    * ``on_track_started(track, previous, gapless)`` when ``track`` becomes
      audible; ``gapless`` is True when it followed ``previous`` on its own;
    * ``on_finished(track)`` when the last track has played out;
    * ``on_error(track, exception)`` when a track could not be played.
    """

    def __init__(self, next_track=None, output_factory=None,
                 on_track_started=None, on_finished=None, on_error=None):
        if output_factory is None:
            from .output import Output as output_factory
        self._factory = output_factory
        self._next_track = next_track or (lambda track: None)
        self.on_track_started = on_track_started
        self.on_finished = on_finished
        self.on_error = on_error

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._gen = 0
        self._job = None                    # (gen, track, start seconds)
        self._closing = False
        self._q = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._events = queue.Queue()
        self._drained = threading.Event()

        self._out = None
        self._out_fmt = None
        self._owned = []                    # tracks whose sources we started
        self._pending = None                # asked for, not audible yet
        self._decoding = None               # what the decode thread is on
        self._repeek = False

        # Touched by the audio thread.
        self._left = None
        self._off = 0
        self._audible = None
        self._audible_gen = -1
        self._frame = 0
        self._hold_gen = -1                 # generation that has played out
        self._paused = False
        self._muted = False
        self._gain = 1.0
        self.underrun_frames = 0
        self._flags = 0

        for name, target in (("decode", self._decode_loop),
                             ("events", self._event_loop)):
            threading.Thread(target=target, daemon=True,
                             name="native-audio-" + name).start()

    # -- control -----------------------------------------------------------

    def play(self, track, start=0.0):
        """Play ``track`` now, replacing whatever is playing.

        ``start`` is in seconds; negative counts back from the end.
        """
        with self._lock:
            self._begin(track, start)
            self._paused = False

    def seek(self, seconds):
        with self._lock:
            track = self._audible or self._pending
            if track is not None:
                seconds = max(0.0, seconds)
                self._begin(track, seconds)
                if track.rate:              # report the target meanwhile
                    self._frame = int(seconds * track.rate)

    def invalidate_next(self):
        """What follows the current track has changed."""
        with self._lock:
            track = self._audible
            if track is not None and self._decoding is not track:
                # Already decoding the old successor: redo from here.
                self._begin(track, self.position)
            else:
                self._repeek = True

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    @property
    def paused(self):
        return self._paused

    def stop(self):
        with self._lock:
            self._gen += 1
            self._job = None
            self._flush()
            self._audible = self._pending = None
            self._paused = False
            out = self._take_output()
            self._release(keep=())
        if out is not None:
            out.close(drain=False)

    def close(self):
        self.stop()
        with self._lock:
            self._closing = True
            self._cond.notify_all()
        self._events.put(None)

    @property
    def volume(self):
        return self._gain

    @volume.setter
    def volume(self, value):
        self._gain = min(1.0, max(0.0, float(value)))

    @property
    def muted(self):
        return self._muted

    @muted.setter
    def muted(self, value):
        self._muted = bool(value)

    @property
    def current(self):
        return self._audible or self._pending

    @property
    def playing(self):
        return self.current is not None and not self._paused

    @property
    def position(self):
        """Seconds into the current track, as heard (device delay removed)."""
        track, out = self._audible, self._out
        if track is None or not track.rate:
            return 0.0
        delay = out.latency if out is not None else 0.0
        return max(0.0, self._frame / track.rate - delay)

    @property
    def status_flags(self):
        """Times the device reported running dry, over every stream so far."""
        out = self._out
        return self._flags + (out.status_flags if out is not None else 0)

    def _begin(self, track, start):
        self._gen += 1
        self._flush()
        if track is not self._audible:
            self._audible = None
            self._frame = 0
            self._release(keep=(track,))    # incl. a download now unwanted
        self._pending = track
        self._job = (self._gen, track, start)
        self._cond.notify_all()

    def _flush(self):
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._drained.set()                 # nobody is waiting on old audio

    def _release(self, keep):
        for track in self._owned:
            if not any(track is k for k in keep):
                track.source.discard()
        self._owned = [t for t in self._owned if any(t is k for k in keep)]

    def _own(self, track):
        with self._lock:
            if not any(track is t for t in self._owned):
                self._owned.append(track)

    # -- output ------------------------------------------------------------

    def _open_output(self, gen, fmt):
        if self._out is not None:
            return
        self._check(gen)
        rate, channels, dtype = fmt
        # Not under the lock: a busy device is retried for a while, and
        # stop() must not have to wait for that.
        out = self._factory(rate, channels, dtype, self._fill)
        with self._lock:
            if gen == self._gen:
                self._out, self._out_fmt = out, fmt
                out.start()
                return
        out.close(drain=False)
        raise _Stale()

    def _take_output(self):
        """Detach the stream; the caller closes it, outside the lock."""
        with self._lock:
            out, self._out = self._out, None
            if out is not None:
                self._flags += getattr(out, "status_flags", 0)
            return out

    def _close_output(self, drain=True):
        out = self._take_output()
        if out is not None:
            out.close(drain)

    # -- decode thread -----------------------------------------------------

    def _check(self, gen):
        if gen != self._gen:
            raise _Stale()

    def _push(self, block):
        while True:
            self._check(block.gen)
            try:
                self._q.put(block, timeout=0.1)
                return
            except queue.Full:
                pass

    def _pad(self, gen, fmt):
        # Stopping a stream discards what the device has not played yet
        # (38 ms, when measured), so real audio is followed by silence.
        rate, channels, dtype = fmt
        self._push(_Block(gen, "pad", data=np.zeros(
            (int(rate * TAIL_SECONDS), channels), dtype=dtype)))

    def _decode_loop(self):
        while True:
            with self._cond:
                self._cond.wait_for(
                    lambda: self._job is not None or self._closing)
                if self._closing:
                    return
                gen, track, start = self._job
                self._job = None
            try:
                self._decode_from(gen, track, start)
            except _Stale:
                pass
            except Exception as e:
                if gen == self._gen:        # else: cancelled under us
                    log.exception("playback of %r failed", self._decoding)
                    self._events.put(("error", gen, self._decoding, e))
            finally:
                self._decoding = None

    def _decode_from(self, gen, track, start):
        fmt = None
        while track is not None:
            self._check(gen)
            self._decoding = track
            self._own(track)
            track.source.start()
            dec = Decoder(track.source)
            try:
                self._check(gen)
                track.rate, track.channels = dec.rate, dec.channels
                track.frames, track.dtype = dec.frames, dec.dtype
                if self._out is not None and self._out_fmt != dec.format:
                    self._change_format(gen)
                fmt = dec.format
                pos = int(start * dec.rate)
                if pos < 0:
                    pos += dec.frames
                pos = dec.seek(pos)
                peeked, self._repeek = None, True
                while True:
                    if self._repeek and track.source.done:
                        # One download at a time: the next track starts
                        # arriving once this one is complete.
                        self._repeek = False
                        nxt = self._next_track(track)
                        if peeked is not None and peeked is not nxt:
                            peeked.source.cancel()
                        peeked = nxt
                        if nxt is not None:
                            self._own(nxt)
                            nxt.source.start()
                    data = dec.read(BLOCK)
                    if not len(data):
                        break
                    self._push(_Block(gen, "pcm", track, pos, data))
                    pos += len(data)
                    if self._out is None and \
                            self._q.qsize() >= PREROLL_BLOCKS:
                        self._open_output(gen, fmt)
            finally:
                dec.close()
            if track.source.error is not None:
                raise track.source.error
            nxt = self._next_track(track)
            if peeked is not None and peeked is not nxt:
                peeked.source.cancel()
            last, track, start = track, nxt, 0.0
        self._pad(gen, fmt)
        self._push(_Block(gen, "end", last))
        self._open_output(gen, fmt)         # a track shorter than the preroll

    def _change_format(self, gen):
        """The next track needs a different stream: play out, then reopen."""
        self._pad(gen, self._out_fmt)
        self._drained.clear()
        self._push(_Block(gen, "drain"))
        while not self._drained.wait(0.1):
            self._check(gen)
        self._check(gen)
        self._close_output()
        self._hold_gen = -1                 # same generation, new stream

    # -- audio thread ------------------------------------------------------

    def _fill(self, out):
        """Fill ``out``; returns how many frames of it came from the queue."""
        frames, filled, gen = len(out), 0, self._gen
        if not self._paused and self._hold_gen != gen:
            while filled < frames:
                block = self._left
                if block is None:
                    try:
                        block = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if block.gen != gen:
                        continue
                    if block.kind == "drain":
                        self._hold_gen = gen
                        self._drained.set()
                        break
                    if block.kind == "end":
                        self._hold_gen = gen
                        self._events.put(("finished", gen, block.track))
                        break
                    if block.kind == "pcm" and block.track is not self._audible:
                        previous, self._audible = self._audible, block.track
                        self._events.put((
                            "track", gen, block.track, previous,
                            self._audible_gen == gen))
                    self._audible_gen = gen
                    self._off = 0
                elif block.gen != gen:
                    self._left = None
                    continue
                n = min(len(block.data) - self._off, frames - filled)
                out[filled:filled + n] = block.data[self._off:self._off + n]
                filled += n
                self._off += n
                if block.kind == "pcm":
                    self._frame = block.start + self._off
                self._left = block if self._off < len(block.data) else None
            if filled < frames and self._hold_gen != gen \
                    and self._audible is not None:
                self.underrun_frames += frames - filled
        out[filled:] = 0
        if filled:
            if self._muted:
                out[:filled] = 0
            elif self._gain != 1.0:
                out[:filled] = apply_gain(out[:filled], self._gain)
        return filled

    # -- event thread ------------------------------------------------------

    def _event_loop(self):
        while True:
            event = self._events.get()
            if event is None:
                return
            try:
                self._handle(*event)
            except Exception:
                log.exception("event handler failed")

    def _handle(self, kind, gen, track, *rest):
        out, drain = None, True
        with self._lock:
            if gen != self._gen:
                return
            if kind == "track":
                self._pending = None
                previous = rest[0]          # it has just left: free its file
                self._release([t for t in self._owned if t is not previous]
                              + [track])
            else:
                if kind == "error":
                    self._gen += 1
                    self._flush()
                    drain = False
                out = self._take_output()
                self._audible = self._pending = None
                self._release(keep=())
        if out is not None:
            out.close(drain)
        callback = {"track": self.on_track_started,
                    "finished": self.on_finished,
                    "error": self.on_error}[kind]
        if callback is not None:
            callback(track, *rest)
