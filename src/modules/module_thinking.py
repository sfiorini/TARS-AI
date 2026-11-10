"""Self-refreshing thinking-phrase set (in-memory only, no persistence).

A background daemon periodically asks the LLM for a fresh set of thinking
filler phrases (what TARS says while waiting for the AI).  The set lives in
memory only — a restart falls back to the configured `thinking_responses`.

Import cycle (documented at BOTH sites): this module needs
`raw_complete_llm` from `module_llm`, and `module_llm` needs
`get_filler_phrase` from this module.  Both sides therefore import lazily
INSIDE functions — never at module top level.
"""
import json
import random
import re
import threading

from modules.module_config import load_config

CONFIG = load_config()   # cached dict — the same object every module binds

_lock = threading.Lock()
_generated = []           # swapped atomically under _lock (in-memory only)
_stop_event = threading.Event()
_thread = None             # named daemon "thinking-refresh"


def get_filler_phrase():
    """Return a filler phrase: generated set if non-empty, else the
    configured phrases, else None (the tool loop then speaks its hardcoded
    holding-line fallback)."""
    with _lock:
        if _generated:
            return random.choice(_generated)
    raw = CONFIG["CHAR"].get('thinking_responses', '[]')
    try:
        phrases = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        phrases = []
    if isinstance(phrases, list):
        valid = [p for p in phrases
                 if isinstance(p, str) and p.strip()]
        if valid:
            return random.choice(valid)
    return None


def _validate_phrases(obj):
    """Validate an LLM-produced phrase set.

    Pipeline: non-empty list -> strings only -> strip -> drop >40 chars or
    >6 words -> dedupe (case-insensitive) -> require >=3 survivors, else
    None (the caller keeps the old set)."""
    if not isinstance(obj, list) or not obj:
        return None
    out, seen = [], set()
    for p in obj:
        if not isinstance(p, str):
            continue
        p = p.strip()
        if not p or len(p) > 40 or len(p.split()) > 6:
            continue
        if p.lower() in seen:
            continue
        seen.add(p.lower())
        out.append(p)
    return out if len(out) >= 3 else None


def _build_prompt():
    """Prompt for a fresh phrase set.  _prepare_request_data forces
    response_format=json_object, so the prompt must ask for exactly the
    {"phrases": [...]} shape (and _refresh_once tolerates the main-reply
    schema too)."""
    name = CONFIG["CHAR"].get("character_name", "TARS")
    return (
        'Generate a JSON object with a single key "phrases" holding a list '
        f'of 10 short thinking filler phrases for {name} to say while '
        'waiting for the AI. Each phrase: max 6 words, max 40 characters, '
        'casual spoken style, no quotes inside phrases. Reply with exactly '
        '{"phrases": ["...", ...]} and nothing else.'
    )


def _refresh_once(timeout=30):
    """Ask the LLM for a fresh phrase set; swap under the lock on success.

    Returns True on swap, False on any failure (the old set is kept)."""
    # Lazy import — breaks the module_llm <-> module_thinking cycle.
    from modules.module_llm import raw_complete_llm
    reply = raw_complete_llm(_build_prompt(), istext=True, timeout=timeout)
    if not reply:
        return False
    phrases = None
    try:
        parsed = json.loads(reply)
        if isinstance(parsed, dict):
            phrases = _validate_phrases(parsed.get("phrases"))
        elif isinstance(parsed, list):
            phrases = _validate_phrases(parsed)
    except (json.JSONDecodeError, TypeError):
        pass
    if phrases is None:                     # tolerate markdown fences
        m = re.search(r"\[.*\]", reply, re.S)
        if m:
            try:
                phrases = _validate_phrases(json.loads(m.group(0)))
            except (json.JSONDecodeError, TypeError):
                pass
    if phrases is None:
        return False
    global _generated
    with _lock:
        # Assign a NEW list (never mutate in place): a caller holding the
        # old list is never mutated mid-iteration.
        _generated = phrases
    from modules.module_messageQue import queue_message
    queue_message(f"THINKING: refreshed {len(phrases)} phrases")
    return True


def _refresh_loop(stop_event):
    """Daemon body: refresh immediately, then wait minutes*60 between
    refreshes.  The body is try/except-wrapped so one failure never kills
    the cycle."""
    try:
        minutes = int(CONFIG["CHAR"].get("thinking_refresh_minutes", 30) or 30)
    except (TypeError, ValueError):
        minutes = 30
    while not stop_event.is_set():
        try:
            _refresh_once()
        except Exception as e:
            from modules.module_messageQue import queue_message
            queue_message(f"WARN: thinking refresh failed: {e}")
        stop_event.wait(minutes * 60)


def start_thinking_refresh():
    """Start the refresh daemon (idempotent — cputemp daemon pattern:
    threading.Event + wait(interval) + named daemon + is_alive guard)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop_event.clear()
    _thread = threading.Thread(target=_refresh_loop, args=(_stop_event,),
                               daemon=True, name="thinking-refresh")
    _thread.start()
    return True


def stop_thinking_refresh():
    _stop_event.set()


def generated_phrase_count():
    with _lock:
        return len(_generated)
