"""The player for a headless audio sink: cast commands in, gapless FLAC out.

``AudioSinkManager`` stands where ``PlayerManager`` does -- the same method
names, called by the same event handler and timeline thread -- but drives
``native_audio`` instead of mpv, and never imports mpv.

What it does on top of the engine is keep the app's queue (``Media`` /
``Video``) and the engine's tracks in step:

* ``play()`` resolves a Video to a track (``sink_source.native_track``) and
  starts it;
* the engine asks ``_following()`` what comes after a track, twice -- early,
  to fetch it ahead, and at the end, for real. Building the next Video costs a
  server request and the engine wants the same object both times, so the
  answer is cached, and dropped when the queue is edited;
* when the engine reports that the next track has become audible, the
  manager's "current video" moves with it. That is the gapless advance: no
  ``play()`` call is involved.

Only FLAC that the server hands over unmodified can be played. Anything else
is refused and logged, and a queue stops at the first such track.

Not here yet: reporting to the server (``_report_*`` and ``send_timeline``
are hooks for it), SyncPlay, repeat and shuffle.
"""
import logging
import threading

from .conf import settings
from .native_audio.engine import GaplessPlayer
from .sink_source import NotNative, native_track

log = logging.getLogger("sink_player")


class _NoSyncPlay:
    """SyncPlay is not supported by the sink; every question is answered no."""

    client = None

    def in_group(self):
        return False

    def is_enabled(self):
        return False

    def process_group_update(self, _arguments):
        log.info("Ignoring a SyncPlay group update: not supported by the "
                 "audio sink.")

    def process_command(self, _arguments):
        pass


class _NoMenu:
    is_menu_shown = False


class AudioSinkManager:
    def __init__(self, engine_factory=GaplessPlayer, resolve=native_track,
                 output_factory=None):
        self._lock = threading.RLock()
        self._resolve = resolve
        self._video = None
        #: Track -> the Track that follows it (or None); see _following.
        self._next = {}
        self._track = None                  # the engine's track for _video
        self._announced = None              # the Video reported as started
        self.syncplay = _NoSyncPlay()
        self.menu = _NoMenu()
        # Set by the app once its threads exist; poked when state changes.
        self.timeline_trigger = None
        self.action_trigger = None
        self.on_reopen_failed = None
        self.on_playstate = None
        self.on_stopped_at = None
        self.repeat_mode = "none"

        if output_factory is None:
            output_factory = self._open_output
        self._engine = engine_factory(
            next_track=self._following,
            output_factory=output_factory,
            on_track_started=self._on_track_started,
            on_finished=self._on_finished,
            on_error=self._on_error)
        self._engine.volume = min(100, max(0, settings.music_volume)) / 100

    @staticmethod
    def _open_output(rate, channels, dtype, fill):
        from .native_audio.output import Output

        return Output(rate, channels, dtype, fill,
                      device=settings.sink_device or None)

    @staticmethod
    def _ram_limit():
        return max(0, int(settings.sink_ram_limit_mb)) << 20

    def _poke(self):
        trigger = self.timeline_trigger
        if trigger is not None:
            trigger.set()

    # -- starting and stopping ----------------------------------------------

    def play(self, video, offset=0, **_ignored):
        """Play ``video`` now, from ``offset`` seconds. False if it cannot.

        The extra keyword arguments are the mpv player's (photos, track
        memory, SyncPlay); none applies to audio here.
        """
        if video is None:
            return False
        with self._lock:
            track = self._known_track(video)
        if track is None:
            try:
                track = self._resolve(video, self._ram_limit())
            except NotNative as e:
                log.error("Cannot play this on the audio sink: %s", e)
                return False
            except Exception:
                log.exception("Could not get a stream for %s",
                              getattr(video, "item_id", video))
                return False
        with self._lock:
            previous = self._video
            if previous is not None and previous is not video:
                self._stopped(previous, finished=False)
            self._video, self._track = video, track
            self._next = {}
            self._engine.play(track, offset or 0)
        self._poke()
        return True

    def _known_track(self, video):
        """A track already resolved for ``video`` -- usually because it was
        the next one, in which case its download is under way or done."""
        for track in self._next.values():
            if track is not None and track.key is video:
                return track
        return None

    def stop(self, *_args, **_kwargs):
        with self._lock:
            video, self._video, self._track = self._video, None, None
            self._next = {}
            self._engine.stop()
            if video is not None:
                self._stopped(video, finished=False)
        self._poke()

    def terminate(self):
        self.stop()
        self._engine.close()

    # The mpv player's names for the same thing.
    stop_and_close = stop
    stop_to_browser = stop

    # -- the queue ------------------------------------------------------------

    def _following(self, track):
        """What plays after ``track``. Called on the engine's decode thread."""
        if not settings.auto_play:
            return None
        with self._lock:
            if track in self._next:
                return self._next[track]
        video = track.key
        media = getattr(video, "parent", None)
        following = None
        if media is not None and media.has_next:
            try:
                nxt = media.get_next().video    # a server request
                if nxt is not None:
                    following = self._resolve(nxt, self._ram_limit())
            except NotNative as e:
                log.warning("The queue stops after this track: %s", e)
            except Exception:
                log.exception("Could not prepare the next track")
        with self._lock:
            # A queue edit while we were asking: do not cache a stale answer.
            if self._track is track or track in self._next.values():
                self._next[track] = following
        return following

    def upd_player_hide(self):
        """The queue was edited (the mpv player's name: it refreshed its
        on-screen controls). Here: forget what was going to play next."""
        with self._lock:
            self._next = {}
            self._engine.invalidate_next()

    queue_changed = upd_player_hide

    def play_next(self):
        video = self._video
        if video is None or not video.parent.has_next:
            return False
        with self._lock:
            track = self._next.get(self._track)
        nxt = track.key if track is not None else video.parent.get_next().video
        return self._advance_to(video, nxt)

    def play_prev(self):
        video = self._video
        if video is None or not video.parent.has_prev:
            return False
        return self._advance_to(video, video.parent.get_prev().video)

    def skip_to(self, key):
        video = self._video
        media = video.parent.get_from_key(key) if video is not None else None
        if media is None:
            return False
        return self._advance_to(video, media.get_video(0))

    def _advance_to(self, video, new_video):
        if new_video is None:
            return False
        with self._lock:
            # Skipped, so it counts as finished -- as the mpv player reports.
            self._stopped(video, finished=True)
            self._video = None              # already reported
            if self.play(new_video):
                return True
            # The old track has been reported finished and the new one
            # cannot be played: silence, not the old track carrying on.
            self._track, self._next = None, {}
            self._engine.stop()
        self._poke()
        return False

    # -- engine callbacks (its event thread) ----------------------------------

    def _on_track_started(self, track, previous, gapless):
        with self._lock:
            if gapless:
                if previous is not self._track:
                    return                  # overtaken by a newer play()
                # The engine moved on by itself: follow it.
                self._next.pop(previous, None)
                if previous is not None:
                    self._stopped(previous.key, finished=True)
                self._video, self._track = track.key, track
            elif track is not self._track:
                return                      # overtaken by a newer play()
            self._started(track.key)
        self._poke()

    def _on_finished(self, track):
        with self._lock:
            if track is not self._track:
                return
            video, self._video, self._track = self._video, None, None
            self._next = {}
            if video is not None:
                self._stopped(video, finished=True)
        self._poke()

    def _on_error(self, track, error):
        log.error("Playback failed: %s", error)
        with self._lock:
            if track is not None and track is not self._track \
                    and track not in self._next.values():
                return
            video, self._video, self._track = self._video, None, None
            self._next = {}
            if video is not None:
                self._stopped(video, finished=False)
        self._poke()

    # -- transport ------------------------------------------------------------

    def toggle_pause(self):
        if self._video is not None:
            if self._engine.paused:
                self._engine.resume()
            else:
                self._engine.pause()
            self._poke()

    def pause_if_playing(self):
        if self._video is not None and not self._engine.paused:
            self._engine.pause()
        self._poke()

    def play_if_paused(self):
        if self._video is not None and self._engine.paused:
            self._engine.resume()
        self._poke()

    def set_paused(self, paused):
        if paused:
            self.pause_if_playing()
        else:
            self.play_if_paused()

    def seek(self, offset, absolute=False, **_ignored):
        if self._video is None:
            return
        target = offset if absolute else self._engine.position + offset
        self._engine.seek(max(0.0, target))
        self._poke()

    def set_volume(self, pct, notify=True):
        self._engine.volume = pct / 100
        if notify:
            self._poke()

    def get_volume(self, percent=False):
        volume = self._engine.volume
        return round(volume * 100) if percent else volume

    def set_mute(self, muted):
        self._engine.muted = bool(muted)
        self._poke()

    # -- state ----------------------------------------------------------------

    def get_video(self):
        return self._video

    def has_video(self):
        return self._video is not None

    def is_active(self):
        return self._video is not None

    def is_paused(self):
        return self._video is not None and self._engine.paused

    def is_playing(self):
        return self._video is not None and not self._engine.paused

    def get_state(self):
        if self._video is None:
            return "stopped"
        return "paused" if self._engine.paused else "playing"

    @property
    def position(self):
        """Seconds into the current track, as heard."""
        return self._engine.position if self._video is not None else 0.0

    @property
    def underrun_frames(self):
        return self._engine.underrun_frames

    def get_queue_ids(self):
        video = self._video
        if video is None:
            return []
        return [q.get("Id") for q in video.parent.queue if q.get("Id")]

    # -- reporting: filled in by the next step --------------------------------

    def _started(self, video):
        self._announced = video
        self._report_started(video)

    def _stopped(self, video, finished):
        # Only for an item that was announced: one paused or skipped before
        # any of it was heard never started, as far as the server knows.
        if self._announced is video:
            self._announced = None
            self._report_stopped(video, finished)

    def _report_started(self, video):
        """``video`` has become audible."""

    def _report_stopped(self, video, finished):
        """``video`` is no longer playing; ``finished`` if it ran to its end
        or was skipped past."""

    def send_timeline(self):
        """Progress report; called by the timeline thread every few seconds."""

    # -- parts of the mpv player's surface that mean nothing without a screen -

    def update(self):
        """The action thread's pump. The engine has its own threads."""

    def playback_starting(self):
        pass

    def playback_started(self):
        pass

    def push_playstate(self, stopped=False):
        pass

    def _maybe_save_volume(self):
        pass

    def idle_quit(self, *_args, **_kwargs):
        pass

    def menu_action(self, _action):
        pass

    def toggle_fullscreen(self):
        pass

    def screenshot(self):
        pass

    def set_streams(self, _aid, _sid):
        pass
