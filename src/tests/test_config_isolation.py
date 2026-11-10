"""Regression tests: the suite must be isolated from the ambient src/config.ini.

Defect (seen on the robot): module_config.load_config() resolves its config
path as ``os.environ.get('TARS_CONFIG_PATH') or <src>/config.ini``.  When the
suite ran without TARS_CONFIG_PATH set, every load_config() call — including
the import-time one in module_tts — read whatever machine-specific
src/config.ini happened to exist, so the suite passed on CI and dev machines
but failed on the robot (whose config.ini carries a customised
tts_fallback_list).

The session bootstrap lives in conftest.py: it copies the tracked
config.ini.template into a temp dir and points TARS_CONFIG_PATH at it before
any test module (and therefore any ``modules.*`` import) runs.  These tests
pin that contract.
"""
import configparser
import os
import unittest

from modules import module_config  # noqa: E402

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_AMBIENT = os.path.join(_SRC, "config.ini")
_TEMPLATE = os.path.join(_SRC, "config.ini.template")

# The customised line exactly as it exists on the robot's real src/config.ini
# — the value that made the suite fail there.
_TEMPLATE_LINE = "tts_fallback_list = external,piper,espeak"
_ROBOT_LINE = "tts_fallback_list = speechify,elevenlabs,external,piper,espeak"
_TEMPLATE_FALLBACK = ["external", "piper", "espeak"]


def _customised_ambient_text():
    """Template text with the robot's customised tts_fallback_list line."""
    with open(_TEMPLATE, encoding="utf-8") as fh:
        text = fh.read()
    assert _TEMPLATE_LINE in text, "template lost its tts_fallback_list line"
    return text.replace(_TEMPLATE_LINE, _ROBOT_LINE)


class TestSessionConfigIsolation(unittest.TestCase):
    def test_tars_config_path_points_at_temp_copy_not_ambient_config(self):
        path = os.environ.get("TARS_CONFIG_PATH")
        self.assertTrue(path, "conftest must set TARS_CONFIG_PATH for the session")
        self.assertTrue(os.path.isfile(path), f"TARS_CONFIG_PATH={path} is not a file")
        self.assertNotEqual(
            os.path.realpath(path), os.path.realpath(_AMBIENT),
            "TARS_CONFIG_PATH must not point at the ambient src/config.ini")
        parser = configparser.ConfigParser()
        parser.read(path)
        self.assertEqual(parser.get("TTS", "tts_fallback_list"),
                         "external,piper,espeak")

    def test_load_config_reads_template_with_customised_ambient_config(self):
        # Reproduce the robot: a customised src/config.ini exists on disk.
        backup = None
        if os.path.exists(_AMBIENT):
            with open(_AMBIENT, encoding="utf-8") as fh:
                backup = fh.read()
        with open(_AMBIENT, "w", encoding="utf-8") as fh:
            fh.write(_customised_ambient_text())

        def _restore():
            if backup is None:
                if os.path.exists(_AMBIENT):
                    os.remove(_AMBIENT)
            else:
                with open(_AMBIENT, "w", encoding="utf-8") as fh:
                    fh.write(backup)
        self.addCleanup(_restore)

        # Sanity: the hazardous scenario is really installed — the ambient
        # file on disk now carries the robot's customised fallback list.
        parser = configparser.ConfigParser()
        parser.read(_AMBIENT)
        self.assertEqual(parser.get("TTS", "tts_fallback_list"),
                         "speechify,elevenlabs,external,piper,espeak")

        # load_config() must still return the TEMPLATE defaults.
        old_cache = module_config._config_cache
        module_config._config_cache = None
        self.addCleanup(setattr, module_config, "_config_cache", old_cache)
        cfg = module_config.load_config()
        self.assertEqual(cfg["TTS"].tts_fallback_list, _TEMPLATE_FALLBACK)


class TestBargeinKeyDefaults(unittest.TestCase):
    """S-M3-01: the three new [STT] barge-in keys resolve from the template."""

    def test_bargein_key_defaults(self):
        old_cache = module_config._config_cache
        module_config._config_cache = None
        self.addCleanup(setattr, module_config, "_config_cache", old_cache)
        cfg = module_config.load_config()
        stt = cfg["STT"]
        self.assertEqual(stt["bargein_bleed_factor"], 2.5)
        self.assertEqual(stt["bargein_min_candidates"], 2)
        self.assertIs(stt["bargein_listen_after_interrupt"], True)


if __name__ == "__main__":
    unittest.main()
