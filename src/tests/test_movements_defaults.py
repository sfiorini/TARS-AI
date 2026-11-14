"""M0 backend story tests: MovementStore.get_defaults() + /movements/defaults route."""
import ast
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

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
    board = types.ModuleType("board")
    board.SCL = "SCL"
    board.SDA = "SDA"
    busio = types.ModuleType("busio")
    busio.I2C = mock.MagicMock()
    pca9685 = types.ModuleType("adafruit_pca9685")
    pca9685.PCA9685 = mock.MagicMock()
    sys.modules["board"] = board
    sys.modules["busio"] = busio
    sys.modules["adafruit_pca9685"] = pca9685


_DEFAULTS_PATH = os.path.join(_SRC, "movements_defaults.json")
_CHATUI_PATH = os.path.join(_SRC, "modules", "module_chatui.py")


class _StoreTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        _install_fake_hardware()
        from modules.module_movementstore import MovementStore
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_path = os.path.join(self.tmp, "movements.json")
        self.store = MovementStore(store_path=self.store_path, defaults_path=_DEFAULTS_PATH)
        self.store.load()

    def _write_store(self, movements):
        with open(self.store_path, "w") as fh:
            json.dump({"version": 1, "movements": movements}, fh)

    def _defaults_movements(self):
        with open(_DEFAULTS_PATH, "r") as fh:
            return json.load(fh)["movements"]


class TestDefaultsAccessor(_StoreTestCase):
    def test_get_defaults_returns_exact_tracked_defaults(self):
        first = self.store.load().get("bow")
        self.store.add("custom_m0_test", {
            "name": "Custom", "type": "legs_only", "description": "custom",
            "steps": [{"left_height": 0, "right_height": 0, "left_leg": 0, "right_leg": 0,
                       "left_main": 0, "left_forearm": 0, "left_hand": 0,
                       "right_main": 0, "right_forearm": 0, "right_hand": 0,
                       "speed": 0.85, "hold_time": 0.5, "movement": None}],
            "steps_arms": None,
        })
        self.assertEqual(self.store.get_defaults(), self._defaults_movements())
        self.assertNotIn("custom_m0_test", self.store.get_defaults())

    def test_get_defaults_returns_deep_copy(self):
        # Hold ONE shared source dict: _load_defaults re-reads the file on every
        # call, so two live calls would mask a missing deepcopy (audit M0-B F1).
        cached = self._defaults_movements()
        with mock.patch.object(self.store, "_load_defaults", return_value=cached):
            first = self.store.get_defaults()
            first["bow"]["name"] = "x"
            self.assertEqual(cached["bow"]["name"], "Bow")
            self.assertEqual(self.store.get_defaults()["bow"]["name"], "Bow")

    def test_get_defaults_method_is_pure_read(self):
        with open(self.store_path, "rb") as fh:
            before = fh.read()
        method = next(n for n in ast.walk(ast.parse(open(os.path.join(_SRC, "modules", "module_movementstore.py")).read()))
                      if isinstance(n, ast.FunctionDef) and n.name == "get_defaults")
        names = {n.func.attr for n in ast.walk(method) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(names & {"save", "add", "update", "delete", "reset_from_defaults", "export_to", "import_from", "validate_entry"})
        self.assertFalse(any(isinstance(n, ast.Name) and n.id in {"servoctl", "acquire_moving"} for n in ast.walk(method)))
        self.store.get_defaults()
        with open(self.store_path, "rb") as fh:
            self.assertEqual(before, fh.read())


class TestDefaultsRoute(_StoreTestCase):
    def test_defaults_route_registered_as_read_only_get(self):
        tree = ast.parse(open(_CHATUI_PATH).read())
        route = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "movements_defaults"
        )
        decorators = [
            dec for dec in route.decorator_list
            if isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "route"
        ]
        self.assertEqual(len(decorators), 1)
        decorator = decorators[0]
        self.assertIsInstance(decorator.args[0], ast.Constant)
        self.assertEqual(decorator.args[0].value, "/movements/defaults")
        methods = next((kw.value for kw in decorator.keywords if kw.arg == "methods"), None)
        self.assertIsInstance(methods, ast.List)
        self.assertEqual([item.value for item in methods.elts], ["GET"])

    def test_defaults_route_handler_is_read_only_wrapper(self):
        tree = ast.parse(open(_CHATUI_PATH).read())
        handler = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "movements_defaults")
        attrs = {n.attr for n in ast.walk(handler) if isinstance(n, ast.Attribute)}
        self.assertTrue({"get_movement_store", "get_defaults", "jsonify"} & attrs)
        self.assertFalse(attrs & {"add", "update", "delete", "save", "reset_from_defaults", "prune", "export_to", "import_from", "servoctl"})
        self.assertIn("get_defaults", {n.attr for n in ast.walk(handler) if isinstance(n, ast.Attribute)})


if __name__ == "__main__":
    unittest.main()