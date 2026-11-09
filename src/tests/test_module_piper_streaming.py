"""Tests: Piper streaming rewrite (M4, decisions D5 + D6).

- one WAV chunk per synthesize() chunk, WAV-wrapped at the voice's native
  rate (22050 Hz mono 16-bit)
- ONNX inference AND the sox FX subprocess run OFF the asyncio event loop
  (asyncio.to_thread) — the Pi-5 freeze fix (D5)
- speaker_id reaches SynthesisConfig instead of the dead voice.config
  assignment (D6 behaviour change)
- version-adaptive: synthesize() (piper-tts >= 1.3) / synthesize_stream_raw()
  (piper-tts 1.2.x); a voice with neither API raises before the first chunk
- sox failure mid-utterance yields the unprocessed WAV and continues
- early aclose() is clean (no torn state, model stays resident)

Stub pattern copied from test_module_piper_fx.py: module_piper imports
sounddevice / soundfile / piper, which load libasound at import time and
don't exist on CI runners — stub them and patch load_config.
"""
import asyncio
import ctypes
import os
import threading
import sys
import types
import unittest
import wave
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


class TestModuleDocstring(unittest.TestCase):
    def test_docstring_documents_speaker_id_behaviour_change(self):
        # D6: the speaker_id behaviour change must be called out in the
        # module docstring (not silently fixed), with the config escape hatch.
        doc = module_piper.__doc__ or ""
        self.assertIn("SynthesisConfig", doc)
        self.assertIn("speaker_id", doc)
        self.assertIn("piper_multispeaker", doc)


class TestPiperStreamingChunks(unittest.TestCase):
    def test_one_wav_chunk_per_synthesize_chunk(self):
        voice = _stream_voice(n=3)
        with mock.patch.object(module_piper, "voice", voice), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg()), \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper(
                "One. Two. Three."))
        self.assertIsNone(raised)
        self.assertEqual(len(chunks), 3)
        for c in chunks:
            with wave.open(c) as w:
                self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()),
                                 (22050, 1, 2))

    def test_empty_text_never_loads_voice_or_synthesizes(self):
        with mock.patch.object(module_piper, "_ensure_voice_loaded") as load, \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("   "))
        self.assertIsNone(raised)
        self.assertEqual(chunks, [])
        load.assert_not_called()


class TestSpeakerIdPassthrough(unittest.TestCase):
    def test_multispeaker_emotion_maps_to_synthesis_config_speaker_id(self):
        record = {}
        voice = _stream_voice(record=record)
        cfg = _tts_cfg(piper_multispeaker=True, piper_speaker_joy=0,
                       piper_speaker_neutral=1)
        with mock.patch.object(module_piper, "voice", voice), \
             mock.patch.object(module_piper, "CONFIG", cfg), \
             mock.patch.object(module_piper, "queue_message"):
            _collect(module_piper.text_to_speech_with_pipelining_piper("Hi.", emotion="joy"))
        self.assertEqual(record["syn_config"].speaker_id, 0)

    def test_multispeaker_off_passes_none(self):
        record = {}
        voice = _stream_voice(record=record)
        with mock.patch.object(module_piper, "voice", voice), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg(piper_multispeaker=False)), \
             mock.patch.object(module_piper, "queue_message"):
            _collect(module_piper.text_to_speech_with_pipelining_piper("Hi.", emotion="joy"))
        self.assertIsNone(record["syn_config"].speaker_id)


class TestVersionAdaptive(unittest.TestCase):
    def test_legacy_stream_raw_voice_yields_raw_bytes_path(self):
        voice = types.SimpleNamespace()
        voice.config = types.SimpleNamespace(sample_rate=22050)
        voice.synthesize_stream_raw = lambda text, speaker_id=None: iter([b"\x11\x22" * 64])
        with mock.patch.object(module_piper, "voice", voice), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg()), \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hi."))
        self.assertIsNone(raised)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].getvalue().startswith(b"RIFF"))

    def test_piper_12x_voice_exposing_both_apis_takes_stream_raw_branch(self):
        """piper-tts 1.2.x exposes BOTH synthesize and synthesize_stream_raw,
        so the branch cannot be chosen by synthesize's presence (1.2.x would
        enter the >= 1.3 branch and die on `from piper import SynthesisConfig`).
        Key on synthesize_stream_raw's presence instead — only 1.2.x has it
        (verified absent on 1.8.0)."""
        calls = {"synthesize": 0, "stream_raw": 0}
        voice = types.SimpleNamespace(config=types.SimpleNamespace(sample_rate=22050))
        def synthesize(text, syn_config=None):
            calls["synthesize"] += 1
            yield types.SimpleNamespace(audio_int16_bytes=b"SYN-BRANCH-PCM")
        def synthesize_stream_raw(text, speaker_id=None):
            calls["stream_raw"] += 1
            return iter([b"RAW-BRANCH-PCM"])
        voice.synthesize = synthesize
        voice.synthesize_stream_raw = synthesize_stream_raw
        with mock.patch.object(module_piper, "voice", voice), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg()), \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hi."))
        self.assertIsNone(raised)
        self.assertEqual(calls, {"synthesize": 0, "stream_raw": 1})
        self.assertEqual(len(chunks), 1)
        self.assertIn(b"RAW-BRANCH-PCM", chunks[0].getvalue())  # WAV-wrapped raw PCM

    def test_synthesize_only_voice_takes_synthesis_config_branch(self):
        """A voice exposing ONLY synthesize (piper-tts >= 1.3, e.g. the
        robot's 1.8.0) must take the SynthesisConfig branch — regression
        guard for the 1.2.x-first reordering."""
        record = {}
        voice = _stream_voice(pcm=b"SYN-BRANCH-PCM", record=record)
        self.assertFalse(hasattr(voice, "synthesize_stream_raw"))
        with mock.patch.object(module_piper, "voice", voice), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg()), \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hi."))
        self.assertIsNone(raised)
        self.assertEqual(len(chunks), 1)
        self.assertIn(b"SYN-BRANCH-PCM", chunks[0].getvalue())
        self.assertIsInstance(record["syn_config"], _SynthesisConfig)

    def test_api_less_voice_raises_before_first_chunk(self):
        voice = types.SimpleNamespace()   # neither synthesize nor synthesize_stream_raw
        voice.config = types.SimpleNamespace(sample_rate=22050)
        with mock.patch.object(module_piper, "voice", voice), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg()), \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hi."))
        self.assertEqual(chunks, [])
        self.assertIsInstance(raised, AttributeError)   # chain failover shape


class TestStreamingFx(unittest.TestCase):
    def test_fx_off_never_forks_sox(self):
        with mock.patch.object(module_piper, "voice", _stream_voice()), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg(piper_robot_fx=False)), \
             mock.patch.object(module_piper.subprocess, "Popen") as popen, \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hi."))
        self.assertIsNone(raised)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].getvalue().startswith(b"RIFF"))
        popen.assert_not_called()

    def test_sox_failure_mid_utterance_yields_unprocessed_and_continues(self):
        popen, _ = _sox_popen(returncode=1, out=b"", err=b"sox failed")
        with mock.patch.object(module_piper, "voice", _stream_voice(n=3)), \
             mock.patch.object(module_piper, "CONFIG",
                               _tts_cfg(piper_robot_fx=True, piper_fx=DEFAULT_FX)), \
             mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", popen), \
             mock.patch.object(module_piper, "queue_message") as qmsg:
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hi."))
        self.assertIsNone(raised)
        self.assertEqual(len(chunks), 3)          # utterance continues
        for c in chunks:
            self.assertTrue(c.getvalue().startswith(b"RIFF"))   # unprocessed, not lost
        self.assertTrue(any("sox FX chain failed" in str(call.args[0])
                            for call in qmsg.call_args_list))


class TestOffLoopAndAclose(unittest.TestCase):
    def test_onnx_and_sox_run_off_main_thread(self):
        import threading
        main_thread = threading.current_thread()
        seen = {}

        def slow_synthesize(text, syn_config=None):
            seen["onnx_thread"] = threading.current_thread()
            yield types.SimpleNamespace(audio_int16_bytes=b"\x11\x22" * 64)

        voice = types.SimpleNamespace(config=types.SimpleNamespace(sample_rate=22050),
                                      synthesize=slow_synthesize)
        popen, _ = _sox_popen()
        def popen_recorder(*a, **k):
            seen["sox_thread"] = threading.current_thread()
            return popen(*a, **k)
        with mock.patch.object(module_piper, "voice", voice), \
             mock.patch.object(module_piper, "CONFIG",
                               _tts_cfg(piper_robot_fx=True, piper_fx=DEFAULT_FX)), \
             mock.patch.object(module_piper.shutil, "which", return_value="/usr/bin/sox"), \
             mock.patch.object(module_piper.subprocess, "Popen", side_effect=popen_recorder), \
             mock.patch.object(module_piper, "queue_message"):
            chunks, raised = _collect(module_piper.text_to_speech_with_pipelining_piper("Hi."))
        self.assertIsNone(raised)
        self.assertIsNot(seen["onnx_thread"], main_thread)
        self.assertIsNot(seen["sox_thread"], main_thread)

    def test_early_aclose_is_clean(self):
        with mock.patch.object(module_piper, "voice", _stream_voice(n=3)), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg()), \
             mock.patch.object(module_piper, "queue_message"):
            async def run():
                agen = module_piper.text_to_speech_with_pipelining_piper("One. Two. Three.")
                first = await agen.__anext__()
                self.assertTrue(first.getvalue().startswith(b"RIFF"))
                await agen.aclose()
            asyncio.run(run())

    def test_cancel_while_worker_inside_generator_does_not_raise(self):
        """P3: barge-in / the per-utterance deadline cancels the consuming
        task while a worker thread is still INSIDE the plain generator
        (ONNX inference mid-sentence). The finally's it.close() then runs on
        the event-loop thread against a RUNNING generator — close() raises
        ValueError('generator already executing'), which the chain swallows,
        leaving the generator never actually closed. The close must be
        guarded: cancellation must surface as CancelledError, never
        ValueError."""
        inside = threading.Event()
        release = threading.Event()

        def blocking_synthesize(text, syn_config=None):
            inside.set()                       # worker thread is INSIDE the generator
            release.wait(10)                   # simulate ONNX inference still running
            yield types.SimpleNamespace(audio_int16_bytes=b"\x11\x22" * 64)

        voice = types.SimpleNamespace(config=types.SimpleNamespace(sample_rate=22050),
                                      synthesize=blocking_synthesize)
        with mock.patch.object(module_piper, "voice", voice), \
             mock.patch.object(module_piper, "CONFIG", _tts_cfg()), \
             mock.patch.object(module_piper, "queue_message"):
            async def run():
                agen = module_piper.text_to_speech_with_pipelining_piper("Hi.")
                nxt = asyncio.ensure_future(agen.__anext__())
                # poll ASYNC so the loop keeps running the __anext__ task
                for _ in range(200):
                    await asyncio.sleep(0.05)
                    if inside.is_set():
                        break
                self.assertTrue(inside.is_set())   # worker is mid-synthesis
                nxt.cancel()                        # barge-in cancels the consumer
                try:
                    with self.assertRaises(asyncio.CancelledError):
                        await nxt                    # ValueError here = the P3
                finally:
                    release.set()                   # let the worker finish + exit
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
