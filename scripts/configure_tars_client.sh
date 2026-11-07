#!/bin/bash
# Configure the TARS client (robot): Fish.audio cloud TTS primary with an
# ordered fallback chain (speechify -> elevenlabs -> external mini server ->
# local piper -> espeak); ElevenLabs cloud STT primary; vision/embeddings
# offloaded to mini.
# Also sets the robot voice config: Fish.audio public "Tars" voice
# (fishaudio_reference_id) and the local-Piper sox robot-FX chain.
#
# Usage: ./scripts/configure_tars_client.sh <EXTERNAL_API_KEY>
# Idempotent: safe to run multiple times. [LLM] is intentionally untouched.
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <EXTERNAL_API_KEY>"
    echo "  <EXTERNAL_API_KEY> is the api_key from the mini server's config-server.ini"
    exit 1
fi

sed --version 2>/dev/null | grep -q 'GNU sed' || { echo "ERROR: this script requires GNU sed (standard on the robot's Debian)" >&2; exit 1; }

API_KEY="$1"
if [[ "$API_KEY" =~ [^A-Za-z0-9._~+/-] ]]; then
    echo "ERROR: API key contains unexpected characters (expected url-safe token chars)" >&2
    exit 1
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/src/config.ini"
ENV_FILE="$ROOT/.env"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: $CONFIG not found — run from a TARS-AI checkout with src/config.ini present"
    exit 1
fi

CONFIG_BACKUP="$(mktemp)"
cp "$CONFIG" "$CONFIG_BACKUP"

# set_kv <SECTION> <KEY> <VALUE>
# Replaces KEY = ... inside [SECTION] (section-scoped: [LLM] also has a
# base_url which must NOT change). Inserts after the section header if the
# key is missing; appends a new section at EOF if the section is missing.
set_kv() {
    local section="$1" key="$2" value="$3"
    if grep -q "^\[${section}\]" "$CONFIG"; then
        if sed -n "/^\[${section}\]/,/^\[/p" "$CONFIG" | grep -qE "^[[:space:]]*${key}[[:space:]]*="; then
            sed -i "/^\[${section}\]/,/^\[/{s|^[[:space:]]*${key}[[:space:]]*=.*|${key} = ${value}|}" "$CONFIG"
        else
            TMP="$(mktemp)"
            awk -v sect="$section" -v kv="${key} = ${value}" '
                $0 ~ "^\\[" sect "\\][[:space:]]*$" { print; print kv; next }
                { print }
            ' "$CONFIG" > "$TMP" && mv "$TMP" "$CONFIG"
        fi
    else
        printf '\n[%s]\n%s = %s\n' "$section" "$key" "$value" >> "$CONFIG"
    fi
}

# --- [TTS]: Fish.audio primary; ordered fallback chain on the robot:
# fishaudio -> speechify -> elevenlabs -> external (mini) -> piper (local) -> espeak ---
set_kv TTS ttsoption fishaudio
set_kv TTS tts_fallback_list speechify,elevenlabs,external,piper,espeak
set_kv TTS ttsurl http://mini:5678

# --- [TTS]: voice/model config ---
# Fish.audio model: s2.1-pro-free = free developer tier. When the free offer ends
# and requests start failing with HTTP 402, top up the account and change this to
# s2.1-pro in src/config.ini (config-only change).
set_kv TTS fishaudio_model s2.1-pro-free
# Fish.audio latency mode: low (fastest) | balanced | normal (best quality)
set_kv TTS fishaudio_latency low
# Fish.audio voice: set only if empty, so a custom voice id in config.ini is kept.
# Default: public "Tars" voice by brenardspag — https://fish.audio/m/6a57e35f5c8244dbb5d012a3c434ec16/
# (API key comes from FISH_AUDIO_API_KEY in .env — never written here)
if ! sed -n "/^\[TTS\]/,/^\[/p" "$CONFIG" | grep -qE "^[[:space:]]*fishaudio_reference_id[[:space:]]*=[[:space:]]*[^[:space:]]"; then
    set_kv TTS fishaudio_reference_id 6a57e35f5c8244dbb5d012a3c434ec16
fi
# Local-Piper robot voice: sox FX chain applied post-synthesis (needs sox installed)
set_kv TTS piper_robot_fx True
set_kv TTS piper_fx "chorus 0.5 0.9 50 0.4 0.25 2 -t overdrive 10 pitch -100"

# --- [STT]: ElevenLabs Scribe (cloud) primary; external (mini) whisper fallback ---
set_kv STT stt_processor elevenlabs
set_kv STT external_url http://mini:5678

# --- [VISION]: external (mini) BLIP captioning ---
set_kv VISION vision_processor external
set_kv VISION use_llm_backend False
set_kv VISION base_url http://mini:5678

# --- [RAG]: external (mini) embeddings ---
set_kv RAG embedding_source external
set_kv RAG embedding_url http://mini:5678

# --- .env: upsert EXTERNAL_API_KEY (delete existing line, append new) ---
touch "$ENV_FILE"
sed -i -E '/^[[:space:]]*(export[[:space:]]+)?EXTERNAL_API_KEY[[:space:]]*=/d' "$ENV_FILE"
echo "EXTERNAL_API_KEY=\"$API_KEY\"" >> "$ENV_FILE"

echo "=== src/config.ini changes ==="
diff -u "$CONFIG_BACKUP" "$CONFIG" || true
rm -f "$CONFIG_BACKUP"
echo "=== .env ==="
echo "EXTERNAL_API_KEY updated (value hidden)"
echo "Client configuration complete."
