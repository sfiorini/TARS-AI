"""Agent Skills tests (M5): frontmatter validation, install/store CRUD,
load_skill/run_skill_script (real subprocess, D7), descriptors, prompt
block 2 + budget, CLI, docs parity.

The shipped dice-roll example is exercised FOR REAL — its script runs via
actual subprocesses, never mocked (D7).
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

from modules.module_agent_skills import (  # noqa: E402
    parse_skill_md, validate_skill, decode_tars_metadata,
)

_DICE_DEFAULTS = os.path.join(_SRC, "agent_skills_defaults", "dice-roll")

SKILL_MD_TEMPLATE = """---
name: {name}
description: {description}
license: MIT
metadata:
  tars_parameters: '{tars_parameters}'
---
# Skill body

Instructions here.
"""


class _SkillDirTestCase(unittest.TestCase):
    """tmp skill dirs with crafted SKILL.md files."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_skill(self, name="stub-skill", dirname=None, body=None,
                    description="a stub skill for tests",
                    tars_parameters='{"properties": {}}'):
        skill_dir = os.path.join(self.tmp, dirname or name)
        os.makedirs(skill_dir, exist_ok=True)
        content = body if body is not None else SKILL_MD_TEMPLATE.format(
            name=name, description=description,
            tars_parameters=tars_parameters)
        with open(os.path.join(skill_dir, "SKILL.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(content)
        return skill_dir


class TestFrontmatter(_SkillDirTestCase):
    """Spec-pure frontmatter parse + validation (S-M5-02, D3)."""

    def test_dice_roll_validates_clean(self):
        errors = validate_skill(_DICE_DEFAULTS,
                                parse_skill_md(os.path.join(
                                    _DICE_DEFAULTS, "SKILL.md")))
        self.assertEqual(errors, [])

    def test_rejects_seventh_top_level_key(self):
        skill_dir = self.write_skill(
            body=SKILL_MD_TEMPLATE.format(
                name="stub-skill", description="d",
                tars_parameters='{{}}')
            + "\nparameters: not allowed at top level\n")
        # the extra line must land INSIDE the frontmatter
        content = open(os.path.join(skill_dir, "SKILL.md")).read()
        content = content.replace(
            "license: MIT\n",
            "license: MIT\nparameters: sneaky top-level key\n")
        with open(os.path.join(skill_dir, "SKILL.md"), "w") as fh:
            fh.write(content)
        errors = validate_skill(skill_dir,
                                parse_skill_md(os.path.join(
                                    skill_dir, "SKILL.md")))
        self.assertTrue(any("parameters" in e for e in errors),
                        f"seventh top-level key must be rejected: {errors}")

    def test_rejects_name_dir_mismatch(self):
        skill_dir = self.write_skill(name="mismatch-name",
                                     dirname="dir-name")
        errors = validate_skill(skill_dir,
                                parse_skill_md(os.path.join(
                                    skill_dir, "SKILL.md")))
        self.assertTrue(any("parent directory" in e for e in errors),
                        f"name/dir mismatch must be rejected: {errors}")

    def test_rejects_bad_name_chars(self):
        skill_dir = self.write_skill(name="Dice_Roll",
                                     dirname="Dice_Roll")
        errors = validate_skill(skill_dir,
                                parse_skill_md(os.path.join(
                                    skill_dir, "SKILL.md")))
        self.assertTrue(any("name" in e.lower() for e in errors),
                        f"bad name chars must be rejected: {errors}")

    def test_rejects_non_json_tars_metadata(self):
        skill_dir = self.write_skill(
            tars_parameters="not json at all {{{")
        errors = validate_skill(skill_dir,
                                parse_skill_md(os.path.join(
                                    skill_dir, "SKILL.md")))
        self.assertTrue(any("tars_parameters" in e for e in errors),
                        f"non-JSON tars metadata must be rejected: {errors}")

    def test_parameters_decoded(self):
        data = parse_skill_md(os.path.join(_DICE_DEFAULTS, "SKILL.md"))
        decoded = decode_tars_metadata(data)
        self.assertIn("properties", decoded["parameters"])
        self.assertIn("count", decoded["parameters"]["properties"])
        self.assertEqual(decoded["parameters"]["properties"]["count"]
                         ["description"],
                         "How many dice to roll (1-10)")
        self.assertIsNone(decoded["config"])

    def test_minimal_skill_loads(self):
        # a SKILL.md with ONLY name + description must load clean (D3)
        skill_dir = self.write_skill(
            dirname="minimal-skill",
            body="---\nname: minimal-skill\n"
                 "description: just the two required spec fields\n---\n"
                 "# Body\n")
        errors = validate_skill(skill_dir,
                                parse_skill_md(os.path.join(
                                    skill_dir, "SKILL.md")))
        self.assertEqual(errors, [])


import zipfile  # noqa: E402

from modules.module_agent_skills import AgentSkillStore  # noqa: E402

_DEFAULTS_DIR = os.path.join(_SRC, "agent_skills_defaults")


class _PoolTestCase(_SkillDirTestCase):
    """An AgentSkillStore over a tmp pool."""

    def setUp(self):
        super().setUp()
        self.pool = os.path.join(self.tmp, "agent_skills")
        os.makedirs(self.pool)
        self.store = AgentSkillStore(pool_dir=self.pool,
                                    defaults_dir=_DEFAULTS_DIR)


class TestInstall(_PoolTestCase):
    """Install from folder/zip, conflicts, uninstall (S-M5-03)."""

    def test_install_dice_roll_from_defaults_folder(self):
        ok, errors = self.store.install_from_folder(_DICE_DEFAULTS)
        self.assertTrue(ok, errors)
        names = [s["name"] for s in self.store.list_skills()]
        self.assertIn("dice-roll", names)
        # the real script came along
        self.assertTrue(os.path.isfile(
            os.path.join(self.pool, "dice-roll", "scripts",
                         "roll_dice.py")))

    def test_conflict_rejected_without_force(self):
        ok, _ = self.store.install_from_folder(_DICE_DEFAULTS)
        self.assertTrue(ok)
        ok, errors = self.store.install_from_folder(_DICE_DEFAULTS)
        self.assertFalse(ok)
        self.assertTrue(any("already installed" in e for e in errors),
                        errors)
        ok, errors = self.store.install_from_folder(_DICE_DEFAULTS,
                                                   force=True)
        self.assertTrue(ok, errors)

    def test_zip_traversal_rejected(self):
        zip_path = os.path.join(self.tmp, "evil.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../evil/SKILL.md",
                        "---\nname: evil\ndescription: bad\n---\n")
        ok, errors = self.store.install_from_zip(zip_path)
        self.assertFalse(ok)
        self.assertTrue(any(".." in e or "traversal" in e.lower()
                            for e in errors), errors)
        # nothing extracted into the pool
        self.assertEqual(os.listdir(self.pool), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil")))

    def test_zip_install_dice_roll(self):
        zip_path = os.path.join(self.tmp, "dice.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for root, _dirs, files in os.walk(_DICE_DEFAULTS):
                for fname in files:
                    full = os.path.join(root, fname)
                    zf.write(full, os.path.relpath(full,
                                                  _DICE_DEFAULTS))
        ok, errors = self.store.install_from_zip(zip_path)
        self.assertTrue(ok, errors)
        names = [s["name"] for s in self.store.list_skills()]
        self.assertIn("dice-roll", names)

    def test_uninstall_removes_dir(self):
        ok, _ = self.store.install_from_folder(_DICE_DEFAULTS)
        self.assertTrue(ok)
        self.store.uninstall("dice-roll")
        self.assertFalse(os.path.exists(
            os.path.join(self.pool, "dice-roll")))
        self.assertEqual(self.store.list_skills(), [])


if __name__ == "__main__":
    unittest.main()
