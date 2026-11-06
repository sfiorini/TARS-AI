# Server Mode

`TARS-AI_Server/` is a **companion server** that offloads the heavy AI workloads from the Raspberry Pi to a more powerful machine on your network (the "server host"). The robot keeps the wake word, the LLM chat, and orchestration on-device, and calls the server over HTTP with a Bearer key. Client-side setup is one script: `scripts/configure_tars_client.sh` (see [Configuration](configuration.md)).

## Minimum hardware requirements

Any always-on machine reachable from the robot over the LAN works — the server is **CPU-only**:

| Resource | Minimum | Notes |
|---|---|---|
| CPU | 4+ x86_64 cores | whisper STT is the heaviest load (~10–15 s per utterance on 4 shared cores; faster on more cores) |
| RAM | 16 GB | whisper + BLIP vision + embeddings models loaded together |
| Disk | ~20 GB free | the `models` Docker volume caches whisper/vision/embedding models |
| GPU | **not required** | the Dockerfile installs CPU-only torch |
| OS | Linux with Docker (or any OS for the non-Docker run) | Ubuntu 24.04 x86_64 is a known-good setup |
| Network | LAN reachability from the Pi | the robot calls `http://<server-host>:5678` |

## The server: FastAPI app on :5678

- **`TARS-AI_Server/app-server.py`** — FastAPI + uvicorn, default **port 5678** (`[server] port` in `config-server.ini`; `--port` to override).
- **Auth**: `config-server.ini` → `[server] api_key = …`. On first boot with an empty key the server **auto-generates one** (`secrets.token_urlsafe(24)`) and logs it. All API requests must send `Authorization: Bearer <api_key>` (otherwise 401/403). The web UI (`/`, `/playground`, `/ui`, `/docs`) accepts the session cookie *or* the Bearer token.
- **Endpoints**: `GET /health`, `POST /save_audio`, `POST /transcribe` (STT — faster-whisper + Silero VAD; also `WS /ws/stt`), `POST /tts/generate` (Piper ONNX), `GET /tts/voices`, `POST /caption` (vision BLIP), `POST /v1/embeddings` (RAG, `all-MiniLM-L6-v2`), `POST /generate_image` (Stable Diffusion), `POST /generate_music` (ACE-Step). Services can be limited with `--services stt tts …`.
- Non-Docker run on any OS: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-server.txt && python app-server.py` (Windows: `run-server.bat`). Details in `TARS-AI_Server/README.txt`.

## Docker deploy (CPU-only) on the server host

The repo ships a CPU-only container (the server host has no GPU):

- **`Dockerfile`**: `python:3.11-slim`; installs `espeak-ng` + `libgomp1` (OpenMP for torch/whisper); installs **CPU torch first** (`pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu`, after stripping the CUDA index lines from `requirements-server.txt`) so the later `pip install -r` doesn't pull a CUDA build; copies `app-server.py` + `www/`. `EXPOSE 5678`, `CMD ["python", "app-server.py"]`.
- **`docker-compose.yml`** (`services: tars-server`):
  - `ports: "5678:5678"`
  - `./config-server.ini:/app/config-server.ini` — **the file must exist on the host before `docker compose up`**; if absent, Docker creates a *directory* at the mount point and the server starts with auth disabled.
  - `models:/app/models` — named volume; all model loaders (whisper, vision, embeddings) use explicit `cache_dir` under `/app/models`, so downloads persist across rebuilds.
  - `./voices:/app/tts` — bind mount for the Piper voice weights (`TARS.onnx` + `TARS.onnx.json`). Only `TARS.onnx` is absent from the repo (gitignored); `TARS.onnx.json` is committed — copy them from the robot's `src/character/TARS/voice/` onto the server host first, or the server logs "Found 0 voices".
  - `restart: unless-stopped` + a healthcheck that GETs `http://127.0.0.1:5678/health` every 30 s (5 s timeout, 120 s start period for model loading).

## Deploy path on the server host: /srv, root workflow

All docker projects on the server host live under **`/srv`** (root-owned), so git and docker commands there run as root:

```bash
# one-time clone
ssh <server-host> 'sudo git clone -b V3 https://github.com/sfiorini/TARS-AI.git /srv/TARS-AI'

# seed config (generate a key yourself) and bring the container up
ssh <server-host> 'sudo su - -c "cd /srv/TARS-AI/TARS-AI_Server && \
  printf \"[server]\nport = 5678\napi_key = \$(openssl rand -urlsafe 24)\n\" > config-server.ini && \
  mkdir -p voices && \
  docker compose up -d --build"'

# wait for health, then read the key to hand to the robot
curl -s http://<server-host>:5678/health
ssh <server-host> 'sudo grep ^api_key /srv/TARS-AI/TARS-AI_Server/config-server.ini'
```

!!! note
    `config.ini` and `.env` are gitignored on both machines, so config changes deploy via committed scripts (`scripts/configure_tars_client.sh`), not via `git pull` overwrites.

## Client side: point the robot at the server host

On the Pi, once:

```bash
./scripts/configure_tars_client.sh <EXTERNAL_API_KEY>
```

Sets `[TTS] ttsoption = elevenlabs` + `ttsurl = http://<server-host>:5678`, `[STT] stt_processor = elevenlabs` + `external_url = http://<server-host>:5678`, `[VISION] vision_processor = external, use_llm_backend = False, base_url = http://<server-host>:5678`, `[RAG] embedding_source = external, embedding_url = http://<server-host>:5678`, and upserts `EXTERNAL_API_KEY` in `.env`. `[LLM]` stays on the Pi.

## Verifying the server

```bash
KEY=$(ssh <server-host> 'sudo grep ^api_key /srv/TARS-AI/TARS-AI_Server/config-server.ini' | cut -d= -f2 | tr -d ' ')

curl -s http://<server-host>:5678/health                                          # no auth needed
curl -s -o /dev/null -w '%{http_code}\n' http://<server-host>:5678/save_audio    # expect 401 or 403 without key
curl -s -H "Authorization: Bearer $KEY" http://<server-host>:5678/tts/voices     # list Piper voices (expect TARS)
curl -s -H "Authorization: Bearer $KEY" -F "audio=@test.wav" http://<server-host>:5678/save_audio
curl -s -H "Authorization: Bearer $KEY" -X POST http://<server-host>:5678/tts/generate \
     -H 'Content-Type: application/json' -d '{"text":"hello","voice":"TARS"}' -o tts.wav
curl -s -H "Authorization: Bearer $KEY" -X POST http://<server-host>:5678/v1/embeddings \
     -H 'Content-Type: application/json' -d '{"input":"hello"}' | head -c 120
```

The robot's runtime flow: ElevenLabs Scribe is primary STT, the server host's whisper (`external_url`, 30 s timeout) is the automatic fallback; TTS is ElevenLabs → external Piper → local piper → espeak (see [Voice Pipeline](voice-pipeline.md)).

## `config-server.ini`

Seeded on the host before `docker compose up` (see the warning above). Minimal form:

```ini
[server]
port = 5678
api_key = <random-token>     # auto-generated on first boot if left empty
```

The same file also holds per-service model choices (`[stt]`, `[tts]`, `[vision]`, `[embeddings]`, … — see the defaults inside `app-server.py` around line 391, e.g. whisper model, `all-MiniLM-L6-v2` embeddings); the web UI at `http://<server-host>:5678/ui` edits them.

## Services and CLI

| Service | Endpoint(s) | Model |
|---|---|---|
| STT | `POST /save_audio`, `POST /transcribe`, `WS /ws/stt` | faster-whisper + Silero VAD |
| TTS | `POST /tts/generate`, `GET /tts/voices` | Piper ONNX (voice from `./voices`) |
| Vision | `POST /caption` | BLIP captioning |
| Embeddings | `POST /v1/embeddings` | sentence-transformers `all-MiniLM-L6-v2` |
| Image gen | `POST /generate_image` | Stable Diffusion (diffusers) |
| Music gen | `POST /generate_music` | ACE-Step |
| LLM (optional) | `/v1/chat/completions`-compatible | Qwen / llama.cpp GGUF |

CLI: `python app-server.py [--services stt tts …] [--port 8080] [--llm-model Qwen/Qwen3-8B] [--no-imagegen --no-embeddings]`. Models download to `/app/models` (the `models` volume) on first use — expect several GB on first boot, which is why the compose healthcheck has a 120 s start period.

## Operating the container

```bash
ssh <server-host> 'sudo su - -c "cd /srv/TARS-AI/TARS-AI_Server && docker compose ps"'
ssh <server-host> 'sudo su - -c "cd /srv/TARS-AI/TARS-AI_Server && docker compose logs -f --tail=100"'
ssh <server-host> 'sudo su - -c "cd /srv/TARS-AI/TARS-AI_Server && docker compose restart"'
ssh <server-host> 'sudo su - -c "cd /srv/TARS-AI/TARS-AI_Server && docker compose up -d --build"'   # after git pull
ssh <server-host> 'sudo su - -c "docker system df"'   # reclaim space if models volumes pile up
```

## `config-server.ini` layout

Defaults written by `app-server.py` (`_CONFIG_DEFAULTS`):

```ini
[server]     port = 5678, api_key = <generated on first boot if empty>
[services]   stt/tts/llm/vision = true; imagegen/musicgen/embeddings = false
[stt]        whisper_model = large-v3, compute_type = auto, vad_filter = true, device = auto
[llm]        model = Qwen/Qwen3-4B, backend = auto, n_ctx = 4096, ...
[tts]        voices_dir, default_voice, cache_size = 100
[vision]     model = Salesforce/blip-image-captioning-base
[embeddings] model = all-MiniLM-L6-v2
```

For the robot offload setup keep `llm = false` (the Pi calls Mistral directly) and enable `embeddings = true` — this avoids loading a multi-GB LLM on the server host. Change services via the server's own UI at `http://<server-host>:5678/ui` (`GET/POST /api/settings`) or edit the file and `POST /config/reload`.

## Endpoint reference (all API paths need `Authorization: Bearer <api_key>`)

| Method | Path | Purpose | Pi uses it as |
|---|---|---|---|
| GET | `/health` | status/uptime/device/latency | compose healthcheck |
| POST | `/save_audio` | upload WAV for STT | `[STT] external_url` fallback |
| POST | `/transcribe` | whisper transcription | — |
| POST | `/tts/generate` | Piper TTS → WAV | `[TTS] ttsurl` fallback |
| GET | `/tts/voices` | list Piper voices | — |
| POST | `/caption` | BLIP image caption | `[VISION] base_url` (`external`) |
| POST | `/v1/embeddings` | text embeddings | `[RAG] embedding_url` |
| POST | `/v1/chat/completions`, GET `/v1/models` | OpenAI-compatible LLM | optional `[LLM] base_url` |
| GET | `/models/status`, POST `/models/{svc}/unload\|reload` | model lifecycle | — |
| GET | `/`, `/playground`, `/ui`, `/docs`, `/logs` | dashboard / playground / settings / OpenAPI / logs | you |
