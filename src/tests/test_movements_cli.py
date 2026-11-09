"""app-movements.py CLI tests — real subprocess runs, real exit codes.

The CLI operates on a temp store via the TARS_MOVEMENTS_STORE override
(falls back to src/movements.json); TARS_CONFIG_PATH points at a temp
config so the CLI never touches repo state.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CLI = os.path.join(_REPO, "src", "app-movements.py")
_SRC = os.path.join(_REPO, "src")


class _CliTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_path = os.path.join(self.tmp, "movements.json")
        shutil.copyfile(os.path.join(_SRC, "config.ini.template"),
                        os.path.join(self.tmp, "config.ini"))
        self.env = dict(os.environ)
        self.env["TARS_CONFIG_PATH"] = os.path.join(self.tmp, "config.ini")
        self.env["TARS_MOVEMENTS_STORE"] = self.store_path

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, _CLI] + list(args),
            cwd=_REPO, env=self.env,
            capture_output=True, text=True, timeout=60)

    def seed(self):
        """Seed the temp store from defaults (like first app start)."""
        result = self.run_cli("reset")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _read_store(self):
        with open(self.store_path) as fh:
            return json.load(fh)

    def _write_store(self, data):
        with open(self.store_path, "w") as fh:
            json.dump(data, fh, indent=2)


class TestMovementsCli(_CliTestCase):
    def test_list_shows_33(self):
        self.seed()
        result = self.run_cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 33)
        self.assertIn("step_forward", result.stdout)
        # id<tab>type<tab>name per line
        parts = lines[0].split("\t")
        self.assertEqual(len(parts), 3)

    def test_show_unknown_exits_1(self):
        self.seed()
        result = self.run_cli("show", "flurb")
        self.assertEqual(result.returncode, 1)
        self.assertIn("flurb", result.stderr)

    def test_validate_clean_store_exits_0(self):
        self.seed()
        result = self.run_cli("validate")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_validate_reports_bad_entry(self):
        self.seed()
        data = self._read_store()
        data["movements"]["bow"]["steps"][0]["speed"] = 42  # out of range
        self._write_store(data)
        result = self.run_cli("validate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("bow", result.stderr)
        # validate never touches the file
        with open(self.store_path) as fh:
            self.assertEqual(json.load(fh), data)

    def test_reset_roundtrip(self):
        self.seed()
        data = self._read_store()
        del data["movements"]["bow"]
        self._write_store(data)
        self.assertEqual(self.run_cli("show", "bow").returncode, 1)
        result = self.run_cli("reset")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_cli("show", "bow").returncode, 0)

    def test_edit_rejects_invalid_entry(self):
        self.seed()
        editor = os.path.join(self.tmp, "bad_editor.sh")
        with open(editor, "w") as fh:
            fh.write('#!/bin/sh\necho \'{"bad": 1}\' > "$1"\n')
        os.chmod(editor, 0o755)
        self.env["EDITOR"] = editor
        result = self.run_cli("edit", "bow")
        self.assertEqual(result.returncode, 1)
        self.assertIn("bow", result.stderr)
        # store unchanged
        result2 = self.run_cli("show", "bow")
        self.assertEqual(result2.returncode, 0)

    def test_edit_updates_entry(self):
        self.seed()
        editor = os.path.join(self.tmp, "good_editor.sh")
        with open(editor, "w") as fh:
            fh.write('#!/bin/sh\n'
                     'python3 - "$1" <<\'EOF\'\n'
                     'import json, sys\n'
                     'path = sys.argv[1]\n'
                     'entry = json.load(open(path))\n'
                     'entry["description"] = "edited via CLI test"\n'
                     'json.dump(entry, open(path, "w"), indent=2)\n'
                     'EOF\n')
        os.chmod(editor, 0o755)
        self.env["EDITOR"] = editor
        result = self.run_cli("edit", "bow")
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = self.run_cli("show", "bow")
        self.assertEqual(shown.returncode, 0)
        self.assertIn("edited via CLI test", shown.stdout)

    def test_import_rejects_bad_file(self):
        self.seed()
        bad = os.path.join(self.tmp, "bad_import.json")
        with open(bad, "w") as fh:
            json.dump({"version": 1, "movements": {
                "ok_move": {"name": "OK", "type": "legs_only",
                            "description": "fine",
                            "steps": [{"left_height": 50, "right_height": 50,
                                       "left_leg": 50, "right_leg": 50,
                                       "left_main": None, "left_forearm": None,
                                       "left_hand": None, "right_main": None,
                                       "right_forearm": None,
                                       "right_hand": None, "speed": 0.9,
                                       "hold_time": 0.0, "movement": None}],
                            "steps_arms": None},
                "bad_move": {"name": "Bad", "type": "flurb",
                             "description": "nope", "steps": [],
                             "steps_arms": None}}}, fh)
        result = self.run_cli("import", bad)
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad_move", result.stderr)
        # all-or-nothing: nothing imported
        self.assertEqual(self.run_cli("show", "ok_move").returncode, 1)

    def test_export_import_roundtrip(self):
        self.seed()
        export = os.path.join(self.tmp, "export.json")
        result = self.run_cli("export", export)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(export))
        # import into a fresh store reproduces the pool
        self.env["TARS_MOVEMENTS_STORE"] = os.path.join(self.tmp, "other.json")
        result = self.run_cli("import", export)
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = self.run_cli("list")
        self.assertEqual(len([ln for ln in listed.stdout.splitlines()
                              if ln.strip()]), 33)


if __name__ == "__main__":
    unittest.main()
