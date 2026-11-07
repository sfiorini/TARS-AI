"""Unit tests for the Speechify TTS provider (module_speechify.py, plan Task 3).

Covers: success path (JSON {audio_data: base64} -> decoded BytesIO chunk),
request shape (POST https://api.speechify.ai/v1/audio/speech, Authorization
Bearer header, body {input, voice_id, audio_format:"wav", model}), error
classification branched on the body's error.code —
  payment_required / spend_cap_exceeded / spend_budget_exceeded /
  spend_limit_exceeded -> QuotaExhausted (no retry)
  rate_limited / concurrency_limit_reached -> RateLimited (one backoff retry
  honoring the Retry-After response header, then fail)
  401/403/other 4xx -> AuthFailed (no retry)
  5xx / timeout -> one backoff retry then TransientError
voice_id / model / timeout read from config at call time, no API key / empty
text -> no HTTP request at all, and that the API key never leaks into
exceptions or log messages. The typed errors are the SAME classes as
module_fishaudio's so the fallback chain (Task 4) can catch one hierarchy.
"""
import asyncio
import base64
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
from modules import module_speechify  # noqa: E402

SPEECHIFY_URL = "https://api.speechify.ai/v1/audio/speech"
ENV_KEY = "test-speechify-key-abc123"
WAV_BYTES = b"RIFF-fake-wav"
WAV_B64 = base64.b64encode(WAV_BYTES).decode("ascii")

QUOTA_CODES = [
    "payment_required",
    "spend_cap_exceeded",
    "spend_budget_exceeded",
    "spend_limit_exceeded",
]
RATE_CODES = ["rate_limited", "concurrency_limit_reached"]


class _FakeResponse:
    def __init__(self, status_code, payload=None, content=b"", text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text
        self.headers = headers if headers is not None else {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _ok_response(audio_b64=WAV_B64):
    return _FakeResponse(200, payload={"audio_data": audio_b64})


def _err_response(status_code, code, message="some error", headers=None):
    return _FakeResponse(status_code, payload={"error": {"code": code, "message": message}},
                         headers=headers)


def _fake_config(**overrides):
    """Stand-in for load_config()'s return: {"TTS": <attr-accessible obj>}."""
    cfg = {
        "speechify_voice_id": "george",
        "speechify_model": "simba-3.5",
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
    """Run text_to_speech_with_speechify with mocked env/requests/config.

    Returns (chunks, exception, post_mock, queue_message_mock, backoff_mock).
    env_key=None clears ALL ambient env (hermetic missing-key simulation).
    """
    env = {"SPEECHIFY_API_KEY": env_key} if env_key is not None else {}
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
        post.return_value = _ok_response()
    cfg = config if config is not None else _fake_config()
    with mock.patch.dict(os.environ, env, clear=clear_env), \
         mock.patch.object(module_speechify, "CONFIG", cfg), \
         mock.patch.object(module_speechify.requests, "post", post), \
         mock.patch.object(module_speechify, "queue_message") as qmsg, \
         mock.patch.object(module_speechify, "_backoff", new=mock.AsyncMock()) as backoff:
        chunks, raised = _collect(
            module_speechify.text_to_speech_with_speechify(text, **gen_kwargs)
        )
    return chunks, raised, post, qmsg, backoff


class TestSpeechifySuccess(unittest.TestCase):
    def test_success_decodes_base64_audio_into_bytesio_chunk(self):
        chunks, exc, post, _, _ = _run()
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertIsInstance(chunks[0], io.BytesIO)
        self.assertEqual(chunks[0].read(), WAV_BYTES)
        post.assert_called_once()

    def test_request_shape_url_headers_body_timeout(self):
        _, exc, post, _, _ = _run(text="Hello there.")
        self.assertIsNone(exc)
        args, kwargs = post.call_args
        self.assertEqual(args[0], SPEECHIFY_URL)
        # Only the Authorization header carries the key; never logged.
        self.assertEqual(kwargs["headers"], {"Authorization": f"Bearer {ENV_KEY}"})
        self.assertEqual(kwargs["json"], {
            "input": "Hello there.",
            "voice_id": "george",
            "audio_format": "wav",
            "model": "simba-3.5",
        })
        self.assertEqual(kwargs["timeout"], 30.0)

    def test_voice_id_from_config_at_call_time(self):
        cfg = _fake_config(speechify_voice_id="henry")
        _, exc, post, _, _ = _run(config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["json"]["voice_id"], "henry")

    def test_model_from_config_at_call_time(self):
        cfg = _fake_config(speechify_model="simba-3.5-turbo")
        _, exc, post, _, _ = _run(config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["json"]["model"], "simba-3.5-turbo")

    def test_model_default_when_config_key_missing(self):
        cfg = _fake_config()
        del cfg["TTS"].speechify_model  # config has no model -> default pinned
        _, exc, post, _, _ = _run(config=cfg)
        self.assertIsNone(exc)
        self.assertEqual(post.call_args.kwargs["json"]["model"], "simba-3.5")

    def test_explicit_voice_id_and_model_arguments_override_config(self):
        _, exc, post, _, _ = _run(voice_id="cliff", model="simba-v1")
        self.assertIsNone(exc)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["voice_id"], "cliff")
        self.assertEqual(body["model"], "simba-v1")

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
        # Sentences are merged up to 80 chars, so use two long sentences to
        # force two separate requests/chunks.
        s1 = "This is the first long sentence that TARS needs to speak out loud today."
        s2 = "And here is the second long sentence which will not fit into the first chunk."
        chunks, exc, post, _, _ = _run(text=f"{s1} {s2}")
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(chunks[0].read(), WAV_BYTES)
        bodies = [c.kwargs["json"]["input"] for c in post.call_args_list]
        self.assertEqual(bodies, [s1, s2])

    def test_base64_with_whitespace_still_decodes(self):
        b64 = base64.b64encode(WAV_BYTES).decode("ascii")
        spaced = b64[:4] + "\n" + b64[4:]
        chunks, exc, _, _, _ = _run(responses=[_ok_response(audio_b64=spaced)])
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), WAV_BYTES)

    def test_200_without_audio_data_yields_no_chunk_no_exception(self):
        chunks, exc, post, qmsg, _ = _run(responses=[_FakeResponse(200, payload={})])
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        post.assert_called_once()
        warnings = [c for c in qmsg.call_args_list if "WARNING" in str(c.args[0])]
        self.assertTrue(any("audio_data" in str(c.args[0]) for c in warnings))

    def test_200_with_empty_audio_data_yields_no_chunk(self):
        chunks, exc, _, qmsg, _ = _run(responses=[_ok_response(audio_b64="")])
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])

    def test_200_with_invalid_base64_yields_no_chunk_no_exception(self):
        chunks, exc, _, qmsg, _ = _run(responses=[_ok_response(audio_b64="!!!not-base64!!!")])
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        warnings = [c for c in qmsg.call_args_list if "WARNING" in str(c.args[0])]
        self.assertTrue(warnings)

    def test_200_with_non_json_body_yields_no_chunk(self):
        chunks, exc, _, _, _ = _run(responses=[_FakeResponse(200, payload=None, text="ok")])
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])


class TestSpeechifyErrorCodes(unittest.TestCase):
    def test_each_quota_code_raises_quota_exhausted_without_retry(self):
        for code in QUOTA_CODES:
            with self.subTest(code=code):
                responses = [_err_response(402, code, f"quota via {code}")]
                chunks, exc, post, _, backoff = _run(responses=responses)
                self.assertEqual(chunks, [])
                self.assertIsInstance(exc, module_speechify.QuotaExhausted)
                self.assertIn(code, str(exc))
                self.assertEqual(exc.status_code, 402)
                post.assert_called_once()       # no retry on quota
                backoff.assert_not_awaited()

    def test_quota_code_wins_over_status_branching(self):
        # Classification branches on error.code, not only on the status:
        # spend_cap_exceeded at a non-402 status is still QuotaExhausted.
        responses = [_err_response(400, "spend_cap_exceeded", "cap hit")]
        chunks, exc, post, _, backoff = _run(responses=responses)
        self.assertIsInstance(exc, module_speechify.QuotaExhausted)
        post.assert_called_once()
        backoff.assert_not_awaited()

    def test_402_without_code_still_quota_exhausted(self):
        responses = [_FakeResponse(402, payload={"message": "no credit"})]
        _, exc, post, _, _ = _run(responses=responses)
        self.assertIsInstance(exc, module_speechify.QuotaExhausted)
        self.assertEqual(exc.status_code, 402)
        post.assert_called_once()

    def test_each_rate_code_retried_once_then_rate_limited(self):
        for code in RATE_CODES:
            with self.subTest(code=code):
                responses = [
                    _err_response(429, code, "slow down", headers={"Retry-After": "2"}),
                    _err_response(429, code, "slow down", headers={"Retry-After": "2"}),
                ]
                chunks, exc, post, _, backoff = _run(responses=responses)
                self.assertEqual(chunks, [])
                self.assertIsInstance(exc, module_speechify.RateLimited)
                self.assertEqual(exc.status_code, 429)
                self.assertIn(code, str(exc))
                self.assertEqual(post.call_count, 2)   # exactly one retry
                backoff.assert_awaited_once_with(2.0)  # Retry-After honored

    def test_429_without_code_is_rate_limited(self):
        responses = [_FakeResponse(429, payload={"message": "too many"}) for _ in range(5)]
        _, exc, post, _, backoff = _run(responses=responses)
        self.assertIsInstance(exc, module_speechify.RateLimited)
        self.assertEqual(post.call_count, 2)
        backoff.assert_awaited_once()  # no Retry-After -> default backoff

    def test_rate_limited_then_success_yields_chunk(self):
        responses = [
            _err_response(429, "rate_limited", "slow down", headers={"Retry-After": "1"}),
            _ok_response(audio_b64=base64.b64encode(b"RIFF-after-retry").decode("ascii")),
        ]
        chunks, exc, post, _, backoff = _run(responses=responses)
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), b"RIFF-after-retry")
        self.assertEqual(post.call_count, 2)
        backoff.assert_awaited_once_with(1.0)

    def test_retry_after_capped_at_maximum(self):
        responses = [
            _err_response(429, "rate_limited", "slow down", headers={"Retry-After": "3600"}),
            _err_response(429, "rate_limited", "slow down", headers={"Retry-After": "3600"}),
        ]
        _, exc, _, _, backoff = _run(responses=responses)
        self.assertIsInstance(exc, module_speechify.RateLimited)
        # A huge Retry-After must not hang the chain: capped at the max.
        self.assertEqual(backoff.await_args.args[0], module_speechify._MAX_RETRY_AFTER_SECONDS)

    def test_retry_does_not_loop_forever_on_persistent_429(self):
        responses = [_err_response(429, "rate_limited", "slow down") for _ in range(10)]
        _, exc, post, _, _ = _run(responses=responses)
        self.assertIsInstance(exc, module_speechify.RateLimited)
        self.assertEqual(post.call_count, 2)

    def test_401_raises_auth_failed_without_retry(self):
        responses = [_err_response(401, "unauthorized", "invalid api key")]
        chunks, exc, post, _, backoff = _run(responses=responses)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, module_speechify.AuthFailed)
        self.assertEqual(exc.status_code, 401)
        post.assert_called_once()
        backoff.assert_not_awaited()

    def test_401_without_code_still_auth_failed(self):
        responses = [_FakeResponse(401, text="unauthorized")]
        _, exc, post, _, backoff = _run(responses=responses)
        self.assertIsInstance(exc, module_speechify.AuthFailed)
        self.assertIn("unauthorized", str(exc))
        post.assert_called_once()
        backoff.assert_not_awaited()

    def test_403_and_400_raise_auth_failed_without_retry(self):
        for status in (400, 403):
            with self.subTest(status=status):
                responses = [_FakeResponse(status, payload={"error": {"code": "bad", "message": "nope"}})]
                _, exc, post, _, backoff = _run(responses=responses)
                self.assertIsInstance(exc, module_speechify.AuthFailed)
                self.assertEqual(exc.status_code, status)
                post.assert_called_once()
                backoff.assert_not_awaited()

    def test_timeout_retried_once_then_transient(self):
        exc_in = module_speechify.requests.exceptions.Timeout("timed out")
        chunks, exc, post, _, backoff = _run(exc=exc_in)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, module_speechify.TransientError)
        self.assertEqual(post.call_count, 2)
        backoff.assert_awaited_once()

    def test_connection_error_retried_once_then_transient(self):
        exc_in = module_speechify.requests.exceptions.ConnectionError("boom")
        _, exc, post, _, backoff = _run(exc=exc_in)
        self.assertIsInstance(exc, module_speechify.TransientError)
        self.assertEqual(post.call_count, 2)
        backoff.assert_awaited_once()

    def test_5xx_retried_once_then_transient(self):
        responses = [
            _FakeResponse(500, text="internal error"),
            _FakeResponse(503, text="unavailable"),
        ]
        chunks, exc, post, _, backoff = _run(responses=responses)
        self.assertEqual(chunks, [])
        self.assertIsInstance(exc, module_speechify.TransientError)
        self.assertEqual(post.call_count, 2)
        backoff.assert_awaited_once()

    def test_5xx_then_success_yields_chunk(self):
        responses = [_FakeResponse(503, text="unavailable"), _ok_response()]
        chunks, exc, post, _, _ = _run(responses=responses)
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(post.call_count, 2)

    def test_error_message_never_contains_api_key(self):
        responses = [_err_response(402, "payment_required", "no credit")]
        _, exc, post, qmsg, _ = _run(responses=responses)
        self.assertIsNotNone(exc)
        # The key must never leak into exceptions or log messages...
        self.assertNotIn(ENV_KEY, str(exc))
        for call in qmsg.call_args_list:
            self.assertNotIn(ENV_KEY, str(call.args))
        # ...nor into the request body (it belongs only in the auth header).
        for call in post.call_args_list:
            self.assertNotIn(ENV_KEY, str(call.kwargs.get("json"))) 

    def test_typed_errors_are_shared_with_fishaudio_for_the_chain(self):
        # Task 4's chain runner catches one hierarchy: speechify must raise the
        # exact same classes as module_fishaudio, not lookalikes.
        for cls in (module_speechify.QuotaExhausted, module_speechify.RateLimited,
                    module_speechify.AuthFailed, module_speechify.TransientError):
            self.assertIs(cls, getattr(module_fishaudio, cls.__name__))
            self.assertTrue(issubclass(cls, module_fishaudio.TTSProviderError))


class TestSpeechifyGuards(unittest.TestCase):
    def test_missing_api_key_makes_no_request(self):
        chunks, exc, post, qmsg, _ = _run(env_key=None)
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        post.assert_not_called()
        warnings = [c for c in qmsg.call_args_list if "WARNING" in str(c.args[0])]
        self.assertTrue(any("SPEECHIFY_API_KEY" in str(c.args[0]) for c in warnings))

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
