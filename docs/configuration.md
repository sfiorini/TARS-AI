# Configuration

All runtime settings live in two files at the repo root: **`src/config.ini`** (INI, one section per subsystem — seeded from `src/config.ini.template` by `Install.sh`) and **`.env`** (API secrets — seeded from `.env.template`, loaded via `python-dotenv` in `module_config.py`). The web UI config editor reads/writes `src/config.ini` (`GET /get_config`, `POST /save_config`; hover the (i) icon next to any key for its description). After editing, restart TARS (`POST /reboot_program` — see [Running TARS](running.md)).

!!! warning "Never commit secrets"
    `.env` and `src/config.ini` are gitignored. Keys stay on the robot only — never in git, issues, or chat.

## `.env` keys (`.env.template`)

| Key | Used by |
|---|---|
| `OPENAI_API_KEY` | `[LLM] llm_backend = openai`; OpenAI STT/TTS/vision |
| `GROK_API_KEY` | `llm_backend = grok` |
| `DEEPINFRA_API_KEY` | `llm_backend = deepinfra` |
| `OTHER_API_KEY` | `llm_backend = other` — e.g. **Mistral**: `base_url = https://api.mistral.ai`, `other_model = zai-glm-5-3-flash` |
| `ELEVENLABS_API_KEY` | Scribe STT (`module_stt.py`), ElevenLabs TTS (`module_elevenlabs.py`) |
| `EXTERNAL_API_KEY` | Bearer key for the companion mini server — sent as `Authorization: Bearer …` by the STT (`/save_audio`), TTS (`/tts/generate`) and vision clients |
| `VISION_API_KEY` | dedicated vision API key (falls back to the LLM key) |
| `DISCORD_TOKEN` | `skill_discord` |
| `HA_TOKEN` | `skill_home_assistant` |

`module_config.get_api_key()` maps the active `[LLM] llm_backend` to its env var and injects it as `CONFIG['LLM']['api_key']`, sent as `Authorization: Bearer <key>`.

## `src/config.ini` sections (`src/config.ini.template`)

### `[CHAR]` — character & user
`character_card_path` (relative to `src/`, default `character/TARS/TARS.json`), `user_name`, `user_details`, `location_name`, `latitude`/`longitude` (location-aware answers), `responses` (phrases said right after the wake word, JSON array), `enable_thinking_responses` + `thinking_responses` (filler phrases while the LLM thinks).

### `[LLM]` — the brain, called directly from the Pi
`llm_backend` (`openai | grok | deepinfra | other`), `base_url` (used by `other`; the Pi POSTs to `{base_url}/v1/chat/completions`), `openai_model` / `grok_model` / `other_model`, `systemprompt`, `contextsize` (2000–8000 history tokens), `max_tokens`, `seed` (-1 = random), `temperature`, `top_p`, `json_mode` (disable for backends without `response_format`), `override_encoding_model` (tokenizer, `cl100k_base` default).

### `[STT]` — wake word + speech-to-text
`language`, `wake_word` (2–3 syllables works best), `wake_word_processor` (`sherpa-onnx | atomik | fastrtc` on Pi5), `atomik_mode` (`auto | model | template`), `sensitivity` (**1–10: 1 = strict, 10 = lenient** — corrected in this fork; see [Voice Pipeline](voice-pipeline.md)), wake gates (`vad_transcript_verify`, `vad_speaker_verify`, `vad_presence_gate`), `stt_processor` (`sherpa-onnx | fastrtc | silero | openai | external | elevenlabs`), `external_url`, `elevenlabs_stt_model` (`scribe_v2`), `vad_method` (`smart-turn | sherpa-onnx | silero | rms`), `use_indicators` (beeps), `enable_bargein` / `bargein_mode` (`fuzzy | voiceprint`) / `bargein_sensitivity`, `speechdelay` (tenths of a second), `speaker_id_enabled` / `speaker_id_threshold`, `mic_amp_gain`, `silence_margin`, `sherpa_onnx_denoise`, `sherpa_onnx_punctuation`.

### `[TTS]` — speech output
`ttsoption` (`espeak | piper | silero | elevenlabs | openai | other | external`), `ttsurl` + `tts_voice` (external server), `toggle_charvoice` (use the character card's voice), `elevenlabs_voice_id`, `elevenlabs_model` (`eleven_multilingual_v2 | …`), `openai_voice`, `piper_multispeaker` + `piper_speaker_<emotion>` (0–7) mapping emotion to speaker id.

### `[EMOTION]`, `[VISION]`, `[RAG]`
`[EMOTION]`: `enabled`, `emotion_method` (`llm` — zero extra RAM | `classifier` — local HF model ~500 MB), `emotion_model` (use the ONNX variant, e.g. `SamLowe/roberta-base-go_emotions-onnx`).
`[VISION]`: `enabled` (~1 GB RAM), `camera_rotation` (0/90/180/270), `vision_processor` (`blip | llm | openai | external`/`server_hosted`), `use_llm_backend`, `base_url`, `vision_model`, `vision_max_tokens`.
`[RAG]`: `enabled`, `enable_topic_tracking`, `strategy` (`hybrid` recommended | `naive`), `context_window`, `max_memories`, `top_k`, `recency_boost_days`, `embedding_source` (`local | openai | external`), `embedding_url`.

### `[ACCESS]`, `[UI]`, `[CONTROLS]`, `[BATTERY]`, `[SERVO]`
`[ACCESS]`: `webui_enabled`, `webui_port` (80 = no port in the URL), `webui_password`, `webui_theme` (auto-discovered from `css/themes/`).
`[UI]`: on-device display — `screen_width`/`screen_height`, `UI_enabled`, `use_camera_module`, `show_mouse`, `fullscreen`, `show_time`, `show_cpu_temp`, `ampm_format`, `app` (`terminal | dashboard | clock | eyes`), `target_fps`, `font_size`, `screensaver_timer`, `screensaver_cycle_interval`, `screensaver_list`.
`[CONTROLS]`: gamepad `enabled`, `ventilate`, `voicemovement` (**required** for voice-triggered movement), `swap_turn_directions`, `invert_y`, `controller_name`.
`[BATTERY]`: `battery_enabled`, `battery_auto_shutdown`, `battery_capacity_mAh`, `battery_initial_voltage`, `battery_cutoff_voltage` (INA260 @ 0x41 — see [Hardware](hardware.md)).
`[SERVO]`: `arms_present` (gates all `has_arms` movements), leg calibration (`leftUpHeight`, `leftDownHeight`, `rightUpHeight`, `rightDownHeight`, `forwardLeftLeg`, `backLeftLeg`, `forwardRightLeg`, `backRightLeg`, `perfect*Offset` trims) and arm ranges (`leftMainMin/Max`, `leftForarmMin/Max`, `leftHandMin/Max` + right equivalents + offsets).

### `[SKILL:<name>]`
Per-skill config and enable flag, e.g. `[SKILL:discord] disabled = true`. The template disables discord, home_assistant, generate_image, generate_music, network_camera, launch_retropie by default. Toggle via the web UI (`POST /toggle_skill`) or by editing the file.

## Pointing the robot at the mini server

`scripts/configure_tars_client.sh <EXTERNAL_API_KEY>` rewrites `src/config.ini` idempotently (section-scoped `sed`; **`[LLM]` is deliberately untouched**) and upserts `EXTERNAL_API_KEY` in `.env`:

```bash
./scripts/configure_tars_client.sh "$API_KEY_FROM_MINI"
# [TTS]    ttsoption = elevenlabs,  ttsurl = http://mini:5678
# [STT]    stt_processor = elevenlabs, external_url = http://mini:5678
# [VISION] vision_processor = external, use_llm_backend = False, base_url = http://mini:5678
# [RAG]    embedding_source = external, embedding_url = http://mini:5678
```

It prints a diff of exactly those changes. The Bearer key is then sent automatically on STT/TTS/vision calls. See [Server Mode](server-mode.md).

## Editing config from the web UI vs by hand

Both write the same file. The web UI config editor (`GET /get_config` / `POST /save_config`) exposes every key of `src/config.ini.template` with its description — hover the (i) icon. Hand-editing is fine too; `module_config.load_config()` re-reads the file (with a small cache), and `module_servoctl.refresh_config()` hot-reloads all `[SERVO]` offsets without touching the PCA9685. Either way, **restart the app** (`POST /reboot_program`) for most changes to take effect.

```bash
curl -b /tmp/tars.cookie http://<pi-ip>/get_config | python3 -m json.tool | head -20
```

## Related files

- `src/dashboard.template.ini` — dashboard layout template used by the web UI.
- `src/config.ini` — your live config (gitignored; seeded from the template by `Install.sh`).
- `.env` — secrets (gitignored; seeded from `.env.template`). `module_config.py` loads it with `python-dotenv` at import time, so a running app picks up `.env` changes only after restart.
- `[SKILL:<name>]` sections — created on demand when a skill defines a `config` schema or is toggled in the web UI; per-skill values are exposed to the skill as `context["skill_config"]`.

!!! note "Section-scoped edits"
    `configure_tars_client.sh` never touches `[LLM]` even though it also has a `base_url` — its `sed` replacements are scoped between the section header and the next `[` line, so `[LLM] base_url` (your direct LLM endpoint) is preserved verbatim.

## Editing config from the shell

`src/config.ini` is INI — edit it directly or with `sed` (section-scoped, like the script does):

```bash
# change wake word sensitivity
sed -i '/^\[STT\]/,/^\[/s/^sensitivity[[:space:]]*=.*/sensitivity = 8/' src/config.ini
# change web UI port (then restart the app)
sed -i '/^\[ACCESS\]/,/^\[/s/^webui_port[[:space:]]*=.*/webui_port = 8080/' src/config.ini
```

Then restart: web UI Config tab → reboot button (`POST /reboot_program`), or `python3 App-Stop.py && ./tars-launcher.sh`. Config is read once at startup — edits never apply live.

## Skills sections

Every `src/skills/skill_*.py` gets an optional `[SKILL:<name>]` section in `config.ini` for `disabled` and custom keys; the template ships with `discord`, `home_assistant`, `generate_image`, `generate_music`, `network_camera`, `launch_retropie` disabled by default. The web UI Config tab lists all discovered skills with toggles (`GET /get_skills`, `POST /toggle_skill`, `POST /save_skill_config`). Authoring new skills: [Development](development.md).
