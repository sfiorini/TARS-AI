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

from modules.module_messageQue import queue_message

PROTOCOL_VERSION = "2025-06-18"

_SERVER_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}\Z")
_TRANSPORTS = ("stdio", "http")


class McpManager:
    """Registry + transports + lifecycle for MCP servers (D4)."""

    def __init__(self, registry_path, template_path):
        self.registry_path = str(registry_path)
        self.template_path = str(template_path)
        self.servers = {}   # name -> registry entry
        self._transports = {}  # name -> transport object (S-M6-04+)

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
