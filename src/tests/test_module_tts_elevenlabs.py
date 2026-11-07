"""Unit tests for Task 5 — timeouts + error classification in the ElevenLabs
TTS provider (src/modules/module_elevenlabs.py).

Covers:
  - per-request HTTP timeout read from [TTS] tts_timeout at call time and
    passed to the aiohttp client on BOTH the streaming and the complete path
    (default 30 s when the key is missing/unparseable)
  - typed error classification consumed by the fallback chain in
    module_tts.py: 429/401 with a quota body -> QuotaExhausted (no retry);
    429 without quota -> one backoff retry then RateLimited; 401/other 4xx
    -> AuthFailed (no retry); 5xx / timeout -> one backoff retry then
    TransientError
  - HTTP 200 with an empty body still returns None (the chain's
    "produced no audio" failover keeps working)
  - missing API key -> no HTTP request at all, warning logged
  - the API key never leaks into exceptions or log messages
  - chain-level: QuotaExhausted raised by the provider makes
    generate_tts_audio fail over to the next provider with the reason logged

Conventions follow test_module_stt_elevenlabs.py / test_module_fishaudio.py:
unittest + mock, sys.path bootstrap, config.ini bootstrapped from the
template, fake response objects, and the optional third-party packages that
CI does not install (elevenlabs, aiohttp) are stubbed in sys.modules before
importing the module under test.
"""
import asyncio
import inspect
import io
import json
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

# ── Stubs for optional third-party packages not installed on CI runners ──
# module_elevenlabs does `from elevenlabs.client import ElevenLabs` at import
# time and `import aiohttp` lazily inside the request helpers, so stubbing
# sys.modules before the import is enough (module_tts imports this module in
# a try/except ImportError, so a missing package is fine there too).
if "elevenlabs" not in sys.modules:
    _el_pkg = types.ModuleType("elevenlabs")
    _el_client = types.ModuleType("elevenlabs.client")

    class _StubElevenLabsClient:
        def __init__(self, *args, **kwargs):
            pass

    _el_client.ElevenLabs = _StubElevenLabsClient
    _el_pkg.client = _el_client
    sys.modules["elevenlabs"] = _el_pkg
    sys.modules["elevenlabs.client"] = _el_client

if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")

    class _StubClientError(Exception):
        pass

    class _StubClientTimeout:
        def __init__(self, total=None, **kwargs):
            self.total = total
            self.kwargs = kwargs

    _aiohttp.ClientError = _StubClientError
    _aiohttp.ClientTimeout = _StubClientTimeout
    _aiohttp.ClientSession = None  # patched per test
    sys.modules["aiohttp"] = _aiohttp

from modules import module_elevenlabs  # noqa: E402
from modules import module_tts  # noqa: E402
from modules.module_fishaudio import (  # noqa: E402
    TTSProviderError,
    QuotaExhausted,
    RateLimited,
    AuthFailed,
    TransientError,
)

EL_KEY = "test-el-key-abc123"
VOICE_ID = "voice-abc-123"
QUOTA_BODY = json.dumps({
    "detail": {"status": "quota_exceeded", "message": "You have exceeded your current quota"}
})
RATE_BODY = json.dumps({
    "detail": {"status": "rate_limited", "message": "Slow down, too many concurrent requests"}
})
AUTH_BODY = json.dumps({
    "detail": {"status": "invalid_api_key", "message": "Incorrect API key provided"}
})


class _Section(dict):
    """Dict with attribute access — stands in for the TTSConfig section."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _fake_config(**overrides):
    cfg = {
        "elevenlabs_api_key": EL_KEY,
        "elevenlabs_voice_id": VOICE_ID,
        "elevenlabs_model": "eleven_multilingual_v2",
        "tts_timeout": 30.0,
    }
    cfg.update(overrides)
    return {"TTS": _Section(**cfg)}


class _FakeAiohttpResponse:
    """Async-context-manager response mimicking aiohttp's ClientResponse."""

    def __init__(self, status=200, body=b"", text=None, read_exc=None):
        self.status = status
        self._body = body
        self._text = text if text is not None else body.decode("utf-8", "replace")
        self._read_exc = read_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._text

    async def read(self):
        if self._read_exc is not None:
            raise self._read_exc
        return self._body


class _FakeClientSession:
    """Stand-in for aiohttp.ClientSession; records every POST."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if not self._outcomes:
            raise AssertionError("unexpected extra POST")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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


def _await(coro):
    async def run():
        try:
            return await coro, None
        except Exception as exc:
            return None, exc
    return asyncio.run(run())


def _run(func_name, text, outcomes, config=None, is_wakeword=False, cache_dir=None):
    """Run a module_elevenlabs entry point with mocked config/session/logs.

    Returns (chunks, exception, session, qmsg_mock, backoff_mock).
    """
    session = _FakeClientSession(outcomes)
    cfg = config if config is not None else _fake_config()
    aiohttp_mod = sys.modules["aiohttp"]
    with mock.patch.object(module_elevenlabs, "CONFIG", cfg), \
         mock.patch.object(module_elevenlabs, "queue_message") as qmsg, \
         mock.patch.object(module_elevenlabs, "_backoff", new=mock.AsyncMock()) as backoff, \
         mock.patch.object(aiohttp_mod, "ClientSession", lambda *a, **k: session), \
         mock.patch.object(module_elevenlabs, "CACHE_DIR", cache_dir or module_elevenlabs.CACHE_DIR):
        if func_name == "text_to_speech_with_pipelining_elevenlabs":
            target = module_elevenlabs.text_to_speech_with_pipelining_elevenlabs(text, is_wakeword)
        else:
            target = getattr(module_elevenlabs, func_name)(text)
        if inspect.isasyncgen(target):
            chunks, exc = _collect(target)
        else:
            result, exc = _await(target)
            chunks = [result] if result is not None else []
    return chunks, exc, session, qmsg, backoff


class TestElevenlabsTimeout(unittest.TestCase):
    def test_streaming_passes_configured_timeout_to_client(self):
        cfg = _fake_config(tts_timeout=7.5)
        chunks, exc, session, _, _ = _run(
            "synthesize_elevenlabs_streaming", "Hello there.",
            [_FakeAiohttpResponse(200, b"RIFF-fake")], config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), b"RIFF-fake")
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(session.posts[0]["timeout"].total, 7.5)

    def test_complete_passes_configured_timeout_to_client(self):
        cfg = _fake_config(tts_timeout=12.25)
        chunks, exc, session, _, _ = _run(
            "synthesize_elevenlabs_complete", "Wake up.",
            [_FakeAiohttpResponse(200, b"RIFF-fake")], config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(session.posts[0]["timeout"].total, 12.25)

    def test_default_timeout_30_when_config_key_missing(self):
        cfg = _fake_config()
        del cfg["TTS"]["tts_timeout"]
        _, exc, session, _, _ = _run(
            "synthesize_elevenlabs_streaming", "Hello.",
            [_FakeAiohttpResponse(200, b"RIFF-fake")], config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(session.posts[0]["timeout"].total, 30.0)

    def test_request_shape_url_headers_payload(self):
        _, exc, session, _, _ = _run(
            "synthesize_elevenlabs_streaming", "Hello there.",
            [_FakeAiohttpResponse(200, b"RIFF-fake")])
        self.assertIsNone(exc)
        post = session.posts[0]
        self.assertEqual(
            post["url"],
            f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream")
        self.assertEqual(post["headers"]["xi-api-key"], EL_KEY)
        self.assertEqual(post["headers"]["Content-Type"], "application/json")
        self.assertEqual(post["json"]["text"], "Hello there.")
        self.assertEqual(post["json"]["model_id"], "eleven_multilingual_v2")
        self.assertEqual(post["json"]["output_format"], "mp3_44100_128")
        self.assertTrue(post["json"]["enable_ssml"])


class TestElevenlabsErrorClassification(unittest.TestCase):
    def test_429_quota_body_raises_quota_exhausted_without_retry(self):
        chunks, exc, session, _, backoff = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(429, text=QUOTA_BODY)])
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, QuotaExhausted)
        self.assertIsInstance(exc, TTSProviderError)
        self.assertEqual(exc.status_code, 429)
        self.assertIn("quota", str(exc).lower())
        self.assertEqual(len(session.posts), 1)  # no retry on quota
        backoff.assert_not_awaited()

    def test_401_quota_body_raises_quota_exhausted(self):
        chunks, exc, session, _, backoff = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(401, text=QUOTA_BODY)])
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, QuotaExhausted)
        self.assertEqual(exc.status_code, 401)
        self.assertEqual(len(session.posts), 1)
        backoff.assert_not_awaited()

    def test_401_invalid_key_raises_auth_failed_without_retry(self):
        chunks, exc, session, _, backoff = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(401, text=AUTH_BODY)])
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, AuthFailed)
        self.assertEqual(exc.status_code, 401)
        self.assertEqual(len(session.posts), 1)
        backoff.assert_not_awaited()

    def test_400_raises_auth_failed_without_retry(self):
        chunks, exc, session, _, _ = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(400, text=json.dumps({"detail": {"status": "bad_request", "message": "nope"}}))])
        self.assertIsInstance(exc, AuthFailed)
        self.assertEqual(len(session.posts), 1)

    def test_429_non_quota_retried_once_then_rate_limited(self):
        chunks, exc, session, _, backoff = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(429, text=RATE_BODY), _FakeAiohttpResponse(429, text=RATE_BODY)])
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, RateLimited)
        self.assertEqual(exc.status_code, 429)
        self.assertEqual(len(session.posts), 2)  # exactly one retry
        backoff.assert_awaited_once()

    def test_500_retried_once_then_transient(self):
        chunks, exc, session, _, backoff = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(500, text="internal error"),
             _FakeAiohttpResponse(502, text="bad gateway")])
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, TransientError)
        self.assertEqual(len(session.posts), 2)
        backoff.assert_awaited_once()

    def test_timeout_retried_once_then_transient(self):
        chunks, exc, session, _, backoff = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [asyncio.TimeoutError(), asyncio.TimeoutError()])
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, TransientError)
        self.assertEqual(len(session.posts), 2)
        backoff.assert_awaited_once()

    def test_read_timeout_mid_response_raises_transient(self):
        chunks, exc, session, _, _ = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(200, read_exc=asyncio.TimeoutError()),
             _FakeAiohttpResponse(200, read_exc=asyncio.TimeoutError())])
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, TransientError)
        self.assertEqual(len(session.posts), 2)

    def test_empty_200_body_returns_none_no_exception(self):
        # The chain treats "no chunks" as failover — keep that path working.
        chunks, exc, session, qmsg, _ = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(200, b"")])
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        self.assertEqual(len(session.posts), 1)
        self.assertTrue(any("empty" in str(c.args[0]).lower() for c in qmsg.call_args_list if c.args))

    def test_missing_api_key_no_request(self):
        cfg = _fake_config(elevenlabs_api_key="")
        chunks, exc, session, qmsg, _ = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(200, b"RIFF-fake")], config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        self.assertEqual(session.posts, [])  # no key => no HTTP call
        self.assertTrue(any("API key" in str(c.args[0]) for c in qmsg.call_args_list if c.args))

    def test_api_key_never_leaks_into_exception_or_logs(self):
        _, exc, _, qmsg, _ = _run(
            "synthesize_elevenlabs_streaming", "Hi.",
            [_FakeAiohttpResponse(401, text=AUTH_BODY)])
        self.assertIsNotNone(exc)
        self.assertNotIn(EL_KEY, str(exc))
        for call in qmsg.call_args_list:
            self.assertNotIn(EL_KEY, str(call))


class TestElevenlabsProviderGenerator(unittest.TestCase):
    def test_generator_yields_chunk_per_sentence_on_success(self):
        s1 = "This is the first long sentence that TARS needs to speak out loud today."
        s2 = "And here is the second long sentence which will not fit into the first chunk."
        chunks, exc, session, _, _ = _run(
            "text_to_speech_with_pipelining_elevenlabs", f"{s1} {s2}",
            [_FakeAiohttpResponse(200, b"RIFF-one"), _FakeAiohttpResponse(200, b"RIFF-two")])
        self.assertIsNone(exc)
        self.assertEqual([c.read() for c in chunks], [b"RIFF-one", b"RIFF-two"])
        self.assertEqual(len(session.posts), 2)

    def test_generator_raises_quota_so_chain_can_fail_over(self):
        chunks, exc, session, _, _ = _run(
            "text_to_speech_with_pipelining_elevenlabs", "Hello there.",
            [_FakeAiohttpResponse(429, text=QUOTA_BODY)])
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, QuotaExhausted)

    def test_generator_wakeword_path_uses_complete_endpoint_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            chunks, exc, session, _, _ = _run(
                "text_to_speech_with_pipelining_elevenlabs", "Wake up.",
                [_FakeAiohttpResponse(200, b"RIFF-wake")], is_wakeword=True, cache_dir=tmp)
            self.assertIsNone(exc)
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0].read(), b"RIFF-wake")
            # wakeword path uses the complete endpoint (no /stream suffix)
            self.assertNotIn("/stream", session.posts[0]["url"])
            cached = [f for f in os.listdir(tmp) if f.startswith("elevenlabs_")]
            self.assertEqual(len(cached), 1)

    def test_generator_wakeword_quota_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            chunks, exc, _, _, _ = _run(
                "text_to_speech_with_pipelining_elevenlabs", "Wake up.",
                [_FakeAiohttpResponse(429, text=QUOTA_BODY)], is_wakeword=True, cache_dir=tmp)
            self.assertEqual(chunks, [])
            self.assertIsInstance(exc, QuotaExhausted)


class TestElevenlabsChainFailover(unittest.TestCase):
    """Chain-level pin: a typed error from the elevenlabs provider makes
    generate_tts_audio fail over to the next provider and log the reason."""

    def test_quota_on_elevenlabs_fails_over_to_next_provider(self):
        cfg_tts = {"TTS": _Section(
            tts_fallback_list=["espeak"], tts_timeout=5.0, ttsurl="", tts_voice="")}
        session = _FakeClientSession([_FakeAiohttpResponse(429, text=QUOTA_BODY)])
        aiohttp_mod = sys.modules["aiohttp"]

        async def fake_espeak(text):
            yield io.BytesIO(b"espeak-audio")

        with mock.patch.object(module_tts, "CONFIG", cfg_tts), \
             mock.patch.object(module_tts, "text_to_speech_with_pipelining_elevenlabs",
                               module_elevenlabs.text_to_speech_with_pipelining_elevenlabs), \
             mock.patch.object(module_tts, "text_to_speech_with_pipelining_espeak", fake_espeak), \
             mock.patch.object(module_tts, "queue_message") as qmsg_tts, \
             mock.patch.object(module_elevenlabs, "CONFIG", _fake_config()), \
             mock.patch.object(module_elevenlabs, "queue_message"), \
             mock.patch.object(module_elevenlabs, "_backoff", new=mock.AsyncMock()), \
             mock.patch.object(aiohttp_mod, "ClientSession", lambda *a, **k: session):
            chunks, exc = _collect(module_tts.generate_tts_audio("Hello there.", "elevenlabs"))

        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), b"espeak-audio")
        logs = [str(c.args[0]) for c in qmsg_tts.call_args_list if c.args]
        self.assertTrue(
            any("QuotaExhausted" in line and "failing over" in line for line in logs),
            f"expected a failover log with the typed reason, got: {logs}")

    def test_timeout_on_elevenlabs_fails_over_with_transient_reason(self):
        cfg_tts = {"TTS": _Section(
            tts_fallback_list=["espeak"], tts_timeout=5.0, ttsurl="", tts_voice="")}
        session = _FakeClientSession([asyncio.TimeoutError(), asyncio.TimeoutError()])
        aiohttp_mod = sys.modules["aiohttp"]

        async def fake_espeak(text):
            yield io.BytesIO(b"espeak-audio")

        with mock.patch.object(module_tts, "CONFIG", cfg_tts), \
             mock.patch.object(module_tts, "text_to_speech_with_pipelining_elevenlabs",
                               module_elevenlabs.text_to_speech_with_pipelining_elevenlabs), \
             mock.patch.object(module_tts, "text_to_speech_with_pipelining_espeak", fake_espeak), \
             mock.patch.object(module_tts, "queue_message") as qmsg_tts, \
             mock.patch.object(module_elevenlabs, "CONFIG", _fake_config()), \
             mock.patch.object(module_elevenlabs, "queue_message"), \
             mock.patch.object(module_elevenlabs, "_backoff", new=mock.AsyncMock()), \
             mock.patch.object(aiohttp_mod, "ClientSession", lambda *a, **k: session):
            chunks, exc = _collect(module_tts.generate_tts_audio("Hello there.", "elevenlabs"))

        self.assertIsNone(exc)
        self.assertEqual([c.read() for c in chunks], [b"espeak-audio"])
        self.assertEqual(len(session.posts), 2)  # one backoff retry, then fail over
        logs = [str(c.args[0]) for c in qmsg_tts.call_args_list if c.args]
        self.assertTrue(
            any("TransientError" in line and "failing over" in line for line in logs),
            f"expected a failover log with the typed reason, got: {logs}")


if __name__ == "__main__":
    unittest.main()
