"""Unit tests for GLM list-of-blocks normalization in module_vision describe backends."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Bootstrap gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
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
            f"(required for module imports): {exc}"
        ) from exc

from modules import module_vision


_GLM_BLOCKS = [
    {"type": "thinking", "thinking": "internal reasoning"},
    {"type": "text", "text": "A robot arm on a desk"},
]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class TestVisionGlmNormalization(unittest.TestCase):
    def test_describe_llm_normalizes_glm_blocks(self):
        fake = _FakeResponse({"choices": [{"message": {"content": _GLM_BLOCKS}}]})
        with mock.patch.object(
                module_vision, "_get_vision_settings",
                return_value=("key", "http://vision.test", "m")), \
             mock.patch.object(module_vision, "CONFIG", {
                 "LLM": {"llm_backend": "other"},
                 "VISION": {"vision_max_tokens": "150"},
             }), \
             mock.patch.object(module_vision, "queue_message"), \
             mock.patch.object(module_vision.requests, "post", return_value=fake) as post:
            result = module_vision._describe_llm(b"img", "what is this?")
        self.assertEqual(result, "A robot arm on a desk")
        self.assertTrue(post.called)

    def test_describe_openai_normalizes_glm_blocks(self):
        fake = _FakeResponse({"choices": [{"message": {"content": _GLM_BLOCKS}}]})
        with mock.patch.object(module_vision, "CONFIG", {
                 "LLM": {"api_key": "key"},
                 "VISION": {"use_llm_backend": True},
             }), \
             mock.patch.object(module_vision, "queue_message"), \
             mock.patch.object(module_vision.requests, "post", return_value=fake):
            result = module_vision._describe_openai(b"img", "what is this?")
        self.assertEqual(result, "A robot arm on a desk")


if __name__ == "__main__":
    unittest.main()
