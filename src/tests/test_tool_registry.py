"""Tool registry tests (M3): dataclasses, registry semantics, builtin
adapter, byte-identical prompt block.

M3 is behaviour-neutral: the registry is the seam the deferred loop (M4)
will dispatch through.  dispatch must NEVER raise — exceptions become
ToolResult(content=str(e), is_error=True).
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

from modules.module_toolregistry import (  # noqa: E402
    ToolDescriptor, ToolResult, ToolContext, ToolRegistry,
    get_registry, reset_registry,
)


def _descriptor(name="probe", kind="builtin", execute=None,
                description="a probe tool", parameters=None,
                result_bearing=False):
    return ToolDescriptor(
        name=name, kind=kind, description=description,
        parameters=parameters, result_bearing=result_bearing,
        timeout_s=60.0, execute=execute or (lambda params, ctx: "ok"))


class TestRegistryCore(unittest.TestCase):
    """ToolRegistry register/get/dispatch semantics (S-M3-01)."""

    def setUp(self):
        super().setUp()
        self.registry = ToolRegistry()

    def test_register_and_get(self):
        d = _descriptor(name="probe")
        self.registry.register(d)
        self.assertIs(self.registry.get("probe"), d)
        self.assertIsNone(self.registry.get("missing"))

    def test_duplicate_name_keeps_first(self):
        first = _descriptor(name="x", kind="builtin")
        second = _descriptor(name="x", kind="mcp")
        with mock.patch("modules.module_toolregistry.queue_message") as qm:
            self.registry.register(first)
            self.registry.register(second)
        self.assertEqual(self.registry.get("x").kind, "builtin")
        # one entry per name, later one disabled
        self.assertEqual([d.name for d in self.registry.iter_descriptors()
                          if d.name == "x"], ["x"])
        self.assertIn("duplicate tool 'x' from mcp",
                      qm.call_args[0][0])

    def test_dispatch_unknown_returns_is_error(self):
        result = self.registry.dispatch("nope", {}, ToolContext())
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.is_error)
        self.assertIn("Unknown function: nope", result.content)

    def test_dispatch_never_raises(self):
        def boom(params, ctx):
            raise ValueError("boom message")
        self.registry.register(_descriptor(name="exploder", execute=boom))
        result = self.registry.dispatch("exploder", {}, ToolContext())
        self.assertTrue(result.is_error)
        self.assertIn("boom message", result.content)
        self.assertIsInstance(result, ToolResult)

    def test_dispatch_wraps_string_result(self):
        self.registry.register(
            _descriptor(name="greeter", execute=lambda p, c: "hello there"))
        result = self.registry.dispatch("greeter", {}, ToolContext())
        self.assertEqual(result.content, "hello there")
        self.assertFalse(result.is_error)

    def test_get_prompt_summaries(self):
        self.registry.register(_descriptor(
            name="probe", description="probe tool",
            parameters={"properties": {"q": {"type": "string"}},
                        "required": ["q"]}))
        summaries = self.registry.get_prompt_summaries()
        self.assertEqual(summaries,
                         [("probe", "probe tool",
                           {"properties": {"q": {"type": "string"}},
                            "required": ["q"]})])

    def test_singleton_and_reset(self):
        reset_registry()
        first = get_registry()
        second = get_registry()
        self.assertIs(first, second)
        reset_registry()
        self.assertIsNot(get_registry(), first)


if __name__ == "__main__":
    unittest.main()
