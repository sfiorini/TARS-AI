"""[UI-DEFAULTS] The web-UI config form must render a field even when the
user's config.ini predates that field.

Root cause this pins: ``module_chatui.get_config()`` only emitted a field
``if field_name in config[section_name]``, so every existing installation
silently hid the fields of any feature that added a new config key.
Measured live 2026-11-12: ``thinking_refresh_enabled`` and
``thinking_refresh_minutes`` were absent from the robot's config.ini and
therefore invisible in the UI, even though ``config.ini.template`` shipped
them and ``load_config()`` supplied defaults (False / 120).

The merge lives in ``module_config.resolve_ui_config_values()`` because the
suite cannot import ``module_chatui`` — it pulls in Pi-only
``board``/``busio``/``adafruit_pca9685`` via ``module_servoctl``.
"""
import ast
import configparser
import os
import unittest

from modules.module_config import CONFIG_METADATA, resolve_ui_config_values

# Absolute paths: importing module_config runs load_config(), which chdirs
# the process (module_config.py:442) — a relative path would resolve
# somewhere else entirely.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TEMPLATE = os.path.join(_SRC, "config.ini.template")


def _parser(text):
    p = configparser.RawConfigParser()
    p.optionxform = str
    p.read_string(text)
    return p


class TestResolveUiConfigValues(unittest.TestCase):
    def test_missing_key_falls_back_to_template(self):
        """THE bug: a key absent from config.ini must still render."""
        user = _parser("[CHAR]\nuser_name = stefano\n")
        default = _parser(
            "[CHAR]\nuser_name = TARS\nthinking_refresh_enabled = false\n")
        out = resolve_ui_config_values(user, default, {"CHAR": {
            "user_name": {}, "thinking_refresh_enabled": {}}})
        self.assertEqual(out["CHAR"]["thinking_refresh_enabled"], "false")
        self.assertEqual(out["CHAR"]["user_name"], "stefano")

    def test_user_value_wins_over_template(self):
        user = _parser("[CHAR]\nthinking_refresh_enabled = true\n")
        default = _parser("[CHAR]\nthinking_refresh_enabled = false\n")
        out = resolve_ui_config_values(
            user, default, {"CHAR": {"thinking_refresh_enabled": {}}})
        self.assertEqual(out["CHAR"]["thinking_refresh_enabled"], "true")

    def test_field_in_neither_is_omitted(self):
        user = _parser("[CHAR]\nuser_name = x\n")
        default = _parser("[CHAR]\nuser_name = y\n")
        out = resolve_ui_config_values(user, default, {"CHAR": {"ghost": {}}})
        self.assertNotIn("ghost", out["CHAR"])

    def test_missing_section_falls_back_to_template(self):
        user = _parser("[CHAR]\nuser_name = x\n")
        default = _parser("[CHAR]\nuser_name = y\n[TTS]\nttsoption = piper\n")
        out = resolve_ui_config_values(user, default, {"TTS": {"ttsoption": {}}})
        self.assertEqual(out["TTS"]["ttsoption"], "piper")

    def test_section_in_neither_is_omitted(self):
        user = _parser("[CHAR]\nuser_name = x\n")
        default = _parser("[CHAR]\nuser_name = y\n")
        out = resolve_ui_config_values(user, default, {"NOPE": {"a": {}}})
        self.assertNotIn("NOPE", out)

    def test_dunder_keys_skipped(self):
        user = _parser("[CHAR]\nuser_name = x\n")
        default = _parser("[CHAR]\nuser_name = y\n")
        out = resolve_ui_config_values(
            user, default, {"CHAR": {"__description__": "d", "user_name": {}}})
        self.assertEqual(list(out["CHAR"]), ["user_name"])

    def test_ui_field_order_preserved(self):
        """The form renders in CONFIG_METADATA order, not file order."""
        user = _parser("[CHAR]\nb = 2\na = 1\n")
        out = resolve_ui_config_values(user, _parser(""),
                                       {"CHAR": {"a": {}, "b": {}}})
        self.assertEqual(list(out["CHAR"]), ["a", "b"])

    def test_empty_user_parser_renders_from_template(self):
        with open(_TEMPLATE) as fh:
            tpl = _parser(fh.read())
        out = resolve_ui_config_values(_parser(""), tpl, {"CHAR": {
            "user_name": {}, "thinking_refresh_enabled": {}}})
        self.assertIn("user_name", out["CHAR"])
        self.assertIn("thinking_refresh_enabled", out["CHAR"])


class TestTemplateCoversEveryUiField(unittest.TestCase):
    """The fallback is only as good as the template. If a feature adds a
    CONFIG_METADATA field but forgets config.ini.template, the field stays
    invisible on every existing install — the very bug this change fixes."""

    # Dead key: referenced only by load_config()/CONFIG_METADATA, read by
    # nothing (no skill, route or JS). Deliberately absent from the template
    # so the UI does not offer a toggle that does nothing.
    KNOWN_DEAD = {"ACCESS.remote_access_enabled"}

    def test_every_metadata_field_is_in_the_template(self):
        with open(_TEMPLATE) as fh:
            tpl = _parser(fh.read())
        missing = []
        for section, fields in CONFIG_METADATA.items():
            for field in fields:
                if field.startswith("__"):
                    continue
                key = f"{section}.{field}"
                if key in self.KNOWN_DEAD:
                    continue
                if not tpl.has_section(section) or field not in tpl[section]:
                    missing.append(key)
        self.assertEqual(missing, [])


class TestGetConfigWiring(unittest.TestCase):
    """The helper is useless if get_config() stops calling it, and the suite
    cannot import module_chatui (Pi-only board/busio via module_servoctl),
    so the wiring is pinned by AST. Fails if the old disk-only filter is
    restored."""

    def setUp(self):
        self.path = os.path.join(_SRC, "modules", "module_chatui.py")
        with open(self.path) as fh:
            self.source = fh.read()
        tree = ast.parse(self.source)
        self.fn = next((n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef)
                        and n.name == "get_config"), None)

    def test_get_config_exists(self):
        self.assertIsNotNone(self.fn, "get_config() not found in module_chatui")

    def test_get_config_calls_the_resolver(self):
        names = set()
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    names.add(node.func.attr)
        self.assertIn("resolve_ui_config_values", names)

    def test_old_disk_only_filter_is_gone(self):
        body = ast.get_source_segment(self.source, self.fn)
        self.assertNotIn("if field_name in config[section_name]", body)
        self.assertNotIn("if section_name not in config.sections()", body)


if __name__ == "__main__":
    unittest.main()
