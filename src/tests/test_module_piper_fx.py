"""Unit tests for Task 0 (voice config + setup script + attribution):
- [TTS] piper_robot_fx / piper_fx config keys (template, TTSConfig, load_config)
- sox robot-FX chain applied post-synthesis in module_piper when enabled
- scripts/configure_tars_client.sh sets the Fish.audio "Tars" reference id + piper FX
- scripts/setup_tts_voice.sh exists, is executable, carries CC BY 4.0 attribution
  and never prints the API key
- ATTRIBUTION.md / LICENSE / docs/voice-pipeline.md document the voice sources
"""
import asyncio
import configparser
import ctypes
import io
import os
import re
import shlex
import sys
import types
import unittest
from unittest import mock


# Bootstrap gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_INI = os.path.join(_SRC, "config.ini")
if not os.path.exists(_CONFIG_INI):
    _TEMPLATE = os.path.join(_SRC, "config.ini.template")
    try:
        import shutil
        shutil.copyfile(_TEMPLATE, _CONFIG_INI)
    except OSError as exc:
        raise RuntimeError(
            "Cannot bootstrap src/config.ini from src/config.ini.template "
            f"(required for module imports): {exc}"
        ) from exc

_REPO = os.path.abspath(os.path.join(_SRC, ".."))

DEFAULT_FX = "pitch -150 flanger 5 3 0 70 0.4 triangle 25 quad gain -n -3"
# Per-robot chain written by scripts/configure_tars_client.sh: re-auditioned
# for the stock en_US-hfc_male-medium voice (piper_voice) the user prefers.
ROBOT_FX = "pitch -50 flanger 5 3 0 70 0.4 triangle 25 quad gain -n -3"
FISH_TARS_REF = "6a57e35f5c8244dbb5d012a3c434ec16"


def _fake_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


class _SynthesisConfig:
    """Stub for piper-tts >= 1.3 SynthesisConfig (records the speaker_id)."""
    def __init__(self, speaker_id=None):
        self.speaker_id = speaker_id


# module_piper imports sounddevice / soundfile / piper and loads libasound at
# import time — none of which exist on a CI runner or a dev laptop. Stub them
# and patch load_config so no voice model is loaded/downloaded at import.
# The stub piper package MUST carry SynthesisConfig: _iter_pcm_chunks imports
# it lazily at synthesis time.
_STUBS = {
    "sounddevice": _fake_module("sounddevice"),
    "soundfile": _fake_module("soundfile"),
    "piper": _fake_module("piper", SynthesisConfig=_SynthesisConfig),
    "piper.voice": _fake_module("piper.voice", PiperVoice=mock.MagicMock()),
}
_STUBS["piper"].voice = _STUBS["piper.voice"]

_FAKE_CONFIG = {
    "CHAR": {"character_card_path": "character/TARS/TARS.json"},
    "TTS": {
        "ttsoption": "espeak",  # not "piper" -> no model load/download at import
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
    from modules import module_piper  # noqa: E402


def setUpModule():
    # _iter_pcm_chunks imports SynthesisConfig from piper DEFERRED, i.e. at
    # synthesis/test time — long after the import-time patch.dict above has
    # exited and removed the stubs. Re-install them for this file's tests.
    sys.modules.update(_STUBS)


def tearDownModule():
    # Do not leak the stub-imported module into other test files.
    sys.modules.pop("modules.module_piper", None)
    for _name in _STUBS:
        sys.modules.pop(_name, None)


def _tts_cfg(**overrides):
    tts = {
        "ttsoption": "piper",
        "piper_multispeaker": False,
        "piper_robot_fx": False,
        "piper_fx": "",
    }
    tts.update(overrides)
    return {"CHAR": {"character_card_path": "character/TARS/TARS.json"}, "TTS": tts}


def _fake_voice():
    voice = types.SimpleNamespace()
    voice.config = types.SimpleNamespace(sample_rate=22050, speaker_id=None)

    def synthesize_wav(text, wav_file):
        wav_file.writeframes(b"\x11\x22" * 64)

    voice.synthesize_wav = synthesize_wav
    return voice


def _stream_voice(pcm=b"\x11\x22" * 64, n=1, sample_rate=22050, record=None):
    voice = types.SimpleNamespace()
    voice.config = types.SimpleNamespace(sample_rate=sample_rate)
    def synthesize(text, syn_config=None):
        if record is not None:
            record["text"] = text
            record["syn_config"] = syn_config
        for _ in range(n):
            yield types.SimpleNamespace(audio_int16_bytes=pcm)
    voice.synthesize = synthesize
    return voice


def _collect(agen):
    """Drive an async generator to completion; return (chunks, exception)."""
    async def _run():
        out = []
        try:
            async for chunk in agen:
                out.append(chunk)
            return out, None
        except Exception as exc:
            return out, exc
    return asyncio.run(_run())


def _sox_popen(returncode=0, out=b"PROCESSED-WAV", err=b""):
    proc = mock.MagicMock()
    proc.communicate.return_value = (out, err)
    proc.returncode = returncode
    popen = mock.MagicMock(return_value=proc)
    return popen, proc


class TestPiperFxConfig(unittest.TestCase):
    def test_template_declares_piper_fx_keys(self):
        parser = configparser.ConfigParser()
        parser.read(os.path.join(_SRC, "config.ini.template"))
        self.assertTrue(parser.has_section("TTS"))
        self.assertTrue(parser.getboolean("TTS", "piper_robot_fx"))
        self.assertEqual(parser.get("TTS", "piper_fx"), DEFAULT_FX)

    def test_ttsconfig_from_config_dict_reads_piper_fx(self):
        cfg = module_config.TTSConfig.from_config_dict({
            "ttsoption": "piper",
            "toggle_charvoice": True,
            "tts_voice": "",
            "is_talking_override": False,
            "is_talking": False,
            "global_timer_paused": False,
            "piper_robot_fx": True,
            "piper_fx": DEFAULT_FX,
        })
        self.assertTrue(cfg.piper_robot_fx)
        self.assertEqual(cfg.piper_fx, DEFAULT_FX)
        self.assertTrue(cfg["piper_robot_fx"])
        self.assertEqual(cfg.get("piper_fx"), DEFAULT_FX)

    def test_ttsconfig_from_config_dict_defaults_and_bool_coercion(self):
        base = {
            "ttsoption": "piper",
            "toggle_charvoice": True,
            "tts_voice": "",
            "is_talking_override": False,
            "is_talking": False,
            "global_timer_paused": False,
        }
        cfg = module_config.TTSConfig.from_config_dict(dict(base))
        self.assertFalse(cfg.piper_robot_fx)
        self.assertIsNone(cfg.piper_fx)
        cfg_str = module_config.TTSConfig.from_config_dict(
            dict(base, piper_robot_fx="True", piper_fx="pitch 300"))
        self.assertTrue(cfg_str.piper_robot_fx)
        self.assertEqual(cfg_str.piper_fx, "pitch 300")
        cfg_off = module_config.TTSConfig.from_config_dict(
            dict(base, piper_robot_fx="False"))
        self.assertFalse(cfg_off.piper_robot_fx)

    def test_load_config_tts_section_exposes_piper_fx_keys(self):
        cfg = module_config.load_config()
        tts = cfg["TTS"]
        # Keys must always be present (dataclass defaults) regardless of ini.
        self.assertIsInstance(tts["piper_robot_fx"], bool)
        self.assertIn(tts["piper_fx"], (None, "")) if tts["piper_fx"] is None else self.assertIsInstance(tts["piper_fx"], str)


class TestRunSoxChain(unittest.TestCase):
    def test_success_pipes_wav_through_sox(self):
        popen, proc = _sox_popen(returncode=0, out=b"PROCESSED-WAV")
        with mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen):
            out = module_piper._run_sox_chain(b"RAWWAV", DEFAULT_FX)
        self.assertEqual(out, b"PROCESSED-WAV")
        cmd = popen.call_args.args[0]
        # stdin/stdout are '-' but the wav format must be declared explicitly
        # (sox cannot sniff the type of a pipe)
        self.assertEqual(cmd[:6], ["/usr/bin/sox", "-t", "wav", "-", "-t", "wav"])
        self.assertEqual(cmd[6], "-")
        self.assertEqual(cmd[7:], shlex.split(DEFAULT_FX))
        self.assertEqual(popen.call_args.kwargs["stdin"], module_piper.subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["stdout"], module_piper.subprocess.PIPE)
        proc.communicate.assert_called_once_with(input=b"RAWWAV", timeout=module_piper.SOX_TIMEOUT)

    def test_sox_missing_returns_none_and_warns(self):
        popen = mock.MagicMock()
        with mock.patch.object(module_piper.shutil, "which", return_value=None), \
             mock.patch.object(module_piper.subprocess, "Popen", popen), \
             mock.patch.object(module_piper, "queue_message") as qmsg:
            out = module_piper._run_sox_chain(b"RAWWAV", DEFAULT_FX)
        self.assertIsNone(out)
        popen.assert_not_called()
        warnings = [c.args[0] for c in qmsg.call_args_list if "WARNING" in c.args[0]]
        self.assertTrue(any("sox" in w for w in warnings))

    def test_sox_failure_returns_none_and_warns(self):
        popen, _ = _sox_popen(returncode=1, out=b"", err=b"boom")
        with mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen), \
             mock.patch.object(module_piper, "queue_message") as qmsg:
            out = module_piper._run_sox_chain(b"RAWWAV", DEFAULT_FX)
        self.assertIsNone(out)
        warnings = [c.args[0] for c in qmsg.call_args_list if "WARNING" in c.args[0]]
        self.assertTrue(any("failed" in w for w in warnings))

    def test_sox_exception_returns_none(self):
        popen = mock.MagicMock(side_effect=OSError("no pipe"))
        with mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen), \
             mock.patch.object(module_piper, "queue_message"):
            out = module_piper._run_sox_chain(b"RAWWAV", DEFAULT_FX)
        self.assertIsNone(out)


class TestApplyPiperFx(unittest.TestCase):
    def test_disabled_passthrough_without_sox(self):
        buf = io.BytesIO(b"RIFF-raw-wav")
        with mock.patch.object(module_piper, "CONFIG", _tts_cfg(piper_robot_fx=False)), \
             mock.patch.object(module_piper.shutil, "which") as which, \
             mock.patch.object(module_piper.subprocess, "Popen") as popen:
            out = module_piper.apply_piper_fx(buf)
        self.assertIs(out, buf)
        which.assert_not_called()
        popen.assert_not_called()

    def test_enabled_runs_sox_and_returns_processed(self):
        buf = io.BytesIO(b"RIFF-raw-wav")
        popen, _ = _sox_popen(returncode=0, out=b"PROCESSED-WAV")
        with mock.patch.object(module_piper, "CONFIG",
                               _tts_cfg(piper_robot_fx=True, piper_fx=DEFAULT_FX)), \
             mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen):
            out = module_piper.apply_piper_fx(buf)
        self.assertEqual(out.getvalue(), b"PROCESSED-WAV")
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[7:], shlex.split(DEFAULT_FX))

    def test_empty_chain_falls_back_to_default_recipe(self):
        buf = io.BytesIO(b"RIFF-raw-wav")
        popen, _ = _sox_popen(returncode=0, out=b"PROCESSED-WAV")
        with mock.patch.object(module_piper, "CONFIG",
                               _tts_cfg(piper_robot_fx=True, piper_fx="")), \
             mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen):
            module_piper.apply_piper_fx(buf)
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[7:], shlex.split(DEFAULT_FX))

    def test_custom_chain_used_verbatim(self):
        buf = io.BytesIO(b"RIFF-raw-wav")
        popen, _ = _sox_popen(returncode=0, out=b"PROCESSED-WAV")
        with mock.patch.object(module_piper, "CONFIG",
                               _tts_cfg(piper_robot_fx=True, piper_fx="pitch 300")), \
             mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen):
            module_piper.apply_piper_fx(buf)
        self.assertEqual(popen.call_args.args[0][7:], ["pitch", "300"])

    def test_sox_missing_returns_original_buffer(self):
        buf = io.BytesIO(b"RIFF-raw-wav")
        with mock.patch.object(module_piper, "CONFIG",
                               _tts_cfg(piper_robot_fx=True, piper_fx=DEFAULT_FX)), \
             mock.patch.object(module_piper.shutil, "which", return_value=None), \
             mock.patch.object(module_piper.subprocess, "Popen") as popen, \
             mock.patch.object(module_piper, "queue_message"):
            out = module_piper.apply_piper_fx(buf)
        self.assertIs(out, buf)
        self.assertEqual(out.getvalue(), b"RIFF-raw-wav")
        popen.assert_not_called()

    def test_sox_failure_returns_original_buffer(self):
        buf = io.BytesIO(b"RIFF-raw-wav")
        popen, _ = _sox_popen(returncode=2, out=b"", err=b"bad chain")
        with mock.patch.object(module_piper, "CONFIG",
                               _tts_cfg(piper_robot_fx=True, piper_fx=DEFAULT_FX)), \
             mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen), \
             mock.patch.object(module_piper, "queue_message"):
            out = module_piper.apply_piper_fx(buf)
        self.assertIs(out, buf)
        self.assertEqual(out.getvalue(), b"RIFF-raw-wav")

    def test_unexpected_error_returns_original_buffer(self):
        buf = io.BytesIO(b"RIFF-raw-wav")
        with mock.patch.object(module_piper, "CONFIG",
                               _tts_cfg(piper_robot_fx=True, piper_fx=DEFAULT_FX)), \
             mock.patch.object(module_piper, "_run_sox_chain",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(module_piper, "queue_message"):
            out = module_piper.apply_piper_fx(buf)
        self.assertIs(out, buf)

    def test_config_read_at_call_time(self):
        buf = io.BytesIO(b"RIFF-raw-wav")
        popen, _ = _sox_popen(returncode=0, out=b"PROCESSED-WAV")
        with mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen):
            with mock.patch.object(module_piper, "CONFIG", _tts_cfg(piper_robot_fx=False)):
                module_piper.apply_piper_fx(buf)
            popen.assert_not_called()
            with mock.patch.object(module_piper, "CONFIG",
                                   _tts_cfg(piper_robot_fx=True, piper_fx=DEFAULT_FX)):
                module_piper.apply_piper_fx(buf)
            popen.assert_called_once()


class TestPiperPipelineAppliesFx(unittest.TestCase):
    def test_fx_disabled_yields_raw_wav(self):
        with mock.patch.object(module_piper, "voice", _stream_voice()), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg(piper_robot_fx=False)), \
             mock.patch.object(module_piper.subprocess, "Popen") as popen, \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hello there"))
        self.assertIsNone(raised)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].getvalue().startswith(b"RIFF"))
        popen.assert_not_called()

    def test_fx_enabled_yields_sox_processed_audio(self):
        popen, _ = _sox_popen(returncode=0, out=b"PROCESSED-WAV")
        with mock.patch.object(module_piper, "voice", _stream_voice()), \
             mock.patch.object(module_piper, "CONFIG",
                               _tts_cfg(piper_robot_fx=True, piper_fx=DEFAULT_FX)), \
             mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen), \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hello there"))
        self.assertIsNone(raised)
        self.assertEqual([c.getvalue() for c in chunks], [b"PROCESSED-WAV"])
        popen.assert_called_once()
        # sox received the raw piper wav on stdin
        self.assertTrue(popen.return_value.communicate.call_args.kwargs["input"].startswith(b"RIFF"))

    def test_no_voice_yields_nothing(self):
        with mock.patch.object(module_piper, "voice", None), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg()), \
             mock.patch.object(module_piper, "queue_message") as qmsg:
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hello"))
        self.assertIsNone(raised)
        self.assertEqual(chunks, [])
        self.assertTrue(qmsg.called)

    def test_synthesize_wav_only_voice_fails_over_cleanly(self):
        # A voice exposing only synthesize_wav (piper 1.0-era) has neither
        # synthesize nor synthesize_stream_raw -> AttributeError BEFORE the
        # first chunk -> the chain's bare-Exception failover handles it.
        with mock.patch.object(module_piper, "voice", _fake_voice()), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg()), \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hello"))
        self.assertEqual(chunks, [])
        self.assertIsInstance(raised, AttributeError)


class TestRobotVoiceOpsFiles(unittest.TestCase):
    def test_configure_script_sets_fish_tars_voice_and_piper_fx(self):
        path = os.path.join(_REPO, "scripts", "configure_tars_client.sh")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(f"set_kv TTS fishaudio_reference_id {FISH_TARS_REF}", text)
        self.assertIn("set_kv TTS piper_robot_fx True", text)
        # per-robot chain (re-auditioned for the piper_voice stock timbre)
        self.assertIn(ROBOT_FX, text)
        # Never write a literal API key into .env/config from this script.
        self.assertIsNone(re.search(r'FISH_AUDIO_API_KEY\s*=\s*"[^"]+"', text))

    def test_setup_tts_voice_script_exists_executable_with_attribution(self):
        path = os.path.join(_REPO, "scripts", "setup_tts_voice.sh")
        self.assertTrue(os.path.isfile(path), "scripts/setup_tts_voice.sh missing")
        self.assertTrue(os.access(path, os.X_OK), "setup_tts_voice.sh not executable")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("https://api.fish.audio/v1/model", text)
        self.assertIn("voices=@", text)
        self.assertIn("815828", text)
        self.assertIn("RICHERlandTV", text)
        self.assertIn("CC BY 4.0", text)
        self.assertIn("FISH_AUDIO_API_KEY", text)
        # The key must be read from the environment, never echoed/printed:
        # no echo line may expand the variable, and no literal key may be set.
        for line in text.splitlines():
            if "echo" in line and re.search(r'\$\{?FISH_AUDIO_API_KEY', line):
                self.fail(f"script may print the API key: {line}")
        self.assertIsNone(re.search(r'FISH_AUDIO_API_KEY\s*=\s*"[^"$][^"]+"', text),
                          "script must not hardcode an API key")

    def test_attribution_md_documents_voice_sources(self):
        with open(os.path.join(_REPO, "ATTRIBUTION.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("815828", text)
        self.assertIn("RICHERlandTV", text)
        self.assertIn("CC BY 4.0", text)
        self.assertIn(FISH_TARS_REF, text)
        self.assertIn("brenardspag", text)

    def test_license_notes_voice_attribution(self):
        with open(os.path.join(_REPO, "LICENSE"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("ATTRIBUTION.md", text)
        self.assertIn("815828", text)

    def test_voice_pipeline_doc_lists_voice_sources(self):
        with open(os.path.join(_REPO, "docs", "voice-pipeline.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(FISH_TARS_REF, text)
        self.assertIn("api.speechify.ai/v1/voices", text)
        self.assertIn("prosody", text)
        self.assertIn("piper_robot_fx", text)
        self.assertIn("flanger", text)
        self.assertIn("815828", text)

    def test_piper_fx_chain_agrees_across_all_locations(self):
        # An EMPTY piper_fx silently falls back to DEFAULT_PIPER_FX, so ALL
        # repo copies of the chain must carry the SAME string or a partial
        # change resurrects the old sound. Every file that carries the chain
        # (grep-verified; this test's DEFAULT_FX is the pin):
        # src/modules/module_piper.py (DEFAULT_PIPER_FX), src/config.ini.template,
        # docs/voice-pipeline.md, docs/configuration.md, ATTRIBUTION.md.
        # A future copy added anywhere in the repo MUST be asserted here too.
        #
        # EXCEPT scripts/configure_tars_client.sh: per-robot chain differences
        # belong there (it writes an EXPLICIT value, so the empty-fallback
        # hazard does not apply). The robot chain is pinned separately below
        # so it cannot drift silently either.
        self.assertEqual(module_piper.DEFAULT_PIPER_FX, DEFAULT_FX)
        parser = configparser.ConfigParser()
        parser.read(os.path.join(_SRC, "config.ini.template"))
        self.assertEqual(parser.get("TTS", "piper_fx"), DEFAULT_FX)
        with open(os.path.join(_REPO, "scripts", "configure_tars_client.sh"), encoding="utf-8") as fh:
            script = fh.read()
        m = re.search(r'set_kv TTS piper_fx "([^"]+)"', script)
        self.assertIsNotNone(m, "configure script must set piper_fx")
        self.assertEqual(m.group(1), ROBOT_FX)
        with open(os.path.join(_REPO, "docs", "voice-pipeline.md"), encoding="utf-8") as fh:
            self.assertIn(DEFAULT_FX, fh.read())
        with open(os.path.join(_REPO, "docs", "configuration.md"), encoding="utf-8") as fh:
            self.assertIn(DEFAULT_FX, fh.read())
        with open(os.path.join(_REPO, "ATTRIBUTION.md"), encoding="utf-8") as fh:
            self.assertIn(DEFAULT_FX, fh.read())


if __name__ == "__main__":
    unittest.main()
