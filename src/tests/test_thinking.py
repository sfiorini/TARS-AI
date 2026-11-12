"""Milestone 1 tests: module_thinking — categories, bank, sanitizer,
tool map, classifier, fallback chain. Pure and isolated (no wiring)."""
import os
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
        # NEVER the repo's config.ini (it ships a non-empty floor list)
        mt.CONFIG = {"CHAR": {"thinking_responses": "[]"}}
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
        t = mock.patch("modules.module_llm.threading.Thread", _ImmediateThread)
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
        mt.CONFIG = {"CHAR": {"thinking_responses": "[]"}}
        self._played = []

        async def _fake_play(text, *a, **k):
            # coroutine stub — see TestHotPathSelection.setUp (a plain
            # lambda makes asyncio.run raise and log a spurious error)
            self._played.append(text)

        p = mock.patch("modules.module_tts.play_audio_chunks", new=_fake_play)
        p.start(); self.addCleanup(p.stop)
        t = mock.patch("modules.module_llm.threading.Thread", _ImmediateThread)
        t.start(); self.addCleanup(t.stop)

    def tearDown(self):
        mt.reset_dedup()
        llm.CONFIG = self._cfg
        mt.CONFIG = self._mt_cfg
        mt._refreshed.clear()

    def test_second_fire_same_turn_suppressed(self):
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
        # the config floor reads mt.CONFIG — never the repo's config.ini
        mt.CONFIG = {"CHAR": {"thinking_responses": "[]"}}
        self._played = []

        async def _fake_play(text, *a, **k):
            # coroutine stub — see TestHotPathSelection.setUp
            self._played.append(text)

        p = mock.patch("modules.module_tts.play_audio_chunks", new=_fake_play)
        p.start(); self.addCleanup(p.stop)
        t = mock.patch("modules.module_llm.threading.Thread", _ImmediateThread)
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
            'if tool_names is None and user_text and category != "general":',
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


if __name__ == "__main__":
    unittest.main()
