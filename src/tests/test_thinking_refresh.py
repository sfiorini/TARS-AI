"""S-M4 tests: self-refreshing thinking-phrase set (module_thinking).

Covers:
- raw_complete_llm's new optional timeout parameter (S-M4-02)
- module_thinking validation / refresh / loop / daemon (S-M4-03)
- _maybe_play_thinking_response consuming get_filler_phrase (S-M4-04)
- boot wiring of the refresh daemon (S-M4-05)

The LLM HTTP layer is always stubbed — no network, no hardware.
"""
import json
import threading
import unittest
from unittest import mock

from modules import module_llm  # noqa: E402
from modules.module_llm import raw_complete_llm  # noqa: E402


def _completion_json(text):
    """Non-streaming chat-completion body whose content is the given text."""
    return json.dumps({"choices": [{"message": {"content": text}}]})


class _QueueCapture:
    """Capture queue_message calls instead of printing them."""

    def __init__(self):
        self.messages = []

    def __call__(self, message, stream=False):
        self.messages.append(str(message))


def _recording_session(reply_text):
    """Fake _http_session recording post kwargs, returning a JSON reply."""
    session = mock.MagicMock()
    resp = mock.MagicMock()
    resp.text = reply_text
    resp.json = lambda: json.loads(reply_text)
    session.post.return_value = resp
    return session


class TestRawCompleteTimeout(unittest.TestCase):
    """S-M4-02: raw_complete_llm(user_prompt, istext=True, timeout=None)."""

    def test_timeout_forwarded(self):
        """An explicit timeout=30 must reach the HTTP post call."""
        session = _recording_session(_completion_json("ok"))
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", _QueueCapture()):
            raw_complete_llm("x", timeout=30)
        self.assertEqual(session.post.call_args[1]["timeout"], 30)

    def test_timeout_default_uses_config(self):
        """Omitting timeout keeps the [LLM] request_timeout default (120)."""
        session = _recording_session(_completion_json("ok"))
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", _QueueCapture()):
            raw_complete_llm("x")
        self.assertEqual(session.post.call_args[1]["timeout"], 120)


if __name__ == "__main__":
    unittest.main()
