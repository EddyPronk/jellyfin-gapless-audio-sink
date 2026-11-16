"""Where a track's bytes come from. Nothing is written to disk.

A track from the server is handled one of two ways, by its size:

* up to ``ram_limit`` bytes it is downloaded whole into memory, and can be
  fetched ahead of time while the previous track plays -- this is what makes
  the join gapless;
* above that it is *streamed*: read from the server as it is decoded, through
  a window of a couple of megabytes, and never held whole. It is not fetched
  ahead, so the join into it may have a gap if the server is slow to answer.

Two tracks can be in memory at once (the one playing and the next), so the
worst case is twice ``ram_limit``.
"""
import io
import logging
import os
import threading

log = logging.getLogger("native_audio.fetcher")

#: Default size up to which a track is held in memory: about 15 minutes of
#: CD-quality FLAC.
DEFAULT_RAM_LIMIT = 100 << 20

#: Streaming: how much already-read data is kept for short backward seeks
#: (the FLAC decoder makes them), and how far ahead of the window a seek may
#: land before reading on is cheaper than a new request.
_KEEP_BEHIND = 1 << 20
_SKIP_AHEAD = 256 << 10


class FetchError(IOError):
    pass


class Source:
    """A track's bytes. ``open()`` gives a seekable binary file."""

    error = None
    done = True

    def start(self):
        """Begin fetching, if there is anything to fetch. Idempotent."""

    def open(self):
        raise NotImplementedError

    def cancel(self):
        """Stop fetching. Readers blocked on it give up."""

    def discard(self):
        """Cancel and free whatever is held."""
        self.cancel()


class LocalSource(Source):
    """A file that is already on disk."""

    def __init__(self, path):
        self.path = path

    def open(self):
        return open(self.path, "rb")

    def __repr__(self):
        return "LocalSource(%r)" % (self.path,)


class HttpSource(Source):
    """A track on the server; see the module docstring for the two modes.

    The URL is never logged: it may carry an API key.
    """

    def __init__(self, url, headers=None, ram_limit=DEFAULT_RAM_LIMIT,
                 timeout=30, chunk=1 << 16, label=None):
        self._url = url
        self._headers = dict(headers or {})
        # Content-Length has to be the size of the file, not of a
        # compressed body.
        self._headers.setdefault("Accept-Encoding", "identity")
        self._ram_limit = ram_limit
        self._timeout = timeout
        self._chunk = chunk
        self.label = label or "track"
        self.mode = None            # "ram" or "stream", once the size is known
        self.total = None           # bytes, from Content-Length
        self.written = 0            # bytes in memory ("ram" mode)
        self.done = False           # nothing more will arrive on its own
        self.error = None
        self._buf = None
        self._cancelled = False
        self._thread = None
        self._cond = threading.Condition()

    def __repr__(self):
        return "HttpSource(%s, %s, %s bytes)" % (
            self.label, self.mode, self.total)

    def request(self, start=0):
        """An open streaming response, from byte ``start``."""
        import requests

        headers = dict(self._headers)
        if start:
            headers["Range"] = "bytes=%d-" % start
        r = requests.get(self._url, headers=headers, stream=True,
                         timeout=self._timeout)
        try:
            r.raise_for_status()
            if start and r.status_code != 206:
                raise FetchError("%s: the server ignored a range request"
                                 % self.label)
        except Exception:
            r.close()
            raise
        return r

    def start(self):
        with self._cond:
            if self._thread is not None or self._cancelled:
                return
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="fetch-" + self.label)
            self._thread.start()

    def _run(self):
        try:
            with self.request() as r:
                length = r.headers.get("Content-Length")
                total = int(length) if length else None
                if total is None or total > self._ram_limit:
                    if total is None:
                        raise FetchError("%s: the server did not say how "
                                         "big it is" % self.label)
                    with self._cond:
                        self.total, self.mode = total, "stream"
                        self._cond.notify_all()
                    log.info("%s is %d MB: streaming it, not holding it",
                             self.label, total >> 20)
                    return                  # readers make their own requests
                buf = bytearray(total)
                with self._cond:
                    self.total, self.mode, self._buf = total, "ram", buf
                    self._cond.notify_all()
                for data in r.iter_content(self._chunk):
                    if self._cancelled:
                        return
                    end = self.written + len(data)
                    if end > total:
                        raise FetchError("%s: more data than announced"
                                         % self.label)
                    buf[self.written:end] = data
                    with self._cond:
                        self.written = end
                        self._cond.notify_all()
            if self.written != total:
                raise FetchError("%s: got %d of %d bytes"
                                 % (self.label, self.written, total))
            log.info("fetched %s into memory, %d kB", self.label,
                     total // 1000)
        except Exception as e:
            if not self._cancelled:
                self.fail(e)
        finally:
            with self._cond:
                self.done = True
                self._cond.notify_all()

    def fail(self, e):
        log.warning("fetching %s failed: %s", self.label, type(e).__name__)
        self.error = e if isinstance(e, FetchError) else FetchError(
            "%s: %s" % (self.label, type(e).__name__))

    @property
    def cancelled(self):
        return self._cancelled

    def _raise_if_failed(self):
        if self.error is not None:
            raise self.error
        if self._cancelled:
            raise FetchError("%s: cancelled" % self.label)

    def wait_available(self, upto):
        """("ram" mode) Block until ``upto`` bytes have arrived, or all have.

        Returns how many bytes can be read (at most ``upto``).
        """
        with self._cond:
            self._cond.wait_for(lambda: self.written >= upto or self.done
                                or self._cancelled)
            self._raise_if_failed()
            return min(self.written, upto)

    def size(self):
        """Total size in bytes; blocks until the server has said."""
        with self._cond:
            self._cond.wait_for(lambda: self.mode is not None or self.done
                                or self._cancelled)
            self._raise_if_failed()
            return self.total

    def open(self):
        self.start()
        self.size()
        return (_MemoryReader if self.mode == "ram" else _StreamReader)(self)

    def cancel(self):
        with self._cond:
            self._cancelled = True
            self._cond.notify_all()

    def discard(self):
        with self._cond:
            self._cancelled = True
            self._buf = None                # the memory goes back now
            self._cond.notify_all()


class _Reader(io.RawIOBase):
    def __init__(self, source):
        super().__init__()
        self._src = source
        self._pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            self._pos = offset
        elif whence == os.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = self._src.total + offset
        return self._pos


class _MemoryReader(_Reader):
    """Reads a track held in memory, waiting for bytes not yet arrived."""

    def readinto(self, b):
        # Never a short read while more is coming: libsndfile takes one as
        # the end of the file.
        try:
            end = self._src.wait_available(self._pos + len(b))
        except FetchError:
            # Failed (source.error says why; the engine reads it when the
            # decoder stops) or cancelled. Raising would only be swallowed,
            # noisily, by libsndfile's callback.
            return 0
        buf = self._src._buf
        n = end - self._pos
        if n <= 0 or buf is None:
            return 0
        b[:n] = memoryview(buf)[self._pos:end]
        self._pos = end
        return n


class _StreamReader(_Reader):
    """Reads a track from the server as it goes, through a small window.

    Runs on the decode thread. A seek outside the window is a new ranged
    request; a dropped connection is resumed once from where it stopped.
    """

    def __init__(self, source):
        super().__init__(source)
        self._base = 0                      # file offset of _buf[0]
        self._buf = bytearray()
        self._resp = None
        self._chunks = None

    def _connect(self, start):
        self._disconnect()
        self._resp = self._src.request(start)
        self._chunks = self._resp.iter_content(self._src._chunk)
        self._base, self._buf = start, bytearray()

    def _disconnect(self):
        if self._resp is not None:
            self._resp.close()
        self._resp = self._chunks = None

    def _fill(self, upto):
        """Extend the window to file offset ``upto``."""
        retried = False
        while self._base + len(self._buf) < upto:
            try:
                data = next(self._chunks, None)
            except Exception:
                if retried:
                    raise
                retried = True              # resume where the window ends
                end, kept = self._base + len(self._buf), self._buf
                self._disconnect()
                self._resp = self._src.request(end)
                self._chunks = self._resp.iter_content(self._src._chunk)
                self._buf = kept
                continue
            if data is None:
                break
            self._buf += data

    def readinto(self, b):
        src = self._src
        if src.cancelled or src.error is not None:
            return 0
        want = min(len(b), src.total - self._pos)
        if want <= 0:
            return 0
        try:
            end = self._base + len(self._buf)
            if self._resp is None or self._pos < self._base \
                    or self._pos > end + _SKIP_AHEAD:
                self._connect(self._pos)
            self._fill(self._pos + want)
        except Exception as e:
            # Raising here would only be swallowed by libsndfile's callback;
            # the engine looks at source.error when the decoder stops.
            if not src.cancelled:
                src.fail(e)
            return 0
        off = self._pos - self._base
        n = min(want, len(self._buf) - off)
        if n < want and not src.cancelled:
            src.fail(FetchError("%s: the stream ended early" % src.label))
        b[:n] = memoryview(self._buf)[off:off + n]
        self._pos += n
        cut = self._pos - _KEEP_BEHIND - self._base
        if cut > _KEEP_BEHIND:              # in megabyte steps, not per read
            del self._buf[:cut]
            self._base += cut
        return n

    def close(self):
        self._disconnect()
        super().close()
