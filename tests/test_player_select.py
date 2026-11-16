"""``audio_sink`` picks the player, and in sink mode mpv is never imported.

Each case runs in a fresh interpreter: the choice is made at import time, so
it cannot be re-made inside one process, and "was ``player`` imported" is
only a meaningful question in a process that started clean.

Skipped where numpy / soundfile are not installed (the ``sink`` extra).
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "soundfile"))

PROBE = r"""
import sys, types
sys.argv = [sys.argv[0]]
from jellyfin_mpv_shim.conf import settings
settings.audio_sink = %(sink)r
if not settings.audio_sink:
    # A stand-in for the mpv player module. The real one starts mpv when it
    # is imported -- with a window -- and a test must not do that.
    fake = types.ModuleType("jellyfin_mpv_shim.player")
    class PlayerManager:
        def terminate(self): pass
    fake.playerManager, fake._mpv_errors = PlayerManager(), (OSError,)
    sys.modules["jellyfin_mpv_shim.player"] = fake
# Everything that takes cast commands or reports progress:
from jellyfin_mpv_shim import event_handler, timeline, action_thread
from jellyfin_mpv_shim.player_select import playerManager
from jellyfin_mpv_shim.clients import capabilities
same = (event_handler.playerManager is playerManager
        and timeline.playerManager is playerManager
        and action_thread.playerManager is playerManager)
caps = capabilities()
print(type(playerManager).__name__, same,
      "jellyfin_mpv_shim.player" in sys.modules, "mpv" in sys.modules,
      ",".join(caps["PlayableMediaTypes"]),
      "DisplayContent" in caps["SupportedCommands"],
      "SetVolume" in caps["SupportedCommands"])
playerManager.terminate()
"""


def probe(sink):
    out = subprocess.run(
        [sys.executable, "-c", PROBE % {"sink": sink}], cwd=REPO,
        capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise AssertionError(out.stderr[-2000:])
    return out.stdout.split()


@unittest.skipUnless(HAVE_DEPS, "needs numpy and soundfile (the sink extra)")
class PlayerSelectTest(unittest.TestCase):
    def test_sink_mode_uses_the_sink_everywhere_and_never_loads_mpv(self):
        name, same, player_loaded, mpv_loaded, media, display, volume = \
            probe(True)
        self.assertEqual(name, "AudioSinkManager")
        self.assertEqual(same, "True", "one player object for every module")
        self.assertEqual(player_loaded, "False")
        self.assertEqual(mpv_loaded, "False")
        self.assertEqual(media, "Audio")
        self.assertEqual(display, "False", "no screen to show things on")
        self.assertEqual(volume, "True")

    def test_the_default_is_still_the_mpv_player(self):
        name, same, player_loaded, _mpv, media, display, _volume = \
            probe(False)
        self.assertEqual(name, "PlayerManager")
        self.assertEqual(same, "True")
        self.assertEqual(player_loaded, "True")
        self.assertEqual(media, "Video,Audio,Photo")
        self.assertEqual(display, "True")


if __name__ == "__main__":
    unittest.main()
