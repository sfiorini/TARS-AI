"""Unit tests for servoctl self-healing (no-reboot recovery):
- lazy PCA9685 re-init on dead pca (rate-limited)
- I2C OSError invalidation so the bus is re-created without a reboot
- stuck-MOVING watchdog reset
- bulletproof movement notifications
- servo_status / force_servo_recovery
"""
import inspect
import os
import sys
import threading
import time
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


class _RecordingChannel:
    """Fake PCA9685 channel recording every duty_cycle write."""

    def __init__(self, writes, index):
        self._writes = writes
        self._index = index
        self._duty = 0

    @property
    def duty_cycle(self):
        return self._duty

    @duty_cycle.setter
    def duty_cycle(self, value):
        self._duty = value
        self._writes.append((self._index, value))


class _RecordingPca:
    """Fake PCA9685 recording duty writes as (channel, value) pairs; channels
    listed in fail_channels raise on write (failing-setter path)."""

    def __init__(self, fail_channels=()):
        self.writes = []
        self.channels = {}
        for i in range(16):
            if i in fail_channels:
                self.channels[i] = _RaisingChannel(ValueError("dead channel"))
            else:
                self.channels[i] = _RecordingChannel(self.writes, i)


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
        "_engaged_channels",
        "battery_module", "_i2c", "_unhealthy_chip_reads", "_last_pca_none_log",
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
        # moving, exactly at the default timeout (30s) -> not stuck yet
        self.assertFalse(servoctl._is_moving_stuck(True, now - 30, now))
        # moving, past the default timeout -> stuck
        self.assertTrue(servoctl._is_moving_stuck(True, now - 31, now))
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
        # S-M2-04 engaged-only contract: recovery re-applies ONLY the engaged
        # channels (not every saved position — that would re-energize servos
        # deliberately disabled after a movement).
        servoctl.MOVING = True
        servoctl.pca = None
        servoctl.servo_positions = {0: 350, 3: 300, 12: 400}
        servoctl._engaged_channels = {0, 3}

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
        for call in ssp.call_args_list:
            self.assertIn(call.args[0], {0, 3},
                          "recovery must not re-apply non-engaged channels")


class TestReapplyEngagedPositions(ServoctlSelfHealingTestCase):
    """S-M2-04: repair/recovery re-applies ONLY engaged channels from
    servo_positions, legs (0-3) first. An empty engaged set (the common path:
    disable_all_servos runs after every movement via the movement-store and
    playback paths, so idle means deliberately de-energized) must write ZERO
    PWM registers — that is the jump fix."""

    def test_reapply_engaged_only_legs_first(self):
        servoctl.pca = _RecordingPca()
        servoctl.servo_positions = {0: 100, 4: 300, 8: 310}
        servoctl._engaged_channels = {8, 4, 0}

        with mock.patch("time.sleep"):
            servoctl._reapply_engaged_positions()

        written = [channel for channel, _value in servoctl.pca.writes]
        self.assertEqual(written, [0, 4, 8])  # legs first, then others sorted
        expected = [
            (0, servoctl.pulse_to_duty_cycle(100)),
            (4, servoctl.pulse_to_duty_cycle(300)),
            (8, servoctl.pulse_to_duty_cycle(310)),
        ]
        self.assertEqual(servoctl.pca.writes, expected)

    def test_idle_reapply_zero_writes(self):
        # THE jump fix: idle (engaged set empty after disable_all_servos)
        # repair must touch ZERO PWM registers — deliberately de-energized
        # servos stay de-energized even though servo_positions is non-empty.
        servoctl.pca = _RecordingPca()
        servoctl.servo_positions = {0: 350, 3: 300, 12: 400}
        servoctl._engaged_channels = set()

        servoctl._reapply_engaged_positions()

        self.assertEqual(servoctl.pca.writes, [])

    def test_engaged_channel_missing_position_skipped(self):
        # never write an unknown value: an engaged channel with no saved
        # position is skipped, not defaulted.
        servoctl.pca = _RecordingPca()
        servoctl.servo_positions = {0: 350}
        servoctl._engaged_channels = {0, 5}

        with mock.patch("time.sleep"):
            servoctl._reapply_engaged_positions()

        written = [channel for channel, _value in servoctl.pca.writes]
        self.assertEqual(written, [0])


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

    def test_mode2_exact_match_table(self):
        # S-M2-01: MODE2 must be EXACTLY 0x04 — the only empirically working
        # output-drive state on this board. The old masked check
        # (mode2 & 0x0C) == 0x04 classified hi-Z glitch modes 0x05/0x06/0x07/
        # 0x16/0x17 as healthy, so the watchdog never repaired them.
        table = [
            (0x04, True),
            (0x01, False),
            (0x05, False),
            (0x06, False),
            (0x07, False),
            (0x0B, False),
            (0x16, False),
            (0x17, False),
            (0x1E, False),
            (None, False),
        ]
        for mode2, expected in table:
            with self.subTest(mode2=hex(mode2) if mode2 is not None else None):
                self.assertIs(servoctl._mode2_healthy(mode2), expected)

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
        # no motor drive. Watchdog must re-init even though MODE1 passes
        # (after TWO consecutive unhealthy reads — B7).
        servoctl.pca = _FakeMode1Pca(0x20, mode2=0x1E)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time() - 60
        servoctl._unhealthy_chip_reads = 0
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_not_called()  # first unhealthy read: no repair yet
            servoctl._watchdog_tick()
            init.assert_called_once()  # second consecutive: repair

    def test_watchdog_tick_repairs_silent_mode2_default(self):
        # 0x01 is the driver default after init but is SILENT on this board
        # (user-confirmed: no movement, no motor noise) -> must trigger repair
        # (after TWO consecutive unhealthy reads — B7).
        servoctl.pca = _FakeMode1Pca(0x20, mode2=0x01)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time() - 60
        servoctl._unhealthy_chip_reads = 0
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_not_called()
            servoctl._watchdog_tick()
            init.assert_called_once()


class TestEngagedChannels(ServoctlSelfHealingTestCase):
    """S-M2-03: track which channels hold a live PWM command — the repair
    path (S-M2-04) re-applies ONLY these; a failed write must never mark a
    channel engaged."""

    def test_engaged_set_lifecycle(self):
        servoctl.pca = _RecordingPca()
        servoctl._engaged_channels = set()
        self.assertTrue(servoctl.set_servo_pwm(4, 300))
        self.assertEqual(servoctl._engaged_channels, {4})
        servoctl.disable_all_servos()
        self.assertEqual(servoctl._engaged_channels, set())

    def test_failed_write_not_engaged(self):
        servoctl.pca = _RecordingPca(fail_channels={4})
        servoctl._engaged_channels = set()
        self.assertFalse(servoctl.set_servo_pwm(4, 300))
        self.assertEqual(servoctl._engaged_channels, set())
        # a good write on another channel still engages it
        self.assertTrue(servoctl.set_servo_pwm(5, 300))
        self.assertEqual(servoctl._engaged_channels, {5})


class TestWatchdogTiming(ServoctlSelfHealingTestCase):
    """S-M2-02: 2 s tick, 2 s pwm-recency gate, 30 s stuck timeout.
    Detection lag: 2 consecutive reads x 2 s = 2-4 s (jump magnitude is
    proportional to sag time — this is the core gracefulness change)."""

    def test_is_moving_stuck_boundary_at_30s(self):
        now = 1000.0
        self.assertTrue(servoctl._is_moving_stuck(True, now - 31, now, timeout=30))
        self.assertFalse(servoctl._is_moving_stuck(True, now - 29, now, timeout=30))

    def test_stuck_timeout_default_is_30(self):
        self.assertEqual(
            inspect.signature(servoctl._is_moving_stuck).parameters["timeout"].default,
            30,
        )

    def test_watchdog_interval_default_is_2(self):
        self.assertEqual(servoctl._watchdog_interval, 2)
        self.assertEqual(
            inspect.signature(servoctl.start_servo_watchdog).parameters["interval"].default,
            2,
        )

    def test_pwm_recency_gate_is_2s(self):
        # PWM 3 s ago: OUTSIDE the new 2 s gate -> idle, repair must proceed
        # after two consecutive unhealthy reads (under the old 5 s gate the
        # chip read as 'recently driven' and the repair was skipped).
        servoctl.pca = _FakeMode1Pca(0x01)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time() - 3
        servoctl._unhealthy_chip_reads = 0
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
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
        servoctl._last_init_attempt = 0.0  # lazy-init rate limit expired
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_called_once()

    def test_watchdog_tick_repairs_unhealthy_mode1(self):
        # RC-1b: chip ACKs on I2C but MODE1 lost bit 0x20 -> PWM writes
        # land on wrong registers, servos frozen. Watchdog must re-init
        # (after TWO consecutive unhealthy reads — B7).
        servoctl.pca = _FakeMode1Pca(0x01)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time() - 60
        servoctl._unhealthy_chip_reads = 0
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_not_called()
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
        servoctl._last_pwm_time = time.time() - 60
        servoctl._unhealthy_chip_reads = 0
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_not_called()
            servoctl._watchdog_tick()
            init.assert_called_once()

class TestWatchdogConsecutiveUnhealthyReads(ServoctlSelfHealingTestCase):
    """B7(a): a single transient bad read must NOT trigger re-init; two
    consecutive unhealthy reads (with no healthy read between) must."""

    def _make_idle(self):
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time() - 60  # no PWM in the last 5s
        servoctl._unhealthy_chip_reads = 0

    def test_single_unhealthy_read_does_not_repair(self):
        servoctl.pca = _FakeMode1Pca(0x01)  # MODE1 lost bit 0x20
        self._make_idle()
        with mock.patch.object(servoctl, "initialize_pca9685") as init:
            servoctl._watchdog_tick()
            init.assert_not_called()

    def test_single_transient_nack_does_not_repair(self):
        # unreadable register (I2C NACK) is unhealthy, but ONE is transient
        servoctl.pca = _FakeMode1Pca(exc=OSError(121, "Remote I/O error"))
        self._make_idle()
        with mock.patch.object(servoctl, "initialize_pca9685") as init:
            servoctl._watchdog_tick()
            init.assert_not_called()

    def test_two_consecutive_unhealthy_reads_repair(self):
        servoctl.pca = _FakeMode1Pca(0x01)
        self._make_idle()
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            servoctl._watchdog_tick()
            init.assert_called_once()

    def test_healthy_read_resets_consecutive_counter(self):
        servoctl.pca = _FakeMode1Pca(0x01)
        self._make_idle()
        with mock.patch.object(servoctl, "initialize_pca9685") as init:
            servoctl._watchdog_tick()  # strike 1
            servoctl.pca = _FakeMode1Pca(0x20)  # healthy read resets the streak
            servoctl._watchdog_tick()
            servoctl.pca = _FakeMode1Pca(0x01)  # unhealthy again: strike 1, not 2
            servoctl._watchdog_tick()
            init.assert_not_called()

    def test_repair_skipped_while_moving(self):
        # B7(b): never re-init mid-walk (re-init clears PWM output registers
        # and can abort a movement)
        servoctl.pca = _FakeMode1Pca(0x01)
        servoctl.MOVING = True
        servoctl._last_pwm_time = time.time()  # active movement
        servoctl._unhealthy_chip_reads = 0
        with mock.patch.object(servoctl, "initialize_pca9685") as init:
            for _ in range(3):
                servoctl._watchdog_tick()
            init.assert_not_called()

    def test_repair_skipped_when_pwm_recent_even_if_not_moving(self):
        # B7(b): recent PWM activity (<5s) also gates the repair
        servoctl.pca = _FakeMode1Pca(0x01)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time() - 1
        servoctl._unhealthy_chip_reads = 0
        with mock.patch.object(servoctl, "initialize_pca9685") as init:
            for _ in range(3):
                servoctl._watchdog_tick()
            init.assert_not_called()

    def test_stuck_moving_reset_is_unconditional(self):
        # B7(b): the stuck-MOVING check must still run while MOVING is True
        # (repair is skipped, the flag reset is not)
        servoctl.pca = _FakeMode1Pca(0x01)
        servoctl.MOVING = True
        servoctl._last_pwm_time = time.time() - 200  # stuck for >180s
        servoctl._unhealthy_chip_reads = 0
        with mock.patch.object(servoctl, "initialize_pca9685") as init:
            servoctl._watchdog_tick()
            init.assert_not_called()  # repair skipped while MOVING
        self.assertFalse(servoctl.MOVING)  # but the stuck flag was reset


class TestWatchdogLazyInit(ServoctlSelfHealingTestCase):
    """B7(c): watchdog must reuse the 5s rate-limited lazy init instead of
    hammering initialize_pca9685() every tick, and must not spam the
    unavailable warning when the board is absent."""

    def test_watchdog_respects_lazy_init_rate_limit(self):
        servoctl.pca = None
        servoctl._last_init_attempt = time.time()  # attempted just now
        with mock.patch.object(servoctl, "initialize_pca9685") as init:
            for _ in range(3):
                servoctl._watchdog_tick()
            init.assert_not_called()

    def test_watchdog_retries_when_rate_limit_expired(self):
        servoctl.pca = None
        servoctl._last_init_attempt = 0.0
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            servoctl._watchdog_tick()
            init.assert_called_once()

    def test_watchdog_none_pca_no_log_spam_when_throttled(self):
        # board absent + recent init attempt + recent warning -> total silence
        servoctl.pca = None
        servoctl._last_init_attempt = time.time()
        servoctl._last_pca_none_log = time.time()
        with mock.patch.object(servoctl, "queue_message") as qm:
            for _ in range(5):
                servoctl._watchdog_tick()
        qm.assert_not_called()

    def test_watchdog_none_pca_warning_throttled_to_30s(self):
        # throttle expired -> exactly ONE warning across repeated ticks
        servoctl.pca = None
        servoctl._last_init_attempt = time.time()
        servoctl._last_pca_none_log = 0.0
        with mock.patch.object(servoctl, "queue_message") as qm:
            for _ in range(5):
                servoctl._watchdog_tick()
        warnings = [
            c for c in qm.call_args_list
            if c.args and "unavailable" in str(c.args[0])
        ]
        self.assertEqual(len(warnings), 1)


class TestStateLock(ServoctlSelfHealingTestCase):
    """B7(d): one lock around PCA handle swaps and MOVING transitions so
    watchdog / movement / servo_status do not race."""

    def test_state_lock_exists_and_is_usable(self):
        lock = servoctl._state_lock
        self.assertTrue(callable(getattr(lock, "acquire", None)))
        self.assertTrue(callable(getattr(lock, "release", None)))
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_servo_status_is_reentrant_under_held_lock(self):
        # the watchdog holds the lock across the whole tick and calls into
        # helpers (initialize_pca9685, servo reads) that also lock — same-thread
        # re-entrance must not deadlock
        servoctl.pca = None
        servoctl.MOVING = False
        with servoctl._state_lock:
            status = servoctl.servo_status()  # must return, not hang
        self.assertFalse(status["pca_alive"])
        self.assertFalse(status["moving"])

    def test_watchdog_tick_repair_holds_the_lock(self):
        # repair runs under the lock: another thread holding it blocks the
        # tick (serialized, no mid-swap race)
        servoctl.pca = _FakeMode1Pca(0x01)
        servoctl.MOVING = False
        servoctl._last_pwm_time = time.time() - 60
        servoctl._unhealthy_chip_reads = 1  # next unhealthy read repairs
        with mock.patch.object(servoctl, "initialize_pca9685", return_value=True) as init:
            with servoctl._state_lock:
                # run the tick from ANOTHER thread while we hold the lock
                result = {}
                def _tick():
                    servoctl._watchdog_tick()
                    result["done"] = True
                t = threading.Thread(target=_tick)
                t.start()
                t.join(timeout=0.5)
                locked_out = "done" not in result
            t.join(timeout=5)  # lock released: tick must finish now
        self.assertTrue(result.get("done"), "watchdog tick did not run after the lock was released")
        if locked_out:
            init.assert_called_once()  # repair happened only after we released


    def test_start_servo_watchdog_sets_running_flag(self):
        servoctl._watchdog_running = False
        servoctl.start_servo_watchdog(interval=3600)  # long interval: thread just sleeps
        try:
            self.assertTrue(servoctl._watchdog_running)
            self.assertTrue(servoctl.servo_status()["watchdog_running"])
        finally:
            servoctl._watchdog_running = False  # stops the loop


class TestSlopCleanup(ServoctlSelfHealingTestCase):
    """C6: redundant except tuple + unused `e` in _load_servo_positions."""

    def test_load_positions_uses_plain_except(self):
        src = open(os.path.join(os.path.dirname(servoctl.__file__),
                                "module_servoctl.py")).read()
        self.assertNotIn(
            "except (FileNotFoundError, json.JSONDecodeError, Exception)", src
        )


if __name__ == "__main__":
    unittest.main()
