"""S-M1-13 boot wiring — source-level structural assertions.

The hub-watchdog start and the BLAS env vars cannot be exercised without
booting the app on the robot, so like `test_gitignore_hygiene.py` these
tests assert on source structure:

- `module_main.startup_initialization` must call `start_hub_watchdog`
  inside a `try/except Exception` (a watchdog failure must never block
  boot — mirrors the servo-watchdog block).
- `App-Start.py` must set `OPENBLAS_NUM_THREADS=1` / `OMP_NUM_THREADS=1`
  via `os.environ.setdefault` BEFORE the line that launches
  `python app.py` (numpy reads them at import; setting inside app.py is
  too late), and the launch must not pass an explicit `env=` so the
  parent environment (with these vars) is inherited by the child.
"""
import ast
import os
import unittest
from unittest import mock

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _src(rel):
    with open(os.path.join(_REPO, rel), encoding="utf-8") as f:
        return f.read()


class TestStartupInitializationWatchdog(unittest.TestCase):
    def _startup_initialization(self):
        tree = ast.parse(_src("src/modules/module_main.py"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "startup_initialization"):
                return node
        self.fail("startup_initialization not found in module_main.py")

    def test_hub_watchdog_started_inside_try_except(self):
        fn = self._startup_initialization()
        guarded = []
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Try) and node.handlers):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    name = getattr(sub.func, "attr",
                                   getattr(sub.func, "id", ""))
                    if name == "start_hub_watchdog":
                        guarded.append(node)
        self.assertTrue(guarded, "startup_initialization must call "
                        "start_hub_watchdog inside a try/except so a "
                        "watchdog failure never blocks boot")

    def test_watchdog_guard_catches_exception(self):
        fn = self._startup_initialization()
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
        watchdog_tries = [t for t in tries
                          if any(getattr(sub.func, "attr",
                                         getattr(sub.func, "id", ""))
                                 == "start_hub_watchdog"
                                 for sub in ast.walk(t)
                                 if isinstance(sub, ast.Call))]
        self.assertTrue(watchdog_tries)
        for t in watchdog_tries:
            handler_types = []
            for h in t.handlers:
                if h.type is None:
                    handler_types.append("bare")
                else:
                    handler_types.append(getattr(h.type, "id",
                                                getattr(h.type, "attr", "?")))
            self.assertIn("Exception", handler_types,
                          "the start_hub_watchdog guard must catch "
                          "Exception (like the servo-watchdog block)")

    def _watchdog_try_node(self):
        """The innermost try in startup_initialization whose DIRECT body
        calls start_hub_watchdog — not the servo one, and not the
        enclosing function-wide try that also contains the call."""
        fn = self._startup_initialization()
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Try) and node.handlers):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.Try):
                    continue          # nested guard — found at its own level
                for sub in ast.walk(stmt):
                    if (isinstance(sub, ast.Call)
                            and getattr(sub.func, "attr",
                                        getattr(sub.func, "id", ""))
                            == "start_hub_watchdog"):
                        return node
        self.fail("start_hub_watchdog try block not found")

    def test_watchdog_failure_logs_warning(self):
        """F4: the guard must not be a silent `except Exception: pass` —
        the failure path emits the real WARNING line. Executes the ACTUAL
        try/except block from module_main.py (extracted via AST) with a
        raising start_hub_watchdog and asserts on the queue_message
        mock's call args, matching the real format string."""
        node = self._watchdog_try_node()
        code = compile(ast.unparse(node), "<hub-watchdog-block>", "exec")
        qm = mock.MagicMock()
        with mock.patch("modules.module_mic.start_hub_watchdog",
                        side_effect=RuntimeError("boom")):
            exec(code, {"queue_message": qm})
        qm.assert_called_once_with(
            "WARNING: Audio hub watchdog not started: boom")


class TestStartupInitializationBatteryWiring(unittest.TestCase):
    """S-M2-06 audit follow-up: the INA260 boot-current sampler in
    servoctl only runs when servoctl.battery_module is set, and
    set_battery_module was only ever called from tests — module_main has
    its own battery_module global and nothing bridged the two, so the
    sampler was dead code in production."""

    def test_startup_initialization_wires_battery_into_servoctl(self):
        src = _src("src/modules/module_main.py")
        self.assertIn("set_battery_module(battery_module)", src,
                      "startup_initialization must wire module_main's "
                      "battery_module global into servoctl via "
                      "set_battery_module")
        # and it must happen BEFORE initialize_servos() in
        # startup_initialization
        fn_start = src.index("def startup_initialization")
        wire_at = src.index("set_battery_module(battery_module)", fn_start)
        init_at = src.index("initialize_servos()", fn_start)
        self.assertLess(wire_at, init_at,
                        "battery must be wired into servoctl BEFORE "
                        "initialize_servos() — the boot soft-start samples "
                        "INA260 current during servo init")


class TestAppStartBlasEnv(unittest.TestCase):
    def _lines(self):
        return _src("App-Start.py").splitlines()

    def test_blas_env_vars_set_via_setdefault(self):
        src = _src("App-Start.py")
        self.assertIn('os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")',
                      src, "App-Start.py must set OPENBLAS_NUM_THREADS=1 "
                      "via os.environ.setdefault (never override a "
                      "user-set value)")
        self.assertIn('os.environ.setdefault("OMP_NUM_THREADS", "1")', src,
                      "App-Start.py must set OMP_NUM_THREADS=1 via "
                      "os.environ.setdefault")

    def test_blas_env_set_before_app_launch(self):
        lines = self._lines()
        setdefault_idx = [i for i, l in enumerate(lines)
                          if "OPENBLAS_NUM_THREADS" in l
                          or "OMP_NUM_THREADS" in l]
        launch_idx = [i for i, l in enumerate(lines)
                      if "python app.py" in l]
        self.assertTrue(setdefault_idx,
                        "no BLAS env assignments found in App-Start.py")
        self.assertTrue(launch_idx,
                        "no app.py launch site found in App-Start.py")
        self.assertLess(max(setdefault_idx), min(launch_idx),
                        "the BLAS env setdefault calls must appear before "
                        "the app.py launch line — numpy reads them at "
                        "import, so setting them inside app.py is too late")

    def test_app_launch_inherits_parent_env(self):
        """The venv Python is launched via subprocess without an explicit
        env=, so the parent's os.environ (with the BLAS vars set above)
        propagates into the child."""
        lines = self._lines()
        launch_idx = [i for i, l in enumerate(lines)
                      if "python app.py" in l]
        self.assertTrue(launch_idx)
        for i in launch_idx:
            # the launch call itself is the subprocess.run line that
            # follows the command-building f-string line
            for j in range(i, min(i + 4, len(lines))):
                if "subprocess.run" in lines[j]:
                    self.assertNotIn("env=", lines[j],
                                     f"line {j + 1}: the app.py launch must "
                                     "not pass an explicit env= — the BLAS "
                                     "vars set in the parent must reach the "
                                     "child")
                    break


if __name__ == "__main__":
    unittest.main()
