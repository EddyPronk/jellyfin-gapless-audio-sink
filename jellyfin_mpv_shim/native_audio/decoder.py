"""FLAC (anything libsndfile reads) to blocks of PCM."""
import soundfile as sf

#: Sample type handed to the sound card for each stored bit depth. Integer
#: all the way, so a 16-bit file reaches a 16-bit DAC untouched.
_DTYPES = {"PCM_16": "int16", "PCM_S8": "int16", "PCM_U8": "int16",
           "PCM_24": "int32", "PCM_32": "int32"}


class Decoder:
    def __init__(self, source):
        self._raw = source.open()
        try:
            self._sf = sf.SoundFile(self._raw)
        except Exception:
            self._raw.close()
            if source.error is not None:    # the download failed: say that,
                raise source.error          # not "format not recognised"
            raise
        self.rate = self._sf.samplerate
        self.channels = self._sf.channels
        self.frames = self._sf.frames
        self.dtype = _DTYPES.get(self._sf.subtype, "float32")

    @property
    def format(self):
        """What the output stream has to be opened as."""
        return self.rate, self.channels, self.dtype

    def seek(self, frame):
        return self._sf.seek(max(0, min(int(frame), self.frames)))

    def read(self, frames):
        """Up to ``frames`` frames, shape (n, channels); empty at the end."""
        return self._sf.read(frames, dtype=self.dtype, always_2d=True)

    def close(self):
        try:
            self._sf.close()
        finally:
            self._raw.close()
