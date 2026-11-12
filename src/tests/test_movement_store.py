"""Movement store tests: golden defaults export, validation, CRUD, playback.

S-M1-02 pins the structural invariants of src/movements_defaults.json
(the tracked, hand-edited source of truth for the shipped movements)
before anything consumes it.  S-M1-03/04/05 cover MovementStore load/validation, CRUD and playback.
"""
import json
import os
import shutil
import sys
import tempfile
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

from modules.module_movement_registry import MOVEMENTS  # noqa: E402

_DEFAULTS_PATH = os.path.join(_SRC, "movements_defaults.json")

_STEP_KEYS = (
    "left_height", "right_height", "left_leg", "right_leg",
    "left_main", "left_forearm", "left_hand",
    "right_main", "right_forearm", "right_hand",
    "speed", "arm_speed", "hold_time", "movement",
)
_ARM_KEYS = ("left_main", "left_forearm", "left_hand",
             "right_main", "right_forearm", "right_hand")


def _valid_entry():
    """A minimal valid movement entry (builder step schema)."""
    return {
        "name": "Test Move", "type": "legs_only",
        "description": "a movement used by the store tests",
        "steps": [{
            "left_height": 50, "right_height": 50,
            "left_leg": 50, "right_leg": 50,
            "left_main": None, "left_forearm": None, "left_hand": None,
            "right_main": None, "right_forearm": None, "right_hand": None,
            "speed": 0.9, "hold_time": 0.0, "movement": None,
        }],
        "steps_arms": None,
    }


def _load_defaults():
    if not os.path.exists(_DEFAULTS_PATH):
        raise AssertionError(
            "src/movements_defaults.json is missing — it is a tracked, "
            "hand-editable file and the source of truth for the shipped "
            "movements (a fresh clone always has it)"
        )
    with open(_DEFAULTS_PATH, "r") as fh:
        return json.load(fh)


class TestDefaultsGolden(unittest.TestCase):
    """Structural invariants of the exported defaults file (S-M1-02)."""

    @classmethod
    def setUpClass(cls):
        cls.data = _load_defaults()

    def test_all_33_ids_present(self):
        self.assertEqual(len(MOVEMENTS), 33)
        self.assertEqual(set(self.data["movements"]), set(MOVEMENTS),
                         "exporter dropped or invented movement ids")

    def test_types_match_registry(self):
        for mid, entry in self.data["movements"].items():
            self.assertEqual(entry["type"], MOVEMENTS[mid]["type"],
                             f"type mismatch for '{mid}'")

    def test_step_schema_fields(self):
        for mid, entry in self.data["movements"].items():
            for branch_name in ("steps", "steps_arms"):
                branch = entry.get(branch_name)
                if branch is None:
                    continue
                self.assertIsInstance(branch, list,
                                      f"'{mid}'.{branch_name} must be a list")
                for idx, step in enumerate(branch):
                    self.assertEqual(
                        sorted(step.keys()), sorted(_STEP_KEYS),
                        f"'{mid}'.{branch_name}[{idx}] has wrong step keys")

    def test_hold_semantics_exported(self):
        arms = self.data["movements"]["happy_dance"]["steps_arms"]
        self.assertIsNotNone(
            arms, "happy_dance must carry an arms branch (it is has_arms)")
        holds = [step[k] for step in arms for k in _ARM_KEYS]
        self.assertIn(-1, holds, "HOLD (-1) arm values were not exported")

    def test_swap_branches_distinct(self):
        movements = self.data["movements"]
        self.assertNotEqual(movements["turn_right"]["steps"],
                            movements["turn_left"]["steps"],
                            "turn_right/turn_left must not collapse to one "
                            "shared step list")

    def test_consecutive_sleeps_accumulate(self):
        # happy_dance has back-to-back time.sleep(0.1) + time.sleep(0.3)
        # (module_movements.py:1060-1061) — the exporter must accumulate
        # them into hold_time 0.4, not overwrite with the last one
        holds = [s["hold_time"]
                 for s in self.data["movements"]["happy_dance"]["steps"]]
        self.assertIn(0.4, holds,
                      "consecutive time.sleep calls must accumulate")

    def test_merged_step_preserves_arm_speed(self):
        # audit A6: when move_arm(..., s_arm) merges into an open
        # move_legs(..., s_leg) step, the arm's OWN speed must survive —
        # happy_dance's legs(80,80,70,70)@0.9 + arm@0.7 step
        steps = self.data["movements"]["happy_dance"]["steps"]
        merged = [s for s in steps
                  if s["right_height"] == 80 and s["right_leg"] == 70
                  and s["right_main"] == 85]
        self.assertEqual(len(merged), 1,
                         "expected exactly one such merged step")
        self.assertEqual(merged[0]["speed"], 0.9)      # the legs' speed
        self.assertEqual(merged[0]["arm_speed"], 0.7)   # the arm's own

    def test_arms_branches_captured(self):
        entry = self.data["movements"]["step_forward"]
        self.assertIsNotNone(
            entry["steps_arms"],
            "step_forward branches on ARMS_PRESENT — the arms branch must "
            "be exported")


class _StoreTestCase(unittest.TestCase):
    """A MovementStore over a tmp store path, defaults from the repo."""

    def setUp(self):
        super().setUp()
        from modules.module_movementstore import MovementStore
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_path = os.path.join(self.tmp, "movements.json")
        self.store = MovementStore(
            store_path=self.store_path,
            defaults_path=_DEFAULTS_PATH)

    def _write_store(self, movements):
        with open(self.store_path, "w") as fh:
            json.dump({"version": 1, "movements": movements}, fh)

    def _defaults_movements(self):
        with open(_DEFAULTS_PATH, "r") as fh:
            return json.load(fh)["movements"]


class TestCrud(_StoreTestCase):
    """MovementStore CRUD + persistence (S-M1-04)."""

    def test_get_all_returns_copy(self):
        self.store.load()
        snapshot = self.store.get_all()
        self.assertEqual(set(snapshot), set(MOVEMENTS))
        snapshot["bow"]["name"] = "MUTATED"
        self.assertNotEqual(self.store.get("bow")["name"], "MUTATED")

    def test_add_rejects_duplicate_id(self):
        self.store.load()
        with self.assertRaises(ValueError):
            self.store.add("bow", _valid_entry())

    def test_add_rejects_invalid_entry(self):
        self.store.load()
        bad = _valid_entry()
        bad["type"] = "flurb"
        with self.assertRaises(ValueError):
            self.store.add("my_move", bad)
        self.assertIsNone(self.store.get("my_move"))

    def test_update_rejects_unknown_id(self):
        self.store.load()
        with self.assertRaises(ValueError):
            self.store.update("never_added", _valid_entry())

    def test_delete_removes_entry(self):
        self.store.load()
        self.store.delete("bow")
        self.assertIsNone(self.store.get("bow"))
        with self.assertRaises(ValueError):
            self.store.delete("bow")

    def test_save_roundtrips_through_a_new_store(self):
        self.store.load()
        self.store.add("my_move", _valid_entry())
        self.store.save()
        from modules.module_movementstore import MovementStore
        reopened = MovementStore(store_path=self.store_path,
                                 defaults_path=_DEFAULTS_PATH)
        reopened.load()
        self.assertIsNotNone(reopened.get("my_move"))
        self.assertEqual(set(reopened.get_all()),
                         set(MOVEMENTS) | {"my_move"})

    def test_export_import_roundtrip(self):
        self.store.load()
        self.store.add("my_move", _valid_entry())
        export_path = os.path.join(self.tmp, "export.json")
        self.store.export_to(export_path)
        # a fresh store over the same import file reproduces the pool
        from modules.module_movementstore import MovementStore
        other = MovementStore(store_path=os.path.join(self.tmp, "other.json"),
                               defaults_path=_DEFAULTS_PATH)
        other.load()
        errors = other.import_from(export_path)
        self.assertEqual(errors, [])
        self.assertEqual(other.get_all(), self.store.get_all())


class TestPartialStepNormalisation(_StoreTestCase):
    """The UI sends partial steps; the store must complete and persist them.

    Live bug: POST /movements with the movements panel's payload returned
    400 "steps[0] has missing keys [...]" because validation demanded all 13
    keys while playback already tolerated partial steps.
    """

    def _ui_payload(self):
        # exactly what movements.html createMovement sends
        return {
            "name": "ui_move", "type": "legs_only",
            "description": "created from the UI",
            "steps": [{
                "left_height": 50, "right_height": 50,
                "left_leg": 50, "right_leg": 50,
                "speed": 0.9, "hold_time": 0.0,
            }],
            "steps_arms": None,
        }

    def test_partial_step_is_accepted_and_completed(self):
        self.store.load()
        self.store.add("ui_move", self._ui_payload())
        stored = self.store.get("ui_move")
        step = stored["steps"][0]
        self.assertEqual(step["left_main"], None)
        self.assertEqual(step["right_hand"], None)
        self.assertEqual(step["movement"], None)
        self.assertEqual(step["speed"], 0.9)
        self.assertEqual(step["hold_time"], 0.0)
        # every schema key present after normalisation
        self.assertEqual(set(step), set(type(self.store)._STEP_DEFAULTS))

    def test_partial_step_survives_a_reload(self):
        self.store.load()
        self.store.add("ui_move", self._ui_payload())
        self.store.load()
        step = self.store.get("ui_move")["steps"][0]
        self.assertEqual(set(step), set(type(self.store)._STEP_DEFAULTS))

    def test_wrong_typed_value_still_rejected(self):
        self.store.load()
        bad = self._ui_payload()
        bad["steps"][0]["left_height"] = "high"
        with self.assertRaises(ValueError):
            self.store.add("ui_bad", bad)

    def test_unknown_step_key_still_rejected(self):
        self.store.load()
        bad = self._ui_payload()
        bad["steps"][0]["sneaky"] = 1
        with self.assertRaises(ValueError):
            self.store.add("ui_bad", bad)

    def test_full_schema_payload_still_works(self):
        self.store.load()
        self.store.add("ui_full", _valid_entry())
        self.assertIsNotNone(self.store.get("ui_full"))


class TestHoldTimeClamp(_StoreTestCase):
    """S-M2-07: movements.json is user-editable at runtime, so a 60 s
    hold must not trip the 30 s stuck-MOVING watchdog — hold_time > 10.0
    is clamped to 10.0 with a queue_message warning (30 s stuck timeout >
    10 s max hold + margin; legit moves write PWM every <= 0.032 s)."""

    def _add_with_hold(self, hold):
        entry = _valid_entry()
        entry["steps"][0]["hold_time"] = hold
        self.store.load()
        with mock.patch("modules.module_movementstore.queue_message") as qm:
            self.store.add("hold_move", entry)
        return qm

    def test_hold_time_60_clamped_to_10(self):
        qm = self._add_with_hold(60)
        step = self.store.get("hold_move")["steps"][0]
        self.assertEqual(step["hold_time"], 10.0,
                         "hold_time 60 must load clamped to 10.0, not raise")
        self.assertTrue(
            any("clamped" in str(call) for call in qm.call_args_list),
            "clamping must emit a queue_message warning")
        # the clamp persists through a reload (movements.json on disk)
        self.store.load()
        self.assertEqual(
            self.store.get("hold_move")["steps"][0]["hold_time"], 10.0,
            "the clamped value must persist to movements.json")

    def test_hold_time_5_unchanged(self):
        qm = self._add_with_hold(5)
        step = self.store.get("hold_move")["steps"][0]
        self.assertEqual(step["hold_time"], 5)
        self.assertFalse(
            any("clamp" in str(call) for call in qm.call_args_list),
            "a legal hold_time must not warn")

    def test_hold_time_negative_still_raises(self):
        entry = _valid_entry()
        entry["steps"][0]["hold_time"] = -1
        self.store.load()
        with self.assertRaises(ValueError):
            self.store.add("hold_bad", entry)


class TestHoldTimeClampPersistsOnLoadImport(_StoreTestCase):
    """Audit F1: the clamp inside _validate_step mutates the step dict
    it is handed — on load()/import_from() (which never ran
    _normalize_steps) it landed on the throwaway copy built by
    _step_defaults, so a hand-edited 60 s hold survived into playback
    and would trip the 30 s stuck-MOVING watchdog mid-hold."""

    def _partial_hold_entry(self):
        # exactly the hand-edit from the audit: a partial step whose
        # only outlier is hold_time 60
        return {
            "name": "Hold Move", "type": "legs_only",
            "description": "a hand-edited entry with a long hold",
            "steps": [{"left_height": 50, "hold_time": 60}],
            "steps_arms": None,
        }

    def test_load_and_import_clamp_partial_step_hold_time(self):
        # load(): movements.json is user-editable at runtime
        self._write_store({"hold_move": self._partial_hold_entry()})
        with mock.patch("modules.module_movementstore.queue_message") as qm:
            self.store.load()
        step = self.store.get("hold_move")["steps"][0]
        self.assertEqual(step["hold_time"], 10.0,
                         "load() must persist the clamp, not discard it")
        # the partial step is completed with the defaults (same as add())
        self.assertEqual(sorted(step), sorted(_STEP_KEYS))
        self.assertTrue(
            any("clamped" in str(call) for call in qm.call_args_list),
            "clamping on load must emit the queue_message warning")

        # import_from(): the same hand-edit via an import file
        import_path = os.path.join(self.tmp, "import.json")
        with open(import_path, "w") as fh:
            json.dump({"version": 1,
                       "movements": {"imported_hold":
                                      self._partial_hold_entry()}}, fh)
        self.assertEqual(self.store.import_from(import_path), [])
        step = self.store.get("imported_hold")["steps"][0]
        self.assertEqual(step["hold_time"], 10.0,
                         "import_from() must persist the clamp too")


class TestResetSemantics(_StoreTestCase):
    """D5 reset semantics: delete + re-insert from defaults (S-M1-04)."""

    def _setup_edited_default_plus_user_entry(self):
        self.store.load()
        default_bow = json.loads(json.dumps(self.store.get("bow")))
        edited = json.loads(json.dumps(default_bow))
        edited["name"] = "Edited Bow"
        edited["description"] = "user edit"
        self.store.update("bow", edited)
        self.store.add("my_move", _valid_entry())
        return default_bow

    def test_reset_restores_edited_default_keeps_user_entry(self):
        default_bow = self._setup_edited_default_plus_user_entry()
        self.store.reset_from_defaults()
        self.assertEqual(self.store.get("bow"), default_bow)
        self.assertIsNotNone(self.store.get("my_move"))

    def test_prune_deletes_orphans(self):
        self._setup_edited_default_plus_user_entry()
        self.store.reset_from_defaults(prune=True)
        self.assertIsNone(self.store.get("my_move"))
        self.assertIn("bow", self.store.get_all())

    def test_import_rejects_invalid_wholesale(self):
        self.store.load()
        before = self.store.get_all()
        good = _valid_entry()
        bad = _valid_entry()
        bad["steps"][0]["speed"] = 5  # out of range
        import_path = os.path.join(self.tmp, "import.json")
        with open(import_path, "w") as fh:
            json.dump({"version": 1,
                       "movements": {"ok_move": good, "bad_move": bad}}, fh)
        errors = self.store.import_from(import_path)
        self.assertEqual(len(errors), 1)
        self.assertIn("bad_move", errors[0])
        # all-or-nothing: nothing was imported, store unchanged
        self.assertIsNone(self.store.get("ok_move"))
        self.assertEqual(self.store.get_all(), before)


def _step(legs=(50, 50, 50, 50), arms=None, speed=0.9, hold=0.0,
          movement=None):
    """A builder-schema step for playback tests."""
    lh, rh, ll, rl = legs
    lm, lf, lhand, rm, rf, rhand = arms if arms else (None,) * 6
    return {
        "left_height": lh, "right_height": rh,
        "left_leg": ll, "right_leg": rl,
        "left_main": lm, "left_forearm": lf, "left_hand": lhand,
        "right_main": rm, "right_forearm": rf, "right_hand": rhand,
        "speed": speed, "hold_time": hold, "movement": movement,
    }


class _PlaybackTestCase(_StoreTestCase):
    """Fake servoctl module (recording calls) + mocked time.sleep."""

    def setUp(self):
        super().setUp()
        fake = types.ModuleType("modules.module_servoctl")
        fake.HOLD = -1
        fake.MOVING = False
        fake.ARMS_PRESENT = False
        fake.legs_calls = []
        fake.arm_calls = []
        fake.disable_calls = []
        fake.notify_start_calls = []
        fake.notify_end_calls = []

        def acquire_moving():
            if fake.MOVING:
                return False
            fake.MOVING = True
            return True

        def set_moving(value):
            fake.MOVING = value

        def move_legs(lh, rh, ll, rl, speed=1.0):
            fake.legs_calls.append((lh, rh, ll, rl, speed))

        def move_arm(lm, lf, lhand, rm, rf, rhand, speed=1.0):
            fake.arm_calls.append((lm, lf, lhand, rm, rf, rhand, speed))

        def disable_all_servos():
            fake.disable_calls.append(True)

        def _notify_movement_start():
            fake.notify_start_calls.append(True)

        def _notify_movement_end():
            fake.notify_end_calls.append(True)

        fake.acquire_moving = acquire_moving
        fake.set_moving = set_moving
        fake.move_legs = move_legs
        fake.move_arm = move_arm
        fake.disable_all_servos = disable_all_servos
        fake._notify_movement_start = _notify_movement_start
        fake._notify_movement_end = _notify_movement_end
        self.servo = fake

        import modules
        self._saved_servo_mod = sys.modules.get("modules.module_servoctl")
        self._saved_servo_attr = getattr(modules, "module_servoctl", None)
        sys.modules["modules.module_servoctl"] = fake
        modules.module_servoctl = fake
        self.addCleanup(self._restore_fake_servoctl)

        time_patcher = mock.patch("modules.module_movementstore.time")
        self.fake_time = time_patcher.start()
        self.addCleanup(time_patcher.stop)

        self.store.load()

    def _restore_fake_servoctl(self):
        import modules
        if self._saved_servo_mod is None:
            sys.modules.pop("modules.module_servoctl", None)
        else:
            sys.modules["modules.module_servoctl"] = self._saved_servo_mod
        if self._saved_servo_attr is None:
            try:
                delattr(modules, "module_servoctl")
            except AttributeError:
                pass
        else:
            modules.module_servoctl = self._saved_servo_attr

    def _expected_legs(self, steps):
        return [(s["left_height"], s["right_height"],
                 s["left_leg"], s["right_leg"], s["speed"])
                for s in steps]


class TestPlayback(_PlaybackTestCase):
    """play_steps + execute semantics (S-M1-05)."""

    def test_play_steps_calls_legs_and_arms(self):
        self.servo.ARMS_PRESENT = True
        ok = self.store.play_steps([
            _step(legs=(10, 20, 30, 40), arms=(1, 2, 3, 4, 5, 6))])
        self.assertTrue(ok)
        self.assertEqual(self.servo.legs_calls, [(10, 20, 30, 40, 0.9)])
        self.assertEqual(self.servo.arm_calls, [(1, 2, 3, 4, 5, 6, 0.9)])
        self.assertEqual(self.servo.disable_calls, [True])
        self.assertFalse(self.servo.MOVING)
        self.assertEqual(self.servo.notify_start_calls, [True])
        self.assertEqual(self.servo.notify_end_calls, [True])

    def test_null_legs_skip_move_legs(self):
        self.servo.ARMS_PRESENT = True
        ok = self.store.play_steps([
            _step(legs=(None, None, None, None),
                  arms=(1, 2, 3, None, None, None))])
        self.assertTrue(ok)
        self.assertEqual(self.servo.legs_calls, [])
        self.assertEqual(len(self.servo.arm_calls), 1)

    def test_hold_minus_one_passed_through(self):
        self.servo.ARMS_PRESENT = True
        self.store.play_steps([
            _step(arms=(None, -1, None, None, None, None))])
        self.assertEqual(len(self.servo.arm_calls), 1)
        self.assertEqual(self.servo.arm_calls[0][1], -1)

    def test_refused_while_moving(self):
        self.servo.MOVING = True
        ok = self.store.play_steps([_step()])
        self.assertFalse(ok)
        self.assertEqual(self.servo.legs_calls, [])
        self.assertEqual(self.servo.arm_calls, [])
        self.assertEqual(self.servo.disable_calls, [])

    def test_execute_picks_arms_branch(self):
        self.servo.ARMS_PRESENT = True
        entry = _valid_entry()
        entry["steps"] = [_step(legs=(11, 12, 13, 14))]
        entry["steps_arms"] = [_step(legs=(21, 22, 23, 24))]
        self.store.add("probe", entry)
        self.assertTrue(self.store.execute("probe"))
        self.assertEqual(self.servo.legs_calls, [(21, 22, 23, 24, 0.9)])

    def test_execute_arm_gated(self):
        self.servo.ARMS_PRESENT = False
        entry = _valid_entry()
        entry["type"] = "has_arms"
        entry["steps"] = [_step(legs=(30, 31, 32, 33))]
        self.store.add("probe_arms", entry)
        with mock.patch("modules.module_movementstore.queue_message") as qm:
            ok = self.store.execute("probe_arms")
        self.assertTrue(ok)
        # audit B5: the leg portion still plays on legs-only builds (the
        # deleted functions ran their move_legs sequence; only move_arm
        # was ineffective) — arms never fire
        self.assertEqual(self.servo.legs_calls,
                         [(30, 31, 32, 33, 0.9)])
        self.assertEqual(self.servo.arm_calls, [])
        qm.assert_called_once_with(
            "MOVEMENTS: 'probe_arms' - arm servos not installed; "
            "playing the leg portion only")

    def test_swap_redirects_turn(self):
        import modules.module_movements as movements_mod
        self.servo.ARMS_PRESENT = False
        expected = self._expected_legs(
            self._defaults_movements()["turn_left"]["steps"])
        with mock.patch.object(movements_mod,
                                "get_swap_turn_directions",
                                return_value=True):
            ok = self.store.execute("turn_right")
        self.assertTrue(ok)
        self.assertEqual(self.servo.legs_calls, expected)

    def test_compose_movement_ref(self):
        self.servo.ARMS_PRESENT = False
        bow = self._defaults_movements()["bow"]["steps"]
        ok = self.store.play_steps([_step(movement="bow")])
        self.assertTrue(ok)
        self.assertEqual(self.servo.legs_calls, self._expected_legs(bow))
        self.assertEqual(self.servo.disable_calls, [True])
        self.assertFalse(self.servo.MOVING)
        self.fake_time.sleep.assert_any_call(3.0)  # bow holds 3s mid-sequence

    def test_arm_speed_used_for_move_arm(self):
        # audit A6: move_arm gets the step's arm_speed when present…
        self.servo.ARMS_PRESENT = True
        step = _step(legs=(10, 20, 30, 40), arms=(1, 2, 3, 4, 5, 6))
        step["speed"] = 0.9
        step["arm_speed"] = 0.7
        self.store.play_steps([step])
        self.assertEqual(self.servo.arm_calls[0][6], 0.7)
        self.assertEqual(self.servo.legs_calls[0][4], 0.9)  # legs keep 0.9
        # …and falls back to speed when absent (backward compatibility
        # with movements.json files written before arm_speed existed)
        plain = _step(legs=(10, 20, 30, 40), arms=(1, 2, 3, 4, 5, 6))
        self.store.play_steps([plain])
        self.assertEqual(self.servo.arm_calls[-1][6], 0.9)

    def test_tail_neutral_appends_neutral_step(self):
        self.servo.ARMS_PRESENT = False
        self.store.play_steps([_step()], tail_neutral=True)
        self.assertEqual(self.servo.legs_calls[-1], (50, 50, 50, 50, 0.8))

    def test_play_steps_clamps_raw_hold_time(self):
        # chatui's _execute_steps passes raw client steps from an HTTP
        # payload straight into play_steps — no validation.  A POST with
        # hold_time 60 must never reach time.sleep(60): it would sit with
        # no PWM write and trip the 30 s stuck-MOVING watchdog mid-hold.
        from modules import module_movementstore
        raw = _step(legs=(10, 20, 30, 40), hold=60)
        with mock.patch("modules.module_movementstore.queue_message") as qm:
            ok = self.store.play_steps([raw])
        self.assertTrue(ok)
        self.fake_time.sleep.assert_called_once_with(
            module_movementstore.MAX_HOLD_TIME)
        self.assertTrue(
            any("clamped" in str(call) for call in qm.call_args_list),
            "clamping at playback must emit a queue_message warning")


class TestSeeding(_StoreTestCase):
    """First start seeds movements.json from the tracked defaults (D5)."""

    def test_seeds_store_from_defaults_when_absent(self):
        from modules import module_movementstore
        tmp_store = os.path.join(self.tmp, "movements.json")
        self.assertFalse(os.path.exists(tmp_store))
        with mock.patch.object(module_movementstore, "_STORE_PATH",
                               tmp_store), \
             mock.patch.object(module_movementstore, "_DEFAULTS_PATH",
                               _DEFAULTS_PATH), \
             mock.patch.object(module_movementstore, "_store_instance",
                               None), \
             mock.patch("modules.module_movementstore.queue_message") as qm:
            store = module_movementstore.get_movement_store()
        self.assertTrue(os.path.exists(tmp_store),
                         "get_movement_store() must seed movements.json")
        self.assertEqual(set(store.movements), set(MOVEMENTS))
        seeded = [call.args[0] for call in qm.call_args_list
                  if "seeded" in call.args[0]]
        self.assertEqual(len(seeded), 1)



class TestRegistryShim(_StoreTestCase):
    """module_movement_registry becomes a store-backed shim (S-M2-01)."""

    def setUp(self):
        super().setUp()
        from modules import module_movement_registry as registry
        from modules import module_movementstore
        self.registry = registry
        self.store_patcher = mock.patch.object(
            module_movementstore, "get_movement_store",
            return_value=self.store)
        self.store_patcher.start()
        self.addCleanup(self.store_patcher.stop)
        # restore the real registry content for subsequent tests
        self.addCleanup(registry.reload_from_store)

    def test_registry_reflects_store_add(self):
        self.store.load()
        self.store.add("my_move", _valid_entry())
        self.registry.reload_from_store()
        self.assertIn("my_move", self.registry.MOVEMENTS)
        self.assertEqual(self.registry.MOVEMENTS["my_move"]["type"],
                         "legs_only")
        self.assertEqual(self.registry.MOVEMENTS["my_move"]["name"],
                         "Test Move")

    def test_registry_entries_carry_only_metadata(self):
        self.store.load()
        self.registry.reload_from_store()
        for mid, meta in self.registry.MOVEMENTS.items():
            self.assertEqual(sorted(meta.keys()),
                             ["description", "name", "type"])

    def test_registry_import_survives_store_failure(self):
        import importlib
        self.store_patcher.stop()  # drop the returning-store patch
        from modules import module_movementstore
        with mock.patch.object(module_movementstore,
                               "get_movement_store",
                               side_effect=RuntimeError("boom")):
            reloaded = importlib.reload(self.registry)
        self.assertEqual(reloaded.MOVEMENTS, {})  # empty, never a crash

    def test_import_refreshes_registry(self):
        # review F3: import_from must refresh the registry so imported ids
        # are immediately dispatchable (no later mutation, no restart)
        self.store.load()
        import_path = os.path.join(self.tmp, "import.json")
        with open(import_path, "w") as fh:
            json.dump({"version": 1,
                       "movements": {"imported_move": _valid_entry()}}, fh)
        self.assertEqual(self.store.import_from(import_path), [])
        self.assertIn("imported_move", self.registry.MOVEMENTS)

    def test_registry_reload_keeps_existing_bindings_live(self):
        # btcontroller/chatui/servoctl hold `from ... import MOVEMENTS`
        # references — reload must mutate the dict in place, not rebind
        bound = self.registry.MOVEMENTS
        self.store.load()
        self.store.add("binding_probe", _valid_entry())
        self.registry.reload_from_store()
        self.assertIs(self.registry.MOVEMENTS, bound)
        self.assertIn("binding_probe", bound)


def _chatui_source():
    with open(os.path.join(_SRC, "modules", "module_chatui.py"),
              encoding="utf-8") as fh:
        return fh.read()


def _route_source(func_name, src):
    start = src.index(f"def {func_name}")
    next_route = src.find("@flask_app.route", start)
    return src[start:next_route if next_route != -1 else len(src)]


class TestRobotMoveDispatch(unittest.TestCase):
    """The motion D-pad's /robot_move must dispatch through the store
    (review F1: the bare movement calls raised NameError after the
    33-function deletion — the D-pad was completely broken)."""

    def test_robot_move_direction_map(self):
        from modules.module_movementstore import ROBOT_MOVE_IDS
        self.assertEqual(ROBOT_MOVE_IDS, {
            "forward": "step_forward",
            "backward": "step_backward",
            "left": "turn_left",
            "right": "turn_right",
            "forward_slow": "walk_forward",
            "backward_slow": "walk_backward",
            "left_slow": "turn_left_slow",
            "right_slow": "turn_right_slow",
        })

    def test_robot_move_route_uses_store_dispatch(self):
        route = _route_source("robot_move", _chatui_source())
        self.assertIn("ROBOT_MOVE_IDS", route)
        self.assertIn("get_movement_store().execute", route)
        self.assertIn("Robot is already moving", route)
        self.assertIn("400", route)  # unknown direction
        for bare in ("step_forward()", "step_backward()", "turn_left()",
                     "turn_right()", "walk_forward()", "walk_backward()",
                     "turn_left_slow()", "turn_right_slow()"):
            self.assertNotIn(bare, route,
                             f"bare movement call '{bare}' must be gone")


class TestThermalVentilateWiring(unittest.TestCase):
    """CPU ventilation wiring (review F2: module_main imported ventilate_on
    from servoctl, which no longer re-exports it — thermal monitoring was
    silently dead)."""

    def test_ventilate_on_lives_in_module_movements(self):
        from modules.module_movements import ventilate_on
        self.assertTrue(callable(ventilate_on))
        import modules.module_servoctl as servoctl
        self.assertFalse(hasattr(servoctl, "ventilate_on"),
                         "servoctl must not re-export ventilate_on")

    def test_module_main_imports_ventilate_on_from_module_movements(self):
        with open(os.path.join(_SRC, "modules", "module_main.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("from modules.module_movements import ventilate_on",
                      src)
        self.assertNotIn("from modules.module_servoctl import ventilate_on",
                          src)


class TestBuilderPreservation(unittest.TestCase):
    """Re-saving a has_arms movement from the builder must keep its
    steps_arms (review F4: the hardcoded None wiped arm choreography)."""

    def test_save_sequence_preserves_existing_steps_arms(self):
        route = _route_source("save_sequence", _chatui_source())
        self.assertIn('existing.get("steps_arms")', route)
        self.assertNotIn('"steps_arms": None', route)


class TestArmGatedLegs(_PlaybackTestCase):
    """Audit B5: on a legs-only build, selecting a has_arms movement
    must still play its LEG choreography — the deleted functions ran
    their move_legs sequence (only move_arm was ineffective); the store
    must not report success for a no-op."""

    def test_has_arms_movement_plays_legs_when_arms_absent(self):
        self.servo.ARMS_PRESENT = False
        # right_hi from the defaults: right-leg raise + wave choreography
        entry = self._defaults_movements()["right_hi"]
        legs_only = [s for s in entry["steps"]
                    if any(s[k] is not None for k in
                           ("left_height", "right_height",
                            "left_leg", "right_leg"))]
        self.assertTrue(legs_only, "right_hi has leg choreography")
        ok = self.store.execute("right_hi")
        self.assertTrue(ok)
        expected = [(s["left_height"], s["right_height"],
                     s["left_leg"], s["right_leg"], s["speed"])
                    for s in legs_only]
        self.assertEqual(self.servo.legs_calls, expected,
                         "the leg portion must play when arms are absent")

    def test_has_arms_movement_plays_arms_branch_when_present(self):
        self.servo.ARMS_PRESENT = True
        ok = self.store.execute("right_hi")
        self.assertTrue(ok)
        self.assertTrue(self.servo.arm_calls,
                        "the arms branch must play when arms are present")


class TestCycleSafetyAtLoadAndImport(_StoreTestCase):
    """Audit A2-remainder: cyclic movement refs must be rejected at EVERY
    entry point — import_from and load, not just add/update."""

    def _cycle_file(self, movements):
        path = os.path.join(self.tmp, "cycle.json")
        with open(path, "w") as fh:
            json.dump({"version": 1, "movements": movements}, fh)
        return path

    def test_import_self_reference_rejected_wholesale(self):
        self.store.load()
        before = self.store.get_all()
        entry = _valid_entry()
        entry["steps"][0]["movement"] = "loop"
        errors = self.store.import_from(
            self._cycle_file({"loop": entry}))
        self.assertEqual(len(errors), 1)
        self.assertIn("loop", errors[0])
        # all-or-nothing: the store is unchanged
        self.assertEqual(self.store.get_all(), before)

    def test_import_two_node_cycle_rejected(self):
        self.store.load()
        before = self.store.get_all()
        a, b = _valid_entry(), _valid_entry()
        a["steps"][0]["movement"] = "cyc_b"
        b["steps"][0]["movement"] = "cyc_a"
        errors = self.store.import_from(
            self._cycle_file({"cyc_a": a, "cyc_b": b}))
        self.assertEqual(len(errors), 1)
        self.assertIn("cyc", errors[0])
        self.assertEqual(self.store.get_all(), before)

    def test_load_cycle_falls_back_to_defaults(self):
        entry = _valid_entry()
        entry["steps"][0]["movement"] = "loop"
        with open(self.store_path, "w") as fh:
            json.dump({"version": 1,
                       "movements": {"loop": entry}}, fh)
        with mock.patch("modules.module_movementstore.queue_message") as qm:
            loaded = self.store.load()
        self.assertEqual(loaded, self._defaults_movements())
        messages = [call.args[0] for call in qm.call_args_list]
        self.assertTrue(any("loop" in m and ("cycle" in m.lower()
                                              or "invalid" in m.lower())
                            for m in messages), messages)
        # and playback of every loaded entry is safe (no recursion)
        for mid in loaded:
            self.assertIn(mid, self.store.movements)


class TestMalformedStepTolerance(_PlaybackTestCase):
    """Audit B4: /play_sequence passes raw client steps into play_steps —
    a step missing keys must not raise KeyError mid-playback (the old
    _execute_steps used .get() defaults; a 500 would leave the servos at
    a partial pose with MOVING released but disable never reached)."""

    def test_partial_step_plays_with_defaults(self):
        # the old semantics: missing keys default (heights/legs 50,
        # speed 0.85, hold 0) instead of raising KeyError
        result = self.store.play_steps([{"hold_time": 1}])
        self.assertTrue(result)
        # the default pose was played
        self.assertEqual(self.servo.legs_calls[0], (50, 50, 50, 50, 0.85))
        self.assertEqual(self.servo.disable_calls, [True])

    def test_non_dict_step_rejected_cleanly(self):
        with self.assertRaises(ValueError):
            self.store.play_steps(["not a step"])

    def test_wrong_typed_value_rejected_and_servos_disabled(self):
        # audit B4-residual: a present-but-wrong-typed value must raise a
        # clean ValueError (not TypeError at move_legs), and the servos
        # must STILL be disabled on the error path — never left energised
        # at a partial pose
        with self.assertRaises(ValueError) as raised:
            self.store.play_steps([{"left_height": "high"}])
        self.assertIn("left_height", str(raised.exception))
        # the error path reached disable_all_servos
        self.assertEqual(self.servo.disable_calls, [True])
        self.assertFalse(self.servo.MOVING)


class TestMovementRefSafety(_StoreTestCase):
    """Audit A2: movement refs must resolve AND be acyclic at write time —
    a self-reference or cycle would recurse forever at playback."""

    def _entry_with_ref(self, ref):
        return {"name": "Ref Move", "type": "legs_only",
                "description": "ref move",
                "steps": [{"left_height": 50, "right_height": 50,
                           "left_leg": 50, "right_leg": 50,
                           "left_main": None, "left_forearm": None,
                           "left_hand": None, "right_main": None,
                           "right_forearm": None, "right_hand": None,
                           "speed": 0.9, "hold_time": 0.0,
                           "movement": ref}],
                "steps_arms": None}

    def test_self_reference_rejected(self):
        self.store.load()
        with self.assertRaises(ValueError):
            self.store.add("loop", self._entry_with_ref("loop"))

    def test_two_node_cycle_rejected(self):
        self.store.load()
        # loop -> bow is valid (bow exists, no cycle yet)
        self.store.add("loop", self._entry_with_ref("bow"))
        # bow -> loop closes the cycle bow -> loop -> bow — rejected
        with self.assertRaises(ValueError):
            self.store.update("bow", self._entry_with_ref("loop"))

    def test_dangling_ref_rejected_at_write_time(self):
        self.store.load()
        with self.assertRaises(ValueError):
            self.store.add("dangling", self._entry_with_ref("ghost_move"))

    def test_valid_composition_still_works(self):
        self.store.load()
        self.store.add("composed", self._entry_with_ref("bow"))
        self.assertIsNotNone(self.store.get("composed"))


class TestValidation(_StoreTestCase):
    """MovementStore entry validation (S-M1-03)."""

    def test_rejects_bad_id(self):
        with self.assertRaises(ValueError):
            self.store.validate_entry("Bad-Id", _valid_entry())

    def test_rejects_bad_type(self):
        entry = _valid_entry()
        entry["type"] = "flurb"
        with self.assertRaises(ValueError):
            self.store.validate_entry("my_move", entry)

    def test_rejects_out_of_range_speed(self):
        entry = _valid_entry()
        entry["steps"][0]["speed"] = 1.5
        with self.assertRaises(ValueError):
            self.store.validate_entry("my_move", entry)

    def test_rejects_unresolvable_movement_ref(self):
        entry = _valid_entry()
        entry["steps"][0]["movement"] = "ghost_move"
        # first pass (structural, no known_ids): no movement-ref check yet
        self.store.validate_entry("my_move", entry)
        # second pass (after all entries load): the ref must resolve
        with self.assertRaises(ValueError):
            self.store.validate_entry("my_move", entry, known_ids={"bow"})

    def test_falls_back_to_defaults_on_corrupt_file(self):
        with open(self.store_path, "w") as fh:
            fh.write("{ this is not json")
        with mock.patch("modules.module_movementstore.queue_message") as qm:
            loaded = self.store.load()
        defaults = self._defaults_movements()
        self.assertEqual(loaded, defaults)
        self.assertEqual(self.store.movements, defaults)
        self.assertEqual(qm.call_count, 1)
        message = qm.call_args[0][0]
        self.assertTrue(message.startswith("MOVEMENTS:"), message)
        self.assertIn("invalid", message)

    def test_falls_back_to_defaults_on_invalid_entry(self):
        bad = _valid_entry()
        bad["steps"][0]["speed"] = 42  # out of range
        self._write_store({"bow": bad})
        with mock.patch("modules.module_movementstore.queue_message") as qm:
            loaded = self.store.load()
        self.assertEqual(loaded, self._defaults_movements())
        qm.assert_called_once_with(
            "MOVEMENTS: invalid entry 'bow': steps[0] speed 42 must be a "
            "number in 0..1")

    def test_is_valid_reports_without_raising(self):
        # clean store file -> ok
        self._write_store({"my_move": _valid_entry()})
        ok, errors = self.store.is_valid()
        self.assertTrue(ok, errors)
        self.assertEqual(errors, [])
        # broken store file -> (False, errors naming the entry)
        bad = _valid_entry()
        bad["steps"][0]["hold_time"] = -2
        self._write_store({"my_move": bad})
        ok, errors = self.store.is_valid()
        self.assertFalse(ok)
        self.assertEqual(len(errors), 1)
        self.assertIn("my_move", errors[0])

    def test_get_movement_store_singleton(self):
        from modules import module_movementstore
        first = module_movementstore.get_movement_store()
        second = module_movementstore.get_movement_store()
        self.assertIs(first, second)
        self.assertEqual(set(first.movements), set(MOVEMENTS))


if __name__ == "__main__":
    unittest.main()
