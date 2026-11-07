"""Unit tests for the ElevenLabs Scribe STT processor (module_stt)."""
import io
import os
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

from modules import module_stt


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _make_self(language="english", model=""):
    """Build a lightweight stand-in for an STTManager instance."""
    return types.SimpleNamespace(
        config={"STT": {
            "language": language,
            "external_url": "http://server.local:5678",
            "elevenlabs_stt_model": model,
        }},
        DEBUG=False,
        MODEL_RATE=16000,
        _record_audio_chunks=mock.MagicMock(return_value=([b"\x01\x00" * 100], None)),
        _chunks_to_wav_buffer=mock.MagicMock(return_value=io.BytesIO(b"RIFF-fake-wav")),
        _emit_result=mock.MagicMock(side_effect=lambda text, extra=None: {"text": text}),
        _transcribe_with_server=mock.MagicMock(return_value={"text": "fallback-result"}),
    )


def _run(fake_self, response=None, exc=None, env_key="test-key-abc123"):
    """Run _transcribe_with_elevenlabs with module-level mocks; return (result, post, queued)."""
    env = {"ELEVENLABS_API_KEY": env_key} if env_key is not None else {}
    # Hermetic: when simulating a missing key, clear ALL ambient env so a
    # real ELEVENLABS_API_KEY on the host (e.g. on tars) cannot leak in.
    clear_env = env_key is None
    post = mock.MagicMock()
    if exc is not None:
        post.side_effect = exc
    elif response is not None:
        post.return_value = response
    with mock.patch.dict(os.environ, env, clear=clear_env), \
         mock.patch.object(module_stt, "queue_message") as qmsg, \
         mock.patch.object(module_stt.requests, "post", post):
        result = module_stt.STTManager._transcribe_with_elevenlabs(fake_self)
    return result, post, qmsg


class TestElevenlabsStt(unittest.TestCase):
    def test_success_emits_result_no_fallback(self):
        fake = _make_self()
        response = _FakeResponse(200, {"language_code": "eng", "text": "hello world"})
        result, post, qmsg = _run(fake, response=response)

        self.assertEqual(result, {"text": "hello world"})
        fake._emit_result.assert_called_once()
        args, kwargs = fake._emit_result.call_args
        self.assertEqual(args[0], "hello world")
        fake._transcribe_with_server.assert_not_called()

        # Request shape: auth header, multipart fields, wav file, timeout
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"], {"xi-api-key": "test-key-abc123"})
        self.assertEqual(kwargs["data"]["model_id"], "scribe_v2")
        self.assertEqual(kwargs["data"]["tag_audio_events"], "false")
        self.assertEqual(kwargs["data"]["language_code"], "en")
        self.assertEqual(kwargs["files"]["file"][0], "audio.wav")
        self.assertEqual(kwargs["timeout"], 15)
        self.assertEqual(kwargs["data"]["timestamps_granularity"], "none")
        self.assertEqual(post.call_args.args[0], "https://api.elevenlabs.io/v1/speech-to-text")

    def test_http_error_falls_back_with_warning(self):
        fake = _make_self()
        response = _FakeResponse(401, text="invalid api key")
        result, post, qmsg = _run(fake, response=response)

        self.assertEqual(result, {"text": "fallback-result"})
        fake._transcribe_with_server.assert_called_once()
        fake._emit_result.assert_not_called()
        warnings = [c for c in qmsg.call_args_list if "WARNING" in c.args[0]]
        self.assertTrue(any("falling back to external STT" in c.args[0] for c in warnings))

    def test_missing_api_key_falls_back_without_request(self):
        fake = _make_self()
        result, post, qmsg = _run(fake, env_key=None)

        self.assertEqual(result, {"text": "fallback-result"})
        fake._transcribe_with_server.assert_called_once()
        post.assert_not_called()
        warnings = [c for c in qmsg.call_args_list if "WARNING" in c.args[0]]
        self.assertTrue(any("falling back to external STT" in c.args[0] for c in warnings))

    def test_empty_text_falls_back(self):
        fake = _make_self()
        response = _FakeResponse(200, {"language_code": "eng", "text": "   "})
        result, post, qmsg = _run(fake, response=response)

        self.assertEqual(result, {"text": "fallback-result"})
        fake._transcribe_with_server.assert_called_once()
        fake._emit_result.assert_not_called()
        warnings = [c for c in qmsg.call_args_list if "WARNING" in c.args[0]]
        self.assertTrue(any("falling back to external STT" in c.args[0] for c in warnings))

    def test_request_exception_falls_back(self):
        fake = _make_self()
        result, post, qmsg = _run(
            fake, exc=module_stt.requests.exceptions.ConnectionError("boom"))

        self.assertEqual(result, {"text": "fallback-result"})
        fake._transcribe_with_server.assert_called_once()
        fake._emit_result.assert_not_called()
        warnings = [c for c in qmsg.call_args_list if "WARNING" in c.args[0]]
        self.assertTrue(any("falling back to external STT" in c.args[0] for c in warnings))

    def test_language_mapping_spanish(self):
        fake = _make_self(language="spanish")
        response = _FakeResponse(200, {"text": "hola mundo"})
        _, post, _ = _run(fake, response=response)
        self.assertEqual(post.call_args.kwargs["data"]["language_code"], "es")

    def test_unknown_language_omits_language_code(self):
        fake = _make_self(language="klingon")
        response = _FakeResponse(200, {"text": "hello"})
        _, post, _ = _run(fake, response=response)
        self.assertNotIn("language_code", post.call_args.kwargs["data"])

    def test_configured_model_overrides_default(self):
        fake = _make_self(model="scribe_v1")
        response = _FakeResponse(200, {"text": "hello"})
        _, post, _ = _run(fake, response=response)
        self.assertEqual(post.call_args.kwargs["data"]["model_id"], "scribe_v1")


if __name__ == "__main__":
    unittest.main()
