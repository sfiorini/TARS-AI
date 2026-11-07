# TARS-AI (sfiorini fork, V3)

TARS-AI is an open-source project that recreates **TARS**, the robot from the film *Interstellar*, as a physical Raspberry Pi-powered robot with an AI brain: wake-word listening, speech-to-text, an LLM personality driven by a character card, emotion detection, text-to-speech, camera vision, long-term memory (RAG), and servo-driven body movements you can trigger by voice or from the web UI. A companion server (`TARS-AI_Server/`) can offload the heavy AI workloads to a more powerful machine on your network.

!!! note "Fork status"
    This repository is the **sfiorini/TARS-AI fork**, branch **`V3`**, which diverges permanently from the upstream [TARS-AI-Community/TARS-AI](https://github.com/TARS-AI-Community/TARS-AI). `V3` is the one and only branch of this fork. The [upstream wiki](https://github.com/TARS-AI-Community/TARS-AI/wiki/Home) covers legacy material only — do not mix its instructions with these pages.

## What V3 adds over upstream

| Delta | Where |
|---|---|
| Self-healing servo watchdog: 15 s tick, MODE1 bit `0x20` + MODE2 `0x04` health checks, lazy PCA9685 re-init, stuck-MOVING reset (>180 s), position re-apply after repair | `src/modules/module_servoctl.py`, [servo-watchdog.md](servo-watchdog.md) |
| Forced `MODE2 = 0x04` at every PCA9685 init — the only output-drive state that moves servos on this board | `initialize_pca9685()` |
| Fork web endpoints `GET /servo_status` and `POST /servo_reinit` (no-reboot recovery) | `src/modules/module_chatui.py`, [web-ui.md](web-ui.md) |
| 33-entry movement registry (19 legs-only + 14 arm) exposed to the LLM via the `execute_movement` skill | `src/modules/module_movement_registry.py`, [movements.md](movements.md) |
| ElevenLabs Scribe `scribe_v2` cloud STT with external-server fallback (30 s timeout) | `src/modules/module_stt.py`, [voice-pipeline.md](voice-pipeline.md) |
| Configurable ordered TTS provider chain — Fish.audio → Speechify → ElevenLabs → external Piper → local Piper → espeak — with typed-error failover (quota / rate-limit / auth / timeout) and per-request timeouts | `src/modules/module_tts.py`, `module_fishaudio.py`, `module_speechify.py`, [voice-pipeline.md](voice-pipeline.md) |
| `_delta_text()` fix: GLM/Mistral `delta.content` list-of-blocks no longer produces empty LLM replies | `src/modules/module_llm.py` |
| `App-Stop.py` TERM→KILL escalation (pygame/SDL swallows the first SIGTERM) | `App-Stop.py`, [running.md](running.md) |
| Piper `./voices` bind mount in the server's docker-compose (voice weights are not in the repo) | `TARS-AI_Server/docker-compose.yml`, [server-mode.md](server-mode.md) |

## Documentation map

- [Quickstart](quickstart.md) — OS image, clone `-b V3`, `Install.sh`, `.env`/`config.ini`, first launch, tests.
- [Hardware & Wiring](hardware.md) — BOM summary, 3D-print folders, PCA9685 @ 0x40 (incl. the batch defect workaround), buck converter, INA260 @ 0x41, wiring SVGs.
- [Configuration](configuration.md) — every `src/config.ini` section and `.env` key; `scripts/configure_tars_client.sh` for pointing at the server host server.
- [Running TARS](running.md) — `tars-launcher.sh`, `App-Start.py`/`App-Stop.py`, XDG autostart, logs, restart without reboot.
- [Web UI](web-ui.md) — Flask dashboard on `[ACCESS] webui_port`, login, tabs, config editor, motion tab, `/servo_status` + `/servo_reinit`.
- [Voice Pipeline](voice-pipeline.md) — wake word sensitivity semantics, Scribe STT, direct-from-Pi LLM, the configurable TTS provider chain.
- [Movements](movements.md) — the movement registry, arm gating, custom sequences, servo tester, how to add a movement.
- [Server Mode](server-mode.md) — FastAPI `app-server.py` on :5678, Bearer auth, CPU-only Docker deploy at `/srv`.
- [Servo Watchdog & Recovery](servo-watchdog.md) — self-healing design, reading MODE1/MODE2, manual `i2cset` recovery.
- [Troubleshooting](troubleshooting.md) — empty LLM replies, STT latency, silent servos, wake word, logs, `vcgencmd get_throttled`.
- [Development](development.md) — repo layout, `src/.venv`, running the tests, authoring skills, CI, contributing.

## License & attribution

Based on **TARS-AI-Community** by **Charles-Olivier Dion (AtomikSpace)**, licensed under [CC-BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) — non-commercial use only, with attribution; commercial use of AtomikSpace contributions requires a separate written license.

- [LICENSE](https://github.com/sfiorini/TARS-AI/blob/V3/LICENSE) — CC-BY-NC 4.0 license text
- [DUAL-LICENSE.md](https://github.com/sfiorini/TARS-AI/blob/V3/DUAL-LICENSE.md) — which files are dual-licensed
- [ATTRIBUTION.md](https://github.com/sfiorini/TARS-AI/blob/V3/ATTRIBUTION.md) — attribution guidelines for derivative work

This documentation site is built with MkDocs Material (`mkdocs.yml`, pages in `docs/`) and deployed to GitHub Pages from branch `V3` by `.github/workflows/docs.yml` — see [Development](development.md).

## Requirements at a glance

- **Raspberry Pi 5** recommended for the full install (all local STT/TTS options, on-device UI, `lgpio` GPIO). Pi 4 / 3 / Zero 2 work with reduced local processing — `Install.sh` picks per-model requirements.
- **64-bit Raspberry Pi OS** (Debian 13 "Trixie" aarch64 is what this fork's robot runs), I2C enabled (`sudo raspi-config` → Interface Options → I2C).
- **API keys** in `.env` — at minimum one LLM key (`OTHER_API_KEY` for Mistral/OpenAI-compatible `base_url`, `OPENAI_API_KEY`, `GROK_API_KEY`, or `DEEPINFRA_API_KEY`); `ELEVENLABS_API_KEY` for Scribe STT / ElevenLabs TTS; optional `FISH_AUDIO_API_KEY` / `SPEECHIFY_API_KEY` for the Fish.audio / Speechify providers in the TTS chain.
- **Hardware** for the moving robot: PCA9685 servo driver (I2C `0x40`), buck converter + battery, optional INA260 (`0x41`), 10 servos, 3D-printed chassis, display/camera/mic/speaker — details in [Hardware & Wiring](hardware.md).
- Optional **companion server** (companion server) for whisper STT, Piper TTS, BLIP vision, embeddings — [Server Mode](server-mode.md).

## Typical workflow

1. Flash OS → `git clone -b V3` → `./Install.sh` → fill `.env` + `src/config.ini` → `./tars-launcher.sh` ([Quickstart](quickstart.md)).
2. Wire and calibrate servos with `src/app-servotester.py`, save values in `[SERVO]` ([Hardware](hardware.md), [Movements](movements.md)).
3. Tune behavior in `src/config.ini` or the web UI Config tab; restart via the UI (`POST /reboot_program`) — no Pi reboot needed ([Running](running.md), [Web UI](web-ui.md)).
4. If something misbehaves, start at [Troubleshooting](troubleshooting.md); servo weirdness almost always means checking `GET /servo_status` first ([Servo Watchdog](servo-watchdog.md)).

## Community & support

- Upstream community Discord: <https://discord.gg/AmE2Gv9EUt> (linked from the upstream README).
- Issues and code: <https://github.com/sfiorini/TARS-AI> (branch `V3`).
- Legacy upstream docs: [TARS-AI-Community wiki](https://github.com/TARS-AI-Community/TARS-AI/wiki/Home) — historical reference only; this fork does not track it.

## Typical setup

Two machines, one robot:

1. **The robot** — a Raspberry Pi 5 inside the 3D-printed body, running this repo from `~/TARS-AI`. It handles the wake word, the LLM chat (called directly from the Pi), orchestration, memory, and the web UI on port 80, and drives the PCA9685 servos over I2C.
2. **The server host (optional)** — a more powerful PC on the same network running `TARS-AI_Server/` in Docker on port 5678, offloading STT (whisper), TTS (Piper), vision captioning, and embeddings. The robot reaches it via `http://<server-host>:5678` with a Bearer key; see [Server Mode](server-mode.md).

## How a conversation flows

Wake word (`hey tars`) → STT (ElevenLabs Scribe, external whisper fallback) → LLM on the Pi (character card + RAG memory + skills, e.g. `execute_movement`) → TTS (configurable chain, e.g. Fish.audio → Speechify → ElevenLabs → external Piper → local piper → espeak) → avatar/emotion on the web UI and the on-device display. Movement commands go through the 33-entry registry to the servos, which are kept healthy by the 15 s watchdog. Details: [Voice Pipeline](voice-pipeline.md), [Movements](movements.md), [Servo Watchdog & Recovery](servo-watchdog.md).

## Hardware at a glance

PCA9685 servo driver at I2C `0x40` (servo rail from a buck converter — see the known batch defect warning), INA260 battery sensor at `0x41`, USB mic/speaker, optional display and CSI camera. Print files in `3d Printer Files/`; wiring diagrams in `Documentation/` (embedded in [Hardware & Wiring](hardware.md)).

## Reading this documentation

Each page starts with a one-paragraph overview and stays practical: real config keys, real file paths, real commands. Admonitions flag the non-obvious:

- `!!! note` — context worth knowing.
- `!!! warning` — gotchas that bite in practice (e.g. the PCA9685 green-terminal defect, MODE2 = 0x04, never committing `.env`).

The pages cross-link heavily; the nav in `mkdocs.yml` lists all twelve. If you only read three: [Quickstart](quickstart.md), [Configuration](configuration.md), [Troubleshooting](troubleshooting.md).

## Conventions used in these pages

- `<pi-ip>` — the Pi's LAN address (`hostname -I` on the robot).
- `<EXTERNAL_API_KEY>` / `<api_key>` — the Bearer token from the server host's `TARS-AI_Server/config-server.ini`.
- Code spans like `MODE2`, `arms_present`, `sensitivity` refer to `src/config.ini` keys or `module_servoctl.py` names exactly as in the repo.
- Shell examples assume you are on the Pi in the repo root (`~/TARS-AI`) unless noted; `curl` examples against the web UI assume a login cookie jar at `/tmp/tars.cookies` (see [Web UI](web-ui.md)).
