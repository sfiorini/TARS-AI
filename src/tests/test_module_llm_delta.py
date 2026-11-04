"""Unit tests for module_llm._delta_text (GLM list-of-blocks streaming fix)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Bootstrap the gitignored src/config.ini from the template: module_llm runs
# load_config() at import time, so a fresh clone without config.ini would
# raise NoSectionError on import and error out the whole test module.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_INI = os.path.join(_SRC, "config.ini")
if not os.path.exists(_CONFIG_INI):
    _TEMPLATE = os.path.join(_SRC, "config.ini.template")
    try:
        import shutil
        shutil.copyfile(_TEMPLATE, _CONFIG_INI)
    except OSError as exc:
        raise RuntimeError(
            "Cannot bootstrap src/config.ini from src/config.ini.template "
            f"(required for module_llm import): {exc}"
        ) from exc

from modules.module_llm import _delta_text


class TestDeltaText(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(_delta_text("hello"), "hello")

    def test_glm_thinking_plus_text_blocks(self):
        content = [
            {"type": "thinking", "thinking": "Let me think..."},
            {"type": "text", "text": "Hi there"},
        ]
        self.assertEqual(_delta_text(content), "Hi there")

    def test_list_of_strings(self):
        self.assertEqual(_delta_text(["Hel", "lo"]), "Hello")

    def test_empty_list(self):
        self.assertEqual(_delta_text([]), "")

    def test_none(self):
        self.assertEqual(_delta_text(None), "")

    def test_bare_dict(self):
        self.assertEqual(_delta_text({"type": "text", "text": "x"}), "")

    def test_text_block_with_non_string_value(self):
        content = [
            {"type": "text", "text": None},
            {"type": "text", "text": 5},
            {"type": "text", "text": "ok"},
        ]
        self.assertEqual(_delta_text(content), "ok")

    def test_mixed_blocks(self):
        content = [
            {"type": "text", "text": "A"},
            {"type": "thinking", "thinking": "B"},
            "C",
        ]
        self.assertEqual(_delta_text(content), "AC")


if __name__ == "__main__":
    unittest.main()
