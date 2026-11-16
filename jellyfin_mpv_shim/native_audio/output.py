"""The sound card: one output stream, pulled from by a callback."""
import logging
import time

log = logging.getLogger("native_audio.output")


class Output:
    """``fill(out)`` is called from the audio thread with an array to fill.

    Defaults come from measurements in docs/native-flac-plan.md: a one-second
    device buffer and a fixed block size, because the callback is Python and
    shares the interpreter with the download and decode threads.
    """

    def __init__(self, rate, channels, dtype, fill, device=None, latency=1.0,
                 blocksize=4096, busy_wait=30.0):
        import sounddevice as sd

        self.status_flags = 0       # times the device ran dry or overflowed

        def callback(out, _frames, _time, status):
            if status:
                self.status_flags += 1
            fill(out)

        deadline = time.monotonic() + busy_wait
        while True:
            try:
                self._stream = sd.OutputStream(
                    samplerate=rate, channels=channels, dtype=dtype,
                    device=device, latency=latency, blocksize=blocksize,
                    dither_off=True, callback=callback)
                break
            except sd.PortAudioError as e:
                # Another player has the DAC (it is shared on the Pi).
                busy = "unavailable" in str(e).lower()
                if not busy or time.monotonic() > deadline:
                    raise
                log.info("output device busy, retrying")
                time.sleep(0.5)

    @property
    def latency(self):
        return self._stream.latency

    def start(self):
        self._stream.start()

    def close(self, drain=True):
        try:
            if drain:
                self._stream.stop()
            else:
                self._stream.abort()
        finally:
            self._stream.close()
