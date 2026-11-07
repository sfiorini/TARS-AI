"""Regression test (A2): module_elevenlabs must be import-safe without a key.

The module used to construct `ElevenLabs(api_key=CONFIG['TTS']['elevenlabs_api_key'])`
at import time. The client was never used (all requests go through the aiohttp
helper), and with no key in the config the real SDK raises ValueError — not
ImportError — which killed the import of module_tts (it only catches ImportError)
and with it the whole app.

The test installs a STRICT elevenlabs stub (construction without a key raises
ValueError, like the real SDK) and re-imports module_elevenlabs with a config
that has no API key: the import must succeed.
"""
import importlib
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Bootstrap gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_INI = os.path.join(_SRC, "config.ini")
if not os.path.exists(_CONFIG_INI):
    import shutil
    shutil.copyfile(os.path.join(_SRC, "config.ini.template"), _CONFIG_INI)


class _StrictElevenLabsClient:
    """Mimics the real SDK: constructing without a usable key raises ValueError."""

    def __init__(self, api_key=None):
        if not api_key:
            raise ValueError("Missing API key.")


def _strict_stub():
    pkg = types.ModuleType("elevenlabs")
    client_mod = types.ModuleType("elevenlabs.client")
    client_mod.ElevenLabs = _StrictElevenLabsClient
    pkg.client = client_mod
    return pkg, client_mod


class TestElevenLabsImportSafety(unittest.TestCase):
    def test_import_without_api_key_does_not_raise(self):
        import modules.module_config as mc

        saved_mod = sys.modules.pop("modules.module_elevenlabs", None)
        pkg, client_mod = _strict_stub()
        fake_config = {"TTS": {"elevenlabs_api_key": ""}}
        try:
            with mock.patch.dict(sys.modules, {"elevenlabs": pkg,
                                               "elevenlabs.client": client_mod}), \
                 mock.patch.object(mc, "load_config", return_value=fake_config), \
                 mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
                # Before the fix this raised ValueError (missing API key) at
                # import time and killed module_tts's import chain.
                importlib.import_module("modules.module_elevenlabs")
        finally:
            sys.modules.pop("modules.module_elevenlabs", None)
            if saved_mod is not None:
                sys.modules["modules.module_elevenlabs"] = saved_mod


if __name__ == "__main__":
    unittest.main()
