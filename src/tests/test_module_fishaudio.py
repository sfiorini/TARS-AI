"""Unit tests for the Fish.audio TTS provider (module_fishaudio.py, plan Task 2).

Covers: success path (raw wav bytes -> BytesIO chunk), request shape
(POST https://api.fish.audio/v1/tts, Authorization Bearer + ALWAYS-present
"model" header, body {text, reference_id, format:"wav", latency from [TTS] fishaudio_latency (default balanced)}),
error classification (402 -> QuotaExhausted no retry; 429 -> one backoff
retry then RateLimited; 400/401 -> AuthFailed no retry; 5xx/timeout -> one
backoff retry then TransientError), model/reference_id/timeout read from
config at call time, no API key / empty text -> no HTTP request at all, and
that the API key never leaks into exceptions or log messages.
"""
import asyncio
import io
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
    _TEMPLATE = os.path.join(_SRC, "config.ini.template")
    try:
        import shutil
        shutil.copyfile(_TEMPLATE, _CONFIG_INI)
    except OSError as exc:
        raise RuntimeError(
            "Cannot bootstrap src/config.ini from src/config.ini.template "
            f"(required for module imports): {exc}"
        ) from exc

from modules import module_fishaudio  # noqa: E402
fa = module_fishaudio

FISH_URL = "https://api.fish.audio/v1/tts"
FISH_TARS_REF = "6a57e35f5c8244dbb5d012a3c434ec16"
ENV_KEY = "test-fish-key-abc123"


class _FakeResponse:
    def __init__(self, status_code, payload=None, content=b"", text=""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _fake_config(**overrides):
    """Stand-in for load_config()'s return: {"TTS": <attr-accessible obj>}."""
    cfg = {
        "fishaudio_model": "s2.1-pro-free",
        "fishaudio_reference_id": "",
        "tts_timeout": 30.0,
    }
    cfg.update(overrides)
    return {"TTS": types.SimpleNamespace(**cfg)}


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


def _run(text="Hello there.", env_key=ENV_KEY, responses=None, exc=None,
         config=None, **gen_kwargs):
    """Run text_to_speech_with_fishaudio with mocked env/requests/config.

    Returns (chunks, exception, post_mock, queue_message_mock, backoff_mock).
    env_key=None clears ALL ambient env (hermetic missing-key simulation).
    """
    env = {"FISH_AUDIO_API_KEY": env_key} if env_key is not None else {}
    clear_env = env_key is None
    post = mock.MagicMock(name="requests.post")
    if exc is not None:
        post.side_effect = exc
    elif responses is not None:
        if len(responses) == 1:
            post.return_value = responses[0]
        else:
            post.side_effect = list(responses)
    else:
        post.return_value = _FakeResponse(200, content=b"RIFF-fake-wav")
    cfg = config if config is not None else _fake_config()
    with mock.patch.dict(os.environ, env, clear=clear_env), \
         mock.patch.object(module_fishaudio, "CONFIG", cfg), \
         mock.patch.object(module_fishaudio.requests, "post", post), \
         mock.patch.object(module_fishaudio, "queue_message") as qmsg, \
         mock.patch.object(module_fishaudio, "_backoff", new=mock.AsyncMock()) as backoff:
        chunks, raised = _collect(
            module_fishaudio.text_to_speech_with_fishaudio(text, **gen_kwargs)
        )
    return chunks, raised, post, qmsg, backoff


class TestFishAudioSuccess(unittest.TestCase):
    def test_success_yields_wav_chunk(self):
        chunks, exc, post, qmsg, _ = _run()
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertIsInstance(chunks[0], io.BytesIO)
        self.assertEqual(chunks[0].read(), b"RIFF-fake-wav")
        post.assert_called_once()

    def test_request_shape_url_headers_body_timeout(self):
        _, exc, post, _, _ = _run(text="Hello there.")
        self.assertIsNone(exc)
        args, kwargs = post.call_args
        self.assertEqual(args[0], FISH_URL)
        # Authorization bearer + model header ALWAYS sent (omitting "model"
        # silently bills the paid model)
        self.assertEqual(kwargs["headers"], {
            "Authorization": f"Bearer {ENV_KEY}",
            "model": "s2.1-pro-free",
        })
        self.assertEqual(kwargs["json"], {
            "text": "Hello there.",
            "format": "wav",
            "latency": "balanced",
        })
        self.assertEqual(kwargs["timeout"], 30.0)

    def test_latency_override_from_config(self):
        cfg = _fake_config()
        cfg = _fake_config(fishaudio_latency="low")
        with mock.patch.object(fa, "CONFIG", cfg), \
             mock.patch.dict(os.environ, {"FISH_AUDIO_API_KEY": "k"}), \
             mock.patch.object(fa.requests, "post", return_value=_FakeResponse(200, content=b"RIFF-x")) as post:
            async def run():
                agen = fa.text_to_speech_with_fishaudio("Hi.")
                return [c async for c in agen]
            asyncio.run(run())
        self.assertEqual(post.call_args.kwargs["json"]["latency"], "low")

    def test_invalid_latency_falls_back_to_balanced(self):
        cfg = _fake_config()
        cfg = _fake_config(fishaudio_latency="turbo")
        with mock.patch.object(fa, "CONFIG", cfg), \
             mock.patch.dict(os.environ, {"FISH_AUDIO_API_KEY": "k"}), \
             mock.patch.object(fa.requests, "post", return_value=_FakeResponse(200, content=b"RIFF-x")) as post:
            async def run():
                agen = fa.text_to_speech_with_fishaudio("Hi.")
                return [c async for c in agen]
            asyncio.run(run())
        self.assertEqual(post.call_args.kwargs["json"]["latency"], "balanced")

    def test_model_header_always_sent_even_when_config_missing(self):
        cfg = _fake_config()
        del cfg["TTS"].fishaudio_model  # config has no model -> default pinned
        _, exc, post, _, _ = _run(config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["headers"]["model"], "s2.1-pro-free")

    def test_custom_model_from_config_at_call_time(self):
        cfg = _fake_config(fishaudio_model="s2.1-pro")
        _, exc, post, _, _ = _run(config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["headers"]["model"], "s2.1-pro")

    def test_explicit_model_argument_overrides_config(self):
        _, exc, post, _, _ = _run(config=_fake_config(fishaudio_model="s2.1-pro-free"),
                                  model="s2.1-pro")
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["headers"]["model"], "s2.1-pro")

    def test_reference_id_sent_when_configured(self):
        cfg = _fake_config(fishaudio_reference_id=FISH_TARS_REF)
        _, exc, post, _, _ = _run(config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["json"]["reference_id"], FISH_TARS_REF)

    def test_reference_id_omitted_from_body_when_empty(self):
        _, exc, post, _, _ = _run()
        self.assertIsNone(exc)
        self.assertNotIn("reference_id", post.call_args.kwargs["json"])

    def test_explicit_reference_id_argument_overrides_config(self):
        _, exc, post, _, _ = _run(reference_id="deadbeef")
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["json"]["reference_id"], "deadbeef")

    def test_timeout_from_config_at_call_time(self):
        cfg = _fake_config(tts_timeout=7.5)
        _, exc, post, _, _ = _run(config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["timeout"], 7.5)

    def test_explicit_timeout_argument_overrides_config(self):
        _, exc, post, _, _ = _run(config=_fake_config(tts_timeout=7.5), timeout=3.0)
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["timeout"], 3.0)

    def test_multi_sentence_text_yields_one_chunk_per_sentence(self):
        # Sentences are merged up to 80 chars (ElevenLabs policy), so use two
        # long sentences to force two separate requests/chunks.
        s1 = "This is the first long sentence that TARS needs to speak out loud today."
        s2 = "And here is the second long sentence which will not fit into the first chunk."
        chunks, exc, post, _, _ = _run(text=f"{s1} {s2}")
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(post.call_count, 2)
        bodies = [c.kwargs["json"]["text"] for c in post.call_args_list]
        self.assertEqual(bodies, [s1, s2])

    def test_empty_200_body_yields_no_chunk_no_exception(self):
        responses = [_FakeResponse(200, content=b"")]
        chunks, exc, post, qmsg, _ = _run(responses=responses)
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        post.assert_called_once()
        warnings = [c for c in qmsg.call_args_list if "WARNING" in str(c.args[0])]
        self.assertTrue(any("empty" in str(c.args[0]).lower() for c in warnings))


class TestFishAudioErrorClassification(unittest.TestCase):
    def test_402_raises_quota_exhausted_without_retry(self):
        responses = [_FakeResponse(402, payload={"message": "no credit left", "status": 402})]
        chunks, exc, post, _, backoff = _run(responses=responses)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, module_fishaudio.QuotaExhausted)
        self.assertIn("no credit left", str(exc))
        self.assertEqual(exc.status_code, 402)
        post.assert_called_once()          # no retry on quota
        backoff.assert_not_awaited()

    def test_429_retried_once_then_rate_limited(self):
        responses = [
            _FakeResponse(429, payload={"message": "concurrency limit", "status": 429}),
            _FakeResponse(429, payload={"message": "concurrency limit", "status": 429}),
        ]
        chunks, exc, post, _, backoff = _run(responses=responses)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, module_fishaudio.RateLimited)
        self.assertEqual(exc.status_code, 429)
        self.assertEqual(post.call_count, 2)   # exactly one backoff retry
        backoff.assert_awaited_once()

    def test_429_then_success_yields_chunk(self):
        responses = [
            _FakeResponse(429, payload={"message": "concurrency limit"}),
            _FakeResponse(200, content=b"RIFF-after-retry"),
        ]
        chunks, exc, post, _, backoff = _run(responses=responses)
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), b"RIFF-after-retry")
        self.assertEqual(post.call_count, 2)
        backoff.assert_awaited_once()

    def test_401_raises_auth_failed_without_retry(self):
        responses = [_FakeResponse(401, payload={"message": "invalid api key", "status": 401})]
        chunks, exc, post, _, backoff = _run(responses=responses)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, module_fishaudio.AuthFailed)
        self.assertEqual(exc.status_code, 401)
        post.assert_called_once()
        backoff.assert_not_awaited()

    def test_400_raises_auth_failed_without_retry(self):
        responses = [_FakeResponse(400, payload={"message": "bad request", "status": 400})]
        _, exc, post, _, backoff = _run(responses=responses)
        self.assertIsInstance(exc, module_fishaudio.AuthFailed)
        post.assert_called_once()
        backoff.assert_not_awaited()

    def test_timeout_retried_once_then_transient(self):
        exc_in = module_fishaudio.requests.exceptions.Timeout("timed out")
        chunks, exc, post, _, backoff = _run(exc=exc_in)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, module_fishaudio.TransientError)
        self.assertEqual(post.call_count, 2)
        backoff.assert_awaited_once()

    def test_connection_error_retried_once_then_transient(self):
        exc_in = module_fishaudio.requests.exceptions.ConnectionError("boom")
        _, exc, post, _, backoff = _run(exc=exc_in)
        self.assertIsInstance(exc, module_fishaudio.TransientError)
        self.assertEqual(post.call_count, 2)
        backoff.assert_awaited_once()

    def test_5xx_retried_once_then_transient(self):
        responses = [
            _FakeResponse(500, text="internal error"),
            _FakeResponse(503, text="unavailable"),
        ]
        chunks, exc, post, _, backoff = _run(responses=responses)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, module_fishaudio.TransientError)
        self.assertEqual(post.call_count, 2)
        backoff.assert_awaited_once()

    def test_5xx_then_success_yields_chunk(self):
        responses = [
            _FakeResponse(503, text="unavailable"),
            _FakeResponse(200, content=b"RIFF-after-5xx"),
        ]
        chunks, exc, post, _, _ = _run(responses=responses)
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(post.call_count, 2)

    def test_retry_does_not_loop_forever_on_persistent_429(self):
        # 10 x 429 must still result in exactly 2 requests (one retry).
        responses = [_FakeResponse(429, payload={"message": "slow down"}) for _ in range(10)]
        _, exc, post, _, _ = _run(responses=responses)
        self.assertIsInstance(exc, module_fishaudio.RateLimited)
        self.assertEqual(post.call_count, 2)

    def test_error_message_never_contains_api_key(self):
        responses = [_FakeResponse(402, payload={"message": "no credit", "status": 402})]
        _, exc, _, qmsg, _ = _run(responses=responses)
        self.assertIsNotNone(exc)
        self.assertNotIn(ENV_KEY, str(exc))
        for call in qmsg.call_args_list:
            self.assertNotIn(ENV_KEY, str(call.args))

    def test_error_body_without_json_uses_text(self):
        responses = [_FakeResponse(401, text="unauthorized")]
        _, exc, _, _, _ = _run(responses=responses)
        self.assertIsInstance(exc, module_fishaudio.AuthFailed)
        self.assertIn("unauthorized", str(exc))

    def test_typed_errors_share_base_class(self):
        for cls in (module_fishaudio.QuotaExhausted, module_fishaudio.RateLimited,
                    module_fishaudio.AuthFailed, module_fishaudio.TransientError):
            self.assertTrue(issubclass(cls, module_fishaudio.TTSProviderError))


class TestFishAudioGuards(unittest.TestCase):
    def test_missing_api_key_makes_no_request(self):
        chunks, exc, post, qmsg, _ = _run(env_key=None)
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        post.assert_not_called()
        warnings = [c for c in qmsg.call_args_list if "WARNING" in str(c.args[0])]
        self.assertTrue(any("FISH_AUDIO_API_KEY" in str(c.args[0]) for c in warnings))

    def test_empty_text_makes_no_request(self):
        chunks, exc, post, _, _ = _run(text="   ")
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        post.assert_not_called()

    def test_none_text_makes_no_request(self):
        chunks, exc, post, _, _ = _run(text=None)
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestNonBlockingHTTP(unittest.TestCase):
    """Regression: requests.post must run in a worker thread (asyncio.to_thread),
    not on the event loop thread — a blocking call froze the whole app during TTS."""

    def test_http_runs_off_event_loop(self):
        import threading
        main_thread = threading.current_thread()
        seen = {}

        def fake_post(*a, **k):
            seen["thread"] = threading.current_thread()
            return _FakeResponse(200, content=b"RIFF-x")

        cfg = _fake_config()
        with mock.patch.object(fa, "CONFIG", cfg), \
             mock.patch.dict(os.environ, {"FISH_AUDIO_API_KEY": "k"}), \
             mock.patch.object(fa.requests, "post", side_effect=fake_post):
            async def run():
                agen = fa.text_to_speech_with_fishaudio("Hi.")
                return [c async for c in agen]
            asyncio.run(run())
        self.assertIn("thread", seen)
        self.assertIsNot(seen["thread"], main_thread)
