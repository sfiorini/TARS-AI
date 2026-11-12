"""Milestone 1 tests: module_thinking — categories, bank, sanitizer,
tool map, classifier, fallback chain. Pure and isolated (no wiring)."""
import unittest

from modules.module_thinking import CATEGORIES, REGISTERS, PHRASE_BANK, sanitize_phrase


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


if __name__ == "__main__":
    unittest.main()
