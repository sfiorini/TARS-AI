# Web UI

TARS-AI serves a **Flask + SocketIO dashboard from the Pi itself** — `src/modules/module_chatui.py` with templates in `src/www/templates/` — started via `socketio.run(flask_app, host="0.0.0.0", port=...)` where the port is `[ACCESS] webui_port` (**default 80**, so the URL is just `http://<pi-ip>/`). SocketIO pushes talking/emotion state to the avatar live.

## Login

Every page except `/login`, `/emotion`, `/start_talking`, `/stop_talking`, `/static` and `/socket.io` requires a session (enforced in a `before_request` hook; `webui_enabled = False` disables the requirement). `POST /login` with the `webui_password` from `[ACCESS]` (template default `tarspassword` — change it) sets a permanent session cookie:

```bash
# grab a session cookie, then use it for API calls
curl -c /tmp/tars.cookie -d "password=YOUR_WEBUI_PASSWORD" http://<pi-ip>/login   # 302 on success
curl -b /tmp/tars.cookie http://<pi-ip>/api/system/metrics
curl -b /tmp/tars.cookie http://<pi-ip>/servo_status
```

`/logout` clears the session.

## Tabs

Templates in `src/www/templates/tabs/`:

| Tab | Contents |
|---|---|
| **chat** | conversation view; voice pipeline test at `/test/voice_pipeline` |
| **dashboard** | memory graph, topics, mood, interactions (`/api/dashboard/*`) |
| **config** | full `config.ini` editor with per-key (i) descriptions |
| **motion** | movement buttons from the registry + sequence player |
| **legs** / **arms** / **body** | direct servo sliders per channel group |
| **builder** | step-sequence editor → movement store entries (`src/movements.json`) |
| **movements** | whole movement pool: play / edit / delete / reset (`/movements/*`) |
| **emotions** | emotion/sprite control (`POST /emotion`, `/api/eyes/mood`) |
| **sys** | system metrics (`/api/system/metrics`), console logs, reboot button, wifi |
| **wifi** | `/api/wifi/status`, networks, connect, hotspot |

Plus `/holo` — the standalone talking-head view (avatar sprites per emotion via `/character_sprite/<emo>/animation/<file>`, pushed over SocketIO as `emotion_change`).

## TTS provider selector

The config tab's TTS provider selector sets **only the primary provider** (`[TTS] ttsoption`) — changing it does **not** disable the fallback chain: the effective chain stays `dedup([ttsoption] + tts_fallback_list)` with `espeak` always appended last, so a failing primary still fails over (each failover is logged with its reason). To go fully offline, also set `tts_fallback_list = external,piper,espeak` (or shorter). See [Voice Pipeline](voice-pipeline.md) for the chain semantics.

## Key endpoints

| Endpoint | Purpose |
|---|---|
| `GET /get_config` / `POST /save_config` | config editor read/write |
| `GET /get_skills` / `POST /toggle_skill` / `POST /save_skill_config` | skill list, enable/disable, per-skill config (`[SKILL:*]`) |
| `GET /get_movements` | movement registry grouped by type (`reset_positions` first) |
| `POST /robot_move` | `{"direction": "forward"|"backward"|"left"|"right"|"*_slow"}` |
| `POST /move_legs`, `POST /move_arms`, `POST /neutral_legs`, `POST /reset_positions`, `POST /disable_servos` | direct servo control |
| `GET /get_arms_status` | `[SERVO] arms_present` |
| `POST /save_sequence`, `GET /get_saved_sequences`, `POST /play_saved_sequence`, `POST /delete_saved_sequence`, `POST /play_sequence` | builder sequences (see [Movements](movements.md)) |
| `GET /get_movement_steps/<name>` | one movement's step list for the builder editor (see [Movements](movements.md)) |
| `GET /agent_skills`, `POST /agent_skills`, `GET /agent_skills/<name>` (full skill detail for the editor), `PUT /agent_skills/<name>`, `DELETE /agent_skills/<name>`, `POST /agent_skills/install`, `POST /agent_skills/<name>/scripts`, `POST /agent_skills/reset` | the Agent Skills pool (see [Agent Skills](skills.md)) |
| `GET /movements`, `POST /movements`, `PUT /movements/<mid>`, `DELETE /movements/<mid>`, `POST /movements/reset` | movement pool CRUD — whole pool list, create/update (400 with the reason on invalid entries), delete (404 unknown), reset with `{"prune": false\|true}` (see [Movements](movements.md)) |
| `POST /process_llm` | send a text chat through the LLM pipeline |
| `POST /upload`, `GET /camera_feed`, `GET /audio_stream`, `GET /get_next_audio_chunk` | camera + audio streaming |
| `GET /api/console/logs` | recent lines of `src/logs/tars-ai.log` |
| `POST /reboot_program`, `GET /boot_id` | restart the app without rebooting the Pi ([running.md](running.md)) |
| `GET /servo_status`, `POST /servo_reinit` | **fork additions** — servo health and no-reboot recovery |
| `GET /mcp`, `POST /mcp`, `PUT /mcp/<name>`, `DELETE /mcp/<name>`, `POST /mcp/<name>/test` | MCP server registry + live status + connection test (see [MCP](mcp.md)) |
| `GET /api/wifi/*`, `GET /api/tunnel/*` | wifi + tunnel (QR) management |

## Motion tab

Driven by the **movement store** (`src/modules/module_movementstore.py`, rebuilt into the registry after every change): `GET /get_movements` returns every movement with id, display name, and type (`legs_only` / `has_arms` / `system`), so movements created in the **movements** tab appear automatically. Arm movements are gated when `[SERVO] arms_present = False`. The **builder** saves step sequences as movement store entries (id = name: lowercase, digits, underscores).

```bash
curl -b /tmp/tars.cookie http://<pi-ip>/get_movements | python3 -m json.tool | head -30
curl -b /tmp/tars.cookie -X POST http://<pi-ip>/robot_move \
     -H 'Content-Type: application/json' -d '{"direction": "forward"}'
```

## Servo health endpoints (fork addition)

See [Servo Watchdog & Recovery](servo-watchdog.md) for the full field reference:

```bash
# GET /servo_status — live health snapshot
curl -b /tmp/tars.cookie http://<pi-ip>/servo_status
# {"pca_alive": true, "moving": false, "last_pwm_age_s": 12.3,
#  "i2c_error_count": 0, "watchdog_running": true, "mode1": 161, "mode2": 4}

# POST /servo_reinit — no-reboot recovery: clears a stuck MOVING flag,
# re-creates the I2C/PCA9685 device, re-applies saved positions, returns new status
curl -b /tmp/tars.cookie -X POST http://<pi-ip>/servo_reinit
```

Healthy: `pca_alive: true`, `watchdog_running: true`, `mode1` has bit `0x20` set, `mode2` == `4` (OUTNE bits `0x0C` == `0x04` — the only drive state that moves servos on this board).

## Config editor from the shell

```bash
curl -b /tmp/tars.cookie http://<pi-ip>/get_config | python3 -m json.tool | head -20
curl -b /tmp/tars.cookie -X POST http://<pi-ip>/save_config \
     -H 'Content-Type: application/json' \
     -d '{"section": "ACCESS", "key": "webui_password", "value": "new-secret"}'
# then restart: curl -b /tmp/tars.cookie -X POST http://<pi-ip>/reboot_program
```
