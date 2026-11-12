"""Milestone 1 tests: module_thinking — categories, bank, sanitizer,
tool map, classifier, fallback chain. Pure and isolated (no wiring)."""
import unittest

from modules.module_thinking import CATEGORIES, REGISTERS, PHRASE_BANK


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


if __name__ == "__main__":
    unittest.main()
