"""Regression tests: MOVING-flag transitions must go through locked helpers.

F1 (review of B7): movement threads mutated servoctl.MOVING with bare
assignments (`servoctl.MOVING = True/False`) while the watchdog reads MOVING
(and _last_pwm_time) under _state_lock — the check-then-set window let the
watchdog see a movement as idle just before its first MOVING=True write and
trigger a chip re-init mid-walk. module_servoctl now exposes:

  - acquire_moving(): atomic test-and-set under _state_lock
  - set_moving(v):    locked write

Movement playback now goes through movementstore.play_steps() (the 33
hardcoded functions are gone); the MOVING-transition assertions run against
play_steps with the fake-hardware servoctl, and the source-format assertion
pins chatui to the delegated playback path.
"""
import os
import re
import shutil
import sys
import threading
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
import modules.module_movements as movements  # noqa: E402  (reduced module must stay importable)


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
    """Playback (movementstore.play_steps) must claim/release MOVING via
    the locked helpers and refuse while another movement holds the flag."""

    def _run_steps(self, steps):
        """Play steps through a tmp MovementStore against the real
        (fake-hardware) servoctl; servo calls and sleeps mocked out."""
        import tempfile
        from modules import module_movementstore
        from modules.module_movementstore import MovementStore
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = MovementStore(
            store_path=os.path.join(tmp, "movements.json"),
            defaults_path=os.path.join(_SRC, "movements_defaults.json"))
        store.load()
        with mock.patch.object(servoctl, "move_legs"), \
             mock.patch.object(servoctl, "move_arm"), \
             mock.patch.object(servoctl, "disable_all_servos"), \
             mock.patch.object(module_movementstore, "time"):
            return store.play_steps(steps)

    @staticmethod
    def _step():
        return {
            "left_height": 50, "right_height": 50,
            "left_leg": 50, "right_leg": 50,
            "left_main": None, "left_forearm": None, "left_hand": None,
            "right_main": None, "right_forearm": None, "right_hand": None,
            "speed": 0.9, "hold_time": 0.0, "movement": None,
        }

    def test_play_steps_sets_and_clears_moving(self):
        self.assertTrue(self._run_steps([self._step()]))
        self.assertFalse(servoctl.MOVING)

    def test_play_steps_rejected_while_moving(self):
        servoctl.MOVING = True
        with mock.patch.object(servoctl, "move_legs") as ml:
            ok = self._run_steps([self._step()])
        self.assertFalse(ok)
        ml.assert_not_called()  # zero servo calls while already moving

    def test_no_bare_moving_write_in_chatui(self):
        """Regression pin (S-M2-03): the builder playback path delegates
        to movementstore.play_steps, whose MOVING transitions go through
        the locked helpers — no bare `_sc.MOVING = ...` in chatui."""
        path = os.path.join(_SRC, "modules", "module_chatui.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        bare = re.findall(r"^\s*_sc\.MOVING\s*=", source, re.M)
        self.assertEqual(bare, [], "bare _sc.MOVING assignment(s) found")


if __name__ == "__main__":
    unittest.main()
