"""Unit tests for Task 5 — timeouts + error classification in the OpenAI TTS
provider (src/modules/module_openai.py).

Covers:
  - per-request HTTP timeout read from [TTS] tts_timeout at call time and
    passed to openai.audio.speech.create (default 30 s when missing)
  - typed error classification consumed by the fallback chain in
    module_tts.py: 429 with a quota body (insufficient_quota / "quota") ->
    QuotaExhausted; 429 otherwise -> RateLimited; 401/4xx -> AuthFailed;
    timeout / connection / 5xx -> TransientError
  - failures raise the typed error instead of yielding None, so the chain's
    failover (zero chunks yielded) actually triggers
  - missing API key -> no request at all (provider skipped before HTTP)
  - the API key never leaks into exceptions or log messages
  - chain-level: QuotaExhausted raised by the provider makes
    generate_tts_audio fail over to the next provider with the reason logged

Conventions follow test_module_stt_elevenlabs.py / test_module_fishaudio.py:
unittest + mock, sys.path bootstrap, config.ini bootstrapped from the
template, and the optional `openai` package (not installed on CI) is stubbed
in sys.modules before importing the module under test.
"""
import asyncio
import importlib
import io
import os
import sys
import tempfile
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

# ── Stub for the optional openai package ──
# module_openai does `import openai` at import time and calls
# openai.audio.speech.create(...), so a stub module with SDK-style exception
# classes (status_code / body attributes) is enough.
#
# The stub must win REGARDLESS of import order. On the robot the real
# `openai` is installed (CI/dev machines do not have it, which is why this
# was never caught there), and any test module imported before this one
# that pulls in modules.module_tts (whose import loads every provider)
# leaves modules.module_openai already imported against the real SDK. The
# old `if "openai" not in sys.modules` guard then skipped the stub and
# these tests built e.g. AuthenticationError(...) against the real
# APIStatusError.__init__, whose signature differs (no `status_code`
# keyword). So: install the stub unconditionally, drop any already-imported
# modules.module_openai and re-import it against the stub, and restore the
# previous sys.modules entries in tearDownModule so test modules that run
# after this one see the state that existed before this file ran.
_oai = types.ModuleType("openai")


class _APIError(Exception):
    def __init__(self, message="", status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class _RateLimitError(_APIError):
    pass


class _AuthenticationError(_APIError):
    pass


class _APIStatusError(_APIError):
    pass


class _APITimeoutError(_APIError):
    pass


class _APIConnectionError(_APIError):
    pass


_oai.APIError = _APIError
_oai.RateLimitError = _RateLimitError
_oai.AuthenticationError = _AuthenticationError
_oai.APIStatusError = _APIStatusError
_oai.APITimeoutError = _APITimeoutError
_oai.APIConnectionError = _APIConnectionError
_oai.api_key = None
_oai.audio = types.SimpleNamespace(speech=types.SimpleNamespace(create=None))

_saved_openai = sys.modules.get("openai")
_saved_module_openai = sys.modules.get("modules.module_openai")
sys.modules["openai"] = _oai
sys.modules.pop("modules.module_openai", None)
module_openai = importlib.import_module("modules.module_openai")


def tearDownModule():
    """Restore the pre-test sys.modules entries (the real openai and the
    module_openai bound to it, when present) so later test modules are not
    affected by the stub."""
    for _name, _saved in (
        ("openai", _saved_openai),
        ("modules.module_openai", _saved_module_openai),
    ):
        if _saved is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _saved


from modules import module_tts  # noqa: E402
from modules.module_fishaudio import (  # noqa: E402
    TTSProviderError,
    QuotaExhausted,
    RateLimited,
    AuthFailed,
    TransientError,
)

OPENAI_KEY = "test-openai-key-abc123"


class _Section(dict):
    """Dict with attribute access — stands in for the TTSConfig section."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _fake_config(**overrides):
    cfg = {
        "openai_api_key": OPENAI_KEY,
        "openai_voice": "onyx",
        "tts_timeout": 30.0,
    }
    cfg.update(overrides)
    return {"TTS": _Section(**cfg)}


class _FakeSpeechResponse:
    """Mimics openai's BinaryAPIResponse (has .read())."""

    def __init__(self, data=b"mp3-fake-bytes"):
        self._data = data

    def read(self):
        return self._data


def _collect(agen):
    """Drive an async generator to completion; return (chunks, exception)."""
    async def run():
        chunks = []
        try:
            async for chunk in agen:
                chunks.append(chunk)
            return chunks, None
        except Exception as exc:
            return chunks, exc
    return asyncio.run(run())


def _run(text, create_side_effect=None, create_return=None, config=None,
         is_wakeword=False, cache_dir=None):
    """Run text_to_speech_with_pipelining_openai with mocked SDK/config/logs.

    Returns (chunks, exception, create_mock, qmsg_mock).
    """
    create = mock.MagicMock(name="openai.audio.speech.create")
    if create_side_effect is not None:
        create.side_effect = create_side_effect
    else:
        create.return_value = create_return or _FakeSpeechResponse()
    cfg = config if config is not None else _fake_config()
    with mock.patch.object(module_openai, "CONFIG", cfg), \
         mock.patch.object(module_openai, "queue_message") as qmsg, \
         mock.patch.object(module_openai.openai.audio.speech, "create", create), \
         mock.patch.object(module_openai, "CACHE_DIR", cache_dir or module_openai.CACHE_DIR):
        chunks, exc = _collect(
            module_openai.text_to_speech_with_pipelining_openai(text, is_wakeword))
    return chunks, exc, create, qmsg


class TestOpenaiTimeout(unittest.TestCase):
    def test_success_passes_configured_timeout_to_sdk(self):
        cfg = _fake_config(tts_timeout=7.5)
        chunks, exc, create, _ = _run("Hello there.", config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertIsInstance(chunks[0], io.BytesIO)
        self.assertEqual(chunks[0].read(), b"mp3-fake-bytes")
        self.assertEqual(create.call_count, 1)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 7.5)
        self.assertEqual(kwargs["model"], "tts-1")
        self.assertEqual(kwargs["voice"], "onyx")
        self.assertEqual(kwargs["input"], "Hello there.")

    def test_default_timeout_30_when_config_key_missing(self):
        cfg = _fake_config()
        del cfg["TTS"]["tts_timeout"]
        _, exc, create, _ = _run("Hello.", config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(create.call_args.kwargs["timeout"], 30.0)

    def test_voice_read_from_config_at_call_time(self):
        cfg = _fake_config(openai_voice="echo")
        _, exc, create, _ = _run("Hello.", config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(create.call_args.kwargs["voice"], "echo")


class TestOpenaiErrorClassification(unittest.TestCase):
    def _err(self, cls, status_code=None, body=None, message="boom"):
        return cls(message, status_code=status_code, body=body)

    def test_429_quota_body_raises_quota_exhausted(self):
        err = self._err(
            module_openai.openai.RateLimitError, status_code=429,
            body={"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}})
        chunks, exc, create, _ = _run("Hi.", create_side_effect=err)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, QuotaExhausted)
        self.assertIsInstance(exc, TTSProviderError)
        self.assertEqual(exc.status_code, 429)
        self.assertIn("quota", str(exc).lower())
        create.assert_called_once()

    def test_429_non_quota_raises_rate_limited(self):
        err = self._err(
            module_openai.openai.RateLimitError, status_code=429,
            body={"error": {"code": "rate_limit_exceeded", "message": "slow down"}})
        chunks, exc, _, _ = _run("Hi.", create_side_effect=err)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, RateLimited)
        self.assertEqual(exc.status_code, 429)

    def test_401_raises_auth_failed(self):
        err = self._err(
            module_openai.openai.AuthenticationError, status_code=401,
            body={"error": {"code": "invalid_api_key", "message": "Incorrect API key"}})
        chunks, exc, _, _ = _run("Hi.", create_side_effect=err)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, AuthFailed)
        self.assertEqual(exc.status_code, 401)

    def test_timeout_raises_transient(self):
        err = self._err(module_openai.openai.APITimeoutError, message="request timed out")
        chunks, exc, _, _ = _run("Hi.", create_side_effect=err)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, TransientError)

    def test_connection_error_raises_transient(self):
        err = self._err(module_openai.openai.APIConnectionError, message="connection failed")
        chunks, exc, _, _ = _run("Hi.", create_side_effect=err)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, TransientError)

    def test_500_raises_transient(self):
        err = self._err(
            module_openai.openai.APIStatusError, status_code=500,
            body={"error": {"message": "internal server error"}})
        chunks, exc, _, _ = _run("Hi.", create_side_effect=err)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, TransientError)

    def test_failure_raises_typed_error_instead_of_yielding_none(self):
        # The chain counts chunks; a None chunk would count as "produced audio"
        # and block failover. Pin that failures raise with zero chunks instead.
        err = self._err(module_openai.openai.RateLimitError, status_code=429,
                        body={"error": {"code": "insufficient_quota", "message": "quota"}})
        chunks, exc, _, _ = _run("Hi.", create_side_effect=err)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, TTSProviderError)

    def test_wakeword_failure_raises_too(self):
        err = self._err(module_openai.openai.AuthenticationError, status_code=401,
                        body={"error": {"code": "invalid_api_key", "message": "bad key"}})
        with tempfile.TemporaryDirectory() as tmp:
            chunks, exc, _, _ = _run("Wake up.", create_side_effect=err,
                                     is_wakeword=True, cache_dir=tmp)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, AuthFailed)

    def test_missing_api_key_no_request(self):
        cfg = _fake_config(openai_api_key="")
        chunks, exc, create, qmsg = _run("Hi.", config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        create.assert_not_called()
        self.assertTrue(any("API key" in str(c.args[0]) or "OPENAI_API_KEY" in str(c.args[0])
                            for c in qmsg.call_args_list if c.args))

    def test_api_key_never_leaks_into_exception_or_logs(self):
        err = self._err(module_openai.openai.AuthenticationError, status_code=401,
                        body={"error": {"code": "invalid_api_key", "message": "Incorrect API key"}})
        chunks, exc, _, qmsg = _run("Hi.", create_side_effect=err)
        self.assertIsNotNone(exc)
        self.assertNotIn(OPENAI_KEY, str(exc))
        for call in qmsg.call_args_list:
            self.assertNotIn(OPENAI_KEY, str(call))


class TestOpenaiWakewordCache(unittest.TestCase):
    def test_wakeword_success_caches_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            chunks, exc, create, _ = _run("Wake up.", is_wakeword=True, cache_dir=tmp)
            self.assertIsNone(exc)
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0].read(), b"mp3-fake-bytes")
            self.assertEqual(create.call_count, 1)
            cached = [f for f in os.listdir(tmp) if f.startswith("openai_")]
            self.assertEqual(len(cached), 1)

            # Second call: served from cache, SDK not called again.
            create2 = mock.MagicMock(side_effect=AssertionError("must not call SDK"))
            with mock.patch.object(module_openai, "CONFIG", _fake_config()), \
                 mock.patch.object(module_openai, "queue_message"), \
                 mock.patch.object(module_openai.openai.audio.speech, "create", create2), \
                 mock.patch.object(module_openai, "CACHE_DIR", tmp):
                chunks2, exc2 = _collect(
                    module_openai.text_to_speech_with_pipelining_openai("Wake up.", True))
            self.assertIsNone(exc2)
            self.assertEqual(len(chunks2), 1)
            self.assertEqual(chunks2[0].read(), b"mp3-fake-bytes")
            create2.assert_not_called()


class TestOpenaiChainFailover(unittest.TestCase):
    """Chain-level pin: a typed error from the openai provider makes
    generate_tts_audio fail over to the next provider and log the reason."""

    def test_quota_on_openai_fails_over_to_next_provider(self):
        cfg_tts = {"TTS": _Section(
            tts_fallback_list=["espeak"], tts_timeout=5.0, ttsurl="", tts_voice="")}
        err = module_openai.openai.RateLimitError(
            "quota", status_code=429,
            body={"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}})

        async def fake_espeak(text):
            yield io.BytesIO(b"espeak-audio")

        with mock.patch.object(module_tts, "CONFIG", cfg_tts), \
             mock.patch.object(module_tts, "text_to_speech_with_pipelining_openai",
                               module_openai.text_to_speech_with_pipelining_openai), \
             mock.patch.object(module_tts, "text_to_speech_with_pipelining_espeak", fake_espeak), \
             mock.patch.object(module_tts, "queue_message") as qmsg_tts, \
             mock.patch.object(module_openai, "CONFIG", _fake_config()), \
             mock.patch.object(module_openai, "queue_message"), \
             mock.patch.object(module_openai.openai.audio.speech, "create",
                               mock.MagicMock(side_effect=err)):
            chunks, exc = _collect(module_tts.generate_tts_audio("Hello there.", "openai"))

        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), b"espeak-audio")
        logs = [str(c.args[0]) for c in qmsg_tts.call_args_list if c.args]
        self.assertTrue(
            any("QuotaExhausted" in line and "failing over" in line for line in logs),
            f"expected a failover log with the typed reason, got: {logs}")


if __name__ == "__main__":
    unittest.main()
