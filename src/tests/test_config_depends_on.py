"""[DEPENDS-ON] The thinking-phrase settings must cascade, and the manual
refresh control must live with them.

Measured by the user 2026-11-12: with ``thinking_refresh_enabled`` OFF the
``thinking_refresh_minutes`` field was still editable — the three thinking
fields were fully independent. And the "Thinking phrases" refresh card
rendered in the **Access** panel while its own settings rendered in
**System** (internal key ``CHAR``).

No JS work is needed for the cascade: ``applyDependencies()`` /
``attachDependencyHandlers()`` (main.js:2289-2325) already implement
``depends_on`` for config fields — ``values`` / ``not_values``,
checkbox-aware — and main.js:1704-1713 already emits the
``data-dep-conds`` / ``data-dep-section`` attributes. This module pins the
metadata and the card's placement.
"""
import os
import unittest

from modules.module_config import CONFIG_METADATA

# Absolute paths: importing module_config runs load_config(), which chdirs
# the process (module_config.py:442).
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MAIN_JS = os.path.join(_SRC, "www", "static", "js", "main.js")

_ACCESS_MARKER = "// Inject Remote Access tunnel controls"
_CARD_MARKER = "// Inject the thinking-phrase refresh controls"


def _dep(field, values):
    return [{"field": field, "values": values}]


class TestThinkingFieldCascade(unittest.TestCase):
    def test_refresh_toggle_depends_on_the_master_switch(self):
        d = CONFIG_METADATA["CHAR"]["thinking_refresh_enabled"]
        self.assertEqual(d.get("depends_on"),
                         _dep("enable_thinking_responses", ["true"]))

    def test_refresh_minutes_depends_on_the_refresh_toggle(self):
        d = CONFIG_METADATA["CHAR"]["thinking_refresh_minutes"]
        self.assertEqual(d.get("depends_on"),
                         _dep("thinking_refresh_enabled", ["true"]))


class TestDependsOnIntegrity(unittest.TestCase):
    """A typo in a depends_on target silently hides the field forever, so
    every condition must name a real field in its OWN section."""

    def test_every_target_exists_in_its_own_section(self):
        bad = []
        for section, fields in CONFIG_METADATA.items():
            for name, d in fields.items():
                if not isinstance(d, dict):
                    continue
                for cond in d.get("depends_on") or []:
                    if cond.get("field") not in fields:
                        bad.append(f"{section}.{name} -> {cond.get('field')}")
        self.assertEqual(bad, [])

    def test_every_condition_has_a_value_list(self):
        bad = []
        for section, fields in CONFIG_METADATA.items():
            for name, d in fields.items():
                if not isinstance(d, dict):
                    continue
                for cond in d.get("depends_on") or []:
                    if not (cond.get("values") or cond.get("not_values")):
                        bad.append(f"{section}.{name}")
        self.assertEqual(bad, [])


class TestRefreshCardPlacement(unittest.TestCase):
    def setUp(self):
        with open(_MAIN_JS) as fh:
            self.js = fh.read()

    def test_card_is_no_longer_in_the_access_block(self):
        start = self.js.index(_ACCESS_MARKER)
        end = self.js.index(_CARD_MARKER)
        self.assertNotIn("thinkingRefreshBtn", self.js[start:end])

    def test_card_is_injected_into_the_system_panel(self):
        start = self.js.index(_CARD_MARKER)
        block = self.js[start:start + 2000]
        self.assertIn("section === 'CHAR'", block)
        self.assertIn("thinkingRefreshBtn", block)

    def test_card_hides_when_fillers_are_disabled(self):
        start = self.js.index(_CARD_MARKER)
        block = self.js[start:start + 2000]
        self.assertIn("data-dep-conds", block)
        self.assertIn("enable_thinking_responses", block)


if __name__ == "__main__":
    unittest.main()
