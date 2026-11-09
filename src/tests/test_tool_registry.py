"""Tool registry tests (M3): dataclasses, registry semantics, builtin
adapter, byte-identical prompt block.

M3 is behaviour-neutral: the registry is the seam the deferred loop (M4)
will dispatch through.  dispatch must NEVER raise — exceptions become
ToolResult(content=str(e), is_error=True).
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

from modules.module_toolregistry import (  # noqa: E402
    ToolDescriptor, ToolResult, ToolContext, ToolRegistry,
    get_registry, reset_registry,
)


def _descriptor(name="probe", kind="builtin", execute=None,
                description="a probe tool", parameters=None,
                result_bearing=False):
    return ToolDescriptor(
        name=name, kind=kind, description=description,
        parameters=parameters, result_bearing=result_bearing,
        timeout_s=60.0, execute=execute or (lambda params, ctx: "ok"))


class TestRegistryCore(unittest.TestCase):
    """ToolRegistry register/get/dispatch semantics (S-M3-01)."""

    def setUp(self):
        super().setUp()
        self.registry = ToolRegistry()

    def test_register_and_get(self):
        d = _descriptor(name="probe")
        self.registry.register(d)
        self.assertIs(self.registry.get("probe"), d)
        self.assertIsNone(self.registry.get("missing"))

    def test_duplicate_name_keeps_first(self):
        first = _descriptor(name="x", kind="builtin")
        second = _descriptor(name="x", kind="mcp")
        with mock.patch("modules.module_toolregistry.queue_message") as qm:
            self.registry.register(first)
            self.registry.register(second)
        self.assertEqual(self.registry.get("x").kind, "builtin")
        # one entry per name, later one disabled
        self.assertEqual([d.name for d in self.registry.iter_descriptors()
                          if d.name == "x"], ["x"])
        self.assertIn("duplicate tool 'x' from mcp",
                      qm.call_args[0][0])

    def test_dispatch_unknown_returns_is_error(self):
        result = self.registry.dispatch("nope", {}, ToolContext())
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.is_error)
        self.assertIn("Unknown function: nope", result.content)

    def test_dispatch_never_raises(self):
        def boom(params, ctx):
            raise ValueError("boom message")
        self.registry.register(_descriptor(name="exploder", execute=boom))
        result = self.registry.dispatch("exploder", {}, ToolContext())
        self.assertTrue(result.is_error)
        self.assertIn("boom message", result.content)
        self.assertIsInstance(result, ToolResult)

    def test_dispatch_wraps_string_result(self):
        self.registry.register(
            _descriptor(name="greeter", execute=lambda p, c: "hello there"))
        result = self.registry.dispatch("greeter", {}, ToolContext())
        self.assertEqual(result.content, "hello there")
        self.assertFalse(result.is_error)

    def test_get_prompt_summaries(self):
        self.registry.register(_descriptor(
            name="probe", description="probe tool",
            parameters={"properties": {"q": {"type": "string"}},
                        "required": ["q"]}))
        summaries = self.registry.get_prompt_summaries()
        self.assertEqual(summaries,
                         [("probe", "probe tool",
                           {"properties": {"q": {"type": "string"}},
                            "required": ["q"]})])

    def test_singleton_and_reset(self):
        reset_registry()
        first = get_registry()
        second = get_registry()
        self.assertIs(first, second)
        reset_registry()
        self.assertIsNot(get_registry(), first)


STUB_SKILL_SOURCE = '''
recorder = []


def execute(params, context):
    recorder.append(params)
    return "done"


SKILL = {
    "name": "stub_search",
    "required_params": ["query"],
    "description": "stub",
    "prompt": "stub",
}
'''


class _StubSkillsDirTestCase(unittest.TestCase):
    """A tmp skills dir with one stub skill, isolated from the real
    `skills` package in sys.modules."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.skills_dir = os.path.join(self.tmp, "skills")
        os.makedirs(self.skills_dir)
        # a regular package wins the sys.path scan over the real src/skills
        with open(os.path.join(self.skills_dir, "__init__.py"), "w"):
            pass
        with open(os.path.join(self.skills_dir,
                               "skill_stub_search.py"), "w") as fh:
            fh.write(STUB_SKILL_SOURCE)
        self._saved_skills = sys.modules.get("skills")
        self._saved_children = {n: m for n, m in sys.modules.items()
                                if n.startswith("skills.")}
        sys.modules.pop("skills", None)
        for n in list(self._saved_children):
            del sys.modules[n]
        sys.path.insert(0, self.tmp)
        self.addCleanup(self._restore_skills_package)

    def _restore_skills_package(self):
        if self.tmp in sys.path:
            sys.path.remove(self.tmp)
        for n in [n for n in sys.modules if n.startswith("skills")]:
            del sys.modules[n]
        if self._saved_skills is not None:
            sys.modules["skills"] = self._saved_skills
        for n, m in self._saved_children.items():
            sys.modules[n] = m
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBuiltinAdapter(unittest.TestCase):
    """SkillManager.iter_descriptors() — the builtin pool adapter (S-M3-02)."""

    @staticmethod
    def _real_manager():
        from modules.module_skills import SkillManager
        manager = SkillManager()
        manager.discover()  # the real src/skills
        return manager

    def test_builtin_descriptors_wrap_skills(self):
        manager = self._real_manager()
        manager._disabled.clear()  # every loaded skill yields a descriptor
        descriptors = list(manager.iter_descriptors())
        self.assertEqual(len(descriptors), len(manager._skills))
        by_name = {d.name: d for d in descriptors}
        self.assertIn("execute_movement", by_name)
        self.assertIn("web_search", by_name)
        self.assertEqual(by_name["execute_movement"].kind, "builtin")
        # D2 rule: followup -> result_bearing
        self.assertFalse(by_name["execute_movement"].result_bearing)
        self.assertTrue(by_name["web_search"].result_bearing)
        # parameters built from required_params
        self.assertEqual(by_name["web_search"].parameters["required"],
                         ["query"])
        self.assertEqual(
            by_name["web_search"].parameters["properties"],
            {"query": {"type": "string"}})

    def test_disabled_skills_yield_no_descriptor(self):
        manager = self._real_manager()
        manager._disabled.add("web_search")
        names = [d.name for d in manager.iter_descriptors()]
        self.assertNotIn("web_search", names)
        self.assertIn("execute_movement", names)  # enabled ones still yield


class TestRequiredParamsGuard(_StubSkillsDirTestCase):
    """The hallucination guard lives in the builtin adapter (S-M3-02):
    empty params on a skill declaring required_params must skip — the
    skill's execute must NEVER run."""

    def test_required_params_guard_skips_empty_params(self):
        from modules.module_skills import SkillManager
        manager = SkillManager()
        manager.discover(self.skills_dir)
        descriptors = {d.name: d for d in manager.iter_descriptors()}
        self.assertIn("stub_search", descriptors)
        stub_module = manager._skills["stub_search"]

        result = descriptors["stub_search"].execute({}, ToolContext())
        self.assertEqual(result.content, "")
        self.assertFalse(result.is_error)
        self.assertEqual(stub_module.recorder, [],
                         "skills.execute must never run on empty params")

        result = descriptors["stub_search"].execute({"query": "x"},
                                                     ToolContext())
        self.assertEqual(result.content, "done")
        self.assertEqual(stub_module.recorder, [{"query": "x"}])


# Golden pins for the followup skills' trigger text (M3 review F2).
# These skills' prompts drive the deferred loop's trigger reliability —
# any reformat must be a conscious change (update the pins deliberately).
# The followup pool is 7 skills (skill_discord has prompt "" and no
# followup flag — its earlier "followup" grep hit was followup_text code).
GOLDEN_FOLLOWUP_TRIGGERS = {
    "capture_camera_view":
        "MUST USE when user asks ANY question about vision/seeing",
    "example": 'MUST call when the user says "skill example"',
    "network_camera": "network/security/IP camera feed",
    "sandbox_exec": "execute Python code to answer the user's question",
    "take_photo": "to KEEP, not to analyze",
    "web_search": "CRITICAL: Weather questions ALWAYS require web_search",
    # tars_radio cannot import on py3.9 dev machines (py3.10+ syntax) —
    # its trigger text is pinned in its source file instead
    "tars_radio": "Use for ANY music PLAYBACK, library management, or "
                  "radio commands",
}
GOLDEN_FOLLOWUP_PROMPT_LENGTHS = {
    "capture_camera_view": 854,
    "example": 233,
    "network_camera": 1009,
    "sandbox_exec": 1203,
    "take_photo": 674,
    "web_search": 1285,
}


class TestBuiltinPromptGolden(unittest.TestCase):
    """Pin the actual builtin prompt content (M3 review F2).

    test_builtin_prompt_block_byte_identical compares the wrapper to
    get_prompt_text(), so both sides move together — this golden test
    pins the content itself: every followup skill's distinctive trigger
    text and per-skill prompt-block lengths.
    """

    @staticmethod
    def _manager():
        from modules.module_skills import SkillManager
        manager = SkillManager()
        manager.discover()
        manager._disabled.clear()
        return manager

    def test_followup_trigger_text_present(self):
        manager = self._manager()
        prompt = manager.get_prompt_text()
        for name, trigger in GOLDEN_FOLLOWUP_TRIGGERS.items():
            if name in manager._skills:  # loadable in this environment
                self.assertIn(trigger, prompt,
                               f"{name}'s trigger text drifted")
            else:  # not importable here (discord lib / py3.10+ syntax) —
                with open(os.path.join(_SRC, "skills",
                                       f"skill_{name}.py"),
                          encoding="utf-8") as fh:
                    self.assertIn(trigger, fh.read(),
                                  f"{name}'s trigger text drifted")

    def test_followup_prompt_lengths_pinned(self):
        manager = self._manager()
        for name, length in GOLDEN_FOLLOWUP_PROMPT_LENGTHS.items():
            meta = manager._skill_meta.get(name)
            self.assertIsNotNone(meta, f"{name} must load")
            self.assertEqual(len(meta.get("prompt", "")), length,
                            f"{name}'s prompt block was reformatted — "
                            f"update the golden pin deliberately")


class TestStartupRegistration(unittest.TestCase):
    """The builtin pool registers into the tool registry at startup
    (S-M3-05 — the same flow app.py runs after initialize_skills())."""

    def test_registry_has_builtin_tools(self):
        from modules import module_skills
        from modules.module_toolregistry import get_registry, reset_registry
        reset_registry()
        self.addCleanup(reset_registry)
        saved = module_skills._manager
        self.addCleanup(setattr, module_skills, "_manager", saved)
        manager = module_skills.initialize_skills()
        registry = get_registry()
        for d in manager.iter_descriptors():
            registry.register(d)
        registered = registry.iter_descriptors()
        enabled = [n for n in manager._skills
                    if n not in manager._disabled]
        # every enabled skill yields exactly one builtin descriptor
        # (19 on the robot with all skills enabled; dev machines load 17 —
        # skill_tars_radio needs py3.10+, skill_discord needs the discord lib)
        self.assertEqual(len(registered), len(enabled))
        self.assertTrue(all(d.kind == "builtin" for d in registered))
        names = {d.name for d in registered}
        self.assertIn("web_search", names)
        self.assertIn("execute_movement", names)


class TestPromptByteIdentity(unittest.TestCase):
    """The builtin prompt block must stay byte-identical to today's
    skills prompt (S-M3-03) — the 8 followup skills depend on their exact
    trigger text."""

    def test_builtin_prompt_block_byte_identical(self):
        from modules import module_skills, module_prompt
        saved = module_skills._manager
        self.addCleanup(setattr, module_skills, "_manager", saved)
        manager = module_skills.initialize_skills()
        expected = manager.get_prompt_text()
        actual = module_prompt._get_tools_prompt_text()
        self.assertEqual(actual, expected)
        self.assertTrue(expected)  # a real prompt, not the fallback

    def test_no_skills_loaded_fallback_preserved(self):
        from modules import module_prompt
        with mock.patch("modules.module_skills.get_skill_manager",
                        return_value=None):
            self.assertEqual(module_prompt._get_tools_prompt_text(),
                             "(No skills loaded)")


if __name__ == "__main__":
    unittest.main()
