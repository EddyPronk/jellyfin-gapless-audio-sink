"""A stand-in sound card for the native audio tests."""
import threading
import time

import numpy as np


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
