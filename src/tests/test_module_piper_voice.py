"""Tests: configurable Piper voice id ([TTS] piper_voice).

The user replaced TARS.onnx with the stock Piper voice en_US-hfc_male-medium
on the robot; piper_voice makes that swap durable across reinstalls:
- '' (default) = character voice character/{char}/voice/{char}.onnx,
  downloaded from the TARS-AI Hugging Face repo (the historical behaviour,
  byte-for-byte).
- a stock Piper voice id (e.g. en_US-hfc_male-medium) = the voice is stored
  as character/{char}/voice/{id}.onnx and downloaded from the upstream
  rhasspy/piper-voices repo.

No test hits the network — urlretrieve is stubbed.

Stub pattern copied from test_module_piper_lazy.py: module_piper imports
sounddevice / soundfile / piper, which load libasound at import time and
don't exist on CI runners — stub them and patch load_config.
"""
import ctypes
import os
import sys
import tempfile
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
        "ttsoption": "espeak",  # NOT piper -> no model load at import
        "piper_multispeaker": False,
        "piper_robot_fx": False,
        "piper_fx": "",
        "piper_voice": "",
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


# Verified on the robot: these upstream rhasspy/piper-voices URLs resolve.
HFC_ONNX_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                "en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx")
ALAN_ONNX_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                 "en/en_GB/alan/medium/en_GB-alan-medium.onnx")


def _cfg(piper_voice=""):
    """CONFIG stand-in with the given [TTS] piper_voice (read at call time)."""
    return {
        "CHAR": {"character_card_path": "character/TARS/TARS.json"},
        "TTS": {
            "ttsoption": "piper",
            "piper_multispeaker": False,
            "piper_robot_fx": False,
            "piper_fx": "",
            "piper_voice": piper_voice,
        },
    }


class TestResolveVoicePath(unittest.TestCase):
    def test_default_empty_is_character_voice_byte_for_byte(self):
        # piper_voice = "" must reproduce the historical import-time path
        with mock.patch.object(mp, "CONFIG", _cfg("")):
            path = mp._resolve_voice_path()
        self.assertEqual(path, mp.model_path)
        self.assertTrue(path.endswith("character/TARS/voice/TARS.onnx"))

    def test_stock_voice_id_changes_path(self):
        with mock.patch.object(mp, "CONFIG", _cfg("en_US-hfc_male-medium")):
            path = mp._resolve_voice_path()
        self.assertTrue(
            path.endswith("character/TARS/voice/en_US-hfc_male-medium.onnx"),
            f"unexpected path: {path}")

    def test_path_is_resolved_at_call_time_not_import_time(self):
        # piper_voice is read per call: the module was imported with "" but a
        # later config change must take effect without re-import.
        with mock.patch.object(mp, "CONFIG", _cfg("en_GB-alan-medium")):
            self.assertTrue(mp._resolve_voice_path().endswith(
                "voice/en_GB-alan-medium.onnx"))
        with mock.patch.object(mp, "CONFIG", _cfg("")):
            self.assertTrue(mp._resolve_voice_path().endswith("voice/TARS.onnx"))


class TestStockVoiceUrls(unittest.TestCase):
    def test_en_us_hfc_male_medium(self):
        onnx, js = mp._stock_piper_voice_urls("en_US-hfc_male-medium")
        self.assertEqual(onnx, HFC_ONNX_URL)
        self.assertEqual(js, HFC_ONNX_URL + ".json")

    def test_en_gb_alan_medium(self):
        onnx, js = mp._stock_piper_voice_urls("en_GB-alan-medium")
        self.assertEqual(onnx, ALAN_ONNX_URL)
        self.assertEqual(js, ALAN_ONNX_URL + ".json")


class TestMalformedVoiceId(unittest.TestCase):
    def test_bogus_id_warns_and_falls_back_to_character_voice(self):
        with mock.patch.object(mp, "CONFIG", _cfg("bogus")), \
             mock.patch.object(mp, "queue_message") as qmsg:
            path = mp._resolve_voice_path()
        # fallback = the character-voice path, not a guessed URL
        self.assertEqual(path, mp.model_path)
        warnings = [c.args[0] for c in qmsg.call_args_list]
        self.assertTrue(any("WARNING" in w for w in warnings),
                        f"expected a WARNING log, got {warnings!r}")
        self.assertTrue(any("piper_voice" in w for w in warnings),
                        f"warning must name piper_voice, got {warnings!r}")

    def test_bogus_id_has_no_urls(self):
        self.assertIsNone(mp._stock_piper_voice_urls("bogus"))


class TestStockVoiceDownload(unittest.TestCase):
    def setUp(self):
        mp.voice = None
        _STUBS["piper.voice"].PiperVoice.load.reset_mock()
        _STUBS["piper.voice"].PiperVoice.load.return_value = _fake_voice

    def tearDown(self):
        mp.voice = None

    def test_download_fetches_both_files_from_piper_voices_repo(self):
        def fake_urlretrieve(url, local):
            with open(local, "wb") as fh:
                fh.write(b"STUB-MODEL")
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "en_US-hfc_male-medium.onnx")
            with mock.patch.object(mp, "urlretrieve",
                                   side_effect=fake_urlretrieve) as ur, \
                 mock.patch.object(mp, "queue_message"):
                self.assertTrue(mp._download_stock_voice(
                    "en_US-hfc_male-medium", dest))
            self.assertTrue(os.path.isfile(dest))
            self.assertTrue(os.path.isfile(dest + ".json"))
        self.assertEqual([c.args[0] for c in ur.call_args_list],
                         [HFC_ONNX_URL, HFC_ONNX_URL + ".json"])

    def test_download_failure_returns_false_and_cleans_up(self):
        def boom(url, local):
            raise OSError("network down")
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "en_US-hfc_male-medium.onnx")
            with mock.patch.object(mp, "urlretrieve", side_effect=boom), \
                 mock.patch.object(mp, "queue_message"):
                self.assertFalse(mp._download_stock_voice(
                    "en_US-hfc_male-medium", dest))
            self.assertFalse(os.path.exists(dest))

    def test_ensure_voice_loaded_downloads_missing_stock_voice(self):
        # missing stock voice -> downloaded from rhasspy/piper-voices, then
        # loaded from the stock-voice path (never the TARS-AI repo)
        def fake_urlretrieve(url, local):
            with open(local, "wb") as fh:
                fh.write(b"STUB-MODEL")
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "en_US-hfc_male-medium.onnx")
            with mock.patch.object(mp, "_resolve_voice",
                                   return_value=(dest, "en_US-hfc_male-medium")), \
                 mock.patch.object(mp, "urlretrieve",
                                   side_effect=fake_urlretrieve) as ur, \
                 mock.patch.object(mp, "queue_message"):
                self.assertTrue(mp._ensure_voice_loaded())
            self.assertIs(mp.voice, _fake_voice)
            _STUBS["piper.voice"].PiperVoice.load.assert_called_once_with(dest)
            self.assertEqual([c.args[0] for c in ur.call_args_list],
                             [HFC_ONNX_URL, HFC_ONNX_URL + ".json"])

    def test_ensure_voice_loaded_keeps_tars_repo_for_character_voice(self):
        # piper_voice = "": the existing _download_voice_model path (TARS-AI
        # repo) must stay in effect — no behaviour change for existing installs
        with mock.patch.object(mp, "_resolve_voice",
                               return_value=(mp.model_path, None)), \
             mock.patch.object(mp.os.path, "isfile", return_value=False), \
             mock.patch.object(mp, "_download_voice_model",
                               return_value=False) as dl, \
             mock.patch.object(mp, "_download_stock_voice",
                               return_value=True) as stock_dl, \
             mock.patch.object(mp, "queue_message"):
            self.assertFalse(mp._ensure_voice_loaded())
        dl.assert_called_once_with("TARS", mp.model_path)
        stock_dl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
