"""Unit tests for servoctl self-healing (no-reboot recovery):
- lazy PCA9685 re-init on dead pca (rate-limited)
- I2C OSError invalidation so the bus is re-created without a reboot
- stuck-MOVING watchdog reset
- bulletproof movement notifications
- servo_status / force_servo_recovery
"""
import os
import sys
import time
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
    if "modules.module_servoctl" in sys.modules:
        return
    board = types.ModuleType("board")
    board.SCL = "SCL"
    board.SDA = "SDA"
    busio = types.ModuleType("busio")
    busio.I2C = mock.MagicMock()
    pca9685 = types.ModuleType("adafruit_pca9685")
    pca9685.PCA9685 = mock.MagicMock()
    for name, mod in (("board", board), ("busio", busio), ("adafruit_pca9685", pca9685)):
        sys.modules.setdefault(name, mod)


_install_fake_hardware()
import modules.module_servoctl as servoctl  # noqa: E402


class _RaisingChannel:
    """Fake PCA9685 channel whose duty_cycle setter always raises."""

    def __init__(self, exc):
        self._exc = exc

    @property
    def duty_cycle(self):
        return 0

    @duty_cycle.setter
    def duty_cycle(self, value):
        raise self._exc


class _FakeMode1Pca:
    """Fake PCA9685 exposing readable mode1_reg/mode2_reg (value or raising)."""

    def __init__(self, mode1=None, mode2=0x04, exc=None):
        self._mode1 = mode1
        self._mode2 = mode2
        self._exc = exc

    @property
    def mode1_reg(self):
        if self._exc is not None:
            raise self._exc
        return self._mode1

    @property
    def mode2_reg(self):
        if self._exc is not None:
            raise self._exc
        return self._mode2


class _FakeI2c:
    """Raw I2C fake for the register-read fallback path."""

    def __init__(self, reg_values=None):
        self._regs = reg_values or {}
        self.last_reg = None

    def writeto(self, addr, buf):
        self.last_reg = buf[0]

    def readfrom(self, addr, n):
        return bytes([self._regs.get(self.last_reg, 0)])

    def writeto_then_readfrom(self, addr, buf, n):
        self.last_reg = buf[0]
        return bytes([self._regs.get(self.last_reg, 0)])


class ServoctlSelfHealingTestCase(unittest.TestCase):
    """Save/restore every servoctl global touched by a test."""

    _TRACKED = (
        "pca", "MOVING", "_last_init_attempt", "_i2c_error_count",
        "_last_pwm_time", "_on_movement_start", "_on_movement_end",
        "servo_positions", "_is_ventilate_operation",
        "_watchdog_running", "_watchdog_thread", "_watchdog_interval",
        "battery_module", "_i2c",
    )

    def setUp(self):
        self._saved = {}
        for key in self._TRACKED:
            if hasattr(servoctl, key):
                self._saved[key] = getattr(servoctl, key)
        self.addCleanup(self._restore_globals)

    def _restore_globals(self):
        for key, value in self._saved.items():
            setattr(servoctl, key, value)


class TestLazyPcaReinit(ServoctlSelfHealingTestCase):

    def test_none_pca_triggers_lazy_reinit(self):
        servoctl.pca = None
        servoctl._last_init_attempt = 0.0
        fake_pca = mock.MagicMock()

        def fake_init():
            servoctl.pca = fake_pca
            return True

        with mock.patch.object(servoctl, "initialize_pca9685", side_effect=fake_init):
            self.assertTrue(servoctl.set_servo_pwm(0, 350))
        self.assertIs(servoctl.pca, fake_pca)
        fake_pca.channels.__getitem__.assert_called_with(0)

    def test_lazy_reinit_rate_limited_to_once_per_5s(self):
        servoctl.pca = None
        servoctl._last_init_attempt = time.time()  # just attempted
        with mock.patch.object(servoctl, "initialize_pca9685") as init:
            self.assertFalse(servoctl.set_servo_pwm(0, 350))
            init.assert_not_called()
        self.assertIsNone(servoctl.pca)

    def test_none_pca_returns_false_when_reinit_fails(self):
        servoctl.pca = None
        servoctl._last_init_attempt = 0.0
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=False):
            self.assertFalse(servoctl.set_servo_pwm(0, 350))
        self.assertIsNone(servoctl.pca)


class TestI2cErrorInvalidation(ServoctlSelfHealingTestCase):

    def test_errno_121_after_retries_invalidates_pca(self):
        fake_pca = mock.MagicMock()
        fake_pca.channels = {0: _RaisingChannel(OSError(121, "Remote I/O error"))}
        servoctl.pca = fake_pca
        before = servoctl._i2c_error_count

        with mock.patch("time.sleep"):
            self.assertFalse(servoctl.set_servo_pwm(0, 350))

        self.assertIsNone(servoctl.pca)
        self.assertEqual(servoctl._i2c_error_count, before + 1)

    def test_other_exception_invalidates_pca(self):
        fake_pca = mock.MagicMock()
        fake_pca.channels = {0: _RaisingChannel(ValueError("dead device"))}
        servoctl.pca = fake_pca
        before = servoctl._i2c_error_count

        self.assertFalse(servoctl.set_servo_pwm(0, 350))

        self.assertIsNone(servoctl.pca)
        self.assertEqual(servoctl._i2c_error_count, before + 1)

    def test_successful_pwm_updates_last_pwm_time(self):
        servoctl.pca = mock.MagicMock()
        servoctl._last_pwm_time = 0.0
        self.assertTrue(servoctl.set_servo_pwm(0, 350))
        self.assertGreater(servoctl._last_pwm_time, 0.0)


class TestIsMovingStuck(ServoctlSelfHealingTestCase):

    def test_boundaries(self):
        now = 1000.0
        # not moving -> never stuck
        self.assertFalse(servoctl._is_moving_stuck(False, now - 999, now))
        # moving, exactly at timeout -> not stuck yet
        self.assertFalse(servoctl._is_moving_stuck(True, now - 180, now))
        # moving, past timeout -> stuck
        self.assertTrue(servoctl._is_moving_stuck(True, now - 181, now))
        # moving, but no PWM activity ever recorded -> stuck
        self.assertTrue(servoctl._is_moving_stuck(True, 0.0, now))
        # custom timeout
        self.assertTrue(servoctl._is_moving_stuck(True, now - 11, now, timeout=10))
        self.assertFalse(servoctl._is_moving_stuck(True, now - 10, now, timeout=10))


class TestBulletproofNotifications(ServoctlSelfHealingTestCase):

    def test_notify_movement_start_survives_raising_callback(self):
        def boom():
            raise RuntimeError("UI pause exploded")

        servoctl.set_movement_callbacks(on_start=boom)
        servoctl._is_ventilate_operation = False
        try:
            servoctl._notify_movement_start()  # must not raise
        finally:
            servoctl.set_movement_callbacks()

    def test_notify_movement_end_survives_raising_callback(self):
        def boom():
            raise RuntimeError("UI resume exploded")

        servoctl.set_movement_callbacks(on_end=boom)
        try:
            servoctl._notify_movement_end()  # must not raise
        finally:
            servoctl.set_movement_callbacks()

    def test_notify_movement_start_survives_raising_signal_servo_activity(self):
        # RC-2 hole: signal_servo_activity() runs BEFORE any inner try/except;
        # if it raises, MOVING stays True in every movement function.
        bad_battery = mock.MagicMock()
        bad_battery.signal_servo_activity.side_effect = RuntimeError("battery exploded")
        servoctl.set_battery_module(bad_battery)
        servoctl._notify_movement_start()  # must not raise

    def test_notify_movement_end_survives_raising_signal_servo_activity(self):
        bad_battery = mock.MagicMock()
        bad_battery.signal_servo_activity.side_effect = RuntimeError("battery exploded")
        servoctl.set_battery_module(bad_battery)
        servoctl._notify_movement_end()  # must not raise


class TestServoStatusAndRecovery(ServoctlSelfHealingTestCase):

    def test_servo_status_returns_expected_keys(self):
        status = servoctl.servo_status()
        self.assertEqual(
            set(status.keys()),
            {"pca_alive", "moving", "last_pwm_age_s", "i2c_error_count", "watchdog_running", "mode1", "mode2"},
        )
        self.assertIsInstance(status["i2c_error_count"], int)
        self.assertIsInstance(status["pca_alive"], bool)
        self.assertIsInstance(status["moving"], bool)
        self.assertIsInstance(status["watchdog_running"], bool)

    def test_force_servo_recovery_reapplies_saved_positions(self):
        servoctl.MOVING = True
        servoctl.pca = None
        servoctl.servo_positions = {0: 350, 3: 300}

        def fake_init():
            servoctl.pca = mock.MagicMock()
            return True

        with mock.patch.object(servoctl, "initialize_pca9685", side_effect=fake_init), \
             mock.patch.object(servoctl, "set_servo_pwm") as ssp:
            status = servoctl.force_servo_recovery()

        self.assertFalse(servoctl.MOVING)
        self.assertTrue(status["pca_alive"])
        ssp.assert_any_call(0, 350)
        ssp.assert_any_call(3, 300)


class TestMode1Health(ServoctlSelfHealingTestCase):

    def test_mode1_healthy_boundaries(self):
        self.assertTrue(servoctl._mode1_healthy(0x20))   # healthy after driver init
        self.assertFalse(servoctl._mode1_healthy(0x01))  # chip-logic-reset broken state
        self.assertFalse(servoctl._mode1_healthy(0x00))
        self.assertTrue(servoctl._mode1_healthy(0xA0))   # RESTART pending + AI set
        self.assertFalse(servoctl._mode1_healthy(None))    # unreadable

    def test_read_mode1_returns_register_value(self):
        servoctl.pca = _FakeMode1Pca(0x20)
        self.assertEqual(servoctl._read_mode1(), 0x20)

    def test_read_mode1_returns_none_on_error(self):
        servoctl.pca = _FakeMode1Pca(exc=OSError(121, "Remote I/O error"))
        self.assertIsNone(servoctl._read_mode1())

    def test_mode2_healthy_boundaries(self):
        # live-verified on the robot: ONLY 0x04 physically drives the servos;
        # 0x01 (driver default after init) is silent; 0x0B/0x1E are hi-Z glitches
        self.assertTrue(servoctl._mode2_healthy(0x04))
        self.assertFalse(servoctl._mode2_healthy(0x01))  # silent despite ACKing
        self.assertFalse(servoctl._mode2_healthy(0x00))
        self.assertFalse(servoctl._mode2_healthy(0x1E))  # observed live: outputs hi-Z, no motor noise
        self.assertFalse(servoctl._mode2_healthy(0x0B))  # observed live: outputs off
        self.assertFalse(servoctl._mode2_healthy(None))   # unreadable

    def test_write_register_defined_before_init_at_module_level(self):
        # Regression: initialize_pca9685() runs at module import time, so
        # _write_register must be defined earlier in the source (a boot-time
        # NameError left pca=None until the watchdog recovered it).
        src = open(os.path.join(os.path.dirname(servoctl.__file__), "module_servoctl.py")).read()
        self.assertLess(src.index("def _write_register"), src.index("def initialize_pca9685"))

    def test_init_writes_mode2_working_value(self):
        # initialize_pca9685 must leave MODE2=0x04, the only live-verified
        # output-drive state on this board
        fake_pca = mock.MagicMock()
        with mock.patch.object(servoctl.busio, "I2C", return_value=mock.MagicMock()), \
             mock.patch.object(servoctl, "PCA9685", return_value=fake_pca):
            self.assertTrue(servoctl.initialize_pca9685())
        self.assertEqual(fake_pca.mode2_reg, 0x04)

    def test_read_mode2_returns_register_value(self):
        servoctl.pca = _FakeMode1Pca(0x20, mode2=0x1E)
        self.assertEqual(servoctl._read_mode2(), 0x1E)

    def test_read_register_raw_fallback_when_driver_attr_missing(self):
        class BarePca:  # no mode1_reg/mode2_reg attributes
            pass
        servoctl.pca = BarePca()
        servoctl._i2c = _FakeI2c({0x00: 0x20, 0x01: 0x04})
        self.assertEqual(servoctl._read_mode1(), 0x20)
        self.assertEqual(servoctl._read_mode2(), 0x04)

    def test_init_failure_clears_stale_pca_handle(self):
        # F1: a failed re-init must not leave a stale non-None pca behind
        servoctl.pca = _FakeMode1Pca(0x20)
        with mock.patch.object(servoctl.busio, "I2C", side_effect=OSError(121, "Remote I/O error")):
            result = servoctl.initialize_pca9685()
        self.assertFalse(result)
        self.assertIsNone(servoctl.pca)

    def test_watchdog_tick_repairs_mode2_outputs_disabled(self):
        # Live failure mode: MODE1 healthy (0x20) but MODE2=0x1E -> outputs hi-Z,
        # no motor drive. Watchdog must re-init even though MODE1 passes.
        servoctl.pca = _FakeMode1Pca(0x20, mode2=0x1E)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time()
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_called_once()

    def test_watchdog_tick_repairs_silent_mode2_default(self):
        # 0x01 is the driver default after init but is SILENT on this board
        # (user-confirmed: no movement, no motor noise) -> must trigger repair
        servoctl.pca = _FakeMode1Pca(0x20, mode2=0x01)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time()
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_called_once()


class TestWatchdog(ServoctlSelfHealingTestCase):

    def test_watchdog_tick_resets_stuck_moving(self):
        servoctl.MOVING = True
        servoctl._last_pwm_time = time.time() - 200
        servoctl.pca = _FakeMode1Pca(0x20)
        servoctl._watchdog_tick()
        self.assertFalse(servoctl.MOVING)

    def test_watchdog_tick_leaves_active_movement_alone(self):
        servoctl.MOVING = True
        servoctl._last_pwm_time = time.time() - 5
        servoctl.pca = _FakeMode1Pca(0x20)
        servoctl._watchdog_tick()
        self.assertTrue(servoctl.MOVING)

    def test_watchdog_tick_retries_pca_init_when_dead(self):
        servoctl.pca = None
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_called_once()

    def test_watchdog_tick_repairs_unhealthy_mode1(self):
        # RC-1b: chip ACKs on I2C but MODE1 lost bit 0x20 -> PWM writes
        # land on wrong registers, servos frozen. Watchdog must re-init.
        servoctl.pca = _FakeMode1Pca(0x01)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time()
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_called_once()

    def test_watchdog_tick_noop_when_mode1_healthy(self):
        servoctl.pca = _FakeMode1Pca(0x20)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time()
        with mock.patch.object(servoctl, "initialize_pca9685") as init:
            servoctl._watchdog_tick()
            init.assert_not_called()

    def test_watchdog_tick_repairs_unreadable_mode1(self):
        servoctl.pca = _FakeMode1Pca(exc=OSError(121, "Remote I/O error"))
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time()
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_called_once()

    def test_start_servo_watchdog_sets_running_flag(self):
        servoctl._watchdog_running = False
        servoctl.start_servo_watchdog(interval=3600)  # long interval: thread just sleeps
        try:
            self.assertTrue(servoctl._watchdog_running)
            self.assertTrue(servoctl.servo_status()["watchdog_running"])
        finally:
            servoctl._watchdog_running = False  # stops the loop


if __name__ == "__main__":
    unittest.main()
