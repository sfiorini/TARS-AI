#!/bin/bash
# setup_tts_voice.sh — optional: clone your own TARS-like voice on Fish.audio.
#
# Sample (attribution REQUIRED — CC BY 4.0):
#   freesound.org #815828 "Things A Machine Might Say 2" by RICHERlandTV
#   https://freesound.org/people/RICHERlandTV/sounds/815828/
#   Preview MP3: https://cdn.freesound.org/previews/815/815828_3450800-hq.mp3
#   License: CC BY 4.0 — https://creativecommons.org/licenses/by/4.0/
#   Attribution line: "Things A Machine Might Say 2" by RICHERlandTV,
#   freesound.org #815828, licensed CC BY 4.0.
#   Do NOT use the CC0 derivative (#832090) without crediting the original.
# The sample file is NOT committed to this repo; it is downloaded to a temp
# dir below (or pass your own local audio file as $1).
#
# What it does:
#   1. cuts a clean 15–25 s single-speaker segment of the sample (ffmpeg or sox)
#   2. uploads it to Fish.audio voice cloning:
#      POST https://api.fish.audio/v1/model  (multipart field voices=@seg.wav)
#      authenticated with $FISH_AUDIO_API_KEY from the environment
#   3. prints the new voice model _id — set it as [TTS] fishaudio_reference_id
#      in src/config.ini to use the clone
#
# Usage:
#   FISH_AUDIO_API_KEY=... ./scripts/setup_tts_voice.sh [path/to/sample.wav|mp3]
#
# The API key is read from the environment and is never printed or logged.
set -euo pipefail

if [ -z "${FISH_AUDIO_API_KEY:-}" ]; then
    echo "ERROR: FISH_AUDIO_API_KEY is not set. Export it first (it lives in .env):" >&2
    echo '  export FISH_AUDIO_API_KEY=<your-fish-key>   # never commit or echo this value' >&2
    exit 1
fi

SAMPLE_URL="https://cdn.freesound.org/previews/815/815828_3450800-hq.mp3"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

if [ "$#" -ge 1 ]; then
    SAMPLE="$1"
    if [ ! -f "$SAMPLE" ]; then
        echo "ERROR: sample file not found: $SAMPLE" >&2
        exit 1
    fi
else
    SAMPLE="$WORKDIR/sample.mp3"
    echo "Downloading CC BY 4.0 sample (freesound #815828 by RICHERlandTV)..."
    curl -fsSL "$SAMPLE_URL" -o "$SAMPLE"
fi

# Cut a clean 15–25 s single-speaker segment (start at 30 s, take 20 s).
SEG="$WORKDIR/segment.wav"
if command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -y -loglevel error -ss 30 -t 20 -i "$SAMPLE" -ar 22050 -ac 1 "$SEG"
elif command -v sox >/dev/null 2>&1; then
    sox "$SAMPLE" -r 22050 -c 1 "$SEG" trim 30 20
else
    echo "ERROR: need ffmpeg or sox installed to cut the segment" >&2
    exit 1
fi

echo "Uploading segment to Fish.audio voice cloning (POST https://api.fish.audio/v1/model)..."
RESPONSE="$(curl -sS -X POST "https://api.fish.audio/v1/model" \
    -H "Authorization: Bearer $FISH_AUDIO_API_KEY" \
    -F "title=TARS voice clone (freesound #815828 by RICHERlandTV, CC BY 4.0)" \
    -F "voices=@${SEG}")"

NEW_ID="$(printf '%s' "$RESPONSE" | python3 -c \
    'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("_id") or d.get("id") or "")
except Exception:
    print("")')"
if [ -z "$NEW_ID" ]; then
    echo "ERROR: could not parse a voice id from the Fish.audio response:" >&2
    echo "$RESPONSE" >&2
    exit 1
fi

echo ""
echo "Voice clone created. Set it in src/config.ini [TTS]:"
echo "  fishaudio_reference_id = $NEW_ID"
echo ""
echo "Attribution (required, CC BY 4.0): \"Things A Machine Might Say 2\" by RICHERlandTV,"
echo "freesound.org #815828 — https://freesound.org/people/RICHERlandTV/sounds/815828/ — CC BY 4.0"
