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
import ast
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


def _disable(field, values):
    return [{"field": field, "values": values}]


class TestThinkingFieldCascade(unittest.TestCase):
    """The thinking settings must never disappear — they grey out instead.
    ``disable_when`` replaces ``depends_on`` on these two fields so the JS
    disables them rather than hiding them."""

    def test_refresh_toggle_disabled_by_the_master_switch(self):
        d = CONFIG_METADATA["CHAR"]["thinking_refresh_enabled"]
        self.assertEqual(d.get("disable_when"),
                         _disable("enable_thinking_responses", ["true"]))
        self.assertNotIn("depends_on", d)

    def test_refresh_minutes_disabled_by_the_refresh_toggle(self):
        d = CONFIG_METADATA["CHAR"]["thinking_refresh_minutes"]
        self.assertEqual(d.get("disable_when"),
                         _disable("thinking_refresh_enabled", ["true"]))
        self.assertNotIn("depends_on", d)


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


def _condition_errors(metadata, attr):
    """Shared validator: every condition under ``attr`` must name a field in
    its OWN section and carry ``values`` or ``not_values``."""
    bad = []
    for section, fields in metadata.items():
        for name, d in fields.items():
            if not isinstance(d, dict):
                continue
            for cond in d.get(attr) or []:
                if cond.get("field") not in fields:
                    bad.append(f"{section}.{name} -> {cond.get('field')}")
                elif not (cond.get("values") or cond.get("not_values")):
                    bad.append(f"{section}.{name} (no values)")
    return bad


class TestDisableWhenIntegrity(unittest.TestCase):
    """A typo in a disable_when target silently leaves the field enabled
    forever, so every condition must name a real field in its OWN section.
    The self-test proves the validator is not vacuous."""

    def test_every_disable_when_target_exists_in_its_own_section(self):
        self.assertEqual(_condition_errors(CONFIG_METADATA, "disable_when"), [])

    def test_disable_when_is_actually_used(self):
        used = [f"{s}.{n}" for s, fields in CONFIG_METADATA.items()
                for n, d in fields.items()
                if isinstance(d, dict) and d.get("disable_when")]
        self.assertIn("CHAR.thinking_refresh_enabled", used)
        self.assertIn("CHAR.thinking_refresh_minutes", used)

    def test_validator_flags_a_bad_target(self):
        metadata = {"CHAR": {
            "a": {"disable_when": [{"field": "ghost", "values": ["true"]}]},
            "b": {"disable_when": [{"field": "a"}]},
        }}
        self.assertEqual(_condition_errors(metadata, "disable_when"),
                         ["CHAR.a -> ghost", "CHAR.b (no values)"])


class TestDisableWhenForwarding(unittest.TestCase):
    """The client can only honour disable_when if module_chatui copies it
    into the per-field payload. The suite cannot import module_chatui
    (Pi-only board/busio via module_servoctl), so pin the copy by AST."""

    def setUp(self):
        path = os.path.join(_SRC, "modules", "module_chatui.py")
        with open(path) as fh:
            tree = ast.parse(fh.read())
        self.fn = next((n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef)
                        and n.name == "get_config"), None)

    def _copied_keys(self):
        keys = set()
        for node in ast.walk(self.fn):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            value = node.value
            # field_options[field_key]['KEY'] = field_def['KEY']
            if not (isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Subscript)
                    and isinstance(value, ast.Subscript)):
                continue
            tkey = target.slice
            vkey = value.slice
            if isinstance(tkey, ast.Constant) and isinstance(vkey, ast.Constant) \
                    and tkey.value == vkey.value:
                keys.add(tkey.value)
        return keys

    def test_get_config_exists(self):
        self.assertIsNotNone(self.fn, "get_config() not found in module_chatui")

    def test_both_condition_keys_are_copied_into_the_payload(self):
        copied = self._copied_keys()
        self.assertIn("depends_on", copied)
        self.assertIn("disable_when", copied)


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
