# Running TARS

How to start, stop, autostart, and restart TARS-AI on the Pi, and where the logs are. All launchers live at the repo root.

## Start

```bash
./tars-launcher.sh
```

`tars-launcher.sh` does three things, then runs `App-Start.py`:

1. `source src/.venv/bin/activate` — the virtualenv created by `Install.sh`.
2. `sudo setcap 'cap_net_bind_service=+ep' "$(readlink -f "$(which python3)")"` — lets Python bind privileged ports (<1024, i.e. the default web UI port 80) without running as root.
3. `python App-Start.py "$@"`, which spawns the app as:

   ```bash
   cd src && source .venv/bin/activate && python app.py show_ui=true [extra key=value args]
   ```

   `show_ui=false` runs headless (no pygame display); extra `key=value` CLI args are forwarded to `app.py` (e.g. `debug=true`, `speed=true`).

On a desktop Pi, `--reboot` mode relaunches the app in a visible `lxterminal` window; over SSH, start detached so the session can close: `ssh -f pi 'nohup ~/TARS-AI/tars-launcher.sh >/dev/null 2>&1 &'`.

## Stop

```bash
python3 App-Stop.py
```

`App-Stop.py` runs `killall xterm`, then `_pkill_escalate("App-Start.py")` and `_pkill_escalate("python app.py")`: `pkill -f <pattern>`, poll `pgrep` for up to 2 s, then `pkill -9 -f <pattern>`. The TERM→KILL escalation is required because **pygame/SDL installs a SIGTERM handler that swallows the first SIGTERM** — a single `pkill` leaves the app alive (verified on the robot). The script cannot kill itself: its cmdline contains `App-Stop.py`, and the patterns `App-Start.py` / `python app.py` don't match it.

## Autostart at boot (XDG, not systemd)

```bash
./toggle-autostart.sh     # toggles on/off, prints the current state
```

When enabled it writes **`~/.config/autostart/tars-ai.desktop`** (XDG autostart entry, `X-GNOME-Autostart-enabled=true`) whose `Exec` is the generated **`lp.sh`**. `lp.sh` sleeps 8 s, sources `~/.bashrc`, exports `DISPLAY=:0`, and opens an `lxterminal` running `src/.venv/bin/python App-Start.py`, logging to `autostart.log` at the repo root. There is deliberately no systemd unit — remove the `.desktop` file to disable autostart.

## Restart without rebooting the Pi

- From the **web UI**: `POST /reboot_program` (sys tab button). The running process spawns `tars-launcher.sh --reboot <old_pid> <python> app.py …` detached; the launcher sleeps 1 s (so the HTTP response reaches your browser), sends TERM to the old PID, waits up to 5 s, then KILL, closes the old terminal, and relaunches in a fresh `lxterminal`.
- From a shell:

  ```bash
  python3 App-Stop.py && ./tars-launcher.sh
  ```

Config changes in `src/config.ini` require this restart to take effect. `GET /boot_id` returns a boot id that changes after every restart, so the UI can detect a restart happened.

## Logs

| Log | Path | Written by |
|---|---|---|
| Main app log | **`src/logs/tars-ai.log`** | `module_messageQue.queue_message()` — every module logs status/errors/warnings here (`src/modules/module_messageQue.py`) |
| Recent lines via web | `GET /api/console/logs` | sys tab console |
| Autostart marker | `autostart.log` (repo root) | `lp.sh` |
| Live stdout | the lxterminal window on the Pi | `App-Start.py` / `app.py` |

```bash
tail -f ~/TARS-AI/src/logs/tars-ai.log
```

Servo-specific events (watchdog repairs, I2C errors, stuck-MOVING resets) appear here as `SERVO:` / `WARNING:` lines — see [Servo Watchdog & Recovery](servo-watchdog.md).

TTS failover lines also land here: `WARNING: TTS provider '…' failed (QuotaExhausted / RateLimited / AuthFailed / TransientError: …) — failing over to '…'` whenever the ordered provider chain switches to the next provider, plus `ERROR: TTS per-utterance deadline exceeded …` if the whole chain ran out of time — see [Voice Pipeline](voice-pipeline.md) and [Troubleshooting](troubleshooting.md).

First-start seeding lines (one per live state file): `MOVEMENTS: seeded movements.json from defaults`, `SKILLS: seeded agent_skills from defaults (N skills)`, `MCP: seeded mcp_servers.json from template`.

MCP lifecycle lines: `MCP: server '<name>' failed to start (attempt N): <reason> — retrying in <delay>s` (exponential restart backoff), `MCP: server '<name>' marked degraded after N consecutive restart attempts` (one line — its tools leave the prompt until a successful call), `MCP: health check found '<name>' dead — it will restart on next use`, and `MCP: '<tool>' dropped from MCP prompt block (budget N chars)` when the prompt budget overflows (agent-skill drops log the same way: `SKILLS: '<name>' dropped from agent-skills prompt block`).

Deferred reply loop lines (result-bearing tool turns) — successes as well as failures, so a completed turn is verifiable in the log: `TOOLLOOP: turn start (source=…) — tools: …` when a turn enters the loop (the result-bearing tool names, truncated), `TOOLLOOP: tool <name> ok in <N>s (<n> chars)` or `TOOLLOOP: tool <name> ERROR in <N>s: <reason>` after each tool runs, `TOOLLOOP: hop <n> follow-up ok (reply <n> chars)` after each follow-up completion, and `TOOLLOOP: turn done (source=…, hops=<n>, <N>s)` on normal completion. Failure lines: `TOOLLOOP: turn cancelled (source=…) — results discarded, not injected into the new turn` when a new utterance abandons an in-flight turn, `TOOLLOOP: turn hit its limit — speaking fallback` when the hop bound (`tool_loop_max_hops`) or the overall deadline stops a turn (followed by the raw tool text), and `TOOLLOOP: follow-up completion failed: …` plus `TOOLLOOP: hop <n> follow-up FAILED — speaking fallback` when the follow-up LLM request errors — see [Voice Pipeline](voice-pipeline.md).

## Other root scripts

- `launch_retropie.sh` — hand control to RetroPie (used by `skill_launch_retropie`).
- `lp.sh` — generated autostart launcher (see above).

## Power check

If behavior is erratic (USB drops, servo stalls, random reboots), check the PSU and thermals:

```bash
vcgencmd get_throttled    # 0x0 = OK; non-zero bits = under-voltage / throttling flags
vcgencmd measure_temp
```

The servo rail must come from the buck converter, not the Pi 5 V pin — see [Hardware & Wiring](hardware.md).
