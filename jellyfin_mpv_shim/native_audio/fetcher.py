"""Where a track's bytes come from, readable before they have all arrived."""
import io
import logging
import os
import tempfile
import threading

log = logging.getLogger("native_audio.fetcher")


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
        """Stop fetching. Readers blocked on it fail."""

    def discard(self):
        """Cancel and free whatever was cached."""
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
    """Downloads a URL into a cache file on a thread of its own.

    The URL is never logged: it may carry an API key.
    """

    def __init__(self, url, headers=None, cache_dir=None, timeout=30,
                 chunk=1 << 16, label=None):
        self._url = url
        self._headers = dict(headers or {})
        # Content-Length has to be the size of what we write, not of a
        # compressed body.
        self._headers.setdefault("Accept-Encoding", "identity")
        self._cache_dir = cache_dir
        self._timeout = timeout
        self._chunk = chunk
        self.label = label or "track"
        self.path = None
        self.written = 0
        self.total = None           # from Content-Length, None until known
        self.done = False
        self.error = None
        self._cancelled = False
        self._discarded = False
        self._thread = None
        self._cond = threading.Condition()

    def __repr__(self):
        return "HttpSource(%s, %d/%s bytes)" % (
            self.label, self.written, self.total)

    def start(self):
        with self._cond:
            if self._thread is not None or self._cancelled:
                return
            fd, self.path = tempfile.mkstemp(
                prefix="track-", suffix=".part", dir=self._cache_dir)
            self._thread = threading.Thread(
                target=self._run, args=(fd,), daemon=True,
                name="fetch-" + self.label)
            self._thread.start()

    def _run(self, fd):
        import requests

        try:
            with os.fdopen(fd, "wb", buffering=0) as out, requests.get(
                    self._url, headers=self._headers, stream=True,
                    timeout=self._timeout) as r:
                r.raise_for_status()
                length = r.headers.get("Content-Length")
                with self._cond:
                    self.total = int(length) if length else None
                    self._cond.notify_all()
                for data in r.iter_content(self._chunk):
                    if self._cancelled:
                        return
                    out.write(data)
                    with self._cond:
                        self.written += len(data)
                        self._cond.notify_all()
            if self.total is not None and self.written != self.total:
                raise FetchError("%s: got %d of %d bytes"
                                 % (self.label, self.written, self.total))
            log.info("fetched %s, %d kB", self.label, self.written // 1000)
        except Exception as e:
            if not self._cancelled:
                log.warning("fetching %s failed: %s", self.label,
                            type(e).__name__)
                self.error = e if isinstance(e, FetchError) else FetchError(
                    "%s: %s" % (self.label, type(e).__name__))
        finally:
            with self._cond:
                self.done = True
                self._cond.notify_all()
            if self._discarded:
                self._remove()

    def _remove(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _raise_if_failed(self):
        if self.error is not None:
            raise self.error
        if self._cancelled:
            raise FetchError("%s: cancelled" % self.label)

    def wait_available(self, upto):
        """Block until ``upto`` bytes are on disk or the download is over.

        Returns how many bytes can be read (at most ``upto``).
        """
        with self._cond:
            self._cond.wait_for(lambda: self.written >= upto or self.done
                                or self._cancelled)
            self._raise_if_failed()
            return min(self.written, upto)

    def size(self):
        """Total size in bytes, as soon as it is known."""
        with self._cond:
            self._cond.wait_for(lambda: self.total is not None or self.done
                                or self._cancelled)
            self._raise_if_failed()
            return self.total if self.total is not None else self.written

    def open(self):
        self.start()
        self._raise_if_failed()
        return _GrowingReader(self)

    def cancel(self):
        with self._cond:
            self._cancelled = True
            self._cond.notify_all()

    def discard(self):
        with self._cond:
            self._cancelled = True
            self._discarded = True
            self._cond.notify_all()
            finished = self.done or self._thread is None
        if finished and self.path:
            self._remove()


class _GrowingReader(io.RawIOBase):
    """Reads an HttpSource's cache file, waiting for bytes not yet written."""

    def __init__(self, source):
        super().__init__()
        self._src = source
        self._f = open(source.path, "rb", buffering=0)
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
            self._pos = self._src.size() + offset
        return self._pos

    def readinto(self, b):
        # Never a short read while more is coming: libsndfile takes one as
        # the end of the file.
        try:
            end = self._src.wait_available(self._pos + len(b))
        except FetchError:
            if self._src.error is None:     # cancelled: playback moved on,
                return 0                    # which is not worth a traceback
            raise
        want = end - self._pos
        if want <= 0:
            return 0
        self._f.seek(self._pos)
        got = 0
        view = memoryview(b)
        while got < want:
            n = self._f.readinto(view[got:want])
            if not n:
                break
            got += n
        self._pos += got
        return got

    def close(self):
        if not self.closed:
            self._f.close()
        super().close()
