#!/usr/bin/env python3
"""Minimal legacy-era MCP stdio server: newline-delimited UTF-8 JSON-RPC.

The canonical "copy this" starting point (D7) and the MCP client's
conformance fixture — tests talk to this server over a real pipe.

Protocol: legacy era (`initialize` + `notifications/initialized`), compact
JSON on stdout (one line per response, never pretty-printed), logs to
stderr only.  `tools/list` returns a SINGLE page with no `nextCursor` key
(exercising the client's termination condition).  `tools/call` errors are
tool-execution errors — a SUCCESSFUL JSON-RPC response with `isError: true`
— never protocol errors.
"""
import sys
import json
import datetime

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {"name": "get_time",
     "description": "Get the current local time as HH:MM.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "add_numbers",
     "description": "Add two numbers.",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
]


def call_tool(name, args):
    if name == "get_time":
        now = datetime.datetime.now().strftime("%H:%M")
        return {"content": [{"type": "text", "text": f"It is {now}."}], "isError": False}
    if name == "add_numbers":
        try:
            a, b = float(args["a"]), float(args["b"])
        except (KeyError, TypeError, ValueError):
            return {"content": [{"type": "text",
                                 "text": "a and b must be numbers"}],
                    "isError": True}          # tool-execution error, not protocol error
        return {"content": [{"type": "text",
                            "text": f"{args['a']} + {args['b']} = {a + b:g}"}],
                "isError": False}
    raise KeyError(name)


def handle(msg):
    if "id" not in msg:                       # notification (e.g. notifications/initialized)
        return None
    method, msg_id = msg.get("method"), msg["id"]
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"protocolVersion": PROTOCOL_VERSION,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "tars-example", "version": "1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": TOOLS}}   # single page: no nextCursor key at all
    if method == "tools/call":
        try:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": call_tool(msg["params"]["name"], msg["params"].get("arguments", {}))}
        except KeyError:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": "Unknown tool"}}
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def main():
    for line in sys.stdin:                    # logs go to stderr only
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)             # malformed JSON: skip, never crash
        except json.JSONDecodeError:
            sys.stderr.write("mcp-example: skipping malformed line\n")
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
