"""Milestone 1 tests: module_thinking — categories, bank, sanitizer,
tool map, classifier, fallback chain. Pure and isolated (no wiring)."""
import ast
import os
import threading
import time
import types
import unittest
from unittest import mock

from modules import module_thinking as mt
from modules import module_llm as llm
from modules.module_thinking import (CATEGORIES, REGISTERS, PHRASE_BANK,
    sanitize_phrase, TOOL_CATEGORY_MAP)


class _ImmediateThread:
    """threading.Thread stand-in that runs the target synchronously —
    the filler must never leak a live thread into the test run."""
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._t = target

    def start(self):
        if self._t:
            self._t()


# [F3] THE NARROWED THREAD SEAM — the single namespace that replaces
# module_llm's OWN `threading` NAME in the hot-path test classes
# (mock.patch.object(llm, "threading", _LLM_THREADING_SEAM)).
# module_llm uses ONLY threading.Thread and threading.Event
# (module_llm.py:185,701,715,719,849) — the seam must expose both,
# or any test reaching the tool-dispatch shim (threading.Event at
# module_llm.py:849) raises AttributeError.
_LLM_THREADING_SEAM = types.SimpleNamespace(Thread=_ImmediateThread,
                                           Event=threading.Event)


class TestLLMThreadingSeam(unittest.TestCase):
    """[F3] seam coverage: module_llm calls BOTH threading.Thread and
    threading.Event (module_llm.py:849 — the tool-dispatch shim). A
    namespace exposing only Thread makes every future test that
    reaches the shim raise AttributeError on Event."""

    def test_seam_exposes_thread_and_event(self):
        self.assertIs(_LLM_THREADING_SEAM.Thread, _ImmediateThread,
                      "the seam must keep the synchronous Thread stub")
        self.assertTrue(hasattr(_LLM_THREADING_SEAM, "Event"),
                        "module_llm.py:849 calls threading.Event() — "
                        "the seam must expose Event")
        self.assertIs(_LLM_THREADING_SEAM.Event, threading.Event,
                      "Event must be the REAL constructor (tests never "
                      "reach the shim today; nothing to stub)")


class TestPhraseBank(unittest.TestCase):
    def test_all_13_categories_present(self):
        self.assertEqual(len(CATEGORIES), 13)
        self.assertEqual(set(CATEGORIES), set(PHRASE_BANK.keys()))

    def test_every_category_has_12_phrases(self):
        for cat in CATEGORIES:
            self.assertEqual(len(PHRASE_BANK[cat]), 12, cat)

    def test_phrases_are_nonempty_strings(self):
        for cat in CATEGORIES:
            for p in PHRASE_BANK[cat]:
                self.assertIsInstance(p, str, (cat, p))
                self.assertTrue(p.strip(), (cat, p))

    def test_registers_defined_for_every_category(self):
        self.assertEqual(set(REGISTERS), set(CATEGORIES))



class TestToolCategoryMap(unittest.TestCase):
    def test_web_search_maps_to_search(self):
        self.assertEqual(TOOL_CATEGORY_MAP["web_search"], "search")

    def test_take_photo_maps_to_vision(self):
        self.assertEqual(TOOL_CATEGORY_MAP["take_photo"], "vision")

    def test_all_twelve_builtin_categories_covered(self):
        self.assertIn("execute_movement", TOOL_CATEGORY_MAP)
        self.assertIn("generate_image", TOOL_CATEGORY_MAP)
        self.assertIn("home_assistant", TOOL_CATEGORY_MAP)
        self.assertIn("sandbox_exec", TOOL_CATEGORY_MAP)
        self.assertIn("reminder", TOOL_CATEGORY_MAP)
        self.assertIn("identify_speaker_name", TOOL_CATEGORY_MAP)
        self.assertIn("load_skill", TOOL_CATEGORY_MAP)

    def test_every_mapped_category_is_valid(self):
        for cat in TOOL_CATEGORY_MAP.values():
            self.assertIn(cat, CATEGORIES)


class TestSanitizePhrase(unittest.TestCase):
    def test_every_shipped_phrase_is_fixed_point(self):
        for cat in CATEGORIES:
            for p in PHRASE_BANK[cat]:
                self.assertEqual(sanitize_phrase(p), p, (cat, p))

    def test_idempotent(self):
        self.assertEqual(sanitize_phrase(sanitize_phrase("Let me… (think) 🤔")),
                         sanitize_phrase("Let me… (think) 🤔"))

    def test_strips_disallowed_chars_keeps_allowed(self):
        self.assertEqual(sanitize_phrase("Hmm— wait (a sec): ok?"),
                         "Hmm wait a sec: ok?")

    def test_non_string_returns_empty(self):
        self.assertEqual(sanitize_phrase(None), "")
        self.assertEqual(sanitize_phrase(42), "")


class TestClassifyIntentKeywords(unittest.TestCase):
    def test_search_beats_vision_on_search_for_a_photo(self):
        self.assertEqual(mt.classify_intent(user_text="search for a photo of a cat"), "search")

    def test_take_a_photo_is_vision(self):
        self.assertEqual(mt.classify_intent(user_text="take a photo"), "vision")

    def test_shutdown_is_system(self):
        self.assertEqual(mt.classify_intent(user_text="shut down the pi"), "system")

    def test_lights_are_smart_home(self):
        self.assertEqual(mt.classify_intent(user_text="turn off the lights"), "smart_home")

    def test_remind_me_is_reminder(self):
        self.assertEqual(mt.classify_intent(user_text="remind me in 5 minutes"), "reminder")

    def test_my_name_is_is_identity(self):
        self.assertEqual(mt.classify_intent(user_text="my name is Joe"), "identity")

    def test_unmatched_text_is_general(self):
        self.assertEqual(mt.classify_intent(user_text="tell me about turtles"), "general")

    def test_empty_inputs_are_general(self):
        self.assertEqual(mt.classify_intent(), "general")
        self.assertEqual(mt.classify_intent(user_text=""), "general")


class TestClassifyIntentRecall(unittest.TestCase):
    """[KEYWORDS-RECALL] The classifier must actually REACH the 12 category
    banks. Measured live on 2026-11-11: 7 of 7 realistic utterances below
    fell through to 'general', so every filler was drawn from the single
    general pool (the user's 29 'hold on' synonyms) and the 156 flavoured
    bank phrases were unreachable. Each row is a real phrasing, not a
    keyword echo."""

    TABLE = (
        # --- the seven measured failures (RED before the fix) ---
        ("turn off the light", "smart_home"),
        ("latest news today", "search"),
        ("what is the latest news", "search"),
        ("turn off Victoria room light", "smart_home"),
        ("switch off the lamp", "smart_home"),
        ("who won the race", "search"),
        ("whats the weather like", "search"),
        # --- the seven already-pinned behaviours (must NOT move) ---
        ("search for a photo of a cat", "search"),
        ("take a photo", "vision"),
        ("shut down the pi", "system"),
        ("turn off the lights", "smart_home"),
        ("remind me in 5 minutes", "reminder"),
        ("my name is Joe", "identity"),
        ("tell me about turtles", "general"),
        # --- coverage across the remaining banks ---
        ("dim the lights in the kitchen", "smart_home"),
        ("open the garage door", "smart_home"),
        ("play some music", "music"),
        ("what song is this", "music"),
        ("what do you see", "vision"),
        ("walk forward", "movement"),
        ("draw me a cat", "creative"),
        ("calculate 15 percent of 200", "compute"),
        ("what's the temperature outside", "search"),
    )

    def test_every_row_reaches_its_bank(self):
        for text, expected in self.TABLE:
            with self.subTest(text=text):
                self.assertEqual(mt.classify_intent(user_text=text), expected)

    def test_empty_still_general(self):
        self.assertEqual(mt.classify_intent(), "general")
        self.assertEqual(mt.classify_intent(user_text=""), "general")

    def test_realistic_traffic_does_not_collapse_into_general(self):
        """The whole point: realistic traffic must not collapse into the
        general pool. Only the deliberately-unclassifiable row
        ("tell me about turtles") may land there. Discriminating by
        construction: it FAILS on the pre-fix KEYWORDS (8 of 24 rows were
        general) and passes only once the banks are genuinely reachable."""
        general = [t for t, _ in self.TABLE
                   if mt.classify_intent(user_text=t) == "general"]
        self.assertEqual(general, ["tell me about turtles"])
        self.assertGreaterEqual(
            len({mt.classify_intent(user_text=t) for t, _ in self.TABLE}), 7)


class TestClassifyIntentTools(unittest.TestCase):
    def test_tool_name_beats_keywords(self):
        self.assertEqual(
            mt.classify_intent(user_text="tell me about turtles",
                               tool_names=["web_search"]), "search")

    def test_mcp_prefix_is_mcp(self):
        self.assertEqual(mt.classify_intent(tool_names=["mcp_hass_turn_on"]),
                         "mcp")

    def test_load_skill_is_skill(self):
        self.assertEqual(mt.classify_intent(tool_names=["load_skill"]),
                         "skill")

    def test_unknown_tool_falls_to_keywords_then_general(self):
        self.assertEqual(
            mt.classify_intent(user_text="tell me about turtles",
                               tool_names=["mystery_tool"]), "general")


class TestClassifyIntentRegistryKinds(unittest.TestCase):
    def test_agent_skill_kind_is_skill(self):
        desc = mock.Mock()
        desc.kind = "agent_skill"
        with mock.patch("modules.module_toolregistry.get_registry",
                        return_value={"custom_agent_tool": desc}):
            self.assertEqual(
                mt.classify_intent(tool_names=["custom_agent_tool"]), "skill")

    def test_mcp_kind_is_mcp(self):
        desc = mock.Mock()
        desc.kind = "mcp"
        with mock.patch("modules.module_toolregistry.get_registry",
                        return_value={"weird_name": desc}):
            self.assertEqual(mt.classify_intent(tool_names=["weird_name"]), "mcp")


class TestFallbackChain(unittest.TestCase):
    def setUp(self):
        self._cfg = mt.CONFIG
        mt.CONFIG = {"CHAR": {"thinking_responses": "[]"}}
        # F3: the floor is read via load_config() (web-UI-editable), so
        # stub it to return the same dict — NEVER the repo's config.ini
        # (dynamic side_effect: mid-test mt.CONFIG reassignments count)
        p = mock.patch.object(mt, "load_config",
                              side_effect=lambda: mt.CONFIG)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        mt.CONFIG = self._cfg

    def test_bank_category_wins(self):
        self.assertIn(mt.get_filler_phrase("search"), mt.PHRASE_BANK["search"])

    def test_unknown_category_falls_to_general_bank(self):
        self.assertIn(mt.get_filler_phrase("nope"), mt.PHRASE_BANK["general"])

    def test_refreshed_pool_wins_when_present(self):
        mt._refreshed["search"] = ["Fresh from the cloud."]
        try:
            self.assertEqual(mt.get_filler_phrase("search"),
                             "Fresh from the cloud.")
        finally:
            mt._refreshed.clear()

    def test_config_floor_used_when_bank_emptied(self):
        mt.CONFIG = {"CHAR": {"thinking_responses":
            '["Config floor phrase."]'}}
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            self.assertEqual(mt.get_filler_phrase("search"),
                             "Config floor phrase.")

    def test_config_garbage_returns_none(self):
        mt.CONFIG = {"CHAR": {"thinking_responses": "not json at all"}}
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            self.assertIsNone(mt.get_filler_phrase("search"))

    def test_all_empty_returns_none(self):
        mt.CONFIG = {"CHAR": {"thinking_responses": "[]"}}
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            self.assertIsNone(mt.get_filler_phrase("search"))

    def test_config_floor_reads_live_config_not_snapshot(self):
        """F3: the fallback floor must be read via load_config() (the
        web-UI edit path), NOT the module-level CONFIG snapshot taken at
        import time — otherwise a web-UI edit is picked up by the daemon
        but ignored by the fallback chain until restart."""
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            live = mock.patch.object(mt, "load_config", return_value={
                "CHAR": {"thinking_responses": '["Live floor phrase."]'}})
            live.start()
            self.addCleanup(live.stop)
            self.assertEqual(mt.get_filler_phrase("search"),
                             "Live floor phrase.")

    def test_config_read_failure_falls_through_to_none(self):
        # F3 guard: the fresh load_config() call must never raise into
        # the voice turn — a config failure falls through to None
        # (the caller then speaks HOLDING_LINE)
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            with mock.patch.object(mt, "load_config",
                                   side_effect=RuntimeError("config gone")):
                self.assertIsNone(mt.get_filler_phrase("search"))


class TestConfigGeneralPoolContract(unittest.TestCase):
    """[F1] CHAR.thinking_responses is the REAL general pool, and an
    explicit [] is REAL silence. The old chain could never reach the
    config list (the curated PHRASE_BANK['general'] is a hard-coded
    non-empty 12-tuple), so a user's custom phrases were dead code and
    the tooltip's "[] = silence" promise was false.

    Contract (four cases):
    - non-empty list  -> it IS the general pool (bank general bypassed)
    - explicit []     -> silence for general (None), bank bypassed
    - key missing     -> the bank's general pool serves
    - specific category with an empty bank -> the config list is the
      universal fallback (then bank general, then None)
    """

    def setUp(self):
        self._cfg = mt.CONFIG
        # NEVER the repo's config.ini — stub load_config to return the
        # test's own dict (the chain reads it FRESH, dynamic side_effect
        # so mid-test mt.CONFIG reassignments count)
        p = mock.patch.object(mt, "load_config",
                              side_effect=lambda: mt.CONFIG)
        p.start()
        self.addCleanup(p.stop)
        mt._refreshed.clear()
        self.addCleanup(mt._refreshed.clear)

    def tearDown(self):
        mt.CONFIG = self._cfg

    def test_nonempty_config_is_the_general_pool(self):
        mt.CONFIG = {"CHAR": {"thinking_responses": '["Hold on, sir."]'}}
        self.assertEqual(mt.get_filler_phrase("general"),
                         "Hold on, sir.",
                         "a non-empty user list must REPLACE the bank's "
                         "general pool, not lose to it")

    def test_nonempty_config_does_not_override_specific_banks(self):
        mt.CONFIG = {"CHAR": {"thinking_responses": '["Hold on, sir."]'}}
        self.assertIn(mt.get_filler_phrase("search"),
                      mt.PHRASE_BANK["search"])

    def test_explicit_empty_list_is_silence_for_general(self):
        mt.CONFIG = {"CHAR": {"thinking_responses": "[]"}}
        self.assertIsNone(mt.get_filler_phrase("general"),
                          "explicit [] must mean silence — the bank's "
                          "general pool must NOT speak over it")

    def test_missing_key_falls_back_to_bank_general(self):
        mt.CONFIG = {"CHAR": {}}  # key absent entirely
        self.assertIn(mt.get_filler_phrase("general"),
                      mt.PHRASE_BANK["general"])

    def test_config_list_is_universal_fallback_for_empty_category(self):
        mt.CONFIG = {"CHAR": {"thinking_responses": '["Hold on, sir."]'}}
        with mock.patch.dict(mt.PHRASE_BANK, {"search": (), "general": ()}):
            self.assertEqual(mt.get_filler_phrase("search"),
                             "Hold on, sir.")

    def test_refreshed_general_pool_still_wins_over_config(self):
        mt._refreshed["general"] = ["Fresh general."]
        mt.CONFIG = {"CHAR": {"thinking_responses": '["Hold on, sir."]'}}
        self.assertEqual(mt.get_filler_phrase("general"), "Fresh general.")


class TestHotPathSelection(unittest.TestCase):
    """S-M2-01: the hot path resolves a category and walks the verified
    fallback chain — never reaching random.choice([])."""

    def setUp(self):
        self._cfg = llm.CONFIG
        self._mt_cfg = mt.CONFIG
        # "TTS" key required: the real _play closure evaluates
        # CONFIG['TTS']['ttsoption'] BEFORE calling the stubbed
        # play_audio_chunks — without it every success-path test logs a
        # spurious "Failed to play thinking response" and _played stays
        # empty
        llm.CONFIG = {"CHAR": {"enable_thinking_responses": True,
                               "thinking_responses": "[]"},
                      "TTS": {"ttsoption": "piper"}}
        # the fallback chain's config floor reads mt.CONFIG — stub it too,
        # NEVER the repo's config.ini (it ships a non-empty floor list).
        # [F1] the key is MISSING (not "[]"): an explicit [] is now
        # SILENCE for general, so these tests use the bank's general
        # pool via the missing-key fallback
        mt.CONFIG = {"CHAR": {}}
        # F3: the floor is now read via load_config() — stub it to return
        # the same dict (dynamic: mid-test mt.CONFIG reassignments count)
        p = mock.patch.object(mt, "load_config",
                              side_effect=lambda: mt.CONFIG)
        p.start()
        self.addCleanup(p.stop)
        mt._refreshed.clear()
        self._played = []

        async def _fake_play(text, *a, **k):
            # MUST be a coroutine: the real _play closure does
            # asyncio.run(play_audio_chunks(...)) — a plain lambda
            # returns a non-coroutine, asyncio.run raises ValueError,
            # and every hot-path test would log a spurious
            # "ERROR: Failed to play thinking response"
            self._played.append(text)

        p = mock.patch("modules.module_tts.play_audio_chunks", new=_fake_play)
        p.start(); self.addCleanup(p.stop)
        # [F2] NARROW SEAM: "modules.module_llm.threading.Thread"
        # resolves module_llm.threading to the GLOBAL threading module,
        # so the patch hit every thread in the process while the test
        # ran. Replace module_llm's OWN `threading` NAME instead (same
        # fix as TestPerTurnDedup.setUp's [F8]).
        t = mock.patch.object(llm, "threading", _LLM_THREADING_SEAM)
        t.start(); self.addCleanup(t.stop)

    def tearDown(self):
        llm.CONFIG = self._cfg
        mt.CONFIG = self._mt_cfg
        mt._refreshed.clear()

    def test_tool_names_select_category_pool(self):
        ok = llm._maybe_play_thinking_response(tool_names=["web_search"])
        self.assertTrue(ok)
        self.assertIn(self._played[0], mt.PHRASE_BANK["search"])

    def test_empty_category_falls_through_without_indexerror(self):
        # pin the empty-category fall-through: bank entry emptied -> general
        # bank serves; random.choice([]) must NEVER be reached
        with mock.patch.dict(mt.PHRASE_BANK, {"search": (), "general": ()}):
            mt.CONFIG = {"CHAR": {"thinking_responses":
                '["Floor phrase."]'}}
            ok = llm._maybe_play_thinking_response(tool_names=["web_search"])
            self.assertTrue(ok)
            self.assertEqual(self._played[0], "Floor phrase.")

    def test_disabled_returns_false(self):
        llm.CONFIG = {"CHAR": {"enable_thinking_responses": False,
                               "thinking_responses": "[]"}}
        self.assertFalse(llm._maybe_play_thinking_response())

    def test_all_pools_empty_returns_false(self):
        with mock.patch.dict(mt.PHRASE_BANK, {c: () for c in mt.CATEGORIES}):
            self.assertFalse(llm._maybe_play_thinking_response())

    def test_config_garbage_returns_false(self):
        llm.CONFIG = {"CHAR": {"enable_thinking_responses": True,
                               "thinking_responses": "garbage{"}}
        # the floor lives in mt.CONFIG — the garbage must be there to
        # exercise the json-garbage branch of the chain
        mt.CONFIG = {"CHAR": {"thinking_responses": "garbage{"}}
        with mock.patch.dict(mt.PHRASE_BANK, {c: () for c in mt.CATEGORIES}):
            self.assertFalse(llm._maybe_play_thinking_response())

    def test_zero_arg_call_still_works(self):
        ok = llm._maybe_play_thinking_response()
        self.assertTrue(ok)  # general pool


class TestPerTurnDedup(unittest.TestCase):
    """S-M2-02 contract through the hot path: one filler per (source,
    epoch) turn; a suppressed second fire returns True (handled) with NO
    second playback."""

    def setUp(self):
        mt.reset_dedup()
        mt._refreshed.clear()
        self._cfg = llm.CONFIG
        # "TTS" key required — see TestHotPathSelection.setUp
        llm.CONFIG = {"CHAR": {"enable_thinking_responses": True,
                               "thinking_responses": "[]"},
                      "TTS": {"ttsoption": "piper"}}
        self._mt_cfg = mt.CONFIG
        # [F1] missing key (not "[]") — an explicit [] is now silence
        # for general; these tests play the bank's general pool
        mt.CONFIG = {"CHAR": {}}
        # F3: the floor is read via load_config() — same stub as above
        p = mock.patch.object(mt, "load_config",
                              side_effect=lambda: mt.CONFIG)
        p.start()
        self.addCleanup(p.stop)
        self._played = []

        async def _fake_play(text, *a, **k):
            # coroutine stub — see TestHotPathSelection.setUp (a plain
            # lambda makes asyncio.run raise and log a spurious error)
            self._played.append(text)

        p = mock.patch("modules.module_tts.play_audio_chunks", new=_fake_play)
        p.start(); self.addCleanup(p.stop)
        # [F8] NARROW SEAM: patching "modules.module_llm.threading.Thread"
        # resolves module_llm.threading to the GLOBAL threading module,
        # so the patch hits every thread created in the process while the
        # test runs (a background thread from an earlier test that starts
        # a thread in this window gets the synchronous stub — the observed
        # 1-in-22 flake). Replace module_llm's OWN `threading` NAME with a
        # bare namespace whose Thread is the synchronous stub: only the
        # code under test (module_llm) sees it, the global threading
        # module is untouched.
        t = mock.patch.object(llm, "threading", _LLM_THREADING_SEAM)
        t.start(); self.addCleanup(t.stop)

    def tearDown(self):
        mt.reset_dedup()
        llm.CONFIG = self._cfg
        mt.CONFIG = self._mt_cfg
        mt._refreshed.clear()

    def test_second_fire_same_turn_suppressed(self):
        # [F8] hermetic: the per-source dedup state is module-global —
        # reset it EXPLICITLY here, not just in setUp, so this test does
        # not depend on setUp/tearDown ordering for its outcome
        mt.reset_dedup()
        self.assertTrue(llm._maybe_play_thinking_response(source="voice"))
        self.assertEqual(len(self._played), 1)
        # same turn again -> True (handled) but NO second playback
        self.assertTrue(llm._maybe_play_thinking_response(source="voice"))
        self.assertEqual(len(self._played), 1)

    def test_new_epoch_plays_again(self):
        from modules.module_toolloop import bump_epoch
        bump_epoch("voice")
        self.assertTrue(llm._maybe_play_thinking_response(source="voice"))
        bump_epoch("voice")
        self.assertTrue(llm._maybe_play_thinking_response(source="voice"))
        self.assertEqual(len(self._played), 2)

    def test_source_none_skips_dedup(self):
        self.assertTrue(llm._maybe_play_thinking_response(source="voice"))
        self.assertTrue(llm._maybe_play_thinking_response())
        self.assertEqual(len(self._played), 2)

    def test_disabled_never_marks(self):
        llm.CONFIG = {"CHAR": {"enable_thinking_responses": False,
                               "thinking_responses": "[]"}}
        self.assertFalse(llm._maybe_play_thinking_response(source="voice"))
        self.assertFalse(mt.already_played_this_turn("voice"))

    def test_concurrent_source_marks_do_not_clobber_each_other(self):
        """[F1] The dedup state must be PER-SOURCE. A single global slot
        is clobbered by any concurrent source's mark: the voice turn
        marks, a webui turn (its own socketio thread) marks, and the
        voice turn's next check then sees the webui key -> not-played.
        Each source must keep its OWN last-played epoch."""
        from modules.module_toolloop import bump_epoch
        bump_epoch("voice")
        bump_epoch("voice")   # voice epoch 2
        bump_epoch("webui")   # webui epoch 1 (concurrent turn)
        mt.mark_played_this_turn("voice")
        mt.mark_played_this_turn("webui")  # must NOT erase voice's mark
        self.assertTrue(mt.already_played_this_turn("voice"),
                        "a concurrent webui mark clobbered the voice "
                        "turn's dedup state")
        self.assertTrue(mt.already_played_this_turn("webui"))

    def test_interleaved_sources_never_double_speak(self):
        """[F1] The audited failure through the hot path: a voice turn
        plays a filler, a concurrent webui turn plays its own, and the
        voice turn's next fire then speaks a SECOND filler on top —
        because the single global dedup slot held the webui key. One
        filler per (source, turn): the voice re-fire must be suppressed
        even though another source played in between."""
        self.assertTrue(llm._maybe_play_thinking_response(source="voice"))
        self.assertTrue(llm._maybe_play_thinking_response(source="webui"))
        # the voice turn fires again (e.g. the toolloop site): still
        # suppressed — True (handled), NO third playback
        self.assertTrue(llm._maybe_play_thinking_response(source="voice"))
        self.assertEqual(len(self._played), 2,
                         "one filler per source — a clobbered dedup "
                         "slot double-speaks over the filler already "
                         "played")


_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestProcessCompletionSource(unittest.TestCase):
    """S-M2-03: process_completion takes a source, and the webui/discord
    epoch bumps move BEFORE the completion call (source-order pins in the
    house style of test_tool_loop.py)."""

    def test_process_completion_accepts_source(self):
        import inspect
        sig = inspect.signature(llm.process_completion)
        self.assertEqual(sig.parameters["source"].default, "voice")

    def test_webui_bumps_before_completion(self):
        with open(os.path.join(_SRC, "modules", "module_chatui.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        i_call = src.index("process_completion(msg")
        i_bump = src.index("bump_epoch('webui')")
        self.assertLess(i_bump, i_call,
                       "webui epoch must bump BEFORE process_completion")

    def test_webui_bump_appears_exactly_once(self):
        with open(os.path.join(_SRC, "modules", "module_chatui.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count("bump_epoch('webui')"), 1)

    def test_discord_bumps_before_completion(self):
        with open(os.path.join(_SRC, "skills", "skill_discord.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        i_call = src.index("process_completion(user_message")
        i_bump = src.index("bump_epoch('discord')")
        self.assertLess(i_bump, i_call)
        self.assertEqual(src.count("bump_epoch('discord')"), 1)


class TestPreLlmSuppression(unittest.TestCase):
    """S-M3-01: the two pre-LLM call sites pass user_text + source — a
    likely tool turn (keyword hit, no tool_names yet) stays silent so the
    model's own reply-0 holding line can speak; general text still fires
    a filler (covers TTFT)."""

    def setUp(self):
        mt.reset_dedup()
        mt._refreshed.clear()
        self._cfg = llm.CONFIG
        # "TTS" key required — see TestHotPathSelection.setUp
        llm.CONFIG = {"CHAR": {"enable_thinking_responses": True,
                               "thinking_responses": "[]"},
                      "TTS": {"ttsoption": "piper"}}
        self._mt_cfg = mt.CONFIG
        # the config floor reads mt.CONFIG — never the repo's config.ini.
        # [F1] missing key (not "[]") — an explicit [] is now silence
        # for general; the not-suppressed tests play the bank's pool
        mt.CONFIG = {"CHAR": {}}
        # F3: the floor is read via load_config() — same stub as above
        p = mock.patch.object(mt, "load_config",
                              side_effect=lambda: mt.CONFIG)
        p.start()
        self.addCleanup(p.stop)
        self._played = []

        async def _fake_play(text, *a, **k):
            # coroutine stub — see TestHotPathSelection.setUp
            self._played.append(text)

        p = mock.patch("modules.module_tts.play_audio_chunks", new=_fake_play)
        p.start(); self.addCleanup(p.stop)
        # [F2] NARROW SEAM — see TestPerTurnDedup.setUp's [F8]: the
        # global-threading patch hit every thread in the process;
        # patch only module_llm's OWN `threading` NAME.
        t = mock.patch.object(llm, "threading", _LLM_THREADING_SEAM)
        t.start(); self.addCleanup(t.stop)

    def tearDown(self):
        mt.reset_dedup()
        llm.CONFIG = self._cfg
        mt.CONFIG = self._mt_cfg
        mt._refreshed.clear()

    def test_tool_category_text_suppressed_at_pre_llm(self):
        ok = llm._maybe_play_thinking_response(
            user_text="search for the weather in Rome", source="voice")
        self.assertFalse(ok)
        self.assertEqual(self._played, [])

    # NOTE: the two tests below invoke _maybe_play_thinking_response
    # directly — they pin CLASSIFIER behaviour (general text is never
    # suppressed; explicit tool_names win). They are NOT branch guards:
    # their inputs make the suppression condition inherently False, so
    # they would stay green even if the branch were deleted. The branch
    # itself is guarded by test_suppression_branch_present below.

    def test_general_text_is_not_suppressed(self):
        ok = llm._maybe_play_thinking_response(
            user_text="tell me about turtles", source="voice")
        self.assertTrue(ok)
        self.assertIn(self._played[0], mt.PHRASE_BANK["general"])

    def test_tool_names_override_suppression(self):
        ok = llm._maybe_play_thinking_response(
            user_text="search for the weather in Rome",
            tool_names=["web_search"], source="voice")
        self.assertTrue(ok)

    def test_suppression_branch_present(self):
        # F1 branch guard: the suppression branch in module_llm is the
        # only thing that keeps likely-tool turns silent before the LLM
        # call. The behaviour tests above cannot catch its removal
        # (their inputs make the condition False either way), so pin the
        # branch's distinguishing line in the source. (_SRC, never a
        # bare relative path — module_llm's import chain chdirs.)
        with open(os.path.join(_SRC, "modules", "module_llm.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(
            'if tool_names is None and user_text and likely_tool_turn(user_text):',
            src)

    def test_get_completion_and_get_parsed_pass_signals(self):
        # source-scan: the two pre-LLM sites must forward user_text and
        # source (Path("modules/...") breaks — module_llm's import chain
        # chdirs the process; _SRC is the house convention)
        with open(os.path.join(_SRC, "modules", "module_llm.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(
            "_maybe_play_thinking_response(user_text=user_prompt, source=source)",
            src)
        self.assertIn(
            "_maybe_play_thinking_response(user_text=prompt, source=source)",
            src)

    def test_toolloop_passes_tool_names_and_source(self):
        # F2: the toolloop filler site must forward tool_names + source
        # too (the third call site of the milestone)
        with open(os.path.join(_SRC, "modules", "module_toolloop.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        i_call = src.index("_maybe_play_thinking_response(")
        window = src[i_call:i_call + 400]
        self.assertIn("tool_names=", window)
        self.assertIn("source=source", window)


ORDINARY_QUESTIONS = (
    # [F2] every one of these MISCLASSIFIED with the bare-noun KEYWORDS
    # (volume->system, convert->compute, dancing->movement, python->
    # compute, lights->smart_home, camera->vision, bow->movement,
    # wave->movement) and was SILENCED — dead air instead of a filler.
    "what is the volume of a sphere",
    "how do I convert celsius to fahrenheit",
    "tell me a joke about a dancing bear",
    "what's a good python tutorial",
    "I love the lights in Paris",
    "explain how a camera works",
    "a recipe for a bow tie pasta",
    "what does a wave look like",
    # [F1-REGRESSION] the substring suppression triggers added for the
    # measured tool phrasings ("look at me", "lights on", "lights off",
    # "set volume", "stop the timer", "cancel my timer", the
    # volume/convert numeric regexes) match INSIDE these ordinary
    # questions — likely_tool_turn returned True and the pre-LLM gate
    # spoke NOTHING for the whole LLM turn (dead air). 9 of 12 measured
    # realistic questions were silenced; the pinned set above could not
    # catch it because none of its members contain the new triggers.
    "why does my cat look at me like that",
    "why does my dog look at me when I cry",
    "how do I stop the timer on my microwave beeping",
    "what is volume 2 of the encyclopedia about",
    "how do I convert 100 dollars into euros when travelling",
    "what does set volume mean in docker compose",
    "why do the lights on my router blink",
    "how do I turn the lights on in dark souls",
    "what does convert 20 mean in a spreadsheet formula",
    # [F2-prefixed] the bare prefixed triggers ("can you see",
    # "can you search", "can you convert", "please convert")
    # matched ANY completion, so these ordinary conversational
    # questions returned likely_tool_turn True -> the pre-LLM gate
    # spoke NOTHING (dead air for the whole turn). The triggers now
    # require a tool-ish completion; these must get a filler.
    "can you see what I mean",
    "can you see the difference between these two",
    "can you convert that into a percentage",
    "please convert that into plain english",
)

GENUINE_TOOL_REQUESTS = (
    # [F1-REGRESSION] the finding's acceptance set. [F4] "send a
    # message to John on Discord" and "add milk to my shopping list"
    # were previously DELETED from this pinned set when they regressed
    # True->False — deleting a failing case from a pinned set is how
    # the regression went unnoticed; they are restored and must
    # suppress again (see test_genuine_tool_requests_are_suppressed).
    "lights on",
    "lights off",
    "turn the lights on",
    "set volume to 50",
    "volume 50",
    "convert 20 dollars to euros",
    "stop the timer",
    "cancel my timer",
    "look at me",
    "search the web for F1 news",
    "take a photo",
    "play some jazz",
    "set a timer for 10 minutes",
    "what's the weather in Rome",
    "turn off the living room lights",
    "send a message to John on Discord",
    "add milk to my shopping list",
)

MISSED_TOOL_PHRASINGS = (
    # [F4] measured misses of the precision gate: genuine tool
    # phrasings that slipped through SUPPRESSION_TRIGGERS (an extra
    # filler utterance, not dead air — non-blocking, but they must
    # suppress again WITHOUT broadening back to the bare-noun
    # KEYWORDS that silenced ordinary questions)
    "cancel my timer",
    "lights on",
    "lights off",
    "turn the lights on",
    "set volume to 50",
    "volume 50",
    "convert 20 dollars to euros",
    "stop the timer",
    "look at me",
)


class TestSuppressionPrecision(unittest.TestCase):
    """[F2] pre-LLM suppression must be HIGH-PRECISION, not high-recall:
    a bare noun hit ("volume", "convert", "dancing", "python",
    "lights", "camera", "bow", "wave") must NOT silence the filler —
    a suppressed turn with an empty reply-0 speaks NOTHING, so a
    misclassified ordinary question got dead air. Genuine tool requests
    (imperative action patterns) must still suppress so reply-0 holds
    the line."""

    def setUp(self):
        mt.reset_dedup()
        mt._refreshed.clear()
        self._cfg = llm.CONFIG
        # "TTS" key required — see TestHotPathSelection.setUp
        llm.CONFIG = {"CHAR": {"enable_thinking_responses": True,
                               "thinking_responses": "[]"},
                      "TTS": {"ttsoption": "piper"}}
        self._mt_cfg = mt.CONFIG
        # [F1] missing key -> the bank's general pool serves
        mt.CONFIG = {"CHAR": {}}
        p = mock.patch.object(mt, "load_config",
                              side_effect=lambda: mt.CONFIG)
        p.start()
        self.addCleanup(p.stop)
        self._played = []

        async def _fake_play(text, *a, **k):
            self._played.append(text)

        p = mock.patch("modules.module_tts.play_audio_chunks", new=_fake_play)
        p.start(); self.addCleanup(p.stop)
        # [F2] NARROW SEAM — see TestPerTurnDedup.setUp's [F8]: the
        # global-threading patch hit every thread in the process;
        # patch only module_llm's OWN `threading` NAME.
        t = mock.patch.object(llm, "threading", _LLM_THREADING_SEAM)
        t.start(); self.addCleanup(t.stop)

    def tearDown(self):
        mt.reset_dedup()
        llm.CONFIG = self._cfg
        mt.CONFIG = self._mt_cfg
        mt._refreshed.clear()

    def test_ordinary_questions_get_a_filler(self):
        for question in ORDINARY_QUESTIONS:
            with self.subTest(question=question):
                mt.reset_dedup()  # each subTest is a fresh turn
                self._played.clear()
                ok = llm._maybe_play_thinking_response(
                    user_text=question, source="voice")
                self.assertTrue(
                    ok, f"ordinary question was silenced: {question!r}")
                self.assertEqual(len(self._played), 1,
                                  f"no filler spoken for {question!r}")

    def test_genuine_tool_requests_are_suppressed(self):
        for request in GENUINE_TOOL_REQUESTS:
            with self.subTest(request=request):
                mt.reset_dedup()  # each subTest is a fresh turn
                self._played.clear()
                ok = llm._maybe_play_thinking_response(
                    user_text=request, source="voice")
                self.assertFalse(
                    ok, f"genuine tool request not suppressed: {request!r}")
                self.assertEqual(self._played, [])

    def test_measured_tool_phrasings_suppress(self):
        """[F4] the recall side of the precision gate: these genuine
        tool phrasings were measured to slip through
        SUPPRESSION_TRIGGERS. They must suppress again — without
        regressing the ordinary-question set above."""
        for phrase in MISSED_TOOL_PHRASINGS:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    mt.likely_tool_turn(phrase),
                    f"genuine tool phrasing not suppressed: {phrase!r}")

    def test_prefixed_triggers_reach_their_own_phrasings(self):
        """[F2] triggers that themselves BEGIN with an allowlist
        prefix ("can you see what's on", "can you search wikipedia",
        "can you convert the units", "please convert this") were
        unreachable: _imperative_head strips the very prefix they
        start with, so the phrasings they were added for regressed
        True->False (dead literals in the config). The full phrase
        must match the ORIGINAL text as a secondary check — still
        imperative position (startswith), so no bare-noun or
        mid-sentence regression."""
        for phrase in (
            "can you see what's on the table",
            "can you search wikipedia for the weather",
            "can you convert the units for me",
            "please convert this",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    mt.likely_tool_turn(phrase),
                    f"prefixed trigger phrasing not suppressed: "
                    f"{phrase!r}")

    def test_prefixed_triggers_require_a_toolish_completion(self):
        """[F2-prefixed] the prefixed suppression triggers used to be
        BARE ("can you see", "can you search", "can you convert",
        "please convert"), so they matched ANY completion and
        silenced ordinary conversational questions ("can you see what
        I mean" -> likely_tool_turn True -> dead air for the whole
        turn). The narrowed triggers must keep the genuine tool
        phrasings suppressed WITHOUT catching the conversational
        completions — the pinned pairs below differ ONLY in the
        completion."""
        must_suppress = (
            "can you see what's on the table",
            "can you search wikipedia for the weather",
            "can you convert the units for me",
            "please convert this",
        )
        must_not_suppress = (
            "can you see what I mean",
            "can you see the difference between these two",
            "can you convert that into a percentage",
            "please convert that into plain english",
        )
        for phrase in must_suppress:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    mt.likely_tool_turn(phrase),
                    f"genuine prefixed tool phrasing not suppressed: "
                    f"{phrase!r}")
        for phrase in must_not_suppress:
            with self.subTest(phrase=phrase):
                self.assertFalse(
                    mt.likely_tool_turn(phrase),
                    f"ordinary question silenced by an over-broad "
                    f"prefixed trigger: {phrase!r}")

    def test_comma_after_prefix_is_recognized(self):
        """[F3] the prefix allowlist accepted only "prefix + space" —
        the natural vocative comma ("hey tars, lights off", "tars,
        lights off", "please, turn the lights off") regressed
        True->False. An optional comma after each prefix must be
        accepted."""
        for phrase in (
            "hey tars, lights off",
            "tars, lights off",
            "please, turn the lights off",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    mt.likely_tool_turn(phrase),
                    f"comma-prefixed phrasing not suppressed: {phrase!r}")


class TestRefreshConfigKeys(unittest.TestCase):
    """S-M4-01: thinking_refresh_enabled / thinking_refresh_minutes live
    in all three homes (load_config, CONFIG_METADATA, template) or web-UI
    saves destroy them (app_cms.py:655-680)."""

    def test_defaults_off_and_120(self):
        # Parse the TEMPLATE, never the repo's config.ini (it ships
        # enable_thinking_responses = false and tests must not read it).
        # _SRC per the test_tool_loop.py:18-22 convention — a bare
        # "../../config.ini.template" would resolve OUTSIDE the repo root
        # (module_llm's import chain chdirs the process).
        import configparser
        cp = configparser.ConfigParser()
        cp.read(os.path.join(_SRC, "config.ini.template"))
        # fallback=None so a MISSING key fails the assert cleanly instead
        # of raising NoOptionError (RED must fail, not error).
        self.assertEqual(
            cp.getboolean('CHAR', 'thinking_refresh_enabled', fallback=None),
            False)
        self.assertEqual(
            cp.getint('CHAR', 'thinking_refresh_minutes', fallback=None),
            120)

    def test_minutes_clamped_to_one(self):
        # Pin the clamp arithmetic AND that the clamp lives at the
        # load_config call site (single clamp site — the F5 trap).
        self.assertEqual(max(1, -5), 1)
        self.assertEqual(max(1, 0), 1)
        with open(os.path.join(_SRC, "modules", "module_config.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(
            "max(1, config.getint('CHAR', 'thinking_refresh_minutes', fallback=120))",
            src)

    def test_metadata_covers_new_keys(self):
        from modules.module_config import CONFIG_METADATA
        char = CONFIG_METADATA['CHAR']
        self.assertIn('thinking_refresh_enabled', char)
        self.assertIn('thinking_refresh_minutes', char)


class TestDaemonCycleSettlesPiggybackedState(unittest.TestCase):
    """[F1] A manual click that piggy-backs on an in-flight cycle sets
    state 'running' WITHOUT spawning a _bg (request_manual_refresh's
    locked-cycle branch) — so ONLY the cycle's completion can settle
    it. The daemon never wrote _refresh_state, so the web UI polled
    GET /api/thinking/refresh/status and showed 'Refreshing…' forever.
    The cycle itself must write the terminal state when it ends."""

    def setUp(self):
        saved_state = dict(mt._refresh_state)
        self.addCleanup(lambda: mt._refresh_state.update(saved_state))
        # force a TERMINAL state: the piggy-back branch is only
        # reachable when the state is not already 'running'
        mt._refresh_state["state"] = "idle"
        mt._refresh_state["last_error"] = None
        saved_last = mt._last_refresh
        self.addCleanup(setattr, mt, "_last_refresh", saved_last)
        # a set stop event would cut the stubbed cycle short and fail
        # the done-path tests spuriously
        was_set = mt._stop_event.is_set()
        mt._stop_event.clear()
        if was_set:
            self.addCleanup(mt._stop_event.set)

    def _daemon_cycle_with_midflight_click(self, verdict):
        """Run a daemon-style cycle in a thread, click the manual
        refresh while it is mid-flight (the piggy-back branch — no _bg
        is spawned), let the cycle finish, and join it."""
        entered = threading.Event()
        release = threading.Event()

        def _slow_refresh_once(category):
            entered.set()      # hold the cycle open mid-flight
            release.wait(10)
            return verdict

        with mock.patch.object(mt, "_refresh_once",
                               side_effect=_slow_refresh_once), \
             mock.patch.object(mt, "_is_idle", return_value=True), \
             mock.patch.object(mt, "queue_message"):
            t = threading.Thread(target=mt._refresh_all_categories,
                                 daemon=True, name="F1DaemonCycle")
            t.start()
            self.assertTrue(entered.wait(5),
                            "the daemon cycle never started")
            # the click lands mid-cycle: piggy-back, no _bg spawned
            self.assertEqual(mt.request_manual_refresh()["state"],
                             "running")
            self.assertEqual(mt.refresh_status()["state"], "running")
            release.set()
            t.join(5)
            self.assertFalse(t.is_alive(), "the daemon cycle never ended")

    def test_piggybacked_click_settles_done_when_cycle_succeeds(self):
        self._daemon_cycle_with_midflight_click(True)
        info = mt.refresh_status()
        self.assertEqual(info["state"], "done",
                         "the cycle the click piggy-backed on has "
                         "ended — the state must settle, or the web UI "
                         "shows 'Refreshing…' forever")
        self.assertIsNone(info["last_error"])

    def test_piggybacked_click_settles_error_when_cycle_fails(self):
        self._daemon_cycle_with_midflight_click(False)
        info = mt.refresh_status()
        self.assertEqual(info["state"], "error",
                         "a piggy-backed click on a FAILED cycle must "
                         "settle to 'error', not hang at 'running'")
        self.assertEqual(info["last_error"], "no category refreshed")

    def test_daemon_cannot_clobber_a_running_manual_refresh(self):
        """The invariant: while a manual refresh genuinely runs its
        OWN cycle it owns the 'running' state, and the daemon path
        cannot settle it — the shared cycle lock means the daemon's
        attempt is a no-cycle None, never a terminal write."""
        entered = threading.Event()
        release = threading.Event()

        def _slow_refresh_once(category):
            entered.set()
            release.wait(10)
            return True

        with mock.patch.object(mt, "_refresh_once",
                               side_effect=_slow_refresh_once), \
             mock.patch.object(mt, "_is_idle", return_value=False), \
             mock.patch.object(mt, "queue_message"):
            # the manual refresh starts its OWN cycle (spawn branch)
            self.assertEqual(mt.request_manual_refresh()["state"],
                             "starting")
            self.assertTrue(entered.wait(5),
                            "the manual cycle never started")
            self.assertEqual(mt.refresh_status()["state"], "running")
            # the daemon's tick lands while the manual cycle runs
            self.assertIsNone(mt._refresh_all_categories(),
                              "the daemon must not stack a cycle on the "
                              "manual one's")
            self.assertEqual(mt.refresh_status()["state"], "running",
                             "a cycle that did not run must not settle "
                             "the state a genuinely-running manual "
                             "refresh owns")
            release.set()
            for _ in range(250):
                if mt.refresh_status()["state"] in ("done", "error"):
                    break
                time.sleep(0.02)
        self.assertEqual(mt.refresh_status()["state"], "done",
                         "the manual _bg settles its own cycle")


class TestCycleSettleIdentity(unittest.TestCase):
    """[F2] The settle runs AFTER the cycle lock is released, so a click
    landing in that window sees a FREE lock and spawns a REAL _bg cycle.
    The previous cycle's settle used to write its verdict over that
    manual cycle's 'running' state — and main.js clears its poller on
    the first non-running state, so the badge stayed wrong for the
    whole session. Invariant: a settle writes ONLY the state its own
    cycle was named to settle (a piggy-backed click names the in-flight
    cycle; a spawned _bg's state carries no name, which no settle
    matches)."""

    def setUp(self):
        saved_state = dict(mt._refresh_state)
        self.addCleanup(lambda: (mt._refresh_state.clear(),
                                 mt._refresh_state.update(saved_state)))
        mt._refresh_state["state"] = "idle"
        mt._refresh_state["last_error"] = None
        saved_last = mt._last_refresh
        self.addCleanup(setattr, mt, "_last_refresh", saved_last)
        # getattr: the identity globals do not exist before the fix —
        # setUp must not ERROR on the pre-fix module (RED fails, never
        # errors)
        saved_running = getattr(mt, "_running_cycle_id", None)
        self.addCleanup(setattr, mt, "_running_cycle_id", saved_running)
        mt._running_cycle_id = None
        # a set stop event would cut the stubbed cycle short and fail
        # the done-path tests spuriously
        was_set = mt._stop_event.is_set()
        mt._stop_event.clear()
        if was_set:
            self.addCleanup(mt._stop_event.set)

    def test_release_window_click_not_clobbered_by_previous_settle(self):
        """A click in the post-release window spawns a REAL manual cycle;
        the previous cycle's settle must leave that state alone."""
        entered = threading.Event()
        release_manual = threading.Event()
        # captured BEFORE the patch below or the wrapper recurses into
        # the mock
        real_settle = mt._settle_refresh_state

        def _gated_refresh_once(category):
            entered.set()
            if threading.current_thread().name == "ThinkingRefreshManual":
                release_manual.wait(10)  # hold the MANUAL cycle open
            return True

        def _click_in_the_release_window(*settle_args):
            # runs in the daemon cycle's finally AFTER the lock is
            # released — the exact F2 window. *settle_args forwards
            # whatever the PRE- or POST-fix settle passes (RED must
            # fail on the assertion, never error on the signature).
            response = mt.request_manual_refresh()
            real_settle(*settle_args)
            return response

        with mock.patch.object(mt, "_refresh_once",
                               side_effect=_gated_refresh_once), \
             mock.patch.object(mt, "_is_idle", return_value=True), \
             mock.patch.object(mt, "queue_message"), \
             mock.patch.object(mt, "_settle_refresh_state",
                               side_effect=_click_in_the_release_window):
            t = threading.Thread(target=mt._refresh_all_categories,
                                 daemon=True, name="F2DaemonCycle")
            t.start()
            self.assertTrue(entered.wait(5),
                            "the daemon cycle never started")
            t.join(5)
            self.assertFalse(t.is_alive(), "the daemon cycle never ended")
            # the click took the SPAWN branch (lock free, state
            # terminal) — a REAL manual cycle is running right now
            self.assertEqual(mt.refresh_status()["state"], "running",
                             "the previous cycle's settle clobbered the "
                             "manual cycle's 'running' state — the web "
                             "UI stops polling on the first non-running "
                             "state and the badge stays wrong for the "
                             "session")
            # let the manual cycle finish and settle its OWN state
            # (inside the patch block — the cycle's remaining
            # categories must keep hitting the mocked _refresh_once)
            release_manual.set()
            for _ in range(250):
                if mt.refresh_status()["state"] in ("done", "error"):
                    break
                time.sleep(0.02)
        info = mt.refresh_status()
        self.assertEqual(info["state"], "done",
                         "the manual cycle must settle its own state")
        self.assertIsNone(info["last_error"])

    def test_manual_success_clears_a_stale_last_error(self):
        """[F2] _bg's terminal write must be self-consistent: 'done'
        must never sit beside a stale last_error — the write site
        clears it, not just the distant spawn-branch start-clear."""
        entered = threading.Event()
        release = threading.Event()

        def _gated_refresh_once(category):
            entered.set()
            release.wait(10)
            return True

        with mock.patch.object(mt, "_refresh_once",
                               side_effect=_gated_refresh_once), \
             mock.patch.object(mt, "queue_message"):
            self.assertEqual(mt.request_manual_refresh()["state"],
                             "starting")
            self.assertTrue(entered.wait(5),
                            "the manual cycle never started")
            # a stale error lands while the refresh runs (whatever set
            # it, the SUCCESS verdict must not ship beside it)
            with mt._manual_lock:
                mt._refresh_state["last_error"] = "stale error"
            release.set()
            # poll INSIDE the patch block — the cycle's remaining
            # categories must keep hitting the mocked _refresh_once
            for _ in range(250):
                if mt.refresh_status()["state"] in ("done", "error"):
                    break
                time.sleep(0.02)
        info = mt.refresh_status()
        self.assertEqual(info["state"], "done")
        self.assertIsNone(info["last_error"],
                         "a successful refresh must clear last_error")


class TestNoImportTimeConfigLoad(unittest.TestCase):
    """[F3] `CONFIG = load_config()` at module level was dead state (no
    production code in the module reads it — get_filler_phrase and
    _refresh_loop call load_config() directly) that forced a full config
    parse AND load_config's process-wide os.chdir() at import; worse, a
    missing config.ini makes load_config sys.exit(1) — a BaseException
    no except-Exception armor can catch. Import-time statements must
    never call load_config. The module-level CONFIG survives ONLY as a
    test seam (tests patch it and stub load_config to return it);
    production never reads it."""

    def test_no_top_level_load_config_call(self):
        with open(os.path.join(_SRC, "modules", "module_thinking.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                continue  # bodies run at call time, not import time
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "load_config"):
                    self.fail(
                        "module_thinking calls load_config() at import "
                        "time — config parse + process-wide chdir + "
                        "sys.exit(1)-on-missing-config risk")

    def test_config_is_a_none_test_seam(self):
        # option (a): the attribute stays (tests patch it), production
        # never populates it — it must rest at None
        self.assertIsNone(mt.CONFIG)


if __name__ == "__main__":
    unittest.main()
