"""M2-S1 Home Assistant standard-skill tests.

Safety: NO HA POSTs anywhere — mocked urlopen + real-subprocess error
paths that exit BEFORE any socket.
"""
import configparser
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_HA_SCRIPT = os.path.join(_SRC, "agent_skills_defaults", "home-assistant",
                          "scripts", "ha_rest.py")


def _load_ha_module():
    """Load ha_rest.py by file path (directory name has a hyphen)."""
    spec = importlib.util.spec_from_file_location("ha_rest", _HA_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── canned fixtures ──────────────────────────────────────────────

def _states_fixture(domains=5, ents_per=10):
    states = []
    for d in range(domains):
        domain = f"sensor_{d}"
        for e in range(ents_per):
            eid = f"{domain}.entity_{e}"
            states.append({
                "entity_id": eid,
                "state": str(e),
                "attributes": {"friendly_name": f"Entity {d}-{e}"},
            })
    return states


def _huge_states_fixture(domains=50, ents_per=100):
    states = []
    for d in range(domains):
        domain = f"very_long_domain_name_{d:03d}"
        for e in range(ents_per):
            eid = f"{domain}.entity_{e:05d}"
            states.append({
                "entity_id": eid,
                "state": "42",
                "attributes": {
                    "friendly_name": f"Very_Long_Entity_Name_{d:03d}_{e:05d}"
                },
            })
    return states


# ══════════════════════════════════════════════════════════════════
#  TestHaRestScript — 19 tests (mocked-urlopen unit tests +
#  real-subprocess error paths)
# ══════════════════════════════════════════════════════════════════

class TestHaRestScript(unittest.TestCase):
    """M2-S1: ha_rest.py contract (D6)."""

    _sentinel_token = "m2-s1-test-token-NEVER-ECHOED"
    _ha_url = "http://192.168.1.50:8123"

    @classmethod
    def setUpClass(cls):
        cls._ha_mod = _load_ha_module()

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="tars-m2s1-")
        self._tmp_config = os.path.join(self._tmp_dir, "config.ini")
        os.environ["HA_TOKEN"] = self._sentinel_token
        os.environ["HA_URL"] = self._ha_url

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _write_config(self, url=None):
        cp = "[SKILL_STD:home-assistant]\n"
        if url is not None:
            cp += f"url = {url}\n"
        else:
            cp += "# no url set\n"
        with open(self._tmp_config, "w") as fh:
            fh.write(cp)

    def _run_main(self, argv, **extra_env):
        """Call ha_mod.main() with mocked sys.argv, capturing stdout/stderr.
        Returns (exit_code, stdout, stderr)."""
        saved_env = {}
        for key, val in extra_env.items():
            saved_env[key] = os.environ.get(key)
            os.environ[key] = val
        try:
            saved_argv = sys.argv
            sys.argv = ["ha_rest.py"] + list(argv)
            captured_stdout = io.StringIO()
            captured_stderr = io.StringIO()
            with mock.patch("sys.stdout", captured_stdout), \
                 mock.patch("sys.stderr", captured_stderr):
                try:
                    self._ha_mod.main()
                    return (0, captured_stdout.getvalue(),
                            captured_stderr.getvalue())
                except SystemExit as e:
                    return (e.code if e.code is not None else 1,
                            captured_stdout.getvalue(),
                            captured_stderr.getvalue())
        finally:
            sys.argv = saved_argv
            for key, val in saved_env.items():
                if val is not None:
                    os.environ[key] = val
                else:
                    os.environ.pop(key, None)

    # ── test 5: list_entities caps ──────────────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_list_entities_caps(self, mock_req_cls, mock_urlopen_cls):
        """Huge fixture → per-domain counts + ≤3 names ≤8 domains ≤600."""
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=cm)
        cm.__exit__ = mock.MagicMock(return_value=False)
        cm.status = 200
        cm.read.return_value = json.dumps(
            _huge_states_fixture(50, 100)).encode("utf-8")
        cm.getheader.return_value = "application/json"
        mock_urlopen_cls.return_value = cm

        ec, out, err = self._run_main(["list_entities"])
        self.assertEqual(ec, 0, err)
        self.assertLessEqual(len(out.strip()), 600,
                             f"stdout {len(out.strip())} > 600: {out!r}")
        self.assertIn(":", out)
        parts = out.strip().split("; ")
        self.assertLessEqual(len(parts), 8, f"capped at 8: got {len(parts)}")
        self.assertNotIn('"entity_id"', out)
        self.assertNotIn('"state"', out)

    # ── test 6: get_state shape + cap ───────────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_get_state_shape_and_cap(self, mock_req_cls, mock_urlopen_cls):
        """get_state format + ≤300 chars even with huge attributes."""
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=cm)
        cm.__exit__ = mock.MagicMock(return_value=False)
        cm.status = 200
        cm.read.return_value = json.dumps({
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {
                "friendly_name": "Kitchen Light",
                "brightness": 200,
                "color_temp": 370,
                "extra_key": "x" * 500,
            },
        }).encode("utf-8")
        cm.getheader.return_value = "application/json"
        mock_urlopen_cls.return_value = cm

        ec, out, err = self._run_main(["get_state", "light.kitchen"])
        self.assertEqual(ec, 0, err)
        self.assertLessEqual(len(out.strip()), 300,
                             f"stdout {len(out.strip())} > 300: {out!r}")
        self.assertIn("light.kitchen: on", out)
        self.assertIn("Kitchen Light", out)
        self.assertNotIn("friendly_name:", out)

    # ── test 7: list_services caps ──────────────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_list_services_caps(self, mock_req_cls, mock_urlopen_cls):
        """≤10 domains × ≤6 services, ≤600 chars."""
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=cm)
        cm.__exit__ = mock.MagicMock(return_value=False)
        cm.status = 200
        svcs = []
        for d in range(20):
            s = {f"s{i}": {} for i in range(10)}
            svcs.append({"domain": f"d{d}", "services": s})
        cm.read.return_value = json.dumps(svcs).encode("utf-8")
        cm.getheader.return_value = "application/json"
        mock_urlopen_cls.return_value = cm

        ec, out, err = self._run_main(["list_services"])
        self.assertEqual(ec, 0, err)
        self.assertLessEqual(len(out.strip()), 600)
        parts = out.strip().split("; ")
        self.assertLessEqual(len(parts), 10,
                             f"capped at 10 domains: got {len(parts)}")

    # ── test 8: call_service summary counts ─────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_call_service_summary_counts_not_dumps(self, mock_req_cls,
                                                    mock_urlopen_cls):
        """25 changed → summary, ≤cap, no raw JSON."""
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=cm)
        cm.__exit__ = mock.MagicMock(return_value=False)
        cm.status = 200
        cm.read.return_value = json.dumps(
            [{"entity_id": f"light.{i}"} for i in range(25)]
        ).encode("utf-8")
        cm.getheader.return_value = "application/json"
        mock_urlopen_cls.return_value = cm

        ec, out, err = self._run_main(
            ["call_service", "", "light", "turn_on", ""])
        self.assertEqual(ec, 0, err)
        self.assertIn("25 entities changed", out)
        self.assertNotIn("[", out)
        self.assertLessEqual(len(out.strip()), 300)

    # ── test 9: call_service merges entity_id ───────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_call_service_merges_entity_id_into_data(self, mock_req_cls,
                                                      mock_urlopen_cls):
        """entity_id merged when provided + data key wins."""
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=cm)
        cm.__exit__ = mock.MagicMock(return_value=False)
        cm.status = 200
        cm.read.return_value = json.dumps([]).encode("utf-8")
        cm.getheader.return_value = "application/json"
        mock_urlopen_cls.return_value = cm

        ec, out, err = self._run_main(
            ["call_service", "light.kitchen", "light", "turn_on", ""])
        self.assertEqual(ec, 0, err)
        # data is a keyword argument to Request
        kwargs = mock_req_cls.call_args.kwargs
        sent_body = json.loads(kwargs["data"].decode("utf-8")
                               if isinstance(kwargs["data"], bytes)
                               else kwargs["data"])
        self.assertEqual(sent_body.get("entity_id"), "light.kitchen")

        mock_req_cls.reset_mock()
        cm.read.return_value = json.dumps([]).encode("utf-8")
        ec, out, err = self._run_main(
            ["call_service", "light.a", "light", "turn_on",
             '{"entity_id": "light.b", "brightness": 50}'])
        self.assertEqual(ec, 0, err)
        kwargs = mock_req_cls.call_args.kwargs
        sent_body = json.loads(kwargs["data"].decode("utf-8")
                               if isinstance(kwargs["data"], bytes)
                               else kwargs["data"])
        self.assertEqual(sent_body.get("entity_id"), "light.b")
        self.assertEqual(sent_body.get("brightness"), 50)

    # ── test 10: url precedence section over env ────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_url_precedence_section_over_env(self, mock_req_cls,
                                              mock_urlopen_cls):
        """Section wins over HA_URL env."""
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=cm)
        cm.__exit__ = mock.MagicMock(return_value=False)
        cm.status = 200
        cm.read.return_value = json.dumps([]).encode("utf-8")
        cm.getheader.return_value = "application/json"
        mock_urlopen_cls.return_value = cm

        section_url = "http://10.0.0.99:8123"
        self._write_config(url=section_url)

        ec, out, err = self._run_main(
            ["list_entities"],
            TARS_CONFIG_PATH=self._tmp_config)
        self.assertEqual(ec, 0, err)
        url_sent = mock_req_cls.call_args[0][0] if mock_req_cls.call_args[0] else mock_req_cls.call_args.kwargs.get("url", "")
        self.assertIn(section_url, url_sent)
        self.assertNotIn(self._ha_url, url_sent)

    # ── test 11: url fallback HA_URL env ────────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_url_fallback_ha_url_env(self, mock_req_cls, mock_urlopen_cls):
        """No section → HA_URL used."""
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=cm)
        cm.__exit__ = mock.MagicMock(return_value=False)
        cm.status = 200
        cm.read.return_value = json.dumps([]).encode("utf-8")
        cm.getheader.return_value = "application/json"
        mock_urlopen_cls.return_value = cm

        self._write_config(url=None)
        ec, out, err = self._run_main(
            ["list_entities"],
            TARS_CONFIG_PATH=self._tmp_config)
        self.assertEqual(ec, 0, err)
        url_sent = mock_req_cls.call_args[0][0] if mock_req_cls.call_args[0] else mock_req_cls.call_args.kwargs.get("url", "")
        self.assertIn(self._ha_url, url_sent)

    # ── test 12: url missing exit1 ──────────────────────────────

    def test_url_missing_exit1_message(self):
        """Neither config nor env → exit 1 with message."""
        ec, out, err = self._run_main(
            ["list_entities"],
            HA_URL="")
        self.assertNotEqual(ec, 0)
        self.assertIn("Home Assistant URL not configured", err)
        self.assertIn("SKILL_STD:home-assistant", err)
        self.assertIn("HA_URL", err)

    # ── test 13: token never echoed ─────────────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_token_never_echoed(self, mock_req_cls, mock_urlopen_cls):
        """Sentinel token absent from ALL stdout/stderr across ALL four
        actions AND the error paths (M2 audit IA-F1/IB-F1: the delivered
        version covered only 2 success-path actions — a token appended to
        a 401 message passed the suite)."""
        import urllib.error

        action_argv = {
            "list_entities": ["list_entities"],
            "get_state": ["get_state", "light.kitchen"],
            "list_services": ["list_services"],
            "call_service": ["call_service", "light.kitchen", "light",
                             "turn_on", "{}"],
        }
        success_payload = {
            "list_entities": _states_fixture(2, 2),
            "get_state": {"entity_id": "light.kitchen", "state": "on",
                          "attributes": {"friendly_name": "Kitchen"}},
            "list_services": [{"domain": "light",
                               "services": {"turn_on": {},
                                            "turn_off": {}}}],
            "call_service": [],
        }

        def _cm(payload):
            cm = mock.MagicMock()
            cm.__enter__ = mock.MagicMock(return_value=cm)
            cm.__exit__ = mock.MagicMock(return_value=False)
            cm.status = 200
            cm.read.return_value = json.dumps(payload).encode("utf-8")
            cm.getheader.return_value = "application/json"
            return cm

        def _check(action, extra_env=None):
            ec, out, err = self._run_main(action_argv[action],
                                          **(extra_env or {}))
            self.assertNotIn(self._sentinel_token, out,
                             f"token leaked on stdout for {action}")
            self.assertNotIn(self._sentinel_token, err,
                             f"token leaked on stderr for {action}")
            return ec

        for action in action_argv:
            mock_urlopen_cls.side_effect = None
            mock_urlopen_cls.return_value = _cm(success_payload[action])
            with self.subTest(action=action, case="success"):
                self.assertEqual(_check(action), 0)
            mock_urlopen_cls.side_effect = urllib.error.HTTPError(
                "http://ha.local/api/states", 401, "Unauthorized", {},
                io.BytesIO(b""))
            with self.subTest(action=action, case="http401"):
                self.assertNotEqual(_check(action), 0)
            mock_urlopen_cls.side_effect = urllib.error.URLError(
                "connection refused")
            with self.subTest(action=action, case="urlerror"):
                self.assertNotEqual(_check(action), 0)
            mock_urlopen_cls.side_effect = None
            bad = _cm([])
            bad.read.return_value = b"<html>not json</html>"
            mock_urlopen_cls.return_value = bad
            with self.subTest(action=action, case="nonjson"):
                self.assertNotEqual(_check(action), 0)
        # missing-token exit (URL resolves via setUp HA_URL)
        mock_urlopen_cls.side_effect = None
        with self.subTest(case="missing-token"):
            ec, out, err = self._run_main(["list_entities"], HA_TOKEN="")
            self.assertNotEqual(ec, 0)
            self.assertNotIn(self._sentinel_token, out + err)
        # missing-URL exit with the token PRESENT — it must not leak
        self._write_config(url=None)
        with self.subTest(case="missing-url"):
            ec, out, err = self._run_main(
                ["list_entities"], HA_URL="",
                TARS_CONFIG_PATH=self._tmp_config)
            self.assertNotEqual(ec, 0)
            self.assertNotIn(self._sentinel_token, out + err)

    # ── test 14: token missing exit1 ────────────────────────────

    def test_token_missing_exit1_message(self):
        """No HA_TOKEN → exit 1 with exact message."""
        ec, out, err = self._run_main(
            ["list_entities"],
            HA_TOKEN="")
        self.assertNotEqual(ec, 0)
        self.assertEqual(err.strip(),
                         "HA_TOKEN not set (add it to .env)")

    # ── test 15: empty args tolerated ───────────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_empty_args_tolerated(self, mock_req_cls, mock_urlopen_cls):
        """Empty-string args behave as absent."""
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=cm)
        cm.__exit__ = mock.MagicMock(return_value=False)
        cm.status = 200
        cm.read.return_value = json.dumps(_states_fixture(1, 1)).encode(
            "utf-8")
        cm.getheader.return_value = "application/json"
        mock_urlopen_cls.return_value = cm

        ec, out, err = self._run_main(
            ["list_entities", "", "", "", ""])
        self.assertEqual(ec, 0, err)

    # ── test 16: unknown action usage exit1 ─────────────────────

    def test_unknown_action_usage_exit1(self):
        """Unknown action → exit 1, usage on stderr."""
        ec, out, err = self._run_main(["bogus_action"])
        self.assertNotEqual(ec, 0)
        self.assertIn("usage:", err.lower())

    # ── test 17: data invalid JSON exit1 ────────────────────────

    def test_data_invalid_json_exit1(self):
        """Non-JSON data → exit 1 with exact message."""
        ec, out, err = self._run_main(
            ["call_service", "", "light", "turn_on", "not-json"])
        self.assertNotEqual(ec, 0)
        self.assertEqual(err.strip(),
                         "data must be a JSON object string")

    # ── test 18: http error one-liner ───────────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_http_error_one_liner(self, mock_req_cls, mock_urlopen_cls):
        """HTTP 401 → one-line msg with hint; 404 → code only."""
        import urllib.error as _ue
        for code, expect_unauth in ((401, True), (404, False)):
            with self.subTest(code=code):
                fp = mock.MagicMock()
                fp.read.return_value = b"{}"

                def _raise(*a, **kw):
                    raise _ue.HTTPError("url", code, "msg", {}, fp)

                mock_urlopen_cls.side_effect = _raise
                ec, out, err = self._run_main(["list_entities"])
                self.assertNotEqual(ec, 0)
                self.assertIn(f"Home Assistant HTTP {code}", err)
                if expect_unauth:
                    self.assertIn("unauthorized", err.lower())
                    self.assertIn("HA_TOKEN", err)
                self.assertEqual(len(err.strip().splitlines()), 1,
                                 f"expected 1 line, got: {err!r}")
                mock_urlopen_cls.side_effect = None

    # ── test 19: url error host only ────────────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_url_error_host_only(self, mock_req_cls, mock_urlopen_cls):
        """URLError → host ONLY, no ://, no userinfo, no path."""
        import urllib.error as _ue

        def _raise(*a, **kw):
            raise _ue.URLError("urlopen error [Errno 61] Connection refused")

        mock_urlopen_cls.side_effect = _raise
        ec, out, err = self._run_main(["list_entities"])
        self.assertNotEqual(ec, 0)
        self.assertNotIn("://", err)
        self.assertNotIn("/api/", err)

    # ── test 20: JSON decode failure ────────────────────────────

    @mock.patch("urllib.request.urlopen")
    @mock.patch("urllib.request.Request")
    def test_json_decode_failure_message(self, mock_req_cls,
                                          mock_urlopen_cls):
        """Non-JSON 200 body → proper error."""
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=cm)
        cm.__exit__ = mock.MagicMock(return_value=False)
        cm.status = 200
        cm.read.return_value = b"<html>Not JSON</html>"
        cm.getheader.return_value = "text/html"
        mock_urlopen_cls.return_value = cm

        ec, out, err = self._run_main(["list_entities"])
        self.assertNotEqual(ec, 0)
        self.assertIn("Unexpected response from Home Assistant", err)

    # ── tests 21-23: REAL subprocess ERROR paths ────────────────

    def test_real_subprocess_missing_token_exit1(self):
        """Real subprocess, no token → exits before socket."""
        proc = subprocess.run(
            [sys.executable, _HA_SCRIPT, "list_entities"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HA_TOKEN": "",
                 "HA_URL": self._ha_url},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr.strip(),
                         "HA_TOKEN not set (add it to .env)")

    def test_real_subprocess_unknown_action_usage(self):
        """Real subprocess, unknown action → exit ≠0 + usage."""
        proc = subprocess.run(
            [sys.executable, _HA_SCRIPT, "bogus"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HA_TOKEN": "x",
                 "HA_URL": self._ha_url},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("usage:", proc.stderr.lower())

    def test_real_subprocess_url_self_sourcing(self):
        """TARS_CONFIG_PATH branch self-sources the URL: token absent and
        NO HA_URL fallback, so the token error (not the URL error) proves
        the URL resolved from the temp config. BRANCH-SENSITIVE (M2 audit
        IB-F5): deleting the TARS_CONFIG_PATH branch falls to the default
        config (no HA url in the suite environment) and then to the empty
        HA_URL → URL error → this test fails. URL is resolved before the
        token check in _request (IB-F5/F6 fix)."""
        self._write_config(url=self._ha_url)
        proc = subprocess.run(
            [sys.executable, _HA_SCRIPT, "list_entities"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ,
                 "HA_TOKEN": "",
                 "HA_URL": "",
                 "TARS_CONFIG_PATH": self._tmp_config},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("HA_TOKEN not set", proc.stderr)
        self.assertNotIn("URL not configured", proc.stderr)
        # Source pin: parents[3] / "config.ini" expression present
        with open(_HA_SCRIPT, "r") as fh:
            source = fh.read()
        self.assertIn('parents[3] / "config.ini"', source)
        # Structural pin (IB-F6): the default-config branch is the ELSE of
        # the TARS_CONFIG_PATH check — no fall-through when it is set.
        self.assertRegex(source, r"if config_path:[\s\S]*?\n    else:")