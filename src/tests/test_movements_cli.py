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

    def test_validate_does_not_seed_missing_store(self):
        # review F7: validate is a read-only diagnostic — it must never
        # create movements.json when absent
        self.assertFalse(os.path.exists(self.store_path))
        result = self.run_cli("validate")
        self.assertFalse(os.path.exists(self.store_path),
                         "validate must not create the store file")
        self.assertIn("no store file", result.stderr)
        self.assertEqual(result.returncode, 1)

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


def _entry(name="New Move"):
    return {"name": name, "type": "legs_only",
            "description": "a movement used by the CLI merge tests",
            "steps": [{"left_height": 50, "right_height": 50,
                       "left_leg": 50, "right_leg": 50,
                       "left_main": None, "left_forearm": None,
                       "left_hand": None, "right_main": None,
                       "right_forearm": None, "right_hand": None,
                       "speed": 0.9, "hold_time": 0.0, "movement": None}],
            "steps_arms": None}


def _pack(movements):
    import tempfile as _tempfile
    fd, path = _tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w") as fh:
        json.dump({"version": 1, "movements": movements}, fh)
    return path


class TestMovementsMergeCli(_CliTestCase):
    """`merge <path> [skip]` — union import with a collision policy."""

    def _mixed_pack(self):
        self.seed()
        bow = self._read_store()["movements"]["bow"]
        colliding = dict(bow)
        colliding["name"] = "CLI Bow"
        return _pack({"cli_new_one": _entry("New One"),
                      "cli_new_two": _entry("New Two"),
                      "bow": colliding})

    def test_merge_reports_counts_and_updates_store(self):
        pack = self._mixed_pack()
        result = self.run_cli("merge", pack)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            f"Merged 3 movement(s) from {pack} "
            f"(added 2, overwritten 1, skipped 0)")
        store = self._read_store()["movements"]
        self.assertIn("cli_new_one", store)
        self.assertIn("cli_new_two", store)
        self.assertEqual(store["bow"]["name"], "CLI Bow")

    def test_merge_skip_flag_keeps_local_versions(self):
        pack = self._mixed_pack()
        result = self.run_cli("merge", pack, "skip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            f"Merged 2 movement(s) from {pack} "
            f"(added 2, overwritten 0, skipped 1)")
        store = self._read_store()["movements"]
        self.assertEqual(store["bow"]["name"], "Bow")
        for entry in store.values():
            self.assertNotEqual(entry["name"], "CLI Bow")

    def test_merge_unknown_flag_shows_usage_and_exits_1(self):
        pack = self._mixed_pack()
        before = self._read_store()
        result = self.run_cli("merge", pack, "skyp")
        self.assertEqual(result.returncode, 1)
        self.assertIn("merge <path> [skip]", result.stderr)
        # all-or-nothing: nothing ran — the store file is untouched
        self.assertEqual(self._read_store(), before)

    def test_merge_malformed_file_clean_error_no_traceback(self):
        self.seed()
        bad = os.path.join(self.tmp, "garbage.json")
        with open(bad, "w") as fh:
            fh.write("{ this is not json")
        result = self.run_cli("merge", bad)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertTrue(result.stderr.strip().startswith(f"{bad}: "),
                        result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_merge_invalid_entry_all_or_nothing(self):
        self.seed()
        bad = _entry("Broken")
        bad["type"] = "flurb"
        pack = _pack({"pack_good": _entry("Good"), "pack_bad": bad})
        result = self.run_cli("merge", pack)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pack_bad", result.stderr)
        store = self._read_store()["movements"]
        self.assertNotIn("pack_good", store)

    def test_merge_respects_store_override_seeding(self):
        pack = _pack(json.loads(
            open(os.path.join(_SRC, "movements_defaults.json")).read()
        )["movements"])
        result = self.run_cli("merge", pack)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(self.store_path),
                        "merge must seed the override store from defaults")
        store = self._read_store()["movements"]
        self.assertEqual(set(store),
                         set(json.load(open(pack))["movements"]))

    def test_usage_documents_merge_command(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("merge <path> [skip]", result.stdout)

    def test_import_replace_stdout_and_exit_unchanged(self):
        self.seed()
        export = os.path.join(self.tmp, "export.json")
        self.assertEqual(self.run_cli("export", export).returncode, 0)
        self.env["TARS_MOVEMENTS_STORE"] = os.path.join(self.tmp, "other.json")
        result = self.run_cli("import", export)
        self.assertEqual(result.returncode, 0, result.stderr)
        # the printed result line is exactly today's contract (the seeding
        # notice line, if any, only precedes it on a fresh override path)
        self.assertEqual(result.stdout.strip().splitlines()[-1],
                         f"Imported 33 movements from {export}")


if __name__ == "__main__":
    unittest.main()
