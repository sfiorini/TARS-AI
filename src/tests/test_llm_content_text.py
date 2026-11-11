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
import re
import sys
import types
import unittest
from pathlib import Path
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


@unittest.skipIf(sys.version_info < (3, 10), "skill_tars_radio uses py3.10+ syntax")
class TestRadioDjIntroUsesExtractor(unittest.TestCase):
    """Same regression in the radio DJ intro path (fallback masked it)."""

    def _run_dj_intro(self, content, payload_capture=None):
        from skills import skill_tars_radio

        response = mock.MagicMock()
        response.ok = True
        response.json.return_value = {"choices": [{"message": {"content": content}}]}

        def _post(url, headers=None, json=None, timeout=None):
            if payload_capture is not None:
                payload_capture.append(json)
            return response

        fake_cfg = {
            "LLM": {"api_key": "k", "llm_backend": "other", "base_url": "https://x",
                    "other_model": "m", "openai_model": "gpt-4o-mini",
                    "grok_model": "grok-3-mini", "temperature": 0.9},
        }
        # `_generate_dj_intro` does `import requests as req` INSIDE the function,
        # which binds the same module object, so patching requests.post works.
        with mock.patch("requests.post", _post), \
             mock.patch("modules.module_config.load_config", return_value=fake_cfg):
            return skill_tars_radio._generate_dj_intro("Test Song")

    def test_block_list_content_is_summarised(self):
        out = self._run_dj_intro([
            {"type": "thinking", "thinking": "reasoning"},
            {"type": "text", "text": "\"Up next on the cosmic frequency: Test Song.\""},
        ])
        self.assertEqual(out, "Up next on the cosmic frequency: Test Song.")

    def test_plain_string_content_still_works(self):
        out = self._run_dj_intro("You are tuning in to TARS Radio.")
        self.assertEqual(out, "You are tuning in to TARS Radio.")

    def test_token_budget_leaves_room_for_reasoning(self):
        captured = []
        self._run_dj_intro([{"type": "text", "text": "answer long enough"}], captured)
        self.assertTrue(captured, "no request payload captured")
        self.assertGreaterEqual(captured[0]["max_tokens"], 500)


_ANTIPATTERN = re.compile(
    r"\[\s*['\"]message['\"]\s*\]\s*\[\s*['\"]content['\"]\s*\]\s*\.\s*strip\s*\("
)


class TestNoRawContentStrip(unittest.TestCase):
    """Source-scan guard: raw ['message']['content'].strip() is the GLM bug.

    Intentionally narrow: this matches only the direct single-line
    ``['message']['content'].strip()`` form, which is exactly what broke.
    It will NOT catch the anti-pattern hoisted into a local variable first
    (``c = ...['content']; c.strip()``) or split across lines — do not weaken
    or broaden the regex to chase those; the behavioural regression tests in
    this file are what pin the real behaviour.
    """

    def test_no_raw_message_content_strip_in_src(self):
        src_root = Path(__file__).resolve().parents[1]
        offenders = []
        for py in sorted(src_root.rglob("*.py")):
            if py.relative_to(src_root).parts[0] == "tests":
                continue
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if _ANTIPATTERN.search(line):
                    offenders.append(f"{py.relative_to(src_root.parent)}:{lineno}")
        self.assertEqual(
            offenders, [],
            "raw `['message']['content'].strip()` — GLM models return a block list; "
            "use `modules.module_llm.content_text()` first",
        )



# module_websearch imports `from ddgs import DDGS` at module scope, but ddgs is
# not a test dependency (it is not installed in the test environment). Stub it
# the same way the TTS tests stub their optional SDKs (see
# test_module_tts_elevenlabs.py) so the patch target
# modules.module_websearch.search_google can be imported. search_google itself
# is patched in every test below, so the stub is never exercised.
if "ddgs" not in sys.modules:
    _ddgs = types.ModuleType("ddgs")

    class _DDGS:  # pragma: no cover - never instantiated, search_google is patched
        pass

    _ddgs.DDGS = _DDGS
    sys.modules["ddgs"] = _ddgs


class TestWebSearchNeverDiscardsResults(unittest.TestCase):
    """A failed SUMMARY must not throw away a successful SEARCH.

    The follow-up LLM is told "web_search: " (empty) when execute() returns
    None, and then says the search returned empty — even though the search
    data was fetched successfully.  Reachable whenever the summarizer call
    fails (provider error, timeout, or the reasoning model exhausting its
    token budget) and the reply-0 was a paraphrase the placeholder heuristic
    does not recognise — which is exactly what the skill's own prompt teaches
    the model to say ("On it, let me pull up the latest for you.").
    """

    def test_summary_failure_with_paraphrased_reply_still_returns_results(self):
        from skills import skill_web_search

        ctx = {
            "bot_response": {"reply": "On it, let me pull up the latest for you."},
            "user_input": "what's the weather in Rome",
        }
        # search_google is imported INSIDE execute(), so patch it at its source.
        with mock.patch("modules.module_websearch.search_google",
                        return_value="Right now in Rome it's 27 degrees and sunny."), \
             mock.patch.object(skill_web_search, "_summarize_search_results", return_value=None):
            out = skill_web_search.execute({"query": "weather Rome"}, ctx)

        self.assertIsNotNone(
            out,
            "valid search data was discarded -> the follow-up LLM is told the "
            "tool returned nothing and says 'the search returned empty'",
        )
        self.assertIn("27 degrees", out)

    def test_summary_failure_with_short_placeholder_still_returns_results(self):
        """The pre-existing branch for a recognised placeholder must keep working."""
        from skills import skill_web_search

        ctx = {
            "bot_response": {"reply": "Let me check that for you."},
            "user_input": "what's the weather in Rome",
        }
        with mock.patch("modules.module_websearch.search_google",
                        return_value="Right now in Rome it's 27 degrees and sunny."), \
             mock.patch.object(skill_web_search, "_summarize_search_results", return_value=None):
            out = skill_web_search.execute({"query": "weather Rome"}, ctx)

        self.assertIsNotNone(out)
        self.assertIn("27 degrees", out)


if __name__ == "__main__":
    unittest.main()
