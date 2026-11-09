"""MCP client tests (M6): raw protocol against the REAL example server
over a REAL pipe (D7 — no transport mocks), then transports, lifecycle,
descriptors, prompt block 3, CLI, and docs parity.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
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

_SERVER = os.path.join(_SRC, "mcp_example_server.py")


class _RealServerPipe(unittest.TestCase):
    """Spawn the REAL example server exactly as McpManager does and speak
    raw JSON-RPC over the pipe.  Every test closes the pipe and waits on
    the process — no orphans, stderr drained."""

    def setUp(self):
        super().setUp()
        self.proc = subprocess.Popen(
            [sys.executable, _SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._close)

    def _close(self):
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc.stderr and not self.proc.stderr.closed:
            self.proc.stderr.read()  # drain

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def read_response(self, timeout=5):
        # line-buffered read with a bounded wait
        import threading
        line_box = []

        def reader():
            line = self.proc.stdout.readline()
            line_box.append(line)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout)
        self.assertTrue(line_box and line_box[0],
                        "no response line from the server")
        return json.loads(line_box[0].strip())

    @staticmethod
    def _request(msg_id, method, params=None):
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        return msg


class TestRawProtocol(_RealServerPipe):
    """Raw JSON-RPC conformance pins (S-M6-02)."""

    def test_handshake(self):
        self.send(self._request(1, "initialize", {}))
        result = self.read_response()
        self.assertEqual(result["id"], 1)
        self.assertEqual(result["result"]["protocolVersion"],
                         "2025-06-18")
        self.assertEqual(result["result"]["serverInfo"]["name"],
                         "tars-example")
        # notifications/initialized accepted: no response, no crash
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send(self._request(2, "tools/list"))
        result2 = self.read_response()
        self.assertEqual(result2["id"], 2)  # server still alive and serving

    def test_tools_list_single_page_no_cursor(self):
        self.send(self._request(1, "tools/list"))
        result = self.read_response()
        tools = result["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools),
                         ["add_numbers", "get_time"])
        # the example server is single-page BY DESIGN: no nextCursor at all
        self.assertNotIn("nextCursor", result["result"])

    def test_call_get_time(self):
        self.send(self._request(1, "tools/call",
                                {"name": "get_time", "arguments": {}}))
        result = self.read_response()["result"]
        self.assertFalse(result["isError"])
        self.assertRegex(result["content"][0]["text"],
                         re.compile(r"It is \d{2}:\d{2}\."))

    def test_call_add_numbers_ok(self):
        self.send(self._request(1, "tools/call",
                                {"name": "add_numbers",
                                 "arguments": {"a": 2, "b": 3}}))
        result = self.read_response()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["text"], "2 + 3 = 5")

    def test_call_add_numbers_iserror(self):
        # a="x" -> a SUCCESSFUL response with isError: true (tool-execution
        # error, never a protocol error)
        self.send(self._request(1, "tools/call",
                                {"name": "add_numbers",
                                 "arguments": {"a": "x", "b": 1}}))
        response = self.read_response()
        self.assertIn("result", response)  # success at the JSON-RPC level
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"],
                         "a and b must be numbers")

    def test_unknown_method_32601(self):
        self.send(self._request(1, "no/such/method"))
        response = self.read_response()
        self.assertEqual(response["error"]["code"], -32601)

    def test_compact_json_on_stdout(self):
        # every response is a SINGLE line — no embedded newlines
        self.send(self._request(1, "tools/list"))
        raw = []
        import threading

        def reader():
            raw.append(self.proc.stdout.readline())

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(5)
        line = raw[0]
        self.assertTrue(line.endswith("\n"))
        self.assertNotIn("\n", line[:-1])  # exactly one newline, at the end
        # and it is compact (no pretty-print spacing)
        self.assertNotIn('": ', line)

    def test_notification_no_response(self):
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # a notification gets NO response — verify by following with a
        # request whose response must be the FIRST line on stdout
        self.send(self._request(7, "tools/list"))
        response = self.read_response()
        self.assertEqual(response["id"], 7)


import tempfile  # noqa: E402


class TestRegistryAndSeeding(unittest.TestCase):
    """Registry validation, template seeding, [MCP] config keys (S-M6-03)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.registry_path = os.path.join(self.tmp, "mcp_servers.json")
        self.template_path = os.path.join(_SRC, "mcp_servers.json.template")

    def _write_registry(self, servers):
        with open(self.registry_path, "w") as fh:
            json.dump({"version": 1, "servers": servers}, fh)

    def test_registry_validates_names(self):
        from modules.module_mcp import McpManager
        self._write_registry({"Bad-Name": {
            "transport": "stdio", "command": "python3", "args": [],
            "env": {}, "cwd": None, "enabled": True, "auto_start": False}})
        manager = McpManager(self.registry_path, self.template_path)
        errors = manager.load_registry()
        self.assertTrue(any("Bad-Name" in e for e in errors), errors)

    def test_registry_rejects_duplicate(self):
        from modules.module_mcp import McpManager
        manager = McpManager(self.registry_path, self.template_path)
        ok, errors = manager.add_server("example", {
            "transport": "stdio", "command": "python3", "args": [],
            "env": {}, "cwd": None, "enabled": True, "auto_start": False})
        self.assertTrue(ok, errors)
        ok, errors = manager.add_server("example", {
            "transport": "stdio", "command": "python3", "args": [],
            "env": {}, "cwd": None, "enabled": True, "auto_start": False})
        self.assertFalse(ok)
        self.assertTrue(any("already" in e.lower() for e in errors),
                        errors)

    def test_transport_must_be_stdio_or_http(self):
        from modules.module_mcp import McpManager
        self._write_registry({"weird": {
            "transport": "carrier-pigeon", "command": "x", "args": [],
            "env": {}, "cwd": None, "enabled": True, "auto_start": False}})
        manager = McpManager(self.registry_path, self.template_path)
        errors = manager.load_registry()
        self.assertTrue(any("carrier-pigeon" in e for e in errors), errors)

    def test_seed_from_template(self):
        from modules.module_mcp import McpManager
        self.assertFalse(os.path.exists(self.registry_path))
        manager = McpManager(self.registry_path, self.template_path)
        manager.seed_if_absent()
        self.assertTrue(os.path.exists(self.registry_path))
        with open(self.registry_path) as fh:
            data = json.load(fh)
        self.assertIn("example", data["servers"])
        self.assertTrue(data["servers"]["example"]["enabled"])
        # present registry is NOT re-seeded
        with open(self.registry_path, "w") as fh:
            json.dump({"version": 1, "servers": {}}, fh)
        manager.seed_if_absent()
        with open(self.registry_path) as fh:
            self.assertEqual(json.load(fh)["servers"], {})

    def test_mcp_keys_default(self):
        from modules import module_config
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        minimal = os.path.join(tmp, "config.ini")
        with open(os.path.join(_SRC, "config.ini.template")) as fh:
            text = fh.read()
        lines, skipping = [], False
        for ln in text.splitlines():
            if ln.strip() == "[MCP]":
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
        mcp = module_config.load_config().get("MCP", {})
        self.assertIs(mcp.get("enabled"), True)
        self.assertEqual(mcp.get("prompt_budget_chars"), 4000)
        self.assertEqual(mcp.get("request_timeout"), 30)
        self.assertEqual(mcp.get("health_interval_s"), 60)
        self.assertEqual(mcp.get("restart_max_attempts"), 3)


class TestStdioTransport(unittest.TestCase):
    """_StdioServer against the REAL example server (S-M6-04)."""

    def _make_server(self, entry=None):
        from modules.module_mcp import _StdioServer
        entry = entry or {"transport": "stdio", "command": sys.executable,
                          "args": [_SERVER], "env": {}, "cwd": None}
        return _StdioServer("example", entry)

    def test_stdio_roundtrip_real_server(self):
        server = self._make_server()
        self.addCleanup(server.stop)
        server.start()
        result = server._request({"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize",
                                  "params": {}})
        self.assertEqual(result["result"]["protocolVersion"], "2025-06-18")
        server._send({"jsonrpc": "2.0",
                      "method": "notifications/initialized"})
        listed = server._request({"jsonrpc": "2.0", "id": 2,
                                  "method": "tools/list"})
        self.assertEqual(sorted(t["name"] for t in listed["result"]["tools"]),
                         ["add_numbers", "get_time"])

    def test_stop_kills_process_group(self):
        server = self._make_server()
        server.start()
        server.stop()
        deadline = time.monotonic() + 3
        while server.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(server.poll(), "process did not exit")
        # no orphan: the process-group kill covered the whole tree
        check = subprocess.run(["pgrep", "-f", "mcp_example_server"],
                               capture_output=True, text=True)
        self.assertEqual(check.stdout.strip(), "",
                         f"orphan server process: {check.stdout}")

    def test_send_is_compact_single_line(self):
        from modules.module_mcp import _StdioServer
        sent = []
        server = self._make_server()
        server._proc = mock.MagicMock()
        server._proc.stdin.write = sent.append
        server._send({"jsonrpc": "2.0", "id": 1, "method": "x",
                      "params": {"a": 1}})
        self.assertEqual(len(sent), 1)
        line = sent[0]
        self.assertTrue(line.endswith("\n"))
        self.assertNotIn("\n", line[:-1])  # no embedded newlines
        self.assertNotIn('": ', line)       # compact separators


FAKE_SERVER_TEMPLATE = '''
import sys, json


def respond(msg_id, result):
    print(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result},
                     separators=(",", ":")), flush=True)


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        respond(msg["id"], {"protocolVersion": "2025-06-18",
                             "capabilities": {},
                             "serverInfo": {"name": "fake", "version": "1"}})
    elif method == "tools/list":
        cursor = msg.get("params", {}).get("cursor")
        if not cursor:
            respond(msg["id"], {"tools": [{"name": "page1_tool",
                                            "description": "page one"}],
                                 "nextCursor": "p2"})
        else:
            respond(msg["id"], {"tools": [{"name": "page2_tool",
                                            "description": "page two"}]})
    elif method == "tools/call":
        respond(msg["id"], %s)
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "error": {"code": -32601, "message": "no"}},
                         separators=(",", ":")), flush=True)
'''


class _ManagerTestCase(unittest.TestCase):
    """A McpManager over a tmp registry + a fake in-test server script."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.registry_path = os.path.join(self.tmp, "mcp_servers.json")
        self.template_path = os.path.join(_SRC, "mcp_servers.json.template")

    def _write_fake_server(self, call_result, name="fake"):  # noqa: ANN001
        script = os.path.join(self.tmp, f"{name}.py")
        with open(script, "w") as fh:
            fh.write(FAKE_SERVER_TEMPLATE % call_result)
        return script

    def _manager_with(self, name, script):
        from modules.module_mcp import McpManager
        with open(self.registry_path, "w") as fh:
            json.dump({"version": 1, "servers": {
                name: {"transport": "stdio", "command": sys.executable,
                       "args": [script], "env": {}, "cwd": None,
                       "enabled": True, "auto_start": False}}}, fh)
        manager = McpManager(self.registry_path, self.template_path)
        self.assertEqual(manager.load_registry(), [])
        self.addCleanup(manager.shutdown)
        return manager

    def _real_manager(self):
        from modules.module_mcp import McpManager
        with open(self.registry_path, "w") as fh:
            json.dump({"version": 1, "servers": {
                "example": {"transport": "stdio", "command": sys.executable,
                             "args": [_SERVER], "env": {}, "cwd": None,
                             "enabled": True, "auto_start": False}}}, fh)
        manager = McpManager(self.registry_path, self.template_path)
        self.assertEqual(manager.load_registry(), [])
        self.addCleanup(manager.shutdown)
        return manager


class TestHandshakeListCall(_ManagerTestCase):
    """Handshake + pagination + tools/call → ToolResult (S-M6-05)."""

    def test_handshake_and_list_real(self):
        manager = self._real_manager()
        tools = manager.list_tools("example")
        self.assertEqual(sorted(t["name"] for t in tools),
                         ["add_numbers", "get_time"])
        # the negotiated version was captured by the handshake
        self.assertEqual(manager._transports["example"].negotiated_version,
                         "2025-06-18")

    def test_call_real_iserror_surfaces(self):
        manager = self._real_manager()
        result = manager.call_tool("example", "add_numbers",
                                   {"a": "x", "b": 1})
        # a tool-execution error is a SUCCESSFUL response -> ToolResult,
        # never an exception
        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "a and b must be numbers")

    def test_call_real_ok(self):
        manager = self._real_manager()
        result = manager.call_tool("example", "add_numbers",
                                   {"a": 2, "b": 3})
        self.assertFalse(result.is_error)
        self.assertIn("5", result.content)

    def test_pagination_follows_nextcursor(self):
        script = self._write_fake_server(
            '{"content": [{"type": "text", "text": "ok"}], '
            '"isError": False}')
        manager = self._manager_with("fake", script)
        tools = manager.list_tools("fake")
        # BOTH pages merged — the loop must follow nextCursor
        self.assertEqual(sorted(t["name"] for t in tools),
                         ["page1_tool", "page2_tool"])

    def test_structured_and_image_content(self):
        script = self._write_fake_server(
            '{"content": [{"type": "image", "mimeType": "image/png", '
            '"data": "..."}, '
            '{"type": "text", "text": "a picture"}], '
            '"structuredContent": {"sum": 5, "detail": "x" * 3000}, '
            '"isError": False}')
        manager = self._manager_with("fake", script)
        result = manager.call_tool("fake", "anything", {})
        self.assertFalse(result.is_error)
        self.assertIn("[image: image/png]", result.content)
        self.assertIn("a picture", result.content)
        self.assertIn("sum", result.content)  # structured appended
        self.assertLessEqual(len(result.content), 10000)  # bounded
        # raw preserved for the UI
        self.assertEqual(result.raw[0]["type"], "image")


if __name__ == "__main__":
    unittest.main()
