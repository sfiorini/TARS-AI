# Troubleshooting

Known symptoms on the V3 fork, their cause, and the fix. Logs first: `tail -f src/logs/tars-ai.log` on the Pi, or the sys tab / `GET /api/console/logs` in the web UI. Servo events appear as `SERVO:` / `WARNING:` / `ERROR:` lines.

## TARS answers with empty replies

**Fixed in this fork.** Mistral's GLM models (`zai-glm-5-3-flash` via `[LLM] llm_backend = other`, `base_url = https://api.mistral.ai`) stream `delta.content` as a **list of content blocks**; upstream code joined the blocks as strings → `TypeError` → the reply was discarded (repeated in `tars-ai.log`). V3 normalizes it in `content_text()` (private `_delta_text` alias kept) (`module_llm.py`; also applied to vision reads in `module_vision.py`; tests `test_module_llm_delta.py`, `test_module_vision_glm.py`). If you still see empty replies: confirm you're on `V3` (`git branch`), check the key in `.env` (`OTHER_API_KEY`), and that `base_url` + `other_model` name a chat-completions endpoint. If `json_mode = True` is rejected by your backend, set it to `False`.

## STT is slow or hears nothing

- If configured, primary STT is **ElevenLabs Scribe `scribe_v2`** (`stt_processor = elevenlabs`, model `elevenlabs_stt_model`, `ELEVENLABS_API_KEY`; template default is `sherpa-onnx`); on provider failure (missing key, HTTP error, request error) it falls back to the external server (`POST {external_url}/save_audio`, Bearer `EXTERNAL_API_KEY`, **30 s timeout** — whisper on a shared-CPU x86_64 server measured ~14 s). An empty Scribe transcript means no speech was detected — the turn simply ends, logged `INFO: ElevenLabs STT returned empty text — treating turn as no speech` (no fallback re-record). Cloud Scribe is usually the fastest; local `sherpa-onnx` avoids network latency entirely.
- Server mode: `curl -s http://<server-host>:5678/health` must answer, and `.env` `EXTERNAL_API_KEY` must equal the server host's `config-server.ini` `api_key`.
- Mic: raise `mic_amp_gain` if too quiet; lower `silence_margin` for more sensitivity; check `speechdelay` isn't cutting you off; `use_indicators = True` gives audible beeps when listening starts/stops.

## TARS is silent / TTS keeps failing over

The ordered chain never hangs: on any provider failure it logs a typed reason and moves on, e.g. `WARNING: TTS provider 'fishaudio' failed (QuotaExhausted: …) — failing over to 'speechify'`. Read the reason in the parentheses — it names the exact problem:

- **`QuotaExhausted`** — the provider has no credit left. Fish.audio free tier (`fishaudio_model = s2.1-pro-free`) exhausts with **HTTP 402**; top up your Fish account and set `fishaudio_model = s2.1-pro` in `src/config.ini` (config-only change, no code). Speechify reports it as `payment_required` / `spend_cap_exceeded`; ElevenLabs as a 429/401 with a quota body.
- **`RateLimited`** — 429 concurrency limit; the provider already did its one backoff retry. Wait or switch primary provider.
- **`AuthFailed`** — 401/4xx: bad or missing credentials for that provider; check the matching key in `.env`.
- **`TransientError`** — 5xx/timeout after the single retry; network or server-side, usually self-heals.

A **missing API key skips the provider before any HTTP request** (`WARNING: FISH_AUDIO_API_KEY not set — skipping Fish.audio TTS (no request sent)`; same pattern for `SPEECHIFY_API_KEY` / `ELEVENLABS_API_KEY` / `OPENAI_API_KEY`) — the key lives in `.env`, never in `config.ini`, and is never logged. If the whole chain yields nothing, `espeak` (always appended last) still speaks.

`ERROR: TTS per-utterance deadline exceeded before/during provider '…' — giving up/stopping` means the whole-chain deadline (`tts_timeout` × chain length) ran out — raise `[TTS] tts_timeout` or shorten `tts_fallback_list`.

If playback itself fails: `WARNING: all PortAudio devices failed (…) — falling back to pw-play` means the USB card was wedged and TARS played the chunk through PipeWire's native API (pw-play subprocess, bypassing the dead ALSA device) — see [Voice Pipeline](voice-pipeline.md) for the full recovery ladder.

## App says it's moving but the servos are silent

The **PCA9685 MODE2 glitch**: the chip still ACKs on I2C, but with MODE2 ≠ `0x04` the outputs don't drive (`0x01` = silent, `0x0B`/`0x1E` = high-impedance). Check and repair:

```bash
curl -b /tmp/tars.cookie http://<pi-ip>/servo_status     # mode2 must be 4; mode1 must have bit 0x20
curl -b /tmp/tars.cookie -X POST http://<pi-ip>/servo_reinit
```

The 2 s watchdog usually repairs this by itself (re-init + re-apply of the **engaged** channels' saved positions from `~/.servo_positions.json`; idle repair writes zero PWM registers, so the robot doesn't jump). If the app is stopped: `sudo i2cset -y 1 0x40 0x01 0x04` and `sudo i2cset -y 1 0x40 0x00 0xA0`. See [Servo Watchdog & Recovery](servo-watchdog.md). If `i2c_error_count` in `/servo_status` keeps rising, suspect wiring or servo-rail power (buck converter + 1000 µF cap across V+/GND — see [hardware.md](hardware.md), including the green-V+ batch defect).

## Wake word doesn't trigger

`[STT] sensitivity` is **1–10: 1 = strict, 10 = lenient** (corrected in this fork — upstream docs had it backwards). Template mode threshold: 0.70 at sens 1 → 0.40 at sens 10; ONNX model mode: 0.80 → 0.40. **Raise the number if TARS doesn't hear you; lower it if it fires randomly.** Also check `wake_word_processor` / `atomik_mode` (`template` needs ~5 on-device recordings of the wake word; `model` uses the ONNX model from `tools/wakeword-trainer`), and that the `vad_*` gates aren't rejecting you.

## App won't stop / port 80 stays busy

`python3 App-Stop.py` uses TERM→KILL escalation (`pkill` → 2 s `pgrep` poll → `pkill -9`) because pygame/SDL swallows the first SIGTERM. If `pgrep -af '[p]ython app.py'` still shows a process, run `App-Stop.py` again. To restart without rebooting the Pi: `POST /reboot_program` in the web UI, or `python3 App-Stop.py && ./tars-launcher.sh` (see [Running TARS](running.md)).

## Web UI redirects to /login or port 80 is refused

- Login uses `[ACCESS] webui_password` — the template default is `tarspassword`; change it in `src/config.ini` and restart.
- Port 80 needs `cap_net_bind_service`: `tars-launcher.sh` sets it via `sudo setcap 'cap_net_bind_service=+ep'` on the venv's Python binary. If you started `app.py` by hand without the launcher, either use the launcher or set `[ACCESS] webui_port` to something > 1024 (e.g. 8080).

## I2C devices missing

`i2cdetect -y 1` should show `0x40` (PCA9685) and `0x41` (INA260). If not: I2C not enabled (`sudo raspi-config` → Interface Options → I2C), wiring/polarity, or the green-terminal V+ batch defect (power the rail via the V+ header pin — [hardware.md](hardware.md)). Battery test: `cd src && source .venv/bin/activate && python ina260_Battery_Sensor_Test.py`.

## Power / thermal problems

```bash
vcgencmd get_throttled    # 0x0 = OK; non-zero = under-voltage / throttle flags
vcgencmd measure_temp
```

Servo rail must come from the buck converter, never the Pi 5 V pin.

## The deferred reply never arrives

After a tool call (web search, camera, script) TARS says a holding line but the final answer never comes:

1. **Hop limit** — a follow-up that keeps asking for more tools stops at `[LLM] tool_loop_max_hops` (default 2) and TARS speaks `"I hit a snag finishing that — check the logs."`. Raise the limit if the task genuinely needs more rounds.
2. **Tool timeout** — a tool slower than `[LLM] tool_call_timeout` (default 60 s) is fed to the model as `ERROR: timeout`; the model usually answers anyway. Raise the timeout for tools that regularly take longer, and check the tool's own log lines for why it is slow.
3. **Barge-in cancellation** — speaking again while a tool is running cancels the in-flight turn by design (epoch cancellation); the results are discarded and the new utterance is processed fresh. Look for `TOOLLOOP: turn cancelled` in the log — that is correct behavior, not a bug.
4. **LLM request failure** — the follow-up completion failing (network, provider) also ends in the snag fallback line. The request timeout is `[LLM] request_timeout` (default 120 s).
5. **Last resort** — set `[LLM] deferred_loop_enabled = false` and restart: result-bearing tools fall back to the old inline blocking behavior for one release while you investigate.

## Tests failing / docs build failing

`python3 -m pytest src/tests -q` must be fully green on V3 (the tests fake GPIO/I2C and bootstrap `src/config.ini` from the template, so they run anywhere with `pip install pytest`). Docs: `pip install -r requirements-docs.txt && mkdocs build --strict` — CI runs exactly this in `.github/workflows/docs.yml` on every push to `V3`.

## Where things live

| What | Where |
|---|---|
| App log | `src/logs/tars-ai.log` |
| Config | `src/config.ini` (seeded from `src/config.ini.template`) |
| Secrets | `.env` (gitignored — never commit) |
| Servo positions | `~/.servo_positions.json` |
| Movement store | `src/movements_defaults.json` (tracked) + `src/movements.json` (live, gitignored) via `src/modules/module_movementstore.py` |
| Servo controller + watchdog | `src/modules/module_servoctl.py` |

## Web UI unreachable

- Port 80 needs privilege: `tars-launcher.sh` runs `sudo setcap 'cap_net_bind_service=+ep' "$(which python3)"` — if you started the app another way, re-run the launcher, or set `[ACCESS] webui_port = 8080`.
- Stuck at `/login`: check `[ACCESS] webui_password` (template default `tarspassword`); `webui_enabled = False` removes the login gate entirely.
- Find the Pi's IP on the Pi itself: `hostname -I`.

## App won't stop / autostart misbehaving

- `python3 App-Stop.py` uses TERM→KILL escalation (pygame/SDL swallows the first SIGTERM) and kills `xterm`, `App-Start.py`, and `python app.py`. If anything survives: `pkill -9 -f "python app.py"`.
- Autostart issues: check `autostart.log` in the repo root and `~/.config/autostart/tars-ai.desktop`; toggle with `./toggle-autostart.sh`. Autostart is XDG (needs the graphical session, `DISPLAY=:0` via `lp.sh`), not systemd.

## Config changes not applying

`src/config.ini` is read once at startup. After editing the file or using the web UI Config tab (`POST /save_config`), restart the app via `POST /reboot_program` (web UI) or `python3 App-Stop.py && ./tars-launcher.sh` — no full Pi reboot needed.

The table above ("Where things live") is the quick reference for key files — character card `src/character/TARS/TARS.json` and Piper voice `src/character/TARS/voice/TARS.onnx(.json)` round it out.
