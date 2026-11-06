"""Unit tests for the execute_movement skill (registry-driven dispatch)."""
import os
import sys
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

from modules import module_movement_registry
from skills import skill_execute_movement


class _FakeMovementsMixin(unittest.TestCase):
    """Install a fake modules.module_movements so the resolver can be tested
    on machines without the servo hardware stack (board/busio/PCA9685)."""

    def setUp(self):
        super().setUp()
        fake = types.ModuleType("modules.module_movements")
        for mid in module_movement_registry.MOVEMENTS:
            setattr(fake, mid, mock.MagicMock(name=mid))
        # Real-looking module attributes that are NOT registry ids — the
        # dispatcher must never resolve them (registry-only dispatch).
        fake.set_swap_turn_directions = mock.MagicMock()
        fake.cleanup = mock.MagicMock()
        self.fake_movements = fake

        import modules
        self._saved_mod = sys.modules.get("modules.module_movements")
        self._saved_attr = getattr(modules, "module_movements", None)
        sys.modules["modules.module_movements"] = fake
        modules.module_movements = fake
        self.addCleanup(self._restore_fake_movements)

    def _restore_fake_movements(self):
        import modules
        if self._saved_mod is None:
            sys.modules.pop("modules.module_movements", None)
        else:
            sys.modules["modules.module_movements"] = self._saved_mod
        if self._saved_attr is None:
            try:
                delattr(modules, "module_movements")
            except AttributeError:
                pass
        else:
            modules.module_movements = self._saved_attr


class TestSkillPrompt(unittest.TestCase):
    """The SKILL prompt must expose the full movement registry."""

    def test_prompt_lists_all_registry_ids(self):
        prompt = skill_execute_movement.SKILL["prompt"]
        for mid in module_movement_registry.MOVEMENTS:
            self.assertIn(f'"{mid}"', prompt, f"registry id '{mid}' missing from prompt")

    def test_prompt_includes_clarifying_leg_vs_arm_note(self):
        prompt = skill_execute_movement.SKILL["prompt"]
        self.assertIn(
            "Note: wave_left and wave_right wave the LEGS", prompt)
        self.assertIn(
            "If the user asks to wave or say hi with a leg, use wave_left or wave_right",
            prompt)

    def test_prompt_shows_description_for_every_registry_entry(self):
        prompt = skill_execute_movement.SKILL["prompt"]
        for mid, entry in module_movement_registry.MOVEMENTS.items():
            description = entry.get("description", "")
            if description:
                self.assertIn(
                    description, prompt,
                    f"description for '{mid}' missing from prompt")

    def test_prompt_groups_legs_and_arms(self):
        prompt = skill_execute_movement.SKILL["prompt"]
        self.assertIn("Legs (always available)", prompt)
        self.assertIn("Arms (only if arms installed)", prompt)

    def test_prompt_keeps_parameter_format_and_examples(self):
        prompt = skill_execute_movement.SKILL["prompt"]
        self.assertIn('{"movements": ["<id>", ...]}', prompt)
        # One leg example and one arm example
        self.assertIn('"wave_left"', prompt)
        self.assertIn('"left_hi"', prompt)
        self.assertIn("Do NOT infer or guess movement", prompt)


class TestResolveMovement(_FakeMovementsMixin):
    """_resolve_movement(movement_id, arms_present) — pure registry dispatcher."""

    def test_legs_move_resolves_to_callable(self):
        fn, reason = skill_execute_movement._resolve_movement("step_forward", arms_present=False)
        self.assertIsNone(reason)
        self.assertIs(fn, self.fake_movements.step_forward)

    def test_arms_move_resolves_with_arms_present(self):
        fn, reason = skill_execute_movement._resolve_movement("left_hi", arms_present=True)
        self.assertIsNone(reason)
        self.assertIs(fn, self.fake_movements.left_hi)

    def test_arms_move_gated_without_arms(self):
        fn, reason = skill_execute_movement._resolve_movement("left_hi", arms_present=False)
        self.assertIsNone(fn)
        self.assertEqual(reason, "arm_gated")

    def test_unknown_id_returns_none(self):
        fn, reason = skill_execute_movement._resolve_movement("flurb", arms_present=True)
        self.assertIsNone(fn)
        self.assertEqual(reason, "unknown")

    def test_registry_only_dispatch_never_resolves_unlisted_attributes(self):
        # "cleanup" is not a module_movements attribute at all; "set_swap_turn_directions"
        # IS one — both are outside the registry and must never resolve, even with
        # arms_present=True. getattr is only ever called on registry ids.
        for dangerous in ("cleanup", "set_swap_turn_directions"):
            fn, reason = skill_execute_movement._resolve_movement(dangerous, arms_present=True)
            self.assertIsNone(fn, f"'{dangerous}' must not resolve")
            self.assertEqual(reason, "unknown")


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
