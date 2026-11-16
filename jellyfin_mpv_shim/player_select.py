"""Which player this run of the app uses.

``playerManager`` is the mpv player, or -- with ``audio_sink`` set -- the
native audio sink. The modules that take cast commands and report progress
import it from here rather than from ``player``, so that in sink mode
``player`` (and with it mpv) is never imported at all.

The choice is made when this module is first imported, so the settings have
to be loaded by then; ``mpv_shim.main`` loads them first.
"""
from .conf import settings

if settings.audio_sink:
    from .sink_player import AudioSinkManager, lock_memory

    lock_memory()
    playerManager = AudioSinkManager()
    #: What a lost mpv raises. The sink has no mpv to lose.
    _mpv_errors = ()
else:
    from .player import playerManager, _mpv_errors  # noqa: F401
