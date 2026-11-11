"""Non-streaming LLM content must go through one extractor.

GLM-family models return ``message.content`` as a LIST of blocks
(``[{"type": "thinking", ...}, {"type": "text", "text": ...}]``) rather than a
string.  The streaming path normalised this from the start (``_delta_text``),
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
