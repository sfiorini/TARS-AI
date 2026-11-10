"""Non-streaming LLM content must go through one extractor.

GLM-family models return ``message.content`` as a LIST of blocks
(``[{"type": "thinking", ...}, {"type": "text", "text": ...}]``) rather than a
string.  The streaming path normalised this from the start (`_delta_text`),
but two non-streaming consumers called ``.strip()`` on the raw value:

  * skills/skill_web_search.py  -> "Search summarization failed: 'list' object
    has no attribute 'strip'" — the only weather path, so asking for the
    weather produced 12 s of silence and returned to listening.
  * skills/skill_tars_radio.py  -> same pattern, would fail identically.

These tests pin the extractor's contract and the two call sites.
"""
import types
import unittest
from unittest import mock

import modules.module_llm as module_llm


class TestContentText(unittest.TestCase):
    def test_plain_string_passthrough(self):
        self.assertEqual(module_llm.content_text("hello"), "hello")

    def test_thinking_block_is_dropped(self):
        content = [
            {"type": "thinking", "thinking": "the user wants the weather"},
            {"type": "text", "text": "It is raining in Rome."},
        ]
        self.assertEqual(module_llm.content_text(content), "It is raining in Rome.")

    def test_multiple_text_blocks_are_joined(self):
        content = [
            {"type": "text", "text": "part one "},
            {"type": "thinking", "thinking": "x"},
            {"type": "text", "text": "part two"},
        ]
        self.assertEqual(module_llm.content_text(content), "part one part two")

    def test_bare_strings_in_a_list(self):
        self.assertEqual(module_llm.content_text(["Hel", "lo"]), "Hello")

    def test_none_and_garbage_yield_empty_string(self):
        for value in (None, 42, {}, [], {"type": "text", "text": "x"}):
            self.assertEqual(module_llm.content_text(value), "")

    def test_non_string_text_field_is_ignored(self):
        self.assertEqual(module_llm.content_text([{"type": "text", "text": None}]), "")

    def test_private_alias_still_works(self):
        """module_vision and the existing delta tests import _delta_text."""
        self.assertEqual(module_llm._delta_text("x"), "x")


class TestWebSearchSummarizeUsesExtractor(unittest.TestCase):
    """The regression that made weather queries go silent."""

    def _run_summarize(self, content, payload_capture=None):
        from skills import skill_web_search

        response = mock.MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": content}}]}
        response.raise_for_status.return_value = None

        def _post(url, headers=None, json=None, timeout=None):
            if payload_capture is not None:
                payload_capture.append(json)
            return response

        fake_cfg = {
            "LLM": {"api_key": "k", "llm_backend": "other", "base_url": "https://x",
                    "other_model": "m", "temperature": 0.7},
            "CHAR": {"character_name": "TARS"},
        }
        # the skill does `import requests` INSIDE the function, so patch the
        # module-level attribute rather than a name on the skill module
        with mock.patch("requests.post", _post), \
             mock.patch("modules.module_config.load_config", return_value=fake_cfg), \
             mock.patch("modules.module_llm.character_manager",
                        types.SimpleNamespace(char_name="TARS"), create=True):
            return skill_web_search._summarize_search_results("results text", "weather in Rome")

    def test_block_list_content_is_summarised(self):
        """Used to raise AttributeError -> None -> 0-char tool result."""
        out = self._run_summarize([
            {"type": "thinking", "thinking": "reasoning"},
            {"type": "text", "text": "It is 27 degrees and raining in Rome."},
        ])
        self.assertEqual(out, "It is 27 degrees and raining in Rome.")

    def test_plain_string_content_still_works(self):
        out = self._run_summarize("Sunny and warm today.")
        self.assertEqual(out, "Sunny and warm today.")

    def test_token_budget_leaves_room_for_reasoning(self):
        """max_tokens was 250 while this model's reasoning alone can exceed it,
        so the text block came back empty.  Keep a real budget."""
        captured = []
        self._run_summarize([{"type": "text", "text": "answer long enough"}], captured)
        self.assertTrue(captured, "no request payload captured")
        self.assertGreaterEqual(captured[0]["max_tokens"], 600)


if __name__ == "__main__":
    unittest.main()
