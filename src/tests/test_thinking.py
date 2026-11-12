"""Milestone 1 tests: module_thinking — categories, bank, sanitizer,
tool map, classifier, fallback chain. Pure and isolated (no wiring)."""
import unittest
from unittest import mock

from modules import module_thinking as mt
from modules.module_thinking import (CATEGORIES, REGISTERS, PHRASE_BANK,
    sanitize_phrase, TOOL_CATEGORY_MAP)


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


if __name__ == "__main__":
    unittest.main()
