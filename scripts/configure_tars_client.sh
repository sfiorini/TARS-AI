#!/bin/bash
# Configure the TARS client (robot) to offload STT/vision/embeddings to the
# mini app-server and enable ElevenLabs TTS with external fallback.
#
# Usage: ./scripts/configure_tars_client.sh <EXTERNAL_API_KEY>
# Idempotent: safe to run multiple times. [LLM] is intentionally untouched.
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <EXTERNAL_API_KEY>"
    echo "  <EXTERNAL_API_KEY> is the api_key from the mini server's config-server.ini"
    exit 1
fi

API_KEY="$1"
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
            awk -v sect="$section" -v kv="${key} = ${value}" '
                { print }
                $0 == "[" sect "]" { print kv }
            ' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
        fi
    else
        printf '\n[%s]\n%s = %s\n' "$section" "$key" "$value" >> "$CONFIG"
    fi
}

# --- [TTS]: ElevenLabs primary, external (mini) fallback ---
set_kv TTS ttsoption elevenlabs
set_kv TTS ttsurl http://mini:5678

# --- [STT]: external (mini) whisper ---
set_kv STT stt_processor external
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
sed -i '/^[[:space:]]*EXTERNAL_API_KEY[[:space:]]*=/d' "$ENV_FILE"
echo "EXTERNAL_API_KEY=\"$API_KEY\"" >> "$ENV_FILE"

echo "=== src/config.ini changes ==="
diff -u "$CONFIG_BACKUP" "$CONFIG" || true
rm -f "$CONFIG_BACKUP"
echo "=== .env ==="
echo "EXTERNAL_API_KEY updated (value hidden)"
echo "Client configuration complete."
