#!/bin/bash
# Configure the TARS client (robot): local Piper TTS primary — the stock
# Piper voice en_US-hfc_male-medium (durable across reinstalls via
# piper_voice) — with an ordered cloud fallback chain (fishaudio ->
# elevenlabs -> speechify -> external server -> espeak); ElevenLabs cloud
# STT primary; vision/embeddings offloaded to the external server.
# Also sets the robot voice config: Fish.audio public "Tars" voice
# (fishaudio_reference_id) and the local-Piper sox robot-FX chain
# (re-auditioned for the hfc_male timbre: pitch -50 instead of the repo
# default's -150).
#
# Usage: ./scripts/configure_tars_client.sh <EXTERNAL_API_KEY>
# Idempotent: safe to run multiple times. [LLM] is intentionally untouched.
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <EXTERNAL_API_KEY>"
    echo "  <EXTERNAL_API_KEY> is the api_key from the external server's config-server.ini"
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

# Hostname of the external TTS/STT/vision/RAG server. Override per machine:
#   SERVER_HOST=myserver.local ./scripts/configure_tars_client.sh <KEY>
SERVER_HOST="${SERVER_HOST:-server.local}"
SERVER_URL="http://${SERVER_HOST}:5678"

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
# The rewrite runs in python3 with LITERAL string matching: keys and values
# may contain sed/regex metacharacters (| & / \ .) that would corrupt a sed
# s/// substitution or match the wrong lines. Values travel as argv, never
# interpolated into a script.
set_kv() {
    local section="$1" key="$2" value="$3"
    if ! grep -q "^\[${section}\]" "$CONFIG"; then
        printf '\n[%s]\n%s = %s\n' "$section" "$key" "$value" >> "$CONFIG"
        return
    fi
    python3 - "$CONFIG" "$section" "$key" "$value" <<'PYEOF'
import os
import sys

path, section, key, value = sys.argv[1:5]
with open(path, encoding="utf-8") as fh:
    lines = fh.readlines()

header = "[" + section + "]"
out = []
i = 0
n = len(lines)
while i < n:
    line = lines[i]
    if not line.startswith(header):
        out.append(line)
        i += 1
        continue
    # Section header found: rewrite its body (up to the next header or EOF).
    out.append(line)
    i += 1
    body = []
    replaced = False
    while i < n and not lines[i].startswith("["):
        body_line = lines[i]
        lhs, sep, _ = body_line.partition("=")
        if sep and lhs.strip() == key:  # literal key match, section-scoped
            body.append(f"{key} = {value}\n")
            replaced = True
        else:
            body.append(body_line)
        i += 1
    if not replaced:  # key missing in section: insert after the header
        body.insert(0, f"{key} = {value}\n")
    out.extend(body)

tmp = path + ".set_kv.tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.writelines(out)
os.replace(tmp, path)
PYEOF
}

# --- [TTS]: local Piper primary (stock voice); ordered cloud fallback chain
# on the robot: piper (local) -> fishaudio -> elevenlabs -> speechify ->
# external -> espeak ---
set_kv TTS ttsoption piper
set_kv TTS tts_fallback_list fishaudio,elevenlabs,speechify,external,espeak
set_kv TTS ttsurl "$SERVER_URL"

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
# Local-Piper voice: the stock Piper voice the user prefers (single speaker,
# 22050 Hz). Stored as character/<name>/voice/en_US-hfc_male-medium.onnx and
# downloaded from the upstream rhasspy/piper-voices repo — survives reinstalls.
set_kv TTS piper_voice en_US-hfc_male-medium
set_kv TTS piper_multispeaker False
# Local-Piper robot voice: sox FX chain applied post-synthesis (needs sox
# installed). Re-auditioned for the hfc_male timbre (pitch -50).
set_kv TTS piper_robot_fx True
set_kv TTS piper_fx "pitch -50 flanger 5 3 0 70 0.4 triangle 25 quad gain -n -3"

# --- [STT]: ElevenLabs Scribe (cloud) primary; external whisper fallback ---
set_kv STT stt_processor elevenlabs
set_kv STT external_url "$SERVER_URL"

# --- [VISION]: external BLIP captioning ---
set_kv VISION vision_processor external
set_kv VISION use_llm_backend False
set_kv VISION base_url "$SERVER_URL"

# --- [RAG]: external embeddings ---
set_kv RAG embedding_source external
set_kv RAG embedding_url "$SERVER_URL"

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
