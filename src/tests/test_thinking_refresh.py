"""Tests for the background filler-phrase refresh (Milestone 4).

S-M4-02: raw_complete_llm gains an optional `timeout` param so refresh
calls can pass timeout=60 — a hung refresh request must not hold the
default 120 s.  The param threads into the request and is re-applied on
G9's retry loop (the retry must preserve it, not drop it).

S-M4-03: the refresh machinery itself — _validate_phrases (charset
fixed-point rule), _parse_phrase_payload (fenced/non-greedy JSON
tolerance + the anti-scavenging rule), per-category prompt with the
category's register, _refresh_once (LLM call OUTSIDE the pool lock,
atomic swap under it, failure keeps the old pool), and
_refresh_all_categories (survives a raising category and advances
_last_refresh after every cycle).

The LLM HTTP layer is always stubbed — no network, no real client.
"""
import unittest
from unittest import mock

from modules import module_llm as llm
from modules import module_thinking as mt


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


class _RefreshStateTestCase(unittest.TestCase):
    """Isolate module_thinking's refresh state between tests.

    Same CONFIG-stub convention as test_thinking.py: replace mt.CONFIG
    with a plain dict (never read the repo's config.ini) and restore it
    in tearDown; save/restore the _refreshed pool and _last_refresh.
    """

    def setUp(self):
        self._cfg = mt.CONFIG
        mt.CONFIG = {"CHAR": {"thinking_responses": "[]"}}
        self._saved_refreshed = dict(mt._refreshed)
        mt._refreshed.clear()
        self._had_last = hasattr(mt, "_last_refresh")
        if self._had_last:
            self._saved_last = mt._last_refresh

    def tearDown(self):
        mt.CONFIG = self._cfg
        mt._refreshed.clear()
        mt._refreshed.update(self._saved_refreshed)
        if self._had_last:
            mt._last_refresh = self._saved_last
        elif hasattr(mt, "_last_refresh"):
            del mt._last_refresh

    def _stub_llm(self, reply=None, side_effect=None):
        """Stub raw_complete_llm (no network, no real client) and silence
        queue_message; returns the stub for call-args inspection."""
        stub = mock.MagicMock(return_value=reply, side_effect=side_effect)
        patcher = mock.patch.object(llm, "raw_complete_llm", stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch.object(mt, "queue_message")
        quiet.start()
        self.addCleanup(quiet.stop)
        return stub


class TestRefreshedPoolChain(_RefreshStateTestCase):
    """Recovered coverage: the fallback chain (M1 built it, M4 keeps it
    pinned). Refreshed pool wins; then bank; then config; then None."""

    def test_generated_set_wins(self):
        mt._refreshed["search"] = ["A."]
        self.assertEqual(mt.get_filler_phrase("search"), "A.")

    def test_config_phrases_when_generated_empty(self):
        # No refreshed entry for the category; the bank is emptied so the
        # config leg of the chain is the one under test.
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            mt.CONFIG["CHAR"]["thinking_responses"] = '["cfg one", "cfg two"]'
            self.assertIn(mt.get_filler_phrase("search"),
                          {"cfg one", "cfg two"})

    def test_none_when_both_empty(self):
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            mt.CONFIG["CHAR"]["thinking_responses"] = "[]"
            self.assertIsNone(mt.get_filler_phrase("search"))

    def test_none_when_config_garbage(self):
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            mt.CONFIG["CHAR"]["thinking_responses"] = "not json"
            self.assertIsNone(mt.get_filler_phrase("search"))


class TestValidatePhrases(unittest.TestCase):
    """S-M4-03: every phrase must be a fixed point of sanitize_phrase —
    the playback path plays text raw, so the charset rule lives here."""

    def test_validate_rejects_bad_charset(self):
        # Parentheses are stripped by the sanitizer, so such a phrase is
        # not a fixed point and must be dropped; the clean one survives.
        self.assertEqual(
            mt._validate_phrases(["No (parentheses)", "ok phrase"]),
            ["ok phrase"])


class TestParsePhrasePayload(unittest.TestCase):
    """S-M4-03: fenced/non-greedy JSON tolerance for the {"phrases": [...]}
    schema."""

    def test_fenced_payload_tolerated(self):
        payload = '"""json\n{"phrases": ["One.", "Two."]}\n"""'
        self.assertEqual(mt._parse_phrase_payload(payload),
                         ["One.", "Two."])


class TestRefreshOnce(_RefreshStateTestCase):
    """S-M4-03: per-category generation via raw_complete_llm(timeout=60),
    atomic swap under the pool lock, failure keeps the old pool."""

    def test_refresh_once_swaps_atomically(self):
        old = mt._refreshed["search"] = ["Old one."]
        self._stub_llm('{"phrases": ["Fresh one."]}')
        self.assertTrue(mt._refresh_once("search"))
        self.assertEqual(mt._refreshed["search"], ["Fresh one."])
        self.assertIsNot(mt._refreshed["search"], old,
                         "the swap must assign a NEW list object")

    def test_refresh_failure_keeps_old_pool(self):
        mt._refreshed["search"] = ["Old one."]
        self._stub_llm(side_effect=RuntimeError("network down"))
        self.assertFalse(mt._refresh_once("search"))
        self.assertEqual(mt._refreshed["search"], ["Old one."])

    def test_reply_schema_rejected_not_scavenged(self):
        # A payload in the conversation schema ("reply") is rejected, not
        # scavenged — at the parse level AND at the refresh level (the
        # old pool survives).
        self.assertIsNone(
            mt._parse_phrase_payload('{"reply": "Let me look that up"}'))
        mt._refreshed["search"] = ["Old one."]
        self._stub_llm('{"reply": "Let me look that up"}')
        self.assertFalse(mt._refresh_once("search"))
        self.assertEqual(mt._refreshed["search"], ["Old one."])

    def test_prompt_carries_category_and_register(self):
        stub = self._stub_llm('{"phrases": ["Fresh one."]}')
        self.assertTrue(mt._refresh_once("music"))
        prompt = stub.call_args[0][0]
        self.assertIn("music", prompt)
        self.assertIn(mt.REGISTERS["music"], prompt)  # "playful"

    def test_raw_called_with_60s_timeout(self):
        stub = self._stub_llm('{"phrases": ["Fresh one."]}')
        mt._refresh_once("search")
        self.assertEqual(stub.call_args.kwargs.get("timeout"), 60)

    def test_swap_is_locked_not_in_place(self):
        old = mt._refreshed["search"] = ["Old one."]
        self._stub_llm('{"phrases": ["Fresh one."]}')
        self.assertTrue(mt._refresh_once("search"))
        self.assertIsNot(mt._refreshed["search"], old)
        self.assertEqual(old, ["Old one."],
                         "the old list must never be mutated in place")


class TestRefreshAllCategories(_RefreshStateTestCase):
    """S-M4-03: the cycle survives a raising category and advances
    _last_refresh after EVERY cycle (no hammering on failure)."""

    def test_loop_survives_failure_and_waits(self):
        mt._last_refresh = 100.0

        def _flaky(category):
            if category == "search":
                raise RuntimeError("boom")
            return True

        with mock.patch.object(mt, "_refresh_once", side_effect=_flaky):
            ok = mt._refresh_all_categories()
        self.assertTrue(ok, "one failing category must not fail the cycle")
        self.assertGreater(mt._last_refresh, 100.0,
                           "_last_refresh must advance after every cycle")


if __name__ == "__main__":
    unittest.main()
