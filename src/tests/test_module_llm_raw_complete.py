"""Tests for raw_complete_llm requesting a NON-streaming response (P1 fix).

Runtime evidence (robot, live): _prepare_request_data hardcodes
``"stream": True`` in the request body, so the backend answers with an SSE
body (``data: {...}\\n\\n`` lines, HTTP 200).  raw_complete_llm then calls
``response.json()`` on that SSE text -> JSONDecodeError ("Expecting value:
line 1 column 1") -> caught by the generic RequestException handler ->
returns None.  Consequences: the deferred tool loop's follow-up completion
never works (the robot speaks raw tool text aloud), and the vision skills
skill_capture_camera_view / skill_network_camera are silently broken.
process_completion is unaffected because it consumes the stream properly;
its dependency on _prepare_request_data's streaming default must NOT change.
"""
import json
import os
import shutil
import unittest
from unittest import mock

# Bootstrap the gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_INI = os.path.join(_SRC, "config.ini")
if not os.path.exists(_CONFIG_INI):
    _TEMPLATE = os.path.join(_SRC, "config.ini.template")
    try:
        shutil.copyfile(_TEMPLATE, _CONFIG_INI)
    except OSError as exc:
        raise RuntimeError(
            "Cannot bootstrap src/config.ini from src/config.ini.template "
            f"(required for module imports): {exc}"
        ) from exc

import requests  # noqa: E402

from modules import module_llm  # noqa: E402
from modules.module_llm import raw_complete_llm, followup_completion  # noqa: E402


def _sse_response(sse_body, status_code=200):
    """Fake requests response carrying an SSE body (what a streaming backend
    actually returns): .json() raises JSONDecodeError like the real one."""
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.text = sse_body

    def _raise():
        raise requests.exceptions.JSONDecodeError(
            "Expecting value: line 1 column 1 (char 0)", sse_body, 0
        )

    resp.json = _raise
    resp.raise_for_status = mock.MagicMock()
    return resp


def _json_response(body_text, status_code=200):
    """Fake requests response carrying a plain (non-streaming) JSON body."""
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.text = body_text
    resp.json = lambda: json.loads(body_text)
    resp.raise_for_status = mock.MagicMock()
    return resp


def _fake_session(json_body, sse_body=None):
    """Fake _http_session whose post() mirrors the real backend contract:
    it answers with the SSE body when the request asked for a stream, and
    with the plain JSON body when it did not."""
    session = mock.MagicMock()

    def _post(url, headers=None, json=None, **kwargs):
        if json is not None and json.get("stream") and sse_body is not None:
            return _sse_response(sse_body)
        return _json_response(json_body)

    session.post.side_effect = _post
    return session


def _completion_json(text):
    """Non-streaming chat-completion body whose content is the given text."""
    return json.dumps({"choices": [{"message": {"content": text}}]})


class _QueueCapture:
    """Capture module_llm's queue_message calls instead of printing them."""

    def __init__(self):
        self.messages = []

    def __call__(self, message, stream=False):
        self.messages.append(str(message))


def _http_error_response(status_code):
    """Fake requests response whose raise_for_status raises a REAL
    requests.HTTPError carrying the status (the retry gate reads
    e.response.status_code)."""
    resp = mock.MagicMock()
    resp.status_code = status_code

    def _raise():
        raise requests.exceptions.HTTPError(
            f"{status_code} error", response=resp)

    resp.raise_for_status = _raise
    return resp


class TestRawCompleteStreaming(unittest.TestCase):
    """[P1] raw_complete_llm must ask the backend for a NON-streaming reply."""

    def test_request_body_sets_stream_false(self):
        """The decisive test: the POST payload must carry stream: False.

        _prepare_request_data hardcodes "stream": True; raw_complete_llm
        consumes a single JSON body via response.json(), so it must override
        the flag before the POST.  Fails today because stream is True.
        """
        session = _fake_session(_completion_json("hello"))
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", _QueueCapture()):
            raw_complete_llm("say hi")
        payload = session.post.call_args[1]["json"]
        self.assertIs(payload["stream"], False)

    def test_non_streaming_json_body_returns_text(self):
        """With a plain JSON body, the reply text is extracted and returned."""
        session = _fake_session(_completion_json("The answer is 5."))
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", _QueueCapture()):
            result = raw_complete_llm("what is 2 + 3?")
        self.assertEqual(result, "The answer is 5.")

    def test_sse_backend_returns_text_because_request_is_non_streaming(self):
        """End-to-end reproduction of the robot failure, inverted by the fix.

        The fake backend honours the request: ask for a stream and you get
        the SSE body (raw_complete_llm then returns None, exactly like on
        the robot); ask for a non-streaming reply and you get parseable JSON.
        """
        sse = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        session = _fake_session(_completion_json("Two plus three is five."),
                                sse_body=sse)
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", _QueueCapture()):
            result = raw_complete_llm("what is 2 + 3?")
        self.assertEqual(result, "Two plus three is five.")

    def test_non_json_body_logs_status_and_truncated_body(self):
        """A non-JSON body must log HTTP status + first ~200 chars, not a
        bare 'Expecting value' that hides the failure mode."""
        session = mock.MagicMock()
        session.post.return_value = _sse_response("data: {oops", status_code=200)
        capture = _QueueCapture()
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", capture):
            result = raw_complete_llm("hello")
        self.assertIsNone(result)
        joined = "\n".join(capture.messages)
        self.assertIn("200", joined)
        self.assertIn("data: {oops", joined)


class TestRawCompleteRetry(unittest.TestCase):
    """[G9] raw_complete_llm retries 429/5xx exactly once (house TTS
    pattern), never auth/client errors, connection errors or non-JSON
    bodies. Every retry test patches the sleep — nothing waits 1 s."""

    def _run(self, session):
        capture = _QueueCapture()
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", capture), \
             mock.patch.object(module_llm.time, "sleep") as sleep:
            result = raw_complete_llm("say hi")
        return result, capture, sleep

    def test_429_retried_once_then_success(self):
        session = mock.MagicMock()
        session.post.side_effect = [
            _http_error_response(429),
            _json_response(_completion_json("recovered")),
        ]
        result, capture, sleep = self._run(session)
        self.assertEqual(result, "recovered")
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(sleep.call_args[0][0], 1.0)
        for call in session.post.call_args_list:
            self.assertIs(call[1]["json"]["stream"], False)

    def test_429_twice_returns_none(self):
        session = mock.MagicMock()
        session.post.side_effect = [
            _http_error_response(429), _http_error_response(429)]
        result, capture, sleep = self._run(session)
        self.assertIsNone(result)
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_503_retried_once_then_success(self):
        session = mock.MagicMock()
        session.post.side_effect = [
            _http_error_response(503),
            _json_response(_completion_json("recovered")),
        ]
        result, capture, sleep = self._run(session)
        self.assertEqual(result, "recovered")
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_401_not_retried(self):
        session = mock.MagicMock()
        session.post.side_effect = [_http_error_response(401)]
        result, capture, sleep = self._run(session)
        self.assertIsNone(result)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(sleep.call_count, 0)

    def test_404_not_retried(self):
        session = mock.MagicMock()
        session.post.side_effect = [_http_error_response(404)]
        result, capture, sleep = self._run(session)
        self.assertIsNone(result)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(sleep.call_count, 0)

    def test_connection_error_not_retried(self):
        session = mock.MagicMock()
        session.post.side_effect = requests.exceptions.ConnectionError(
            "boom")
        result, capture, sleep = self._run(session)
        self.assertIsNone(result)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(sleep.call_count, 0)

    def test_non_json_body_not_retried(self):
        # Clause-order trap guard: requests' JSONDecodeError IS a
        # RequestException but NOT an HTTPError, so the inner retry clause
        # must stay `except requests.HTTPError` — if it were widened to
        # RequestException, this exception would be caught by it. The
        # JSONDecodeError therefore carries a RETRYABLE 429 response so
        # the widened clause WOULD retry (2 posts, 1 sleep) and fail this
        # test on call_count; a response-less exception would pass under
        # BOTH clauses and guard nothing.
        class _Resp:
            status_code = 429
            text = "data: {oops"

            def raise_for_status(self):
                pass

            def json(self):
                raise requests.exceptions.JSONDecodeError(
                    "Expecting value", "data: {oops", 0, response=_Resp())

        session = mock.MagicMock()
        session.post.return_value = _Resp()
        result, capture, sleep = self._run(session)
        self.assertIsNone(result)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(sleep.call_count, 0)


class TestFollowupCompletion(unittest.TestCase):
    """[P1] followup_completion end-to-end with a non-streaming backend."""

    def test_followup_returns_parsed_dict(self):
        """Follow-up carrying tool results must return the parsed reply dict,
        not None — the deferred loop depends on this (D1)."""
        reply_json = json.dumps({"reply": "Two plus three equals five."})
        # stream-aware fake: if the request still asks for a stream (the bug),
        # the backend answers with SSE and the follow-up collapses to None.
        sse = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        session = _fake_session(_completion_json(reply_json), sse_body=sse)
        capture = _QueueCapture()
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", capture), \
             mock.patch.object(module_llm, "build_prompt",
                               return_value="prompt-with-tool-results"):
            result = followup_completion(
                "what is 2 + 3?", "let me check", "mcp_example_add_numbers: 2 + 3 = 5",
                source="voice",
            )
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("reply"), "Two plus three equals five.")


class TestPrepareRequestDataUnchanged(unittest.TestCase):
    """process_completion depends on _prepare_request_data's streaming
    default — the fix must not change it."""

    def test_prepare_request_data_still_defaults_to_streaming(self):
        url, data = module_llm._prepare_request_data(
            module_llm.CONFIG['LLM']['llm_backend'], "hello")
        self.assertIs(data["stream"], True)


if __name__ == "__main__":
    unittest.main()
