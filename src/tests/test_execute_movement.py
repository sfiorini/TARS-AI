"""Unit tests for the execute_movement skill (store-driven dispatch, D5)."""
import os
import shutil
import sys
import tempfile
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

from modules import module_movement_registry
from modules import module_movementstore
from skills import skill_execute_movement


def _valid_step():
    """A minimal builder-schema step."""
    return {
        "left_height": 50, "right_height": 50,
        "left_leg": 50, "right_leg": 50,
        "left_main": None, "left_forearm": None, "left_hand": None,
        "right_main": None, "right_forearm": None, "right_hand": None,
        "speed": 0.9, "hold_time": 0.0, "movement": None,
    }


def _valid_entry():
    return {
        "name": "My Move", "type": "legs_only",
        "description": "test move",
        "steps": [_valid_step()],
        "steps_arms": None,
    }


class _FakeStoreMixin(unittest.TestCase):
    """A tmp MovementStore (defaults content) with a recording execute stub.

    Dispatch moved to movementstore.execute — the fake store replaces the
    old fake-module_movements trick.  get_movement_store is monkeypatched
    so both the resolver and get_prompt()'s registry reload see the tmp
    store; the real registry content is restored afterwards.
    """

    def setUp(self):
        super().setUp()
        from modules.module_movementstore import MovementStore
        # registered FIRST so it runs LAST (after the patcher stops):
        # restore the real registry from the real store for other tests
        self.addCleanup(module_movement_registry.reload_from_store)
        self._tmp = tempfile.mkdtemp()
        self.store = MovementStore(
            store_path=os.path.join(self._tmp, "movements.json"),
            defaults_path=os.path.join(_SRC, "movements_defaults.json"))
        self.store.load()  # live file absent -> defaults content (S-M1-03)
        self.records = []

        def _rec(mid):  # record only — never touches servo hardware
            self.records.append(mid)
            return True

        self.store.execute = _rec
        patcher = mock.patch.object(module_movementstore,
                                    "get_movement_store",
                                    return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)


class TestResolveMovement(_FakeStoreMixin):
    """_resolve_movement(movement_id, arms_present) — pure registry dispatcher."""

    def test_legs_move_resolves_to_store_callable(self):
        fn, reason = skill_execute_movement._resolve_movement(
            "step_forward", arms_present=False)
        self.assertIsNone(reason)
        self.assertTrue(fn())
        self.assertEqual(self.records, ["step_forward"])

    def test_arms_move_resolves_with_arms_present(self):
        fn, reason = skill_execute_movement._resolve_movement(
            "left_hi", arms_present=True)
        self.assertIsNone(reason)
        fn()
        self.assertEqual(self.records, ["left_hi"])

    def test_arms_move_gated_without_arms(self):
        fn, reason = skill_execute_movement._resolve_movement(
            "left_hi", arms_present=False)
        self.assertIsNone(fn)
        self.assertEqual(reason, "arm_gated")
        self.assertEqual(self.records, [])

    def test_unknown_id_returns_none(self):
        fn, reason = skill_execute_movement._resolve_movement(
            "flurb", arms_present=True)
        self.assertIsNone(fn)
        self.assertEqual(reason, "unknown")
        self.assertEqual(self.records, [])

    def test_registry_only_dispatch_never_resolves_unlisted_names(self):
        # "cleanup" is not a store id at all; "set_swap_turn_directions"
        # is a module_movements attribute but outside the registry — both
        # must never resolve, even with arms_present=True.
        for dangerous in ("cleanup", "set_swap_turn_directions"):
            fn, reason = skill_execute_movement._resolve_movement(
                dangerous, arms_present=True)
            self.assertIsNone(fn, f"'{dangerous}' must not resolve")
            self.assertEqual(reason, "unknown")
        self.assertEqual(self.records, [])


class TestSkillPrompt(_FakeStoreMixin):
    """The per-request prompt (D5) exposes the full store pool, bounded."""

    def test_prompt_lists_all_registry_ids(self):
        prompt = skill_execute_movement.get_prompt()
        for mid in module_movement_registry.MOVEMENTS:
            self.assertIn(f'"{mid}"', prompt,
                          f"registry id '{mid}' missing from prompt")

    def test_prompt_includes_clarifying_leg_vs_arm_note(self):
        prompt = skill_execute_movement.get_prompt()
        self.assertIn(
            "Note: wave_left and wave_right wave the LEGS", prompt)
        self.assertIn(
            "If the user asks to wave or say hi with a leg, use wave_left or wave_right",
            prompt)

    def test_prompt_shows_description_for_every_registry_entry(self):
        prompt = skill_execute_movement.get_prompt()
        for mid, entry in module_movement_registry.MOVEMENTS.items():
            description = entry.get("description", "")
            if description:
                self.assertIn(
                    description, prompt,
                    f"description for '{mid}' missing from prompt")

    def test_prompt_groups_legs_and_arms(self):
        prompt = skill_execute_movement.get_prompt()
        self.assertIn("Legs (always available)", prompt)
        self.assertIn("Arms (only if arms installed)", prompt)

    def test_prompt_keeps_parameter_format_and_examples(self):
        prompt = skill_execute_movement.get_prompt()
        self.assertIn('{"movements": ["<id>", ...]}', prompt)
        # One leg example and one arm example
        self.assertIn('"wave_left"', prompt)
        self.assertIn('"left_hi"', prompt)
        self.assertIn("Do NOT infer or guess movement from suggestions or questions",
                      prompt)

    def test_prompt_reflects_store_add(self):
        # D5 pin: the prompt is built from the store at call time — a
        # movement created via UI/CLI is LLM-callable without a restart
        self.store.add("my_move", _valid_entry())
        self.assertIn('"my_move"', skill_execute_movement.get_prompt())

    def test_prompt_bounded(self):
        with mock.patch.object(skill_execute_movement,
                               "PROMPT_BUDGET_CHARS", 1200), \
             mock.patch("modules.module_messageQue.queue_message") as qm:
            prompt = skill_execute_movement.get_prompt()
        self.assertLessEqual(len(prompt), 1200)
        messages = [call.args[0] for call in qm.call_args_list]
        dropped = [m for m in messages if "dropped from skill prompt" in m]
        self.assertTrue(dropped,
                        f"expected drop logs, got: {messages}")
        first_dropped = dropped[0].split("'")[1]
        self.assertNotIn(f'"{first_dropped}"', prompt,
                         "dropped id must be absent from the bounded prompt")


class TestRegistryRefresh(_FakeStoreMixin):
    """Store mutations refresh the registry (S-M2-06 hook)."""

    def test_store_mutation_refreshes_registry(self):
        self.store.add("my_move", _valid_entry())  # no explicit reload
        self.assertIn("my_move", module_movement_registry.MOVEMENTS)
        self.store.delete("my_move")
        self.assertNotIn("my_move", module_movement_registry.MOVEMENTS)


class TestExecuteWiring(unittest.TestCase):
    """execute(parameters, context) — gate + arms_present passthrough."""

    @staticmethod
    def _context(voicemovement, arms_present):
        return {"config": {
            "CONTROLS": {"voicemovement": voicemovement},
            "SERVO": {"arms_present": arms_present},
        }}

    def test_noop_when_voicemovement_disabled(self):
        with mock.patch.object(skill_execute_movement, "_execute_movement") as exec_mock:
            result = skill_execute_movement.execute(
                {"movements": ["step_forward"]}, self._context(False, True))
        self.assertIsNone(result)
        exec_mock.assert_not_called()

    def test_passes_movements_and_arms_present(self):
        with mock.patch.object(skill_execute_movement, "_execute_movement") as exec_mock:
            skill_execute_movement.execute(
                {"movements": ["wave_left"]}, self._context(True, True))
        exec_mock.assert_called_once_with(["wave_left"], True)

    def test_passes_arms_absent_flag(self):
        with mock.patch.object(skill_execute_movement, "_execute_movement") as exec_mock:
            skill_execute_movement.execute(
                {"movements": ["step_forward"]}, self._context(True, False))
        exec_mock.assert_called_once_with(["step_forward"], False)


if __name__ == "__main__":
    unittest.main()
