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
_REPO_ROOT = os.path.abspath(os.path.join(_SRC, ".."))


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


import http.server  # noqa: E402
import threading  # noqa: E402


def _header(headers, name):
    """Case-insensitive header lookup (urllib3 titlecases header names)."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


class _McpHttpFixture:
    """In-test Streamable HTTP server: records request headers, replies
    JSON or SSE, always advertises MCP-Session-Id."""

    SESSION_ID = "sess-123"

    def __init__(self, mode="json"):
        self.mode = mode
        self.recorded = []
        handler_class = self._make_handler()
        self.server = http.server.HTTPServer(("127.0.0.1", 0),
                                             handler_class)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def _make_handler(self):
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                fixture.recorded.append((dict(self.headers),
                                         json.loads(body)))
                msg = json.loads(body)
                if "id" not in msg:  # notification
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if msg.get("method") == "initialize":
                    payload = {"jsonrpc": "2.0", "id": msg["id"],
                               "result": {"protocolVersion": "2025-06-18",
                                           "capabilities": {},
                                           "serverInfo": {"name": "http-fake",
                                                           "version": "1"}}}
                elif msg.get("method") == "tools/list":
                    payload = {"jsonrpc": "2.0", "id": msg["id"],
                               "result": {"tools": [
                                   {"name": "http_tool",
                                    "description": "over http",
                                    "inputSchema": {"type": "object",
                                                    "properties": {}}}]}}
                else:
                    payload = {"jsonrpc": "2.0", "id": msg["id"],
                               "result": {"content": [
                                   {"type": "text", "text": "http result"}],
                                   "isError": False}}
                self.send_response(200)
                self.send_header("MCP-Session-Id", fixture.SESSION_ID)
                if fixture.mode == "json":
                    self.send_header("Content-Type", "application/json")
                    data = json.dumps(payload).encode()
                else:
                    self.send_header("Content-Type",
                                     "text/event-stream")
                    data = ("data: " + json.dumps(payload) + "\n\n").encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        return Handler

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestHttpTransport(unittest.TestCase):
    """Streamable HTTP transport (S-M6-06) — real in-test HTTP fixture."""

    def _manager_over(self, fixture):
        from modules.module_mcp import McpManager
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        registry_path = os.path.join(tmp, "mcp_servers.json")
        with open(registry_path, "w") as fh:
            json.dump({"version": 1, "servers": {
                "httpfake": {"transport": "http",
                             "url": f"http://127.0.0.1:{fixture.port}/",
                             "env": {}, "cwd": None, "enabled": True,
                             "auto_start": False}}}, fh)
        manager = McpManager(registry_path,
                             os.path.join(_SRC, "mcp_servers.json.template"))
        self.assertEqual(manager.load_registry(), [])
        self.addCleanup(manager.shutdown)
        return manager

    def test_http_json_response(self):
        fixture = _McpHttpFixture(mode="json")
        self.addCleanup(fixture.close)
        manager = self._manager_over(fixture)
        tools = manager.list_tools("httpfake")
        self.assertEqual([t["name"] for t in tools], ["http_tool"])
        result = manager.call_tool("httpfake", "http_tool", {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "http result")
        # the session id was captured from the initialize response and
        # sent on every subsequent request
        self.assertEqual(manager._transports["httpfake"].session_id,
                         _McpHttpFixture.SESSION_ID)
        subsequent = [headers for headers, _body in fixture.recorded[1:]]
        self.assertTrue(subsequent)
        for headers in subsequent:
            self.assertEqual(_header(headers, "MCP-Session-Id"),
                             _McpHttpFixture.SESSION_ID)
        # the negotiated protocol version is carried on every request
        # AFTER the handshake (the initialized notification is sent before
        # the version is known — initialize's params carry it instead)
        post_handshake = [headers for headers, _body
                          in fixture.recorded[2:]]
        for headers in post_handshake:
            self.assertEqual(_header(headers, "MCP-Protocol-Version"),
                             "2025-06-18")

    def test_http_sse_response(self):
        fixture = _McpHttpFixture(mode="sse")
        self.addCleanup(fixture.close)
        manager = self._manager_over(fixture)
        tools = manager.list_tools("httpfake")
        self.assertEqual([t["name"] for t in tools], ["http_tool"])
        result = manager.call_tool("httpfake", "http_tool", {})
        self.assertEqual(result.content, "http result")


class TestLifecycle(_ManagerTestCase):
    """Health, restart backoff, degraded state (S-M6-07)."""

    def _real_manager_small_backoff(self):
        manager = self._real_manager()
        manager._backoff_base = 0.01  # production: 1→2→4…→60s
        return manager

    def test_kill_then_restart_recovers(self):
        manager = self._real_manager_small_backoff()
        result = manager.call_tool("example", "add_numbers", {"a": 1, "b": 1})
        self.assertFalse(result.is_error)
        # kill the underlying process — next call transparently restarts
        manager._transports["example"]._proc.kill()
        manager._transports["example"]._proc.wait(timeout=5)
        result = manager.call_tool("example", "add_numbers", {"a": 2, "b": 3})
        self.assertFalse(result.is_error, result.content)
        self.assertIn("5", result.content)

    def test_degraded_after_max_attempts(self):
        from modules.module_mcp import McpManager
        # a server that always fails to start (immediate exit)
        with open(self.registry_path, "w") as fh:
            json.dump({"version": 1, "servers": {
                "broken": {"transport": "stdio", "command": sys.executable,
                           "args": ["-c", "raise SystemExit(1)"],
                           "env": {}, "cwd": None, "enabled": True,
                           "auto_start": False}}}, fh)
        manager = McpManager(self.registry_path, self.template_path)
        self.assertEqual(manager.load_registry(), [])
        self.addCleanup(manager.shutdown)
        manager._backoff_base = 0.01
        with mock.patch.object(manager, "_restart_max_attempts",
                              return_value=2), \
             mock.patch("modules.module_mcp.queue_message") as qm:
            for _ in range(4):
                result = manager.call_tool("broken", "any", {})
                self.assertTrue(result.is_error)
                time.sleep(0.02)  # wait out the tiny test-scale backoff
        self.assertTrue(manager.status()["broken"]["degraded"])
        degraded_lines = [c for c in qm.call_args_list
                          if "degraded" in c.args[0]]
        self.assertEqual(len(degraded_lines), 1,
                         f"exactly one degraded log line, got {len(degraded_lines)}")
        # degraded servers are excluded from the prompt flag
        self.assertFalse(manager.status()["broken"]["in_prompt"])

    def test_backoff_resets_on_success(self):
        from modules.module_mcp import McpManager
        script = self._write_fake_server(
            '{"content": [{"type": "text", "text": "ok"}], '
            '"isError": False}')
        with open(self.registry_path, "w") as fh:
            json.dump({"version": 1, "servers": {
                "flaky": {"transport": "stdio", "command": sys.executable,
                          "args": [script], "env": {}, "cwd": None,
                          "enabled": True, "auto_start": False}}}, fh)
        manager = McpManager(self.registry_path, self.template_path)
        self.assertEqual(manager.load_registry(), [])
        self.addCleanup(manager.shutdown)
        manager._backoff_base = 0.01
        # failure 1: bad command
        manager.servers["flaky"]["command"] = "/no/such/binary"
        result = manager.call_tool("flaky", "x", {})
        self.assertTrue(result.is_error)
        self.assertEqual(manager._state["flaky"]["attempts"], 1)
        # success resets the counter (after the tiny backoff window)
        time.sleep(0.02)
        manager.servers["flaky"]["command"] = sys.executable
        result = manager.call_tool("flaky", "x", {})
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(manager._state["flaky"]["attempts"], 0)
        # failure again -> backoff starts from the base again (attempts 1);
        # kill the live transport so the restart path runs with the bad
        # command (an alive transport short-circuits ensure_started)
        manager.servers["flaky"]["command"] = "/no/such/binary"
        manager._transports["flaky"].stop()
        result = manager.call_tool("flaky", "x", {})
        self.assertTrue(result.is_error)
        self.assertEqual(manager._state["flaky"]["attempts"], 1,
                         "any successful call resets the backoff")
        self.assertTrue(result.is_error)
        self.assertEqual(manager._state["flaky"]["attempts"], 1,
                         "any successful call resets the backoff")

    def test_status_reports_running(self):
        manager = self._real_manager_small_backoff()
        self.assertFalse(manager.status()["example"]["running"])
        manager.list_tools("example")
        self.assertTrue(manager.status()["example"]["running"])
        self.assertTrue(manager.status()["example"]["in_prompt"])


class TestShutdown(_ManagerTestCase):
    """SIGTERM handler + shutdown wiring (S-M6-08)."""

    def test_shutdown_stops_servers(self):
        manager = self._real_manager()
        manager.list_tools("example")  # starts the real server
        transport = manager._transports["example"]
        self.assertIsNone(transport.poll())  # alive
        manager.shutdown()
        deadline = time.monotonic() + 3
        while transport.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(transport.poll(), "server did not exit")
        check = subprocess.run(["pgrep", "-f", "mcp_example_server"],
                               capture_output=True, text=True)
        self.assertEqual(check.stdout.strip(), "",
                         f"orphan server process: {check.stdout}")
        # safe when nothing is running
        manager.shutdown()

    def test_app_py_sigterm_handler(self):
        with open(os.path.join(_SRC, "app.py"), encoding="utf-8") as fh:
            src = fh.read()
        # the handler is installed right after the event exists
        event_idx = src.index("shutdown_event = threading.Event()")
        handler_idx = src.index("signal.signal(signal.SIGTERM")
        self.assertLess(event_idx, handler_idx)
        self.assertIn("shutdown_event.set()",
                      src[handler_idx:handler_idx + 200])
        # the finally block stops MCP before the hard exit
        shutdown_idx = src.index("get_mcp_manager().shutdown()")
        exit_idx = src.index("os._exit(0)")
        self.assertLess(shutdown_idx, exit_idx)

    def test_app_stop_sigterm_first(self):
        with open(os.path.join(_REPO_ROOT, "App-Stop.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        # SIGTERM-first helper that waits up to 10s for a graceful exit
        self.assertIn("def _sigterm_first(", src)
        self.assertIn("wait_s: float = 10.0", src)
        self.assertIn('["kill", "-TERM", pid]', src)
        # and it runs BEFORE the pkill escalation
        self.assertLess(src.index("_sigterm_first(\"python app.py\""),
                        src.index('_pkill_escalate("App-Start.py")'))


class TestDescriptorsAndPromptBlock(_ManagerTestCase):
    """MCP descriptors + prompt block 3 + budget (S-M6-09)."""

    def test_mcp_descriptor_real_call(self):
        from modules.module_toolregistry import ToolRegistry, ToolContext
        manager = self._real_manager()
        registry = ToolRegistry()
        for d in manager.iter_descriptors():
            registry.register(d)
        names = {d.name for d in registry.iter_descriptors()}
        self.assertIn("mcp_example_get_time", names)
        self.assertIn("mcp_example_add_numbers", names)
        # D7 end-to-end: the REAL server runs through the descriptor
        result = registry.dispatch("mcp_example_add_numbers",
                                   {"a": 2, "b": 3}, ToolContext())
        self.assertFalse(result.is_error, result.content)
        self.assertIn("5", result.content)

    def test_mcp_iserror_fed_back(self):
        from modules.module_toolregistry import ToolRegistry, ToolContext
        manager = self._real_manager()
        registry = ToolRegistry()
        for d in manager.iter_descriptors():
            registry.register(d)
        result = registry.dispatch("mcp_example_add_numbers",
                                   {"a": "x", "b": 1}, ToolContext())
        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "a and b must be numbers")

    def test_prompt_block3_budget(self):
        manager = self._real_manager()
        many_tools = [{"name": f"tool_{i:02d}",
                       "description": "x" * 300,
                       "inputSchema": {"type": "object",
                                       "properties": {}}}
                      for i in range(40)]
        with mock.patch.object(manager, "list_tools",
                              return_value=many_tools), \
             mock.patch("modules.module_mcp.queue_message") as qm, \
             mock.patch.object(manager, "_prompt_budget",
                              return_value=600):
            block = manager.build_prompt_block()
        self.assertLessEqual(len(block), 600)
        messages = [call.args[0] for call in qm.call_args_list]
        dropped = [m for m in messages if "dropped from MCP prompt" in m]
        self.assertTrue(dropped, f"expected drop logs, got: {messages}")

    def test_degraded_server_excluded_from_prompt(self):
        manager = self._real_manager()
        manager.list_tools("example")  # starts it
        manager._state["example"]["degraded"] = True
        self.assertEqual([], list(manager.iter_descriptors()))
        self.assertEqual("", manager.build_prompt_block())
        # and status reflects it
        self.assertFalse(manager.status()["example"]["in_prompt"])

    def test_prompt_block_line_shape(self):
        manager = self._real_manager()
        block = manager.build_prompt_block()
        self.assertIn('* "mcp_example_get_time" — ', block)
        self.assertIn('Params: {}', block)
        self.assertIn('* "mcp_example_add_numbers" — ', block)
        self.assertIn('"a": number', block)


class TestMcpCli(unittest.TestCase):
    """app-mcp.py CLI (S-M6-10) — real subprocess, real exit codes."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # the registry lives in src/ (production layout) so the template's
        # registry-relative script path resolves (review F2 semantics)
        self.registry = os.path.join(_SRC, "mcp_servers_test_cli.json")
        self.addCleanup(lambda: os.path.exists(self.registry)
                        and os.remove(self.registry))
        shutil.copyfile(os.path.join(_SRC, "mcp_servers.json.template"),
                        self.registry)
        self.env = dict(os.environ)
        self.env["TARS_MCP_REGISTRY"] = self.registry

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(_SRC, "app-mcp.py")] + list(args),
            cwd=_REPO_ROOT, env=self.env,
            capture_output=True, text=True, timeout=60)

    def test_cli_test_example_prints_tools(self):
        result = self.run_cli("test", "example")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mcp_example_get_time", result.stdout)
        self.assertIn("mcp_example_add_numbers", result.stdout)
        self.assertIn("Get the current local time", result.stdout)

    def test_cli_disable_enable(self):
        result = self.run_cli("disable", "example")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.registry) as fh:
            self.assertFalse(json.load(fh)["servers"]["example"]["enabled"])
        listed = self.run_cli("list")
        self.assertIn("disabled", listed.stdout)
        result = self.run_cli("enable", "example")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.registry) as fh:
            self.assertTrue(json.load(fh)["servers"]["example"]["enabled"])

    def test_cli_add_remove(self):
        result = self.run_cli("add", "myserver", "stdio",
                              sys.executable, _SERVER)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.registry) as fh:
            entry = json.load(fh)["servers"]["myserver"]
        self.assertEqual(entry["transport"], "stdio")
        self.assertEqual(entry["args"], [_SERVER])
        # duplicate rejected (exit 1)
        result = self.run_cli("add", "myserver", "stdio",
                              sys.executable, _SERVER)
        self.assertEqual(result.returncode, 1)
        # invalid name rejected
        result = self.run_cli("add", "Bad-Name", "stdio", "x")
        self.assertEqual(result.returncode, 1)
        result = self.run_cli("remove", "myserver")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.registry) as fh:
            self.assertNotIn("myserver", json.load(fh)["servers"])

    def test_cli_unknown_name_exits_1(self):
        result = self.run_cli("enable", "no-such-server")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no-such-server", result.stderr)


class TestOrphanCleanup(unittest.TestCase):
    """M6 review F1: a server that starts but garbles the handshake must
    not leak a live process — the half-open transport is stopped."""

    def test_failed_handshake_leaves_no_orphan(self):
        from modules.module_mcp import McpManager
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # safety net: never leak the blocker even if the test fails
        self.addCleanup(subprocess.run, ["pkill", "-f", "banner_blocker"],
                        **{"check": False})
        script = os.path.join(tmp, "banner_blocker.py")
        with open(script, "w") as fh:
            fh.write("import sys, time\n"
                     "print('BANNER NOT JSON')\n"
                     "sys.stdout.flush()\n"
                     "time.sleep(30)\n")
        registry = os.path.join(tmp, "mcp_servers.json")
        with open(registry, "w") as fh:
            json.dump({"version": 1, "servers": {
                "banner": {"transport": "stdio", "command": sys.executable,
                           "args": [script], "env": {}, "cwd": None,
                           "enabled": True}}}, fh)
        manager = McpManager(registry,
                             os.path.join(_SRC, "mcp_servers.json.template"))
        self.assertEqual(manager.load_registry(), [])
        self.addCleanup(manager.shutdown)
        result = manager.call_tool("banner", "x", {})
        self.assertTrue(result.is_error)
        # the handshake failed — the just-started process must be dead
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            check = subprocess.run(["pgrep", "-f", "banner_blocker"],
                                   capture_output=True, text=True)
            if check.stdout.strip() == "":
                break
            time.sleep(0.1)
        self.assertEqual(check.stdout.strip(), "",
                         f"orphaned MCP process survived the failed "
                         f"handshake: {check.stdout}")


class TestTemplatePathResolution(unittest.TestCase):
    """M6 review F2: the SHIPPED template entry must resolve regardless of
    the app's cwd — the app chdirs to src/, so relative args must resolve
    against the registry's directory, not the process cwd."""

    def test_template_entry_resolves_from_any_cwd(self):
        from modules.module_mcp import McpManager
        # a registry INSIDE src/ (the production layout) seeded from the
        # ACTUAL shipped template — gitignored-adjacent, cleaned up
        registry = os.path.join(_SRC, "mcp_servers_test_f2.json")
        self.addCleanup(lambda: os.path.exists(registry)
                        and os.remove(registry))
        shutil.copyfile(os.path.join(_SRC, "mcp_servers.json.template"),
                        registry)
        manager = McpManager(
            registry, os.path.join(_SRC, "mcp_servers.json.template"))
        self.assertEqual(manager.load_registry(), [])
        self.addCleanup(manager.shutdown)
        # run from a cwd that is NOT the repo root and NOT src/
        other_cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other_cwd, ignore_errors=True)
        saved_cwd = os.getcwd()
        os.chdir(other_cwd)
        self.addCleanup(os.chdir, saved_cwd)
        tools = manager.list_tools("example")
        self.assertEqual(sorted(t["name"] for t in tools),
                         ["add_numbers", "get_time"])
        # the child cwd defaulted to the registry's directory (src/)
        self.assertEqual(manager._transports["example"].child_cwd,
                         os.path.dirname(registry))


if __name__ == "__main__":
    unittest.main()
