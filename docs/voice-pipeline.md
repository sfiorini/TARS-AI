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

## 2. Speech-to-text

`stt_processor`: `sherpa-onnx | fastrtc | silero | openai | external | elevenlabs`.

**Optional on Pi5: ElevenLabs Scribe** (template default is `sherpa-onnx`; set `stt_processor = elevenlabs` to use Scribe). `stt_processor = elevenlabs` POSTs the recorded utterance to `https://api.elevenlabs.io/v1/speech-to-text` with `ELEVENLABS_API_KEY`, model `[STT] elevenlabs_stt_model` (**`scribe_v2`**, live-verified on the robot), timeout 15 s. On a missing key, empty transcript, or request error it **falls back to the external server** (`_transcribe_with_server`): `POST {external_url}/save_audio` with `Authorization: Bearer $EXTERNAL_API_KEY` and a **30 s timeout** (server-side whisper on a shared-CPU mini measured ~14 s). Set `external_url = http://mini:5678` in [server mode](server-mode.md).

Related: `vad_method` (end-of-speech; `smart-turn` default on Pi5), `speechdelay` (pre-processing silence, tenths of a second; 15 = 1.5 s), `enable_bargein` + `bargein_mode` (`fuzzy | voiceprint`) + `bargein_sensitivity` (1–10, 10 = easiest to interrupt TARS mid-sentence), `speaker_id_enabled`/`speaker_id_threshold` (passive speaker tagging), `use_indicators` (beep on listen start/stop), `sherpa_onnx_denoise`/`sherpa_onnx_punctuation`.

## 3. LLM — called directly from the Pi

`module_llm.py` POSTs to **your own endpoint from the Pi**: `[LLM] llm_backend = other`, `base_url` (e.g. `https://api.mistral.ai`), `other_model` (e.g. **`zai-glm-5-3-flash`**), key from `OTHER_API_KEY` as `Authorization: Bearer …`, to `{base_url}/v1/chat/completions` (deepinfra uses `/v1/openai/chat/completions`). The prompt is assembled from the character card + `systemprompt` + RAG memories + skill definitions; `json_mode = True` requests the structured `{"reply", "function_calls", "new_memories"}` reply that the router then executes.

!!! note "Fork fix: GLM/Mistral list-block content"
    Mistral's GLM models stream `delta.content` as a **list of blocks** (`[{"type":"thinking",…},{"type":"text","text":…}]`). Upstream joined the blocks as strings → `TypeError` → the reply was silently discarded (empty answers in `tars-ai.log`). This fork normalizes it in `_delta_text()` (`module_llm.py`), keeping only `type == "text"` blocks; the same fix is applied to vision reads in `module_vision.py`. Regression tests: `src/tests/test_module_llm_delta.py` (8), `test_module_vision_glm.py` (2).

## 4. Text-to-speech — fallback chain

`generate_tts_audio()` in `module_tts.py` tries, in order:

1. **`elevenlabs`** (`ttsoption = elevenlabs`; `elevenlabs_voice_id`, `elevenlabs_model = eleven_multilingual_v2`) — or the character-card voice when `toggle_charvoice = True`.
2. **External Piper on the mini** — `POST {ttsurl}/tts/generate` (Bearer `EXTERNAL_API_KEY`, 30 s timeout) — used when ElevenLabs yields no audio.
3. **Local piper** (`module_piper.py`) — with `piper_multispeaker`, the detected emotion selects `piper_speaker_<emotion>`.
4. **espeak** (`module_espeak.py`) — final fallback.

`ttsoption` may also be set directly to `piper | espeak | silero | openai | other | external`.

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
                                          LLM: {base_url}/v1/chat/completions (streamed SSE, _delta_text fix)
                                                            │
                                          reply text ──► TTS: elevenlabs ──► external /tts/generate ──► local piper ──► espeak
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
| TTS chain | `src/modules/module_tts.py`, `module_elevenlabs.py`, `module_piper.py`, `module_espeak.py`, `module_silero.py` |
| Emotion | `src/modules/module_emotion` path via `[EMOTION]` (`classifier` HF model or `llm`) |
| Vision | `src/modules/module_vision.py` |
| Orchestration / state / queue | `src/modules/module_main.py`, `module_state.py`, `module_messageQue.py`, `module_router.py` |
