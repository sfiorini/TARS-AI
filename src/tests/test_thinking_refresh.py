"""Tests for the background filler-phrase refresh (Milestone 4).

S-M4-02: raw_complete_llm gains an optional `timeout` param so refresh
calls can pass timeout=60 — a hung refresh request must not hold the
default 120 s.  The param threads into the request and is re-applied on
G9's retry loop (the retry must preserve it, not drop it).
"""
import unittest
from unittest import mock

from modules import module_llm as llm


class TestRawCompleteTimeout(unittest.TestCase):
    def test_timeout_param_used_when_given(self):
        with mock.patch.object(llm, "_http_session") as sess:
            sess.post.return_value.json.return_value = {"choices": [
                {"message": {"content": "ok"}}]}
            llm.raw_complete_llm("hi", timeout=33)
        self.assertEqual(sess.post.call_args.kwargs["timeout"], 33)

    def test_timeout_defaults_to_config(self):
        with mock.patch.object(llm, "_http_session") as sess:
            sess.post.return_value.json.return_value = {"choices": [
                {"message": {"content": "ok"}}]}
            llm.raw_complete_llm("hi")
        self.assertEqual(sess.post.call_args.kwargs["timeout"],
                         llm.CONFIG['LLM'].get('request_timeout', 120))


if __name__ == "__main__":
    unittest.main()
