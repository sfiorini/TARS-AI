"""
Module : MCP Client (Model Context Protocol, D4)
Author: Charles-Olivier Dion (AtomikSpace)
Contact: atomikspace.labs@gmail.com
Copyright (c) 2026 Charles-Olivier Dion

This file is authored by Charles-Olivier Dion and is dual-licensed.

Non-Commercial License:
This file is licensed under Creative Commons Attribution-NonCommercial 4.0 International (CC-BY-NC 4.0).
You may use, modify, and redistribute this file for NON-COMMERCIAL purposes only, with attribution.

Commercial License:
Commercial use (including selling products, paid services, SaaS, subscriptions, Patreon rewards, or derivatives)
requires a separate written license from Charles-Olivier Dion (AtomikSpace).

This license applies only to this file and does not override licenses of other files in the repository.

Legacy-era MCP client (D4): `initialize` handshake + `notifications/initialized`,
stdio (newline-delimited compact UTF-8 JSON-RPC) and Streamable HTTP transports,
mandatory `nextCursor` pagination, `tools/call` results mapped to ToolResult
(`isError: true` is a SUCCESSFUL response fed back to the model per D1).
"""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time

from modules.module_messageQue import queue_message
from modules.module_toolregistry import ToolResult

PROTOCOL_VERSION = "2025-06-18"

_SERVER_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}\Z")
_TRANSPORTS = ("stdio", "http")


class McpServerError(Exception):
    """A server-level failure (degraded / restarting) surfaced to callers."""


class McpManager:
    """Registry + transports + lifecycle for MCP servers (D4)."""

    def __init__(self, registry_path, template_path):
        self.registry_path = str(registry_path)
        self.template_path = str(template_path)
        self.servers = {}   # name -> registry entry
        self._transports = {}  # name -> transport object
        self._tools_cache = {}  # name -> merged tools/list result
        # lifecycle state (S-M6-07): consecutive start failures, backoff
        # deadline, degraded flag
        self._state = {}
        self._backoff_base = 1.0  # production: 1→2→4…→60s (tests shrink it)

    def _restart_max_attempts(self):
        try:
            from modules.module_config import load_config
            return load_config().get("MCP", {}).get(
                "restart_max_attempts", 3)
        except Exception:
            return 3

    # ── handshake + tools/list + tools/call (S-M6-05) ────────────

    def ensure_started(self, name):
        """Lazy start + legacy handshake with restart backoff (S-M6-07).

        poll() health check first; a dead transport takes the restart
        path.  Exponential backoff 1→2→4…→60s between attempts; after
        `restart_max_attempts` CONSECUTIVE failures the server is marked
        degraded (one log line, excluded from the prompt) until a
        successful call resets the counters."""
        transport = self._transports.get(name)
        if transport is not None and transport.poll() is None:
            return transport
        state = self._state.setdefault(
            name, {"attempts": 0, "next_at": 0.0, "degraded": False})
        if state["degraded"]:
            raise McpServerError(
                f"MCP server '{name}' is degraded (restart attempts "
                f"exhausted)")
        if time.monotonic() < state["next_at"]:
            raise McpServerError(
                f"MCP server '{name}' is restarting — retry later")
        entry = self.servers.get(name)
        if entry is None:
            raise ValueError(f"unknown MCP server '{name}'")
        try:
            if entry.get("transport") == "stdio":
                transport = _StdioServer(
                    name, entry,
                    default_cwd=os.path.dirname(self.registry_path))
            else:
                transport = _HttpServer(name, entry)
            transport.start()
            result = transport._request({
                "jsonrpc": "2.0", "id": _next_id(), "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION,
                           "capabilities": {},
                           "clientInfo": {"name": "tars", "version": "3.0"}}},
                timeout=_request_timeout())
            negotiated = (result.get("result", {}).get("protocolVersion")
                          or PROTOCOL_VERSION)
            # legacy handshake: notifications/initialized after the result
            transport._send({"jsonrpc": "2.0",
                             "method": "notifications/initialized"})
            transport.negotiated_version = negotiated
            # NOTE: the HTTP transport captures MCP-Session-Id from the
            # initialize response headers itself (_post) — do not clobber
        except Exception as exc:
            # start/handshake failed: stop the half-open transport —
            # BOTH any previously stored one (restart path) AND the local
            # one we just started (a fresh start is not yet in
            # _transports; without this the spawned process is orphaned —
            # review F1)
            failed = self._transports.pop(name, None)
            if failed is not None:
                try:
                    failed.stop()
                except Exception:  # noqa: BLE001
                    pass
            try:
                transport.stop()
            except Exception:  # noqa: BLE001
                pass
            state["attempts"] += 1
            if state["attempts"] >= self._restart_max_attempts():
                state["degraded"] = True
                queue_message(
                    f"MCP: server '{name}' marked degraded after "
                    f"{state['attempts']} consecutive restart attempts")
            else:
                delay = min(self._backoff_base
                            * 2 ** (state["attempts"] - 1), 60.0)
                state["next_at"] = time.monotonic() + delay
                queue_message(
                    f"MCP: server '{name}' failed to start (attempt "
                    f"{state['attempts']}): {exc} — retrying in {delay:g}s")
            raise
        # any successful start resets the backoff
        state["attempts"] = 0
        state["next_at"] = 0.0
        self._transports[name] = transport
        self._tools_cache.pop(name, None)  # re-fetch on restart
        return transport

    def list_tools(self, name):
        """tools/list — pagination is MANDATORY: follow nextCursor until
        absent, merge pages.  Cached per server; re-fetched on restart."""
        cached = self._tools_cache.get(name)
        if cached is not None:
            return cached
        transport = self.ensure_started(name)
        tools = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = transport._request(
                {"jsonrpc": "2.0", "id": _next_id(),
                 "method": "tools/list", "params": params},
                timeout=_request_timeout())
            page = result.get("result", {})
            tools.extend(page.get("tools") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        self._tools_cache[name] = tools
        return tools

    def call_tool(self, name, tool, args):
        """tools/call → ToolResult.  `isError: true` is a SUCCESSFUL
        JSON-RPC response → ToolResult(is_error=True), fed back to the
        model per D1 — never an exception.  Server-level failures
        (degraded, restarting, dead transport) also surface as
        is_error results — the loop feeds them back to the model."""
        try:
            transport = self.ensure_started(name)
            response = transport._request(
                {"jsonrpc": "2.0", "id": _next_id(), "method": "tools/call",
                 "params": {"name": tool, "arguments": args or {}}},
                timeout=_request_timeout())
        except McpServerError as exc:
            return ToolResult(is_error=True, content=str(exc))
        except Exception as exc:  # noqa: BLE001 — transport failure
            # non-JSON stdout / dead pipe: the transport is failed — the
            # next call takes the restart path
            self._mark_transport_failed(name)
            return ToolResult(is_error=True,
                              content=f"MCP server '{name}' call failed: "
                                      f"{exc}")
        if "error" in response:
            error = response["error"] or {}
            return ToolResult(is_error=True,
                              content=f"MCP error: "
                                      f"{error.get('message', 'unknown')}")
        return _result_from_content(response.get("result") or {})

    def _mark_transport_failed(self, name):
        transport = self._transports.pop(name, None)
        self._tools_cache.pop(name, None)
        if transport is not None:
            try:
                transport.stop()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    def status(self):
        """Per-server lifecycle state for the UI and CLI (S-M6-07)."""
        out = {}
        for name, entry in self.servers.items():
            state = self._state.get(name, {})
            transport = self._transports.get(name)
            running = transport is not None and transport.poll() is None
            degraded = bool(state.get("degraded"))
            enabled = bool(entry.get("enabled", True))
            out[name] = {"running": running,
                         "degraded": degraded,
                         "enabled": enabled,
                         "in_prompt": enabled and not degraded}
        return out

    def register_health_checks(self):
        """Register the periodic health ping per server (app startup).
        Recurring tasks are not persisted — the app re-registers each
        start (module_heartbeat contract)."""
        try:
            from modules.module_config import load_config
            from modules.module_heartbeat import schedule_recurring
        except Exception:
            return
        interval = load_config().get("MCP", {}).get("health_interval_s", 60)
        if not interval or interval <= 0:
            return
        for name in self.servers:
            schedule_recurring(f"mcp_health_{name}", interval,
                               self._health_check, args=(name,))

    # ── descriptors + prompt block 3 (S-M6-09) ─────────────────

    def _prompt_budget(self):
        try:
            from modules.module_config import load_config
            return load_config().get("MCP", {}).get("prompt_budget_chars",
                                                    4000)
        except Exception:
            return 4000

    def iter_descriptors(self):
        """One ToolDescriptor per tools/list entry: mcp_<server>_<tool>.
        Disabled and degraded servers contribute nothing."""
        from modules.module_toolregistry import ToolDescriptor
        for name, entry in self.servers.items():
            if not entry.get("enabled", True):
                continue
            if self._state.get(name, {}).get("degraded"):
                continue  # degraded servers' tools leave the prompt
            try:
                tools = self.list_tools(name)
            except Exception as exc:  # noqa: BLE001 — one bad server
                queue_message(f"MCP: no descriptors from '{name}': {exc}")
                continue
            for tool in tools:
                tool_name = tool.get("name", "")
                if not tool_name:
                    continue
                schema = tool.get("inputSchema") or {
                    "type": "object", "properties": {}}
                yield ToolDescriptor(
                    name=f"mcp_{name}_{tool_name}", kind="mcp",
                    description=tool.get("description", ""),
                    parameters=schema, result_bearing=True,
                    timeout_s=None,
                    execute=self._make_mcp_exec(self, name, tool_name))

    @staticmethod
    def _make_mcp_exec(manager, server, tool):
        # capture the bound manager instance (not the singleton) so the
        # descriptor dispatches through the manager that listed the tool
        def _exec(params, ctx):
            return manager.call_tool(server, tool, params or {})
        return _exec

    def build_prompt_block(self):
        """Prompt block 3: one line per tool from tools/list, bounded by
        [MCP] prompt_budget_chars — same truncate-then-drop-and-log
        mechanic as the agent-skills block."""
        budget = self._prompt_budget()
        entries = []  # (descriptor_name, line)
        for name, entry in self.servers.items():
            if not entry.get("enabled", True):
                continue
            if self._state.get(name, {}).get("degraded"):
                continue
            try:
                tools = self.list_tools(name)
            except Exception as exc:  # noqa: BLE001
                queue_message(f"MCP: '{name}' excluded from prompt: {exc}")
                continue
            for tool in tools:
                descriptor_name = f"mcp_{name}_{tool.get('name', '')}"
                entries.append((descriptor_name, tool))

        def render(items, desc_limit):
            lines = []
            for descriptor_name, tool in items:
                desc = (tool.get("description") or "")[:desc_limit]
                schema = tool.get("inputSchema") or {}
                props = schema.get("properties", {})
                spec = "{" + ", ".join(
                    f'"{key}": {value.get("type", "string")}'
                    for key, value in sorted(props.items())) + "}"
                lines.append(f'* "{descriptor_name}" — {desc} '
                             f'Params: {spec}')
            return "\n".join(lines)

        items = entries
        limit = 200
        for limit in (200, 100, 50):          # 1) truncate descriptions
            if len(render(items, limit)) <= budget:
                break
        dropped = []
        while items and len(render(items, limit)) > budget:
            dropped.append(items.pop(0)[0])   # 2) drop alphabetically
        for descriptor_name in dropped:
            queue_message(f"MCP: '{descriptor_name}' dropped from MCP "
                          f"prompt block (budget {budget} chars)")
        return render(items, limit)

    def _health_check(self, name):
        """Periodic ping: any JSON-RPC response counts as alive; a dead
        or silent transport is stopped so the next call restarts it."""
        transport = self._transports.get(name)
        if transport is None:
            return  # never started — lazy start stays lazy
        if transport.poll() is not None:
            self._mark_transport_failed(name)
            queue_message(f"MCP: health check found '{name}' dead — "
                          f"it will restart on next use")
            return
        try:
            transport._request({"jsonrpc": "2.0", "id": _next_id(),
                                "method": "ping"},
                               timeout=_request_timeout())
        except Exception as exc:  # noqa: BLE001
            self._mark_transport_failed(name)
            queue_message(f"MCP: health ping to '{name}' failed: {exc}")

    def shutdown(self):
        """Stop every running transport (S-M6-08 wires this into the
        app's finally; safe when nothing started)."""
        for name, transport in list(self._transports.items()):
            try:
                transport.stop()
            except Exception as exc:  # noqa: BLE001 — shutdown is best-effort
                queue_message(f"MCP: shutdown of '{name}' failed: {exc}")
        self._transports.clear()
        self._tools_cache.clear()

    # ── registry ────────────────────────────────────────────────

    def load_registry(self):
        """Read + validate the live registry. Returns the error list."""
        errors = []
        self.servers = {}
        if not os.path.exists(self.registry_path):
            return errors
        try:
            with open(self.registry_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            return [f"mcp registry unreadable: {exc}"]
        entries = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return ["mcp registry has no 'servers' object"]
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                errors.append(f"server '{name}': entry must be an object")
                continue
            if not _SERVER_NAME_RE.match(name):
                errors.append(
                    f"invalid server name '{name}' — must be [a-z0-9_] and "
                    f"1-32 chars")
                continue
            transport = entry.get("transport")
            if transport not in _TRANSPORTS:
                errors.append(
                    f"server '{name}': transport must be one of "
                    f"{list(_TRANSPORTS)}, got {transport!r}")
                continue
            self.servers[name] = entry
        return errors

    def save_registry(self):
        """Atomic write of the live registry."""
        payload = json.dumps({"version": 1, "servers": self.servers},
                             indent=2, sort_keys=True)
        tmp_path = self.registry_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
        os.replace(tmp_path, self.registry_path)

    def validate_name(self, name):
        """Server name validation: [a-z0-9_]{1,32} (design §5)."""
        return bool(name) and _SERVER_NAME_RE.match(name)

    def add_server(self, name, entry):
        """Add a server to the live registry (name must be unique)."""
        if not self.validate_name(name):
            return False, [f"invalid server name '{name}' — must be "
                           f"[a-z0-9_] and 1-32 chars"]
        if name in self.servers:
            return False, [f"server '{name}' is already registered"]
        transport = entry.get("transport")
        if transport not in _TRANSPORTS:
            return False, [f"transport must be one of {list(_TRANSPORTS)}"]
        self.load_registry()  # re-read in case the file changed on disk
        if name in self.servers:
            return False, [f"server '{name}' is already registered"]
        self.servers[name] = entry
        self.save_registry()
        return True, []

    def remove_server(self, name):
        if name not in self.servers:
            return False, [f"unknown server '{name}'"]
        del self.servers[name]
        self.save_registry()
        return True, []

    def set_enabled(self, name, enabled):
        if name not in self.servers:
            return False, [f"unknown server '{name}'"]
        self.servers[name]["enabled"] = bool(enabled)
        self.save_registry()
        return True, []

    def seed_if_absent(self):
        """First app start: copy the tracked template → live registry."""
        if os.path.exists(self.registry_path):
            return
        if not os.path.exists(self.template_path):
            return
        shutil.copyfile(self.template_path, self.registry_path)
        queue_message("MCP: seeded mcp_servers.json from template")


_id_counter = [0]


def _next_id():
    _id_counter[0] += 1
    return _id_counter[0]


def _request_timeout():
    try:
        from modules.module_config import load_config
        return load_config().get("MCP", {}).get("request_timeout", 30)
    except Exception:
        return 30


def _result_from_content(result):
    """Map a tools/call result to ToolResult (design §7): text
    concatenated; image/audio → placeholder (raw kept for the UI);
    resources summarised; structuredContent appended, bounded."""
    content_items = result.get("content") or []
    texts = []
    for item in content_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            texts.append(item.get("text", ""))
        elif item_type in ("image", "audio"):
            texts.append(f"[{item_type}: {item.get('mimeType', 'unknown')}]")
        elif item_type in ("resource_link", "resource"):
            texts.append(f"[{item_type}: "
                         f"{item.get('uri') or item.get('name') or ''}]")
        else:
            texts.append(f"[{item_type}: ...]")
    structured = result.get("structuredContent")
    if structured is not None:
        dumped = json.dumps(structured, separators=(",", ":"))
        texts.append(dumped[:2000])  # bounded
    content = "\n".join(text for text in texts if text)
    return ToolResult(content=content,
                      is_error=bool(result.get("isError")),
                      raw=content_items)


class _HttpServer:
    """Streamable HTTP transport (D4): one persistent requests.Session,
    POST per JSON-RPC message, `MCP-Session-Id` (captured from the
    initialize response) + `MCP-Protocol-Version` headers on every
    request, both `application/json` and `text/event-stream` responses
    (SSE `data:` frames parsed, resumption channel ignored).  No httpx —
    the existing requests dependency only."""

    def __init__(self, name, entry):
        self.name = name
        self.entry = entry
        self.negotiated_version = None
        self.session_id = None
        self._session = None

    def start(self):
        import requests
        self._session = requests.Session()

    def poll(self):
        # stdio convention: None while alive, non-None when dead
        # (ensure_started treats poll() is None as alive)
        return None if self._session is not None else 0

    def _headers(self):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        if self.negotiated_version:
            headers["MCP-Protocol-Version"] = self.negotiated_version
        return headers

    def _post(self, msg, timeout):
        response = self._session.post(self.entry["url"], json=msg,
                                      headers=self._headers(),
                                      timeout=timeout)
        response.raise_for_status()
        # capture the session id advertised by the server (initialize)
        session_id = response.headers.get("MCP-Session-Id")
        if session_id:
            self.session_id = session_id
        return response

    def _send(self, msg):
        """Notifications are POSTed; the response is ignored."""
        try:
            self._post(msg, _request_timeout())
        except Exception as exc:  # noqa: BLE001 — notification is fire-and-forget
            queue_message(f"MCP: notification to '{self.name}' failed: {exc}")

    def _request(self, msg, timeout=30):
        response = self._post(msg, timeout)
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            # SSE: take the first JSON-RPC response from the data: frames;
            # the resumption channel is ignored (we don't stream)
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[len("data:"):].strip())
            raise TimeoutError(
                f"MCP server '{self.name}' sent no data: frame")
        return response.json()

    def stop(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None


class _StdioServer:
    """stdio transport: newline-delimited compact UTF-8 JSON-RPC over the
    server's stdin/stdout; stderr drained (rate-limited) on a daemon
    thread — servers are allowed to log there."""

    def __init__(self, name, entry, default_cwd=None):
        self.name = name
        self.entry = entry
        # when the registry entry has no cwd, the child runs in the
        # REGISTRY's directory so relative args resolve against it (the
        # app chdirs to src/ — review F2), not against the process cwd
        self.default_cwd = default_cwd
        self.child_cwd = None
        self._proc = None
        self._stderr_thread = None

    def start(self):
        env = {**os.environ, **(self.entry.get("env") or {})}
        self.child_cwd = (self.entry.get("cwd") or self.default_cwd)
        self._proc = subprocess.Popen(
            [self.entry["command"]] + list(self.entry.get("args") or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env=env, cwd=self.child_cwd,
            start_new_session=True)  # process group: killpg on stop
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True,
            name=f"mcp-stderr-{self.name}")
        self._stderr_thread.start()

    def _drain_stderr(self):
        """Server logs go to stderr (per spec) — drain with rate limiting."""
        window_start = time.monotonic()
        lines_in_window = 0
        suppressed = False
        for line in self._proc.stderr:
            now = time.monotonic()
            if now - window_start >= 1.0:
                window_start, lines_in_window = now, 0
                suppressed = False
            lines_in_window += 1
            if lines_in_window > 10:
                if not suppressed:
                    queue_message(
                        f"MCP: stderr from '{self.name}' suppressed "
                        f"(rate limit)")
                    suppressed = True
                continue
            queue_message(f"MCP[{self.name}]: {line.rstrip()}")

    def _send(self, msg):
        """Compact JSON + newline — never pretty-printed, never embedded
        newlines."""
        self._proc.stdin.write(json.dumps(msg, separators=(",", ":"))
                              + "\n")
        self._proc.stdin.flush()

    def _read(self, timeout):
        """Line-buffered, whitespace-tolerant read of one JSON-RPC message."""
        box = []

        def reader():
            line = self._proc.stdout.readline()
            if line:
                box.append(line)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout)
        if not box:
            raise TimeoutError(f"MCP server '{self.name}' response timeout")
        return json.loads(box[0].strip())

    def _request(self, msg, timeout=30):
        self._send(msg)
        return self._read(timeout)

    def poll(self):
        """The process's return code, or None while running."""
        if self._proc is None:
            return None
        return self._proc.poll()

    def stop(self):
        """Terminate the process GROUP: SIGTERM, wait 2s, SIGKILL."""
        if self._proc is None:
            return
        try:
            os.killpg(self._proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for stream in (self._proc.stdin, self._proc.stdout,
                       self._proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except OSError:
                pass


_manager = None


def get_mcp_manager():
    """Module-level singleton over src/mcp_servers.json + template."""
    global _manager
    if _manager is None:
        src_dir = os.path.dirname(os.path.abspath(__file__))
        base = os.path.dirname(src_dir)
        _manager = McpManager(
            registry_path=os.path.join(base, "mcp_servers.json"),
            template_path=os.path.join(base, "mcp_servers.json.template"))
        _manager.load_registry()
    return _manager
