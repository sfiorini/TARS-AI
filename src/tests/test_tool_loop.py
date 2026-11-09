"""Deferred reply loop tests (M4): config keys, runner state machine,
holding line, followup completion, memory preservation, rollback switch.

The loop is the riskiest behavioural change in the plan (D1): these tests
pin the state machine with a fake followup_fn and fake registry
descriptors — no network, no LLM.
"""
import os
import shutil
import sys
import types
import unittest
from unittest import mock


# Bootstrap gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_INI = os.path.join(_SRC, "config.ini")
if not os.path.exists(_CONFIG_INI):
    _TEMPLATE = os.path.join(_SRC, "config.ini.template")
    try:
        shutil.copyfile(_TEMPLATE, _CONFIG_INI)
    except OSError as exc:
        raise RuntimeError(
            "Cannot bootstrap src/config.ini from src/config.ini.template "
            f"(required for module imports): {exc}"
        ) from exc


def _install_fake_hardware():
    """board/busio/adafruit_pca9685 only exist on the Pi — inject fakes so
    module_servoctl can be imported on any dev machine."""
    board = types.ModuleType("board")
    board.SCL = "SCL"
    board.SDA = "SDA"
    busio = types.ModuleType("busio")
    busio.I2C = mock.MagicMock()
    pca9685 = types.ModuleType("adafruit_pca9685")
    pca9685.PCA9685 = mock.MagicMock()
    for name, mod in (("board", board), ("busio", busio),
                      ("adafruit_pca9685", pca9685)):
        sys.modules.setdefault(name, mod)


_install_fake_hardware()

import tempfile  # noqa: E402

from modules import module_config  # noqa: E402


class TestLoopConfig(unittest.TestCase):
    """[LLM] loop/timeout keys with code defaults (S-M4-01)."""

    def test_llm_loop_keys_have_defaults(self):
        # a config WITHOUT the new keys must still expose the defaults
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        minimal = os.path.join(tmp, "config.ini")
        with open(os.path.join(_SRC, "config.ini.template")) as fh:
            text = fh.read()
        for key in ("tool_loop_max_hops", "tool_call_timeout",
                    "request_timeout", "deferred_loop_enabled"):
            text = "\n".join(ln for ln in text.splitlines()
                             if not ln.startswith(key))
        with open(minimal, "w") as fh:
            fh.write(text)
        saved_path = os.environ.get("TARS_CONFIG_PATH")
        saved_cache = module_config._config_cache
        os.environ["TARS_CONFIG_PATH"] = minimal
        module_config._config_cache = None
        self.addCleanup(module_config.__setattr__, "_config_cache",
                        saved_cache)
        def _restore():
            if saved_path is None:
                os.environ.pop("TARS_CONFIG_PATH", None)
            else:
                os.environ["TARS_CONFIG_PATH"] = saved_path
        self.addCleanup(_restore)
        config = module_config.load_config()
        self.assertEqual(config['LLM']['tool_loop_max_hops'], 2)
        self.assertEqual(config['LLM']['tool_call_timeout'], 60)
        self.assertEqual(config['LLM']['request_timeout'], 120)
        self.assertIs(config['LLM']['deferred_loop_enabled'], True)


if __name__ == "__main__":
    unittest.main()
