"""Session bootstrap: isolate the test suite from the ambient src/config.ini.

module_config.load_config() resolves its config path as
``os.environ.get('TARS_CONFIG_PATH') or <src>/config.ini``.  ``src/config.ini``
is gitignored and machine-specific (on the robot it carries a customised
tts_fallback_list), so a suite run without TARS_CONFIG_PATH reads whatever
happens to be on the machine: it passed on CI and dev machines but failed on
the robot.

This conftest is imported by pytest before any test module — and therefore
before any ``modules.*`` import at collection time — and points
TARS_CONFIG_PATH at a temp copy of the tracked template, so every
load_config() call in every test reads the template defaults, never the
ambient src/config.ini.

Tests that set their own TARS_CONFIG_PATH in their own setup (or at module
import, like test_tts_config.py) run after this bootstrap and simply override
the session default.
"""
import os
import shutil
import tempfile

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TEMPLATE = os.path.join(_SRC, "config.ini.template")

if not os.path.isfile(_TEMPLATE):
    # Fail loudly rather than silently falling back to the ambient
    # src/config.ini (machine-specific, gitignored).
    raise RuntimeError(
        f"src/config.ini.template not found at {_TEMPLATE} — the test suite "
        "needs it to build an isolated config. Refusing to fall back to the "
        "ambient src/config.ini, which is machine-specific."
    )

_TMP_DIR = tempfile.mkdtemp(prefix="tars-test-config-")
_TMP_INI = os.path.join(_TMP_DIR, "config.ini")
shutil.copyfile(_TEMPLATE, _TMP_INI)
os.environ["TARS_CONFIG_PATH"] = _TMP_INI
