"""Barge-in handoff transcription.

Regression: on the robot `wake_word_processor = atomik` and
`bargein_mode = voiceprint`, so neither `sherpa_recognizer` nor
`fastrtc_model` is ever loaded (those loaders only run for the sherpa-onnx /
fastrtc wake-word processors).  `_bargein_transcribe()` therefore returned
None for every interruption, `transcribe_bargein_audio()` returned None,
module_main saw no text, and the app fell back to wake-word listening — the
user had to repeat whatever they said to interrupt.

The handoff must transcribe with the CONFIGURED STT processor, and must never
emit a result (no duplicate round, no speaker-ID side effects).
"""
import io
import os
import types
import unittest
from unittest import mock

import numpy as np

import modules.module_stt as module_stt


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _manager(stt_processor="elevenlabs", quick=None):
    """Lightweight STTManager stand-in (no hardware, no models)."""
    m = types.SimpleNamespace(
        config={"STT": {
            "language": "english",
            "stt_processor": stt_processor,
            "elevenlabs_stt_model": "",
        }},
        DEBUG=False,
        MODEL_RATE=16000,
        silence_threshold=1000.0,
        sherpa_recognizer=None,
        fastrtc_model=None,
        _emit_result=mock.MagicMock(),
        _chunks_to_wav_buffer=mock.MagicMock(return_value=io.BytesIO(b"RIFF-fake")),
        _bargein_transcribe=mock.MagicMock(return_value=quick),
    )
    # Exercise the real fallback method against the stand-in (unbound call),
    # so the test covers production code rather than a stub of it.
    m._transcribe_captured_audio = (
        module_stt.STTManager._transcribe_captured_audio.__get__(m))
    return m


def _run(m, response=None, exc=None, env_key="k-test"):
    env = {"ELEVENLABS_API_KEY": env_key} if env_key else {}
    post = mock.MagicMock()
    if exc is not None:
        post.side_effect = exc
    elif response is not None:
        post.return_value = response
    audio = np.full(16000, 3000, dtype=np.int16)
    with mock.patch.dict(os.environ, env, clear=(env_key is None)), \
         mock.patch.object(module_stt, "queue_message") as qmsg, \
         mock.patch.object(module_stt.requests, "post", post):
        result = module_stt.STTManager.transcribe_bargein_audio(m, audio)
    return result, post, qmsg


class TestBargeinHandoffTranscription(unittest.TestCase):
    def test_uses_configured_processor_when_quick_recognizers_absent(self):
        """THE regression: no sherpa/fastrtc on the robot, yet the
        interruption must still be transcribed."""
        m = _manager()
        result, post, _ = _run(m, response=_FakeResponse(200, {"text": "stop talking"}))
        self.assertEqual(result, {"text": "stop talking"})
        post.assert_called_once()

    def test_does_not_emit_result(self):
        """Transcribing an interruption must not fire the utterance
        callback / speaker-ID pipeline — module_main feeds the text into the
        round itself."""
        m = _manager()
        _run(m, response=_FakeResponse(200, {"text": "wait"}))
        m._emit_result.assert_not_called()

    def test_quick_recognizer_still_wins_when_present(self):
        """If a quick recognizer IS loaded, it is used and no HTTP happens."""
        m = _manager(quick="quick text")
        result, post, _ = _run(m, response=_FakeResponse(200, {"text": "cloud"}))
        self.assertEqual(result, {"text": "quick text"})
        post.assert_not_called()

    def test_returns_none_for_non_elevenlabs_processor(self):
        """No quick recognizer and a processor we cannot drive here -> None
        (caller falls back to wake-word listening) but no exception."""
        m = _manager(stt_processor="whisper")
        result, post, _ = _run(m, response=_FakeResponse(200, {"text": "x"}))
        self.assertIsNone(result)
        post.assert_not_called()

    def test_returns_none_without_api_key(self):
        m = _manager()
        result, post, _ = _run(m, env_key=None)
        self.assertIsNone(result)
        post.assert_not_called()

    def test_http_failure_returns_none_and_warns(self):
        """Fail visible, not silent: a failed handoff transcription logs."""
        m = _manager()
        result, post, qmsg = _run(m, response=_FakeResponse(401))
        self.assertIsNone(result)
        msgs = [c.args[0] for c in qmsg.call_args_list]
        self.assertTrue(any("barge-in" in x.lower() and "transcri" in x.lower() for x in msgs),
                        f"no warning about the failed handoff transcription: {msgs}")

    def test_empty_text_returns_none(self):
        m = _manager()
        result, _, _ = _run(m, response=_FakeResponse(200, {"text": "   "}))
        self.assertIsNone(result)

    def test_request_exception_returns_none_and_warns(self):
        m = _manager()
        result, _, qmsg = _run(m, exc=module_stt.requests.exceptions.ConnectionError("boom"))
        self.assertIsNone(result)
        msgs = [c.args[0] for c in qmsg.call_args_list]
        self.assertTrue(any("barge-in" in x.lower() for x in msgs), f"no warning: {msgs}")


if __name__ == "__main__":
    unittest.main()
