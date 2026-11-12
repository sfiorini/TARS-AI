#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sleep 1

if [ -f "$HOME/.bashrc" ]; then
    source "$HOME/.bashrc"
fi

export DISPLAY=:0

# Tee the app's output to a log as well as the terminal: an autostart failure
# was undiagnosable after the fact (2026-11-12 — cold boot left the robot deaf
# with no trace). PYTHONUNBUFFERED keeps the pipe line-buffered, so a hang
# still leaves the last lines on disk instead of stuck in an 8 KB buffer.
# /tmp is per-boot, so this holds exactly the boot you are debugging.
lxterminal --working-directory="$SCRIPT_DIR" --command="bash -c 'PYTHONUNBUFFERED=1 python3 App-Start.py 2>&1 | tee -a /tmp/tars-autostart.log; exec bash'" &

echo "TARS-AI launched at $(date)" >> "$SCRIPT_DIR/autostart.log"
