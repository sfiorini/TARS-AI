"""Tests: Piper voice loads lazily even when ttsoption != 'piper'.

Regression test for the fallback-chain bug: the voice model was loaded at
import time only when ttsoption == 'piper', so when the ordered TTS chain
fell through to piper (e.g. ttsoption = fishaudio), piper yielded nothing
and the chain silently dropped to espeak.

Stub pattern copied from test_module_piper_fx.py: module_piper imports
sounddevice / soundfile / piper, which load libasound at import time and
don't exist on CI runners — stub them and patch load_config.
"""
import asyncio
import ctypes
import io
import os
import sys
import types
import unittest
from unittest import mock

SRC = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(SRC))


def _fake_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


_fake_voice = mock.MagicMock()
_fake_voice.config.sample_rate = 22050

_STUBS = {
    "sounddevice": _fake_module("sounddevice"),
    "soundfile": _fake_module("soundfile"),
    "piper": _fake_module("piper"),
    "piper.voice": _fake_module("piper.voice", PiperVoice=mock.MagicMock()),
}
_STUBS["piper"].voice = _STUBS["piper.voice"]
_STUBS["piper.voice"].PiperVoice.load = mock.MagicMock(return_value=_fake_voice)

_FAKE_CONFIG = {
    "CHAR": {"character_card_path": "character/TARS/TARS.json"},
    "TTS": {
        "ttsoption": "fishaudio",  # NOT piper -> no model load at import
        "piper_multispeaker": False,
        "piper_robot_fx": False,
        "piper_fx": "",
    },
}

from modules import module_config  # noqa: E402

sys.modules.pop("modules.module_piper", None)
with mock.patch.dict(sys.modules, _STUBS), \
     mock.patch.object(module_config, "load_config", return_value=_FAKE_CONFIG), \
     mock.patch.object(ctypes.cdll, "LoadLibrary", return_value=mock.MagicMock()):
    import modules.module_piper as mp


def tearDownModule():
    # Do not leak the stub-imported module into other test files.
    sys.modules.pop("modules.module_piper", None)


class TestPiperLazyLoad(unittest.TestCase):
    def setUp(self):
        mp.voice = None
        _STUBS["piper.voice"].PiperVoice.load.reset_mock()
        _STUBS["piper.voice"].PiperVoice.load.return_value = _fake_voice

    def test_voice_not_loaded_at_import_when_ttsoption_not_piper(self):
        # module was imported with ttsoption=fishaudio: voice must be None
        self.assertIsNone(mp.voice)

    def test_ensure_voice_loaded_loads_model(self):
        with mock.patch.object(mp.os.path, "isfile", return_value=True), \
             mock.patch.object(mp, "_is_lfs_pointer", return_value=False):
            self.assertTrue(mp._ensure_voice_loaded())
            _STUBS["piper.voice"].PiperVoice.load.assert_called_once_with(mp.model_path)
            self.assertIs(mp.voice, _fake_voice)

    def test_provider_loads_voice_and_yields_audio(self):
        async def run():
            with mock.patch.object(mp.os.path, "isfile", return_value=True), \
                 mock.patch.object(mp, "_is_lfs_pointer", return_value=False), \
                 mock.patch.object(mp, "synthesize", new=mock.AsyncMock(return_value=io.BytesIO(b"RIFFfake"))):
                agen = mp.text_to_speech_with_pipelining_piper("Hello there.")
                return [c async for c in agen]
        chunks = asyncio.run(run())
        self.assertTrue(chunks)
        self.assertIsNotNone(mp.voice)

    def test_provider_yields_nothing_when_model_missing(self):
        async def run():
            with mock.patch.object(mp.os.path, "isfile", return_value=False), \
                 mock.patch.object(mp, "_download_voice_model", return_value=False):
                agen = mp.text_to_speech_with_pipelining_piper("Hello.")
                return [c async for c in agen]
        self.assertEqual(asyncio.run(run()), [])
        self.assertIsNone(mp.voice)


if __name__ == "__main__":
    unittest.main()
