#!/usr/bin/env python3
"""TARS MCP CLI — manage MCP servers (D4).

Commands:
  list                       registry entries: name, transport, enabled, auto_start, status
  add <name> stdio <command> [args...]   add a stdio server
  add <name> http <url>                 add a Streamable HTTP server
  remove <name>              remove a server from the registry
  enable <name> / disable <name>        toggle a server
  test <name>                handshake + tools/list; prints each discovered tool

Operates on src/mcp_servers.json via get_mcp_manager(); the
TARS_MCP_REGISTRY env var overrides the registry path (tests and offline
tooling).  House style: no argparse — sys.argv, and a flushed hard exit
(the CLI imports app modules which start daemon threads, so interpreter
finalization is skipped to avoid shutdown SIGABRT).
"""
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="[MCP] %(message)s")
log = logging.getLogger("mcp")

USAGE = __doc__.strip()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_PATH = os.path.join(_SCRIPT_DIR, "mcp_servers.json.template")


def _hard_exit(code):
    """Flushed hard exit (daemon threads at import time — see module doc)."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def _get_manager():
    from modules.module_mcp import McpManager, get_mcp_manager
    override = os.environ.get("TARS_MCP_REGISTRY")
    if override:
        manager = McpManager(override, _TEMPLATE_PATH)
        manager.load_registry()
        return manager
    return get_mcp_manager()


def cmd_list(manager, args):
    status = manager.status()
    for name in sorted(manager.servers):
        entry = manager.servers[name]
        st = status.get(name, {})
        state = ("degraded" if st.get("degraded")
                 else "running" if st.get("running") else "stopped")
        flags = []
        if not entry.get("enabled", True):
            flags.append("disabled")
        if entry.get("auto_start"):
            flags.append("auto_start")
        suffix = f" ({', '.join(flags)})" if flags else ""
        print(f"{name}\t{entry.get('transport')}\t{state}{suffix}")
    return 0


def cmd_add(manager, args):
    if len(args) < 3:
        print("mcp: add <name> stdio <command> [args...] | "
              "add <name> http <url>", file=sys.stderr)
        return 1
    name, transport = args[0], args[1]
    if transport == "stdio":
        entry = {"transport": "stdio", "command": args[2],
                 "args": list(args[3:]), "env": {}, "cwd": None,
                 "enabled": True, "auto_start": False}
    elif transport == "http":
        entry = {"transport": "http", "url": args[2], "env": {},
                 "cwd": None, "enabled": True, "auto_start": False}
    else:
        print(f"mcp: transport must be stdio or http, got {transport!r}",
              file=sys.stderr)
        return 1
    ok, errors = manager.add_server(name, entry)
    if not ok:
        for error in errors:
            print(f"mcp: {error}", file=sys.stderr)
        return 1
    print(f"Added '{name}'")
    return 0


def cmd_remove(manager, args):
    if not args:
        print("mcp: remove needs a server name", file=sys.stderr)
        return 1
    ok, errors = manager.remove_server(args[0])
    if not ok:
        for error in errors:
            print(f"mcp: {error}", file=sys.stderr)
        return 1
    print(f"Removed '{args[0]}'")
    return 0


def cmd_toggle(manager, args, enabled):
    if not args:
        print(f"mcp: {['disable', 'enable'][int(enabled)]} needs a name",
              file=sys.stderr)
        return 1
    ok, errors = manager.set_enabled(args[0], enabled)
    if not ok:
        for error in errors:
            print(f"mcp: {error}", file=sys.stderr)
        return 1
    print(f"{'Enabled' if enabled else 'Disabled'} '{args[0]}'")
    return 0


def cmd_test(manager, args):
    if not args:
        print("mcp: test needs a server name", file=sys.stderr)
        return 1
    name = args[0]
    try:
        tools = manager.list_tools(name)
    except Exception as exc:  # noqa: BLE001 — report and exit 1
        print(f"mcp: test '{name}' failed: {exc}", file=sys.stderr)
        return 1
    if not tools:
        print(f"mcp: '{name}' exposes no tools")
        return 1
    for tool in tools:
        print(f"mcp_{name}_{tool.get('name', '')} — "
              f"{tool.get('description', '')}")
    manager.shutdown()
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if len(argv) >= 2 else 1
    command, args = argv[1], argv[2:]
    manager = _get_manager()
    if command == "list":
        return cmd_list(manager, args)
    if command == "add":
        return cmd_add(manager, args)
    if command == "remove":
        return cmd_remove(manager, args)
    if command == "enable":
        return cmd_toggle(manager, args, True)
    if command == "disable":
        return cmd_toggle(manager, args, False)
    if command == "test":
        return cmd_test(manager, args)
    print(USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    _code = 0
    try:
        _code = main(sys.argv)
    except Exception:  # noqa: BLE001 — report and exit deterministically
        import traceback
        traceback.print_exc()
        _code = 1
    finally:
        _hard_exit(_code)
