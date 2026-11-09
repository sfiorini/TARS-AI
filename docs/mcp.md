# MCP

TARS is an **MCP client** (Model Context Protocol, decision D4): external tool servers expose tools the AI can call, dispatched through the same deferred reply loop as builtin skills and agent skills. One shipped example server (`example`) is enabled out of the box so the whole path works on a fresh install.

## Protocol basics and the era choice

TARS speaks the **legacy protocol era**: `initialize` with `protocolVersion: "2025-06-18"` and client info → read the result → send `notifications/initialized`. On Streamable HTTP the session id from the `MCP-Session-Id` response header is captured and both `MCP-Session-Id` and `MCP-Protocol-Version` headers are sent on every subsequent request. (The modern era — per-request `_meta`, no sessions — is out of scope but not precluded.)

Two transports (decision D4):

- **stdio** — the server runs as a child process; JSON-RPC travels as newline-delimited compact UTF-8 on stdin/stdout (never pretty-printed, never embedded newlines). Server logs go to stderr — TARS drains them with rate limiting.
- **Streamable HTTP** — one POST per JSON-RPC message; both `application/json` and `text/event-stream` (SSE `data:` frames) responses are handled.

`tools/list` **pagination is mandatory**: TARS follows `nextCursor` until absent and merges pages. `tools/call` results may carry `text`, `image`, `audio`, `resource_link`, and `resource` content plus `structuredContent` — TARS does not assume text-only. A tool-execution error comes back as a **successful JSON-RPC response with `isError: true`** — never a protocol error — and is fed to the model so it can self-correct.

## The registry

`src/mcp_servers.json` is the **live registry** (gitignored, seeded from the tracked `src/mcp_servers.json.template` on first start). Entry anatomy:

```json
"example": {
  "transport": "stdio",
  "command": "python3",
  "args": ["src/mcp_example_server.py"],
  "env": {},
  "cwd": null,
  "enabled": true,
  "auto_start": false
}
```

A Streamable HTTP entry uses `"transport": "http"` and a `"url"` instead of `command`/`args`. Server names are `[a-z0-9_]{1,32}` and unique. Add a server via the web UI **MCP** tab (`POST /mcp`), the CLI, or by editing the template before first start.

## Tool naming and the prompt

Tools are exposed to the model as **`mcp_<server>_<tool>`** — e.g. `mcp_example_get_time`. The MCP prompt block (block 3) carries one line per tool with its description and compact params, bounded by `[MCP] prompt_budget_chars` (default 4000): descriptions are shortened first, then whole tools are dropped alphabetically, and **every dropped name is logged**. Degraded servers' tools leave the prompt.

## Lifecycle

- **Lazy start** on first use (or at boot with `auto_start: true`).
- **Health**: before every call the process is polled; a periodic `ping` runs every `[MCP] health_interval_s` (default 60).
- **Restart with backoff**: exponential 1→2→4…→60s; after `[MCP] restart_max_attempts` (default 3) consecutive failures the server is marked **degraded** (one log line, tools removed from the prompt). Any successful call resets the backoff.
- **Shutdown**: the app installs a SIGTERM handler (it had none) so `App-Stop.py` now stops it gracefully — MCP servers are killed as process groups (`SIGTERM`, 2 s, then `SIGKILL`), leaving no orphans.

## The shipped example (copy this)

`src/mcp_example_server.py` — a single-file, stdlib-only, legacy-era stdio server exposing `get_time` and `add_numbers`. It doubles as the client's conformance fixture: single-page `tools/list` (no `nextCursor`), `isError: false`/`isError: true` tool results, `-32601` for unknown methods.

Verify it works:

```bash
python3 src/app-mcp.py test example
# mcp_example_get_time — Get the current local time as HH:MM.
# mcp_example_add_numbers — Add two numbers.
```

Then by voice: **"what's 2 plus 3"** — TARS says a holding line, the tool runs, and the follow-up speaks "5".

## CLI

```bash
python3 src/app-mcp.py list                 # name, transport, status, flags
python3 src/app-mcp.py add myserver stdio python3 /path/to/server.py
python3 src/app-mcp.py add myserver http http://<pi-ip>:8080/mcp
python3 src/app-mcp.py remove myserver
python3 src/app-mcp.py enable myserver      # or disable
python3 src/app-mcp.py test myserver        # handshake + tools/list
```

## Troubleshooting

- **Server won't start / marked degraded** — check `MCP: server '…' failed to start` log lines (the reason is included); after `restart_max_attempts` consecutive failures the server is degraded until a successful call resets the backoff. `python3 src/app-mcp.py test <name>` shows the failure directly.
- **Session header errors (HTTP)** — TARS sends `MCP-Session-Id` + `MCP-Protocol-Version` on every post-handshake request; a server 400 on the first request usually means the transport era mismatch.
- **A tool is missing from the prompt** — the `[MCP] prompt_budget_chars` budget dropped it (check the `dropped from MCP prompt block` log line); raise the budget or disable unused servers.
- **A tool returned an error** — `isError: true` results are fed back to the model by design so it can retry or answer differently; check the tool's own message in the log.

## Where things live

| Path | What |
|---|---|
| `src/mcp_servers.json` / `.template` | live registry / tracked template |
| `src/modules/module_mcp.py` | registry, transports, lifecycle, descriptors |
| `src/mcp_example_server.py` | the shipped example server |
| `src/app-mcp.py` | the CLI |
| web UI **MCP** tab | the panel (`/mcp/*` endpoints) |
