"""Regression tests: MOVING-flag transitions must go through locked helpers.

F1 (review of B7): movement threads mutated servoctl.MOVING with bare
assignments (`servoctl.MOVING = True/False`) while the watchdog reads MOVING
(and _last_pwm_time) under _state_lock — the check-then-set window let the
watchdog see a movement as idle just before its first MOVING=True write and
trigger a chip re-init mid-walk. module_servoctl now exposes:

  - acquire_moving(): atomic test-and-set under _state_lock
  - set_moving(v):    locked write

and module_movements.py uses them for every MOVING transition.
"""
import os
import re
import sys
import threading
import types
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
import modules.module_servoctl as servoctl  # noqa: E402
import modules.module_movements as movements  # noqa: E402


class MovingFlagTestCase(unittest.TestCase):
    """Reset MOVING between tests."""

    def setUp(self):
        servoctl.MOVING = False


class TestMovingHelpers(MovingFlagTestCase):
    """acquire_moving / set_moving semantics."""

    def test_acquire_moving_claims_when_idle(self):
        self.assertTrue(servoctl.acquire_moving())
        self.assertTrue(servoctl.MOVING)

    def test_acquire_moving_refuses_when_already_moving(self):
        servoctl.MOVING = True
        self.assertFalse(servoctl.acquire_moving())
        self.assertTrue(servoctl.MOVING)  # unchanged, no clobber

    def test_set_moving_writes_value(self):
        servoctl.set_moving(True)
        self.assertTrue(servoctl.MOVING)
        servoctl.set_moving(False)
        self.assertFalse(servoctl.MOVING)

    def test_acquire_moving_is_atomic_under_threads(self):
        """N threads racing acquire_moving: exactly one wins (the bare
        check-then-set pattern could let more than one through)."""
        results = []
        barrier = threading.Barrier(8)

        def racer():
            barrier.wait(timeout=5)
            results.append(servoctl.acquire_moving())

        threads = [threading.Thread(target=racer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(sum(results), 1)
        self.assertTrue(servoctl.MOVING)


class TestMovementsUseLockedHelpers(MovingFlagTestCase):
    """Movement functions must claim/release MOVING via the helpers and
    leave no bare MOVING assignment behind."""

    def _run_movement(self, name):
        """Run one movement with the servo calls and sleeps mocked out."""
        with mock.patch.object(movements, "move_legs"), \
             mock.patch.object(movements, "move_arm"), \
             mock.patch.object(movements, "disable_all_servos"), \
             mock.patch.object(movements.time, "sleep"):
            getattr(movements, name)()

    def test_movement_sets_and_clears_moving(self):
        self._run_movement("step_forward")
        self.assertFalse(servoctl.MOVING)

    def test_movement_is_rejected_while_moving(self):
        servoctl.MOVING = True
        with mock.patch.object(movements, "move_legs") as ml:
            self._run_movement("step_forward")
        ml.assert_not_called()  # second movement no-ops, MOVING untouched

    def test_no_bare_moving_assignment_remains(self):
        """Regression pin: every MOVING write in module_movements.py goes
        through the locked helpers — no bare `servoctl.MOVING = ...`."""
        path = os.path.join(_SRC, "modules", "module_movements.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        bare = re.findall(r"^\s*servoctl\.MOVING\s*=", source, re.M)
        self.assertEqual(bare, [], "bare servoctl.MOVING assignment(s) found")

    def test_no_bare_check_then_set_pattern_remains(self):
        """The racy `if not servoctl.MOVING:` read must be replaced by the
        atomic acquire_moving() claim."""
        path = os.path.join(_SRC, "modules", "module_movements.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("if not servoctl.MOVING:", source)


if __name__ == "__main__":
    unittest.main()
