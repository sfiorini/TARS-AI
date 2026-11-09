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


class TestCrud(_PoolTestCase):
    """Create/edit/rename/reset + seed-from-defaults (S-M5-04)."""

    def test_delete_then_reset_restores_dice_roll(self):
        # the D7 round-trip: delete dice-roll, reset, it's back
        self.store.reset()
        self.assertIn("dice-roll",
                     [s["name"] for s in self.store.list_skills()])
        self.store.uninstall("dice-roll")
        self.assertNotIn("dice-roll",
                         [s["name"] for s in self.store.list_skills()])
        self.store.reset()
        self.assertIn("dice-roll",
                     [s["name"] for s in self.store.list_skills()])

    def test_create_validates_name(self):
        ok, errors = self.store.create("Bad_Name", "d", "body")
        self.assertFalse(ok)
        self.assertTrue(any("name" in e.lower() for e in errors), errors)
        ok, errors = self.store.create("good-skill", "a created skill",
                                       "# Body\nDo the thing.")
        self.assertTrue(ok, errors)
        skills = {s["name"]: s for s in self.store.list_skills()}
        self.assertIn("good-skill", skills)
        self.assertEqual(skills["good-skill"]["description"],
                         "a created skill")
        # created skills load clean (spec-pure)
        data, errs = self.store._read_skill("good-skill")
        self.assertEqual(errs, [])

    def test_rename_reparents_dir(self):
        ok, _ = self.store.create("old-name", "d", "body")
        self.assertTrue(ok)
        ok, errors = self.store.rename("old-name", "new-name")
        self.assertTrue(ok, errors)
        self.assertFalse(os.path.exists(os.path.join(self.pool,
                                                    "old-name")))
        data, errs = self.store._read_skill("new-name")
        self.assertEqual(errs, [])
        self.assertEqual(data["name"], "new-name")

    def test_edit_updates_description_only(self):
        ok, _ = self.store.create("edit-me", "before", "ORIGINAL BODY")
        self.assertTrue(ok)
        ok, errors = self.store.edit("edit-me", description="after")
        self.assertTrue(ok, errors)
        data, _ = self.store._read_skill("edit-me")
        self.assertEqual(data["description"], "after")
        with open(os.path.join(self.pool, "edit-me", "SKILL.md")) as fh:
            self.assertIn("ORIGINAL BODY", fh.read())
        ok, errors = self.store.edit("edit-me", body="REPLACED BODY")
        self.assertTrue(ok, errors)
        with open(os.path.join(self.pool, "edit-me", "SKILL.md")) as fh:
            content = fh.read()
        self.assertIn("REPLACED BODY", content)
        self.assertNotIn("ORIGINAL BODY", content)
        self.assertIn("after", content)  # description survived the body edit

    def test_gitkeep_only_pool_seeds(self):
        # M5 review F3: pre-fix clones have a pool dir containing only the
        # legacy .gitkeep — seeding must fire for them too
        shutil.rmtree(self.pool)
        os.makedirs(self.pool)
        with open(os.path.join(self.pool, ".gitkeep"), "w") as fh:
            fh.write("")
        self.store.seed_if_absent()
        self.assertIn("dice-roll",
                     [s["name"] for s in self.store.list_skills()])

    def test_fresh_clone_seeds_and_registers_first_run(self):
        # M5 review F3: on a fresh clone (pool absent) the startup order
        # must seed BEFORE registering descriptors, so dice-roll is
        # callable without a second restart
        shutil.rmtree(self.pool)
        self.store.seed_if_absent()
        names = {d.name for d in self.store.iter_descriptors()}
        self.assertIn("dice-roll", names)
        # and app.py must run seed_if_absent BEFORE the registration loop
        with open(os.path.join(_SRC, "app.py"), encoding="utf-8") as fh:
            src = fh.read()
        seed_idx = src.index("seed_if_absent()")
        register_idx = src.index(
            "for d in get_agent_skill_store().iter_descriptors():")
        self.assertLess(seed_idx, register_idx,
                        "app.py must seed the pool before registering "
                        "descriptors")

    def test_seed_if_absent(self):
        shutil.rmtree(self.pool)
        self.assertFalse(os.path.exists(self.pool))
        self.store.seed_if_absent()
        names = [s["name"] for s in self.store.list_skills()]
        self.assertIn("dice-roll", names)
        # present pool is NOT re-seeded (only absent triggers)
        self.store.uninstall("dice-roll")
        self.store.seed_if_absent()
        self.assertNotIn("dice-roll",
                         [s["name"] for s in self.store.list_skills()])


import re as _re  # noqa: E402

from modules import module_agent_skills  # noqa: E402


class TestLoadAndRun(_PoolTestCase):
    """load_skill / run_skill_script (S-M5-05) — the REAL shipped script
    runs via real subprocesses, never mocked (D7)."""

    def setUp(self):
        super().setUp()
        ok, errors = self.store.install_from_folder(_DICE_DEFAULTS)
        self.assertTrue(ok, errors)

    def test_run_dice_script_real_subprocess(self):
        result = self.store.run_skill_script("dice-roll", "roll_dice.py",
                                             ["2", "20"])
        self.assertFalse(result.is_error)
        self.assertRegex(result.content,
                         _re.compile(r"Rolled 2d20: \d+, \d+ \(total \d+\)"))

    def test_run_dice_script_error_contract(self):
        result = self.store.run_skill_script("dice-roll", "roll_dice.py",
                                             ["99", "6"])
        self.assertTrue(result.is_error)
        self.assertIn("count must be 1-10 and sides must be 2-100",
                      result.content)

    def test_load_skill_returns_body(self):
        result = self.store.load_skill("dice-roll")
        self.assertFalse(result.is_error)
        self.assertIn("run_skill_script", result.content)
        # unknown skill -> is_error with the bounded valid list
        missing = self.store.load_skill("no-such-skill")
        self.assertTrue(missing.is_error)
        self.assertIn("Unknown skill 'no-such-skill'", missing.content)
        self.assertIn("dice-roll", missing.content)

    def test_load_skill_truncates(self):
        with mock.patch.object(
                module_agent_skills, "load_config",
                return_value={"STANDARD_SKILLS": {"body_char_limit": 10}}):
            result = self.store.load_skill("dice-roll")
        self.assertLessEqual(len(result.content), 10)

    def test_script_traversal_rejected(self):
        result = self.store.run_skill_script("dice-roll", "../SKILL.md",
                                             [])
        self.assertTrue(result.is_error)
        self.assertIn("invalid script name", result.content)


class TestStandardSkillsConfig(unittest.TestCase):
    """[STANDARD_SKILLS] keys with code defaults (S-M5-06)."""

    def test_standard_skills_keys_default(self):
        from modules import module_config
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        minimal = os.path.join(tmp, "config.ini")
        with open(os.path.join(_SRC, "config.ini.template")) as fh:
            text = fh.read()
        # strip the [STANDARD_SKILLS] section entirely — code defaults apply
        lines, skipping = [], False
        for ln in text.splitlines():
            if ln.strip() == "[STANDARD_SKILLS]":
                skipping = True
                continue
            if skipping and ln.startswith("["):
                skipping = False
            if not skipping:
                lines.append(ln)
        with open(minimal, "w") as fh:
            fh.write("\n".join(lines))
        saved_path = os.environ.get("TARS_CONFIG_PATH")
        saved_cache = module_config._config_cache
        os.environ["TARS_CONFIG_PATH"] = minimal
        module_config._config_cache = None
        self.addCleanup(module_config.__setattr__, "_config_cache",
                        saved_cache)
        def _restore():
            if saved_path is None:
                os.environ.pop("TARS_CONFIG_PATH", None)
            else:
                os.environ["TARS_CONFIG_PATH"] = saved_path
        self.addCleanup(_restore)
        config = module_config.load_config()
        std = config.get("STANDARD_SKILLS", {})
        self.assertIs(std.get("enabled"), True)
        self.assertEqual(std.get("body_char_limit"), 4000)
        self.assertEqual(std.get("prompt_budget_chars"), 3000)

    def test_std_config_roundtrip(self):
        from modules import module_agent_skills
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = os.path.join(tmp, "config.ini")
        with open(os.path.join(_SRC, "config.ini.template")) as fh:
            shutil.copyfileobj(fh, open(cfg, "w"))
        saved_path = os.environ.get("TARS_CONFIG_PATH")
        os.environ["TARS_CONFIG_PATH"] = cfg

        def _restore():
            if saved_path is None:
                os.environ.pop("TARS_CONFIG_PATH", None)
            else:
                os.environ["TARS_CONFIG_PATH"] = saved_path
        self.addCleanup(_restore)
        module_agent_skills.set_std_config("dice-roll", {"sides": "20"})
        self.assertEqual(module_agent_skills.get_std_config("dice-roll"),
                         {"sides": "20"})
        with open(cfg) as fh:
            content = fh.read()
        self.assertIn("[SKILL_STD:dice-roll]", content)
        # update merges over the previous values
        module_agent_skills.set_std_config("dice-roll", {"count": "2"})
        stored = module_agent_skills.get_std_config("dice-roll")
        self.assertEqual(stored, {"sides": "20", "count": "2"})


class TestDescriptorsAndPrompt(_PoolTestCase):
    """Agent-skill descriptors + prompt block 2 + budget (S-M5-07)."""

    def setUp(self):
        super().setUp()
        ok, errors = self.store.install_from_folder(_DICE_DEFAULTS)
        self.assertTrue(ok, errors)

    def test_dice_roll_descriptor_end_to_end(self):
        from modules.module_toolregistry import ToolRegistry, ToolContext
        registry = ToolRegistry()
        for d in self.store.iter_descriptors():
            registry.register(d)
        # the two meta-tools + the direct dice-roll descriptor
        names = {d.name for d in registry.iter_descriptors()}
        self.assertIn("load_skill", names)
        self.assertIn("run_skill_script", names)
        self.assertIn("dice-roll", names)
        dice = registry.get("dice-roll")
        self.assertEqual(dice.kind, "agent_skill")
        self.assertTrue(dice.result_bearing)
        self.assertIn("count", dice.parameters["properties"])
        # D7: the REAL script runs through the descriptor
        result = registry.dispatch("dice-roll", {"count": 2, "sides": 20},
                                   ToolContext())
        self.assertFalse(result.is_error, result.content)
        self.assertRegex(result.content,
                         _re.compile(r"Rolled 2d20: \d+, \d+ \(total \d+\)"))

    def test_meta_tools_in_prompt(self):
        block, included, dropped = self.store.build_prompt_block()
        self.assertIn("load_skill\n   Loads a skill's full instructions. "
                      'Params: {"name": "<skill-name>"}', block)
        self.assertIn("run_skill_script\n   Runs a skill's script. "
                      'Params: {"skill": "<name>", "script": '
                      '"<filename>", "args": ["..."]}', block)
        self.assertIn("Scripts run with the app's privileges.", block)
        self.assertIn("dice-roll", included)
        self.assertEqual(dropped, [])

    def test_prompt_block_budget_drops_and_logs(self):
        # 30 fake skills with long descriptions + a tiny budget
        for i in range(30):
            ok, _ = self.store.create(
                f"fake-skill-{i:02d}", "x" * 300, "body")
            self.assertTrue(ok)
        with mock.patch.object(
                module_agent_skills, "load_config",
                return_value={"STANDARD_SKILLS": {
                    "prompt_budget_chars": 600}}), \
             mock.patch("modules.module_agent_skills.queue_message") as qm:
            block, included, dropped = self.store.build_prompt_block()
        self.assertLessEqual(len(block), 600)
        self.assertTrue(dropped, "overflow must drop entries alphabetically")
        self.assertEqual(dropped, sorted(dropped))
        messages = [call.args[0] for call in qm.call_args_list]
        self.assertTrue(any("dropped from agent-skills prompt" in m
                            for m in messages), messages)
        for name in dropped:
            self.assertNotIn(f'"{name}"', block)

    def test_parameterized_skill_gets_params_spec(self):
        block, _inc, _dropped = self.store.build_prompt_block()
        self.assertRegex(
            block,
            _re.compile(r'\* "dice-roll" — .* Params: \{"count": number, '
                        r'"sides": number\}'))


import subprocess  # noqa: E402

_REPO = os.path.abspath(os.path.join(_SRC, ".."))
_CLI = os.path.join(_SRC, "app-skills.py")


class TestSkillsCli(unittest.TestCase):
    """app-skills.py CLI (S-M5-09) — real subprocess, real exit codes."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pool = os.path.join(self.tmp, "agent_skills")
        shutil.copyfile(os.path.join(_SRC, "config.ini.template"),
                        os.path.join(self.tmp, "config.ini"))
        self.env = dict(os.environ)
        self.env["TARS_CONFIG_PATH"] = os.path.join(self.tmp, "config.ini")
        self.env["TARS_SKILLS_POOL"] = self.pool

    def run_cli(self, *args):
        return subprocess.run([sys.executable, _CLI] + list(args),
                              cwd=_REPO, env=self.env,
                              capture_output=True, text=True, timeout=60)

    def test_cli_list_shows_dice_roll(self):
        result = self.run_cli("reset", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = self.run_cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("dice-roll", listed.stdout)
        self.assertIn("[params]", listed.stdout)

    def test_cli_validate_real_dice_roll_exits_0(self):
        result = self.run_cli("validate", _DICE_DEFAULTS)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_validate_bad_skill_exits_1(self):
        bad = os.path.join(self.tmp, "bad-skill")
        os.makedirs(bad)
        with open(os.path.join(bad, "SKILL.md"), "w") as fh:
            fh.write("---\nname: bad-skill\ndescription: d\n"
                     "sneaky: top-level\n---\nbody\n")
        result = self.run_cli("validate", bad)
        self.assertEqual(result.returncode, 1)
        self.assertIn("sneaky", result.stderr)

    def test_cli_install_conflict_exits_1(self):
        result = self.run_cli("install", _DICE_DEFAULTS)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("install", _DICE_DEFAULTS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("already installed", result.stderr)
        result = self.run_cli("install", _DICE_DEFAULTS, "--force")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_uninstall_then_reset_restores(self):
        self.run_cli("reset", "--yes")
        result = self.run_cli("uninstall", "dice-roll")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("dice-roll", self.run_cli("list").stdout)
        result = self.run_cli("reset", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dice-roll", self.run_cli("list").stdout)


class TestTraversalSecurity(_PoolTestCase):
    """M5 review F1: `skill`/`name` must never escape the pool — the
    descriptors are LLM-reachable, so traversal here is arbitrary code
    execution / arbitrary read with the app's privileges."""

    def setUp(self):
        super().setUp()
        ok, errors = self.store.install_from_folder(_DICE_DEFAULTS)
        self.assertTrue(ok, errors)
        # an "evil" skill OUTSIDE the pool whose script writes a marker —
        # if traversal is possible, executing it proves the hole
        self.evil_dir = os.path.join(self.tmp, "evil_skill")
        os.makedirs(os.path.join(self.evil_dir, "scripts"))
        self.marker = os.path.join(self.tmp, "executed.marker")
        with open(os.path.join(self.evil_dir, "scripts", "evil.py"), "w") as fh:
            fh.write(f"open({self.marker!r}, 'w').write('pwned')\n")
        with open(os.path.join(self.evil_dir, "SKILL.md"), "w") as fh:
            fh.write("---\nname: evil-skill\ndescription: secret\n---\n"
                      "SECRET BODY\n")

    def test_run_skill_script_traversal_rejected(self):
        # exploit: skill="../evil_skill" resolves OUTSIDE the pool
        result = self.store.run_skill_script("../evil_skill", "evil.py",
                                             [])
        self.assertTrue(result.is_error)
        self.assertFalse(os.path.exists(self.marker),
                         "traversal executed a script outside the pool")

    def test_run_skill_script_dotdot_dotdot_rejected(self):
        # deeper traversal back into the real pool must also be refused
        # (the name rule, not path luck, is the guard)
        result = self.store.run_skill_script(
            "../agent_skills/dice-roll", "roll_dice.py", [])
        self.assertTrue(result.is_error)
        self.assertIn("Unknown skill", result.content)

    def test_load_skill_traversal_rejected(self):
        # arbitrary READ: a SKILL.md outside the pool must not load
        result = self.store.load_skill("../evil_skill")
        self.assertTrue(result.is_error)
        self.assertNotIn("SECRET BODY", result.content)

    def test_symlink_escape_rejected(self):
        # a symlink inside scripts/ pointing outside the pool
        link = os.path.join(self.pool, "dice-roll", "scripts", "link.py")
        os.symlink(os.path.join(self.evil_dir, "scripts", "evil.py"),
                   link)
        result = self.store.run_skill_script("dice-roll", "link.py", [])
        self.assertTrue(result.is_error,
                        "symlink escape must be refused by containment")
        self.assertFalse(os.path.exists(self.marker))


class TestUninstallSecurity(_PoolTestCase):
    """M5 review F2: uninstall/rename must never delete or move anything
    outside the installed pool — an unvalidated name reaches rmtree."""

    def setUp(self):
        super().setUp()
        # a sibling directory the traversal would delete if unguarded
        self.victim = os.path.join(self.tmp, "victim")
        os.makedirs(self.victim)
        with open(os.path.join(self.victim, "keep.txt"), "w") as fh:
            fh.write("do not delete me")

    def test_uninstall_traversal_rejected(self):
        # SAFE exploit first: ../victim deletes test-owned content only.
        # (assertRaises aborts the test here during RED, so the deeper
        # ../../.. exploit below is never reached unfixed)
        with self.assertRaises(ValueError):
            self.store.uninstall("../victim")
        self.assertTrue(os.path.exists(self.victim),
                        "traversal deleted a directory outside the pool")
        # deeper traversal must be rejected by the name rule before any
        # filesystem operation
        with self.assertRaises(ValueError):
            self.store.uninstall("../../..")
        self.assertTrue(os.path.exists(self.tmp))

    def test_uninstall_nonexistent_raises(self):
        with self.assertRaises(ValueError):
            self.store.uninstall("nonexistent")

    def test_rename_traversal_rejected(self):
        # rename's contract is (ok, errors) — the gate must refuse
        ok, errors = self.store.rename("../victim", "stolen")
        self.assertFalse(ok, errors)
        self.assertTrue(os.path.exists(self.victim))
        ok, errors = self.store.rename("../../..", "x")
        self.assertFalse(ok, errors)
        self.assertTrue(os.path.exists(self.tmp))


class TestScriptUpload(_PoolTestCase):
    """M5 review F4: script upload writes into <skill>/scripts/ only —
    the filename obeys the same traversal rules as run_skill_script."""

    def setUp(self):
        super().setUp()
        ok, errors = self.store.install_from_folder(_DICE_DEFAULTS)
        self.assertTrue(ok, errors)

    def test_upload_writes_script(self):
        ok, errors = self.store.add_script("dice-roll", "hello.py",
                                          "print('hi')\n")
        self.assertTrue(ok, errors)
        path = os.path.join(self.pool, "dice-roll", "scripts", "hello.py")
        self.assertTrue(os.path.isfile(path))
        with open(path) as fh:
            self.assertEqual(fh.read(), "print('hi')\n")
        # and it is runnable through the normal contract
        result = self.store.run_skill_script("dice-roll", "hello.py", [])
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "hi")

    def test_upload_traversal_rejected(self):
        for bad in ("../evil.py", "/abs.py", "sub/dir.py", "..\\evil.py",
                    ""):
            ok, errors = self.store.add_script("dice-roll", bad, "x")
            self.assertFalse(ok, f"{bad!r} must be rejected")
        # nothing escaped the pool
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.py")))

    def test_upload_unknown_skill_rejected(self):
        ok, errors = self.store.add_script("no-such-skill", "x.py", "x")
        self.assertFalse(ok, errors)


if __name__ == "__main__":
    unittest.main()
