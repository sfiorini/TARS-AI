# Voice Pipeline

The full loop from wake word to spoken reply — **wake word → STT → LLM (with RAG memory, skills, emotion) → TTS** — orchestrated by `src/modules/module_main.py` using `module_stt.py`, `module_llm.py`, `module_tts.py`. All knobs are in `[STT]`, `[LLM]`, `[TTS]`, `[EMOTION]` (see [Configuration](configuration.md)).

## 1. Wake word

| Key | Meaning |
|---|---|
| `wake_word` | activation phrase, default `hey tars` (2–3 syllables works best) |
| `wake_word_processor` | engine, independent of STT: `sherpa-onnx \| atomik \| fastrtc` on Pi5 (template default `atomik`) |
| `atomik_mode` | `auto` (ONNX model if present, else template) / `model` (ONNX only — train with `tools/wakeword-trainer`) / `template` (cosine similarity against ~5 on-device recordings) |
| `sensitivity` | **1–10: 1 = strict (hard to trigger), 10 = lenient (easy)** — corrected in this fork; upstream docs said the opposite |

Threshold curves in `module_stt._detect_wake_word_atomik`: template mode 0.70 at sens 1 → 0.40 at sens 10; model mode 0.80 → 0.40. **Raise the number if TARS stops hearing you; lower it if it activates randomly.** Also tune `mic_amp_gain` (template default 13.0) and `silence_margin` (default 3.0, lower = more sensitive). Wake gates: `vad_transcript_verify`, `vad_speaker_verify`, `vad_presence_gate`.

## Audio input supervision

The mic capture path is supervised by **two independent layers**, because either can fail alone:

1. **Hub watchdog daemon** — `module_mic.start_hub_watchdog()`, started at boot by `module_main.startup_initialization()` (a watchdog failure logs a WARNING and never blocks boot). A 2 s tick inspects the shared audio hub **even when no listener is active**: the stream-gone-with-consumers hole and TTS-recovery deafness both happen with the wake listener closed, which the listener-side layer cannot see.
2. **The `listenForWakeWord` deadline** (`module_atomik`) — while a listener is active, a 10 s dual-signal deadline. It resets **only** when the detector's check counter advances (check-advance implies chunk-advance, since detection runs from the RT callback). Chunks advancing while detection stays frozen is the swallowed-exception mode and must **not** reset the deadline. After 3 failed L1 recoveries the listener raises `AudioStallError`, which the STT loop escalates to L2.

Both are needed: the watchdog covers the no-listener case, the listener deadline covers in-listen wedges with per-detector diagnostics (`exit` reason, window RMS, check counter).

### The L1/L2 escalation ladder

| Rung | Trigger | Action |
|---|---|---|
| L1 | chunk age ≥ **5 s** with consumers registered (or the listener's 10 s dual-counter stall) | invalidate + reopen the stream — rate-limited to **1 per 10 s** |
| L2 | still stale **10 s** after a fresh L1 | `restart_sound_server()` (~22 s PipeWire restart) |
| CRITICAL | still stale **≥ 60 s** after L2 | repeat L2 at ≥ 60 s cadence with a CRITICAL log — never silent |

Digital silence has its own guard: fresh chunks but **> 60 s** of all-zero audio enters the same ladder independently of the chunk-age signal (a wedged stream often delivers silence, not nothing). A single healthy tick resets the whole ladder; zero consumers never alarms (a grace close is legitimate); `recovery_in_progress` suppresses alarms during the ~22 s L2.

### AUDIO-HUB log lines

A healthy heartbeat logs every 30 s while consumers exist, and every stale transition logs an immediate WARNING discriminator. `rate` is the hub chunk rate, `check_rate` the live listener's detect-counter rate; `check_rate`/`rms` show `-` when no wake listener is active. Each escalation rung logs its own line:

```text
AUDIO-HUB: cb=2(atomik,spectrum) rd=1 stream=open/active rate=63.0/s last_chunk=0.01s last_nonzero=0.4s check_rate=62.5/s exc=0 stalls=0 state=HEALTHY
WARNING: AUDIO-HUB STALE mode=callbacks-stopped chunk_age=7.2s check_rate=0.0/s rms=0.013 exc=3 cb=[atomik,spectrum] -> L1-reopen
WARNING: AUDIO-HUB still stale mode=callbacks-stopped 10s after L1 -> L2 restart_sound_server
CRITICAL: AUDIO-HUB still stale mode=callbacks-stopped 60s after L2 — repeating L2 restart_sound_server
```

`mode=` is one of `callbacks-stopped` | `detect-stalled` | `digital-silence`.

### The four failure modes

The instrumentation distinguishes exactly these:

| Mode | Signature in the AUDIO-HUB lines |
|---|---|
| (a) callbacks stopped | `mode=callbacks-stopped`: `chunk_age` grows past 5 s, `rate=0.0/s` |
| (b) silence delivered | `mode=digital-silence`: chunks fresh (`rate` healthy, `chunk_age` small) but `last_nonzero` age grows past 60 s — or is absent from the very first chunk for ≥ 60 s (muted / echo-cancelled source) — and `rms` ≈ 0 |
| (c) swallowed exception / detect stalled | `mode=detect-stalled`: chunks advance (`chunk_age` small) but `check_rate=0.0/s` — the listener's check counter is frozen while the hub delivers; `exc` shows swallowed consumer exceptions |
| (d) clock latch | a backward wall-clock step made cooldowns permanent — eliminated by the monotonic-clock fix (every deadline uses `time.monotonic()`, which has no NTP step); the listener-side `WAKE stall` line's `exit=cooldown` with healthy rates would be its fingerprint |

While a wake listener is live, `module_atomik.listenForWakeWord` also logs its own per-detector diagnostics on every stall (`hub_chunk`, `check`, `exit` reason, window `rms`) — the listener-side deadline covers mode (c) even between watchdog ticks.

### BLAS thread pools

`App-Start.py` sets `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` (via `os.environ.setdefault`, so a user-set value always wins) **before the venv Python starts** — numpy reads these at import, so setting them inside `app.py` would be too late. BLAS/OpenMP thread pools spin-wait and burn a core on the Pi even when idle. If you launch `app.py` directly, bypassing `App-Start.py`, export both variables first.

## 2. Speech-to-text

`stt_processor`: `sherpa-onnx | fastrtc | silero | openai | external | elevenlabs`.

**Optional on Pi5: ElevenLabs Scribe** (template default is `sherpa-onnx`; set `stt_processor = elevenlabs` to use Scribe). `stt_processor = elevenlabs` POSTs the recorded utterance to `https://api.elevenlabs.io/v1/speech-to-text` with `ELEVENLABS_API_KEY`, model `[STT] elevenlabs_stt_model` (**`scribe_v2`**, live-verified on the robot), timeout 15 s. On a missing key, HTTP error, or request exception it **falls back to the external server** (`_transcribe_with_server`): `POST {external_url}/save_audio` with `Authorization: Bearer $EXTERNAL_API_KEY` and a **30 s timeout** (server-side whisper on a shared-CPU x86_64 server measured ~14 s). An HTTP-200 **empty transcript is the expected no-speech outcome** — the turn ends immediately with `INFO: ElevenLabs STT returned empty text — treating turn as no speech` (no fallback, no re-record). Set `external_url = http://<server-host>:5678` in [server mode](server-mode.md).

Related: `vad_method` (end-of-speech; `smart-turn` default on Pi5), `speechdelay` (pre-processing silence, tenths of a second; 15 = 1.5 s), `enable_bargein` + `bargein_mode` (`fuzzy | voiceprint`) + `bargein_sensitivity` (1–10, 10 = easiest to interrupt TARS mid-sentence), `speaker_id_enabled`/`speaker_id_threshold` (passive speaker tagging), `use_indicators` (beep on listen start/stop), `sherpa_onnx_denoise`/`sherpa_onnx_punctuation`.

## 3. LLM — called directly from the Pi

`module_llm.py` POSTs to **your own endpoint from the Pi**: `[LLM] llm_backend = other`, `base_url` (e.g. `https://api.mistral.ai`), `other_model` (e.g. **`zai-glm-5-3-flash`**), key from `OTHER_API_KEY` as `Authorization: Bearer …`, to `{base_url}/v1/chat/completions` (deepinfra uses `/v1/openai/chat/completions`). The prompt is assembled from the character card + `systemprompt` + RAG memories + skill definitions; `json_mode = True` requests the structured `{"reply", "function_calls", "new_memories"}` reply that the router then executes.

!!! note "Fork fix: GLM/Mistral list-block content"
    Mistral's GLM models stream `delta.content` as a **list of blocks** (`[{"type":"thinking",…},{"type":"text","text":…}]`). Upstream joined the blocks as strings → `TypeError` → the reply was silently discarded (empty answers in `tars-ai.log`). This fork normalizes it in `content_text()` (`module_llm.py`), keeping only `type == "text"` blocks; the same fix is applied to vision reads in `module_vision.py`. Regression tests: `src/tests/test_module_llm_delta.py` (8), `test_module_vision_glm.py` (2).

## Deferred reply loop (D1)

When the model calls a **result-bearing tool** (web search, camera capture, scripts — anything whose result the user is waiting for), the turn enters the deferred reply loop (`module_toolloop.py`): TARS says a short holding line, the tool runs, then a single follow-up LLM completion composes the final spoken reply from the results. **Fire-and-forget tools** (movements, volume, discord sends) never enter the loop — they keep dispatching in the background exactly as before, so a movement is never run twice.

```text
IDLE ──start_turn──► CLASSIFY
  CLASSIFY ──no result-bearing──► IDLE        (caller's existing side-effect thread dispatches; the loop does nothing)
  CLASSIFY ──result-bearing──► TOOLS_RUNNING
  TOOLS_RUNNING ──all done/timeout──► FOLLOWUP_LLM
  FOLLOWUP_LLM ──parsed has result-bearing calls & hops left──► TOOLS_RUNNING
  FOLLOWUP_LLM ──done──► SPEAK_FOLLOWUP ──► IDLE
  any state ──epoch mismatch──► CANCELLED
  any state ──overall deadline──► TIMEOUT (speak fallback, IDLE)
```

- **Holding line**: reply-0 doubles as the natural holding line (the model already says "let me check that") — the loop never repeats it. When reply-0 is empty, the character pack's `thinking_responses` play, and if that mechanism is disabled/empty the loop speaks the hardcoded `"On it — I'll report back."`
- **Hop bound**: `[LLM] tool_loop_max_hops` (default 2) — a follow-up that itself asks for more tools loops at most this many times; at the limit with work still pending, TARS speaks `"I hit a snag finishing that — check the logs."`
- **Timeouts**: per-tool `tool_call_timeout` (default 60 s), per-LLM-request `request_timeout` (default 120 s); overall turn deadline = `max_hops × (tool_call_timeout + request_timeout)`. A timed-out tool is fed to the model as `ERROR: timeout` so it can recover.
- **Cancellation**: every new utterance bumps a per-source epoch; a stale turn goes `CANCELLED` and its results are discarded — they are never injected into the new turn's context. If you speak again while a tool is running, the running turn is abandoned and the new utterance is processed fresh.
- **Rollback**: set `[LLM] deferred_loop_enabled = false` and restart — result-bearing builtin tools fall back to the old inline blocking behavior for one release.

## 4. Text-to-speech — configurable ordered provider chain

`generate_tts_audio()` in `module_tts.py` walks an **ordered provider chain** built at call time:

```
chain = dedup([ttsoption] + [TTS] tts_fallback_list) + ["espeak"]   # espeak always last
```

- `ttsoption` is still the primary selector (web UI unchanged): `espeak | piper | silero | elevenlabs | openai | fishaudio | speechify | other | external`.
- `tts_fallback_list` (comma-separated; a JSON array like `["speechify","piper"]` also works) holds the rest of the chain. Template default: `ttsoption = piper`, `tts_fallback_list = external,piper,espeak` (fully offline).
- On the robot, `scripts/configure_tars_client.sh` sets the local-first chain: `ttsoption = piper` (stock voice `piper_voice = en_US-hfc_male-medium`), `tts_fallback_list = fishaudio,elevenlabs,speechify,external,espeak` → **piper → fishaudio → speechify → elevenlabs → external → espeak**.
- Duplicates are tried once (first occurrence wins); unknown names are skipped with a warning; `other` is not a chain entry itself — it falls through to `tts_fallback_list`, whose `external` entry uses `ttsurl`.

The first provider that yields ≥ 1 audio chunk wins and its chunks are streamed; the chain then stops. Failures are **typed** (hierarchy defined in `module_fishaudio.py`, shared by all providers): `QuotaExhausted` (no credit), `RateLimited`, `AuthFailed`, `TransientError`. Every failover is logged with the reason, e.g. `WARNING: TTS provider 'fishaudio' failed (QuotaExhausted: …) — failing over to 'speechify'`. 429 / 5xx / timeout get exactly **one backoff retry** inside the provider, then fail over — no infinite loops — and a per-utterance deadline (`tts_timeout` × chain length) bounds the whole chain so TARS never hangs. A provider that already yielded audio and then dies mid-stream keeps what was spoken (no voice switch mid-utterance). A missing API key skips the provider **before any HTTP request** (the key is never logged); empty text short-circuits the whole chain.

### Providers

| Provider | Module | Request | Error classification |
|---|---|---|---|
| `fishaudio` | `module_fishaudio.py` | `POST https://api.fish.audio/v1/tts` — `Authorization: Bearer $FISH_AUDIO_API_KEY` **plus a `model` header sent on every request** (`fishaudio_model`, default `s2.1-pro-free`; omitting it silently bills the paid model). **Streaming is the default** (`fishaudio_stream = True`): HTTP-streamed request body `{text, reference_id, format: "pcm", sample_rate: 44100, latency: fishaudio_latency}` → raw 16-bit mono PCM coalesced into ~1 s WAV slices (88 200 B at 44.1 kHz) and played as they arrive — time-to-first-audio ≈ **0.4 s**. `fishaudio_stream = False` = the whole clip in one request: body `{text, reference_id, format: "wav", latency: fishaudio_latency}` → raw wav bytes | 402 → `QuotaExhausted`; 429 (concurrency, no `Retry-After`) → one retry → `RateLimited`; 4xx (except 408, which is transient) → `AuthFailed`; 408/5xx/timeout → one retry → `TransientError` |
| `speechify` | `module_speechify.py` | `POST https://api.speechify.ai/v1/audio/stream` — Bearer `$SPEECHIFY_API_KEY`, body `{input, voice_id, model, output_format}` (`speechify_voice_id` = `george`, `speechify_model` = `simba-3.5`, `speechify_output_format` = `pcm_24000` default) → raw 16-bit mono PCM over chunked transfer, coalesced into ~1 s WAV slices (verified output: 24 kHz mono 16-bit — `framerate=24000, channels=1, sampwidth=2`). Latency, measured on the robot (warm session): **server first byte ≈ 0.1 s** (`Server-Timing` ttfb ≈ 0.105 s), but the **first playable** ~1 s slice lands ≈ **0.9 s** after the call (0.929 s measured) — the client coalesces raw PCM into ~1 s slices, so nothing is playable before a full slice accumulates. `speechify_stream = False` = the legacy path: `POST /v1/audio/speech` per sentence, body `{input, voice_id, audio_format: "wav", model}` → JSON `{audio_data: base64}` | pre-stream errors branch on body `error.code` exactly as before (`payment_required`/`spend_cap_exceeded`/`spend_budget_exceeded`/`spend_limit_exceeded` → `QuotaExhausted`; `rate_limited`/`concurrency_limit_reached` → `RateLimited` honoring `Retry-After`; 401/4xx → `AuthFailed`; 408/5xx/timeout → one retry → `TransientError`). A **mid-stream abort ends the utterance** with a WARNING — no mid-utterance voice switch |
| `elevenlabs` | `module_elevenlabs.py` | `POST https://api.elevenlabs.io/v1/text-to-speech/{elevenlabs_voice_id}/stream` — Bearer `ELEVENLABS_API_KEY`, `elevenlabs_model = eleven_multilingual_v2` (or the character-card voice when `toggle_charvoice = True`) | 429/401 with a quota body → `QuotaExhausted`; 429 otherwise → one retry → `RateLimited`; 401/4xx → `AuthFailed`; 5xx/timeout → one retry → `TransientError` |
| `openai` | `module_openai.py` | OpenAI TTS SDK — `openai_voice`, Bearer `OPENAI_API_KEY` | 429 with quota body (`insufficient_quota`) → `QuotaExhausted`; 429 → `RateLimited`; 401/4xx → `AuthFailed`; timeout/5xx → `TransientError` |
| `external` | `module_tts.py` (`_external_tts_chunks`) | `POST {ttsurl}/tts/generate` — Bearer `EXTERNAL_API_KEY`, optional `voice` = `tts_voice` → raw audio | server-side Piper on the server host |
| `piper` | `module_piper.py` | local, offline; model **lazy-loaded on first use** (`PiperVoice.load()` at the first synthesis) and **pre-loaded in a background warmup thread at startup** when `piper` is anywhere in the fallback chain — no blocking load at boot, no per-utterance reload once warm; synthesis runs **OFF the asyncio event loop** (`asyncio.to_thread`) — one WAV chunk per sentence at the voice's native rate (22050 Hz), so STT, barge-in and the web UI stay responsive during synthesis; `speaker_id` is passed via `SynthesisConfig` (piper-tts ≥ 1.3 / the robot's 1.8.0) | no chunks when the model is not loaded ⇒ chain moves on; with `piper_multispeaker`, the detected emotion selects `piper_speaker_<emotion>` |
| `silero` | `module_silero.py` | local, offline | — |
| `espeak` | `module_espeak.py` | local, offline | always the guaranteed final fallback |

All cloud providers (`fishaudio`, `speechify`, `elevenlabs`, `openai`) and `external` use the per-request HTTP timeout `[TTS] tts_timeout` (default **30 s**), read from config at call time.

**Fish.audio tuning**: `fishaudio_stream` (**True** default) switches the request between HTTP streaming (`format: pcm`, `sample_rate: 44100`, ~1 s WAV slices, time-to-first-audio ≈ 0.4 s) and the whole-clip wav path (`False`, one wav per sentence). `fishaudio_latency` (`low | balanced | normal`) is sent as the request `latency` field — template default **`low`** (fastest first audio); invalid values fall back to `balanced` in the code. **Playback volume**: `tts_volume` (**0.0–2.0**, default **1.0**, clamped at both ends) scales every chunk on every provider/path — 1.5 was the old hardcoded gain.

**Speechify tuning**: `speechify_stream` (**True** default) sends the whole utterance as ONE streamed `/v1/audio/stream` request. Measured on the robot (warm session, `pcm_24000`, 5 chunks for a short sentence): the server's **first byte** arrives ≈ 0.1 s in (`Server-Timing` ttfb ≈ 0.105 s), but the **first playable** slice — TARS actually starting to speak — is ≈ **0.9 s** (0.929 s measured), because the client coalesces the raw PCM into ~1 s WAV slices before anything is playable. Still far ahead of the old per-sentence path, which could not start a 4-sentence reply faster than ~4 s on the Free plan's 1 request/second. `speechify_output_format` (`pcm_24000` default — fastest first audio; `pcm_44100` avoids the local resample; any `pcm_<rate>`) sets the stream rate; non-pcm values fall back to `pcm_24000` with a warning. One `requests.Session` is reused across utterances (warm TTFB ≈ 0.2 s vs ≈ 0.7 s cold).

### Is local Piper fast enough on a Pi 5?

Yes — measured on the robot (Pi 5, `piper-tts 1.8.0`, `sox 14.4.2`, voice lazy-loaded and kept in memory, native rate **22050 Hz**, C2 FX chain live): a one-sentence sample synthesized as **2 chunks** with **time-to-first-chunk 0.280 s** and total synthesis **1.183 s** — inside the RPi5 edge-TTS benchmark band of RTF ≈ 0.10–0.13 (≈ 8–10× realtime) for low/medium voices; high-quality voices sit around RTF 0.66, still fine as a fallback. Synthesis runs **off the event loop** (`asyncio.to_thread`), so STT, barge-in and the web UI stay responsive while a sentence renders. So the local chain (`piper` → `espeak`) is a realistic no-cloud path, not a last-ditch whisper.

**Behavior change (speaker IDs)**: `piper_speaker_<emotion>` now actually takes effect — it is passed as `SynthesisConfig(speaker_id=…)` instead of a dead `voice.config.speaker_id` assignment that no piper-tts version read (multispeaker voices silently used speaker 0 = "anger"). Escape hatch: set `piper_multispeaker = False`, or set the `piper_speaker_*` ids back to `0` to reproduce the old (accidental) sound.

### Voice sources & attribution

- **Fish.audio** uses the public **“Tars” voice** by brenardspag — `fishaudio_reference_id = 6a57e35f5c8244dbb5d012a3c434ec16` (<https://fish.audio/m/6a57e35f5c8244dbb5d012a3c434ec16/>), set on the robot by `scripts/configure_tars_client.sh`. To clone your own voice instead, run `FISH_AUDIO_API_KEY=... ./scripts/setup_tts_voice.sh` — the sample is freesound #815828 by RICHERlandTV, **CC BY 4.0** (see `ATTRIBUTION.md` in the repo root).
- **Speechify**: list the catalog voices to pick the flattest male one — `curl -s https://api.speechify.ai/v1/voices -H "Authorization: Bearer $SPEECHIFY_API_KEY" | python3 -m json.tool` — then set `speechify_voice_id`; wrap the input in `<prosody pitch="-10%" rate="95%">…</prosody>` SSML for a flatter robot read.
- **Local Piper voice selection**: `[TTS] piper_voice` (**empty** default) picks the voice model, resolved at call time (not import time). Empty = the character voice `character/<name>/voice/<name>.onnx`, auto-downloaded from the TARS-AI Hugging Face repo — the historical behaviour, unchanged for existing installs. A stock **Piper** voice id — e.g. `en_US-hfc_male-medium` — stores the voice as `character/<name>/voice/<piper_voice>.onnx` and auto-downloads it from the upstream [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) repo, so a manual voice swap survives a reinstall. The download URL is derived from the id `{lang}_{REGION}-{name}-{quality}` (split on `-`: first segment = locale, last = quality `low|medium|high`, everything in between = speaker name, which may itself contain underscores, e.g. `libritts_r`): `https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang}/{locale}/{name}/{quality}/{voice_id}.onnx` (+ `.onnx.json`) — e.g. `en_US-hfc_male-medium` → `en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx`, `en_GB-alan-medium` → `en/en_GB/alan/medium/en_GB-alan-medium.onnx`. An id that does not match the expected shape logs a WARNING and falls back to the character voice (never a guessed URL). Stock voices are single-speaker (22050 Hz) — set `piper_multispeaker = False` when using one.
- **Local Piper robot FX**: `piper_robot_fx = True` with `piper_fx = pitch -150 flanger 5 3 0 70 0.4 triangle 25 quad gain -n -3` — a sox chain applied post-synthesis in `module_piper.py` (`apply_piper_fx`). Requires `sox` installed; if sox is missing or the chain fails, the raw Piper audio is played instead. (The robot's `configure_tars_client.sh` writes its own re-auditioned chain, `pitch -50 …`, for the `en_US-hfc_male-medium` timbre.)

Attribution for all voice assets lives in `ATTRIBUTION.md` (repo root) with a note in `LICENSE`.

## 5. Emotion

`[EMOTION] enabled = True`; `emotion_method = llm` (the LLM returns the emotion inside its JSON — zero extra RAM) or `classifier` (local HuggingFace `emotion_model`, ~500 MB RAM, 28 fine-grained emotions; use the ONNX variant `SamLowe/roberta-base-go_emotions-onnx`). The emotion drives the avatar sprites (`/character_sprite/<emo>/…`), the RoboEyes mood (`/api/eyes/mood`), TTS voice selection, and is stored in memory.

## 6. Memory (RAG)

Every exchange can add `new_memories`; `[RAG]` controls retrieval (`strategy = hybrid`, `top_k = 5`, `recency_boost_days = 7`, embeddings from `embedding_source = local | openai | external`). Memories are browsable/editable in the web UI dashboard tab.

## Pipeline at a glance

```
mic ──► wake word (atomik, sensitivity 1-10) ──► record until EOS (vad_method)
                                                            │
                                          STT: elevenlabs (scribe_v2) ──fallback──► external {external_url}/save_audio (30 s)
                                                            │
                                          + RAG memories + vision caption + emotion + speaker id
                                                            │
                                          LLM: {base_url}/v1/chat/completions (streamed SSE, content_text fix)
                                                            │
                                          reply text ──► TTS chain: ttsoption ──► tts_fallback_list ──► … ──► espeak
                                          (robot: fishaudio ──► speechify ──► elevenlabs ──► external /tts/generate ──► piper ──► espeak)
                                                            │
                                          speaker + on-device avatar (emotion) + web UI chat
```

## STT processor availability by Pi model

From the `[STT]` template comments (mix wake-word and STT engines freely — they are independent):

| Pi | `stt_processor` options | `wake_word_processor` options | `vad_method` options |
|---|---|---|---|
| Pi5 | sherpa-onnx, fastrtc, silero, openai, external, elevenlabs | sherpa-onnx, atomik, fastrtc | smart-turn, sherpa-onnx, silero, rms |
| Pi4 | sherpa-onnx, openai, external | sherpa-onnx, atomik | smart-turn, sherpa-onnx, silero, rms |
| Pi3 | openai, external | sherpa-onnx, atomik | sherpa-onnx, rms |
| PiZero2 | openai | atomik | rms |

## Barge-in and speaker ID

- `enable_bargein = True` lets you interrupt TARS mid-sentence; `bargein_mode = fuzzy` (word matching against the playing text) or `voiceprint` (`speaker_id` cosine match, threshold `vad_speaker_threshold`); `bargein_sensitivity` 1–10 scales the gate (1 = hard to interrupt, 10 = very sensitive) — `module_stt.py` lines ~222-230.
- `speaker_id_enabled = True` passively tags who spoke (`speaker_id_threshold = 0.75`); the name goes into the LLM context.
- `use_indicators = True` plays a beep when listening starts/stops.

## Module map

| Stage | File |
|---|---|
| Wake word / VAD / STT / barge-in | `src/modules/module_stt.py`, `module_atomik.py`, `module_silero.py` |
| LLM (streaming, prompt building, memories) | `src/modules/module_llm.py`, `module_prompt.py`, `module_memory.py`, `module_hyperdb.py` |
| TTS chain | `src/modules/module_tts.py` (chain runner `_effective_chain` / `generate_tts_audio`), `module_fishaudio.py`, `module_speechify.py`, `module_elevenlabs.py`, `module_openai.py`, `module_piper.py`, `module_espeak.py`, `module_silero.py` |
| Emotion | `src/modules/module_emotion` path via `[EMOTION]` (`classifier` HF model or `llm`) |
| Vision | `src/modules/module_vision.py` |
| Orchestration / state / queue | `src/modules/module_main.py`, `module_state.py`, `module_messageQue.py`, `module_router.py` |
