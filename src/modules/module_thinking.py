"""Thinking-phrase categories, curated bank, and resolution.

Pure, importable everywhere (no Pi-only imports at top level). The refresh
daemon (M4) and the hot path (M2) both live behind small functions so the
hot path never blocks on anything but a dict lookup and a short regex.
"""
import json
import random
import re
import threading
import time

from modules.module_config import load_config
from modules.module_messageQue import queue_message

CONFIG = load_config()

CATEGORIES = ("search", "vision", "movement", "music", "creative",
              "smart_home", "system", "compute", "reminder", "identity",
              "skill", "mcp", "general")

REGISTERS = {
    "search": "focused", "vision": "attentive", "movement": "calm and brief",
    "music": "playful", "creative": "encouraging", "smart_home": "neutral",
    "system": "neutral", "compute": "precise", "reminder": "reassuring",
    "identity": "attentive", "skill": "neutral", "mcp": "neutral",
    "general": "neutral",
}

PHRASE_BANK = {
    "search": ("Searching now.", "Looking that up.", "Checking the latest.",
        "Pulling results.", "Scanning sources.", "Querying the web.",
        "On it, one second.", "Fetching that for you.", "Let me verify.",
        "Digging into it.", "Cross referencing now.", "Fresh data, coming up."),
    "vision": ("Let me take a look.", "Eyes on it.", "Focusing now.",
        "Checking the view.", "Looking around.", "Give me a second to see.",
        "Adjusting my optics.", "Scanning the scene.", "One look, coming right up.",
        "Stepping into observer mode.", "Let me see what we have.",
        "Visual check in progress."),
    "movement": ("Moving.", "On it.", "Right away.", "Give me a moment.",
        "Setting up.", "Easy does it.", "One second.", "Composing myself.",
        "Stand by.", "Almost there.", "Balancing.", "Here goes."),
    "music": ("Cueing that up!", "Let me spin something.", "Dialing in the tunes.",
        "DJ mode, activated.", "Finding that track.", "Ooh, good choice.",
        "Warming up the speakers.", "Track hunt, engaged.",
        "Let me dig through the crates.", "Beats loading.",
        "This one's a classic.", "Music machine, humming."),
    "creative": ("Ooh, fun one.", "Let me imagine that.", "Brushes ready.",
        "Picture this, coming up.", "Creating now.", "Let me sketch that out.",
        "Good idea, give me a second.", "Art mode, engaged.",
        "Painting it now.", "That deserves a good one.",
        "Composing the image.", "Imagining it as we speak."),
    "smart_home": ("Checking the house.", "Sending that now.", "Adjusting it.",
        "One moment.", "Talking to the house.", "On it.",
        "Querying the devices.", "Stand by.", "Routing the command.",
        "Connecting now.", "Almost done.", "Working on it."),
    "system": ("One moment.", "On it.", "Handling that now.",
        "Give me a second.", "Working.", "Stand by.", "Processing.",
        "Right away.", "Almost there.", "Setting that up.", "One second.",
        "Done in a moment."),
    "compute": ("Calculating.", "Computing now.", "Running the numbers.",
        "Crunching that.", "One moment, computing.", "Solving it.",
        "Processing the math.", "Let me work that out.", "Estimating now.",
        "Verifying the result.", "Precision mode, engaged.",
        "Deriving the answer."),
    "reminder": ("Got it, setting that now.", "I will keep track.",
        "Consider it noted.", "Timer coming right up.",
        "You can count on me.", "Noted and setting.",
        "One moment, saving that.", "I have got it.",
        "Setting your reminder.", "No worries, I will remember.",
        "On it, right away.", "Locked in."),
    "identity": ("Let me note that.", "One moment.",
        "Updating my memory of you.", "Got it, right away.",
        "Interesting, noted.", "Adjusting now.", "One second.",
        "Taking that in.", "Noted.", "Give me just a moment.",
        "Committing that to memory.", "Right, on it."),
    "skill": ("Loading that up.", "One moment.", "Opening the skill set.",
        "Fetching the instructions.", "On it.", "Stand by.",
        "Setting that up.", "Right away.", "Preparing it now.",
        "Almost ready.", "One second.", "Working on it."),
    "mcp": ("Connecting now.", "One moment.", "Reaching out.", "On it.",
        "Stand by.", "Routing that.", "Almost there.",
        "Give me a second.", "Setting it up.", "Working.", "In progress.",
        "Right away."),
    "general": ("Let me think.", "One moment.", "Give me a second.",
        "Thinking it through.", "Hold on.", "Working on it.", "Just a moment.",
        "Let me see.", "Hmm, thinking.", "Processing.", "On it.",
        "Give me a beat."),
}

_SANITIZE_RE = re.compile(r'[^a-zA-Z0-9\s.,?!;:"\'<->]')

def sanitize_phrase(text):
    """Mirror of the follow-up TTS sanitizer (module_main.py:393). The
    thinking path plays text RAW (module_llm.py:172-175 — no sanitizer,
    unlike module_main.py:393 and :202-204), so every phrase that reaches
    playback must pass through this first."""
    if not isinstance(text, str):
        return ""
    return _SANITIZE_RE.sub("", text)

TOOL_CATEGORY_MAP = {
    "web_search": "search",
    "capture_camera_view": "vision", "network_camera": "vision",
    "take_photo": "vision",
    "execute_movement": "movement",
    "tars_radio": "music", "generate_music": "music",
    "generate_image": "creative",
    "home_assistant": "smart_home",
    "system_control": "system", "volume": "system",
    "launch_retropie": "system", "browser": "system",
    "sandbox_exec": "compute",
    "reminder": "reminder",
    "identify_speaker_name": "identity", "adjust_persona": "identity",
    "load_skill": "skill", "run_skill_script": "skill",
}

def _tool_kind(name):
    """Registry kind for an unmapped tool name (guarded: registry lookup
    is pure in-memory; any failure means 'unknown')."""
    try:
        from modules.module_toolregistry import get_registry
        d = get_registry().get(name)
        return getattr(d, "kind", "") if d is not None else ""
    except Exception:
        return ""

CATEGORY_ORDER = ("search", "vision", "movement", "music", "creative",
                  "smart_home", "system", "compute", "reminder", "identity",
                  "skill", "mcp", "general")

KEYWORDS = {
    "search": ("search for", "look up", "google", "find me", "can you search",
        "what's the weather", "the weather", "what's in the news",
        "latest headlines", "the news"),
    "vision": ("what do you see", "look at", "what's visible",
        "describe surroundings", "what's in front", "what's around",
        "look around", "check visually", "what's that", "can you see",
        "describe view", "take a photo", "snap a picture", "take my picture",
        "photo of me", "take a pic", "photograph", "capture a photo",
        "camera"),
    "movement": ("walk forward", "turn left", "turn right", "step back",
        "move backward", "go forward", "dance", "dancing", "wave", "waving",
        "bow", "monster", "tilt", "swing legs"),
    "music": ("play some music", "start the radio", "the radio",
        "stop the music", "turn off the music", "next song", "skip this",
        "pause the music", "download this song", "add this to my library",
        "list my music", "what's in my library", "what's playing",
        "what song is this", "make me a song", "generate music",
        "compose a track", "create a beat", "write a song", "make some music",
        "produce a track", "play my songs"),
    "creative": ("generate a photo of", "draw me", "draw a", "create an image",
        "make a picture", "generate artwork", "paint me", "create a portrait",
        "generate an image", "make an image", "create art"),
    "smart_home": ("open the garage", "turn off the lights", "turn on the lights",
        "front door locked", "set the thermostat", "garage", "lights",
        "thermostat"),
    "system": ("exit the program", "quit the program", "close the program",
        "stop the program", "exit tars", "quit tars", "shut down", "shutdown",
        "power off", "volume", "retropie", "emulation station", "retro game",
        "on youtube", "watch the video", "open the website",
        "go to the website"),
    "compute": ("calculate", "do the math", "run this code", "python",
        "convert", "compute", "math problem"),
    "reminder": ("remind me", "set a timer", "set a reminder", "alarm in",
        "what reminders", "show my timers", "cancel my reminder",
        "delete the timer", "cancel reminder", "timer"),
    "identity": ("my name is", "call me", "be more", "be less", "your humor",
        "your sarcasm", "your verbosity", "remember my name"),
    "skill": ("skill example", "run skill", "load skill"),
    "mcp": (),
    "general": (),
}

def classify_intent(user_text=None, tool_names=None):
    """Hybrid resolver: tool-name map -> keyword scan -> general.
    Pure, no I/O, microseconds. Tool names (available only at the
    toolloop site) always win; otherwise the user's text is scanned in
    CATEGORY_ORDER (first match wins, general last)."""
    if tool_names:
        for name in tool_names:
            if not isinstance(name, str):
                continue
            if name in TOOL_CATEGORY_MAP:
                return TOOL_CATEGORY_MAP[name]
            if name.startswith("mcp_"):
                return "mcp"
            kind = _tool_kind(name)
            if kind == "mcp":
                return "mcp"
            if kind == "agent_skill":
                return "skill"
    text = (user_text or "").lower()
    if text:
        for category in CATEGORY_ORDER:
            for kw in KEYWORDS.get(category, ()):
                if kw in text:
                    return category
    return "general"

_pool_lock = threading.Lock()
_refreshed = {}  # category -> list of LLM-refreshed phrases (M4; in-memory only)

def get_filler_phrase(category):
    """The fallback chain. Returns a phrase string or None.

    1. refreshed in-memory pool for the category (M4; empty until a
       successful refresh — generated set wins while it exists)
    2. curated PHRASE_BANK[category]
    3. curated PHRASE_BANK['general']
    4. CONFIG['CHAR']['thinking_responses'] (web-UI-editable floor,
       existing json.loads + isinstance(list) guard)
    5. None -> the caller speaks HOLDING_LINE (module_toolloop.py:38)
    """
    with _pool_lock:
        pool = _refreshed.get(category) or []
    if pool:
        return random.choice(pool)
    bank = PHRASE_BANK.get(category) or ()
    if bank:
        return random.choice(bank)
    general = PHRASE_BANK.get("general") or ()
    if general:
        return random.choice(general)
    raw = CONFIG["CHAR"].get("thinking_responses", "[]")
    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        cfg = []
    if not isinstance(cfg, list):
        cfg = []
    valid = [p for p in cfg if isinstance(p, str) and p.strip()]
    if valid:
        return random.choice(valid)
    return None


_played_lock = threading.Lock()
_last_played_key = None  # (source, epoch) of the last turn that played

def _turn_key(source):
    """Per-turn identity: the toolloop epoch for this source. Lazy import
    (module_toolloop is Mac-safe, but keep the pattern uniform and the
    failure armor returns a stable key)."""
    try:
        from modules.module_toolloop import _current_epoch
        return (source, _current_epoch(source))
    except Exception:
        return (source, None)

def already_played_this_turn(source):
    with _played_lock:
        return _last_played_key == _turn_key(source)

def mark_played_this_turn(source):
    global _last_played_key
    with _played_lock:
        _last_played_key = _turn_key(source)

def reset_dedup():
    """Test hook — clear the dedup marker between tests."""
    global _last_played_key
    with _played_lock:
        _last_played_key = None


# --- M4: cloud refresh machinery (S-M4-03) ------------------------------
# The refreshed pool is IN-MEMORY ONLY — never written back to
# config.ini (the web-UI editor and the daemon must not fight).

_REFRESH_TIMEOUT_S = 60  # a hung refresh call must not hold the 120 s default
_MAX_PHRASES = 12

def _validate_phrases(raw_list):
    """Filter an LLM-produced phrase list (prior art :47-66 shape, plus
    the charset rule): strings only -> strip -> 2..60 chars -> fixed
    point of sanitize_phrase (the playback path plays text raw, so the
    charset rule is enforced here) -> dedupe case-insensitively, capped.
    Returns a filtered list; EMPTY means failure (the caller keeps the
    old pool)."""
    out = []
    seen = set()
    for p in raw_list or []:
        if not isinstance(p, str):
            continue
        p = p.strip()
        if not (2 <= len(p) <= 60):
            continue
        if sanitize_phrase(p) != p:
            continue
        if p.lower() in seen:
            continue
        seen.add(p.lower())
        out.append(p)
        if len(out) >= _MAX_PHRASES:
            break
    return out

def _parse_phrase_payload(text):
    """Fenced/non-greedy JSON tolerance (prior art :96-113), adapted to
    the {"phrases": [...]} schema (json_object backends reject bare
    arrays). ANTI-SCAVENGING: a payload that parses as {"reply": ...}
    (the conversation schema) is REJECTED, not scavenged."""
    if not isinstance(text, str) or not text.strip():
        return None
    body = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", body, re.DOTALL)
    if m:
        body = m.group(1)
    start = body.find("{")
    if start != -1:
        end = body.find("}", start)          # non-greedy: first object
        if end != -1:
            try:
                obj = json.loads(body[start:end + 1])
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                if "reply" in obj:           # conversation schema — reject
                    return None
                phrases = obj.get("phrases")
                return phrases if isinstance(phrases, list) else None
            return None
    start = body.find("[")
    end = body.rfind("]")
    if start != -1 and end > start:
        try:
            arr = json.loads(body[start:end + 1])
            return arr if isinstance(arr, list) else None
        except json.JSONDecodeError:
            return None
    return None

def _build_refresh_prompt(category):
    """Per-category prompt carrying the category's register. The allowed
    charset matches what sanitize_phrase keeps EXACTLY — note: NO hyphens
    (the sanitizer's `<->` is the range 0x3C-0x3E, so hyphens are
    stripped) and no parentheses or emoji."""
    return (
        "Generate 12 short filler phrases a robot says out loud while it "
        f"works on a {category} task. Register: {REGISTERS[category]}. "
        "Rules: each phrase is 2 to 6 words, using only ASCII letters, "
        "digits, spaces, and these punctuation marks: . , ? ! ; : ' \" < > "
        "(the robot's sanitizer strips anything else); no emoji, no "
        "parentheses, no hyphens or dashes. Respond with ONLY a JSON "
        'object like {"phrases": ["phrase one", "phrase two"]}'
    )

def _refresh_once(category):
    """One category's refresh. Returns True on success. Failure keeps
    the old pool (never empties it). The LLM call happens OUTSIDE the
    pool lock — the lock is held only for the atomic swap."""
    from modules.module_llm import raw_complete_llm  # lazy both sides
    try:
        raw = raw_complete_llm(_build_refresh_prompt(category),
                              timeout=_REFRESH_TIMEOUT_S)
    except Exception as e:
        queue_message(f"WARNING: thinking refresh LLM call failed "
                      f"({category}): {e}")
        return False
    phrases = _validate_phrases(_parse_phrase_payload(raw))
    if not phrases:
        queue_message(f"WARNING: thinking refresh yielded no valid "
                      f"phrases for {category}")
        return False
    with _pool_lock:
        _refreshed[category] = list(phrases)  # atomic: NEW list, never mutate
    return True

def _refresh_all_categories():
    """One full refresh cycle over every category. Survives a raising
    category; advances _last_refresh after EVERY cycle (success or not —
    no hammering on persistent failure)."""
    global _last_refresh
    ok_any = False
    for category in CATEGORIES:
        try:
            if _refresh_once(category):
                ok_any = True
        except Exception as e:
            queue_message(f"WARNING: thinking refresh error ({category}): {e}")
    _last_refresh = time.time()
    return ok_any


# --- M4: idle-gated background refresh daemon (S-M4-04) -----------------

_stop_event = threading.Event()
_thread = None
_start_lock = threading.Lock()
_last_refresh = 0.0
_IDLE_TEMP_C = 70.0

def _is_idle():
    """ALL of: STANDBY, not MOVING, not TTS-playing, CPU < 70 C.
    ANY import/check failure -> False (cannot prove idle -> don't run).
    module_servoctl imports Pi-only board/busio/adafruit_pca9685 at top
    level — the lazy import here is MANDATORY or importing this module
    breaks on the dev Mac."""
    try:
        from modules.module_state import get_tars_state, TarsState
        if get_tars_state() != TarsState.STANDBY:
            return False
        from modules import module_servoctl as servoctl  # lazy: Pi-only deps
        if servoctl.MOVING:
            return False
        from modules.module_tts import is_tts_playing  # lazy: heavy deps
        if is_tts_playing():
            return False
        from modules.module_cputemp import get_thermal_status
        temp = get_thermal_status().get("current_temp")
        if temp is None or temp >= _IDLE_TEMP_C:
            return False
        return True
    except Exception:
        return False

def _refresh_interval_s():
    try:
        minutes = load_config().get("CHAR", {}).get(
            "thinking_refresh_minutes", 120)
    except Exception:
        minutes = 120
    return max(1, int(minutes)) * 60

def _refresh_loop():
    while not _stop_event.is_set():
        _stop_event.wait(60)  # cheap tick: the real gates decide below
        if _stop_event.is_set():
            return
        try:
            if not load_config().get("CHAR", {}).get(
                    "thinking_refresh_enabled", False):
                continue
            if time.time() - _last_refresh < _refresh_interval_s():
                continue
            if not _is_idle():
                continue
            _refresh_all_categories()
        except Exception as e:
            queue_message(f"WARNING: thinking refresh loop error: {e}")

def start():
    """Start the daemon (idempotent). Boot wiring calls this only when
    thinking_refresh_enabled is true."""
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_refresh_loop, daemon=True,
                                   name="ThinkingRefresh")
        _thread.start()

def stop():
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None

def generated_phrase_count():
    with _pool_lock:
        return sum(len(v) for v in _refreshed.values())

_manual_lock = threading.Lock()
_refresh_state = {"state": "idle", "last_error": None}

def request_manual_refresh():
    """Manual (web-UI) trigger — copies the /api/tunnel/start pattern:
    idempotent, daemon thread, returns immediately. Bypasses the idle
    and interval gates (explicit user intent) but never blocks a turn."""
    with _manual_lock:
        if _refresh_state["state"] == "running":
            return {"state": "running"}
        _refresh_state["state"] = "running"
        _refresh_state["last_error"] = None

    def _bg():
        try:
            ok = _refresh_all_categories()
            with _manual_lock:
                _refresh_state["state"] = "done" if ok else "error"
                if not ok:
                    _refresh_state["last_error"] = "no category refreshed"
        except Exception as e:
            with _manual_lock:
                _refresh_state["state"] = "error"
                _refresh_state["last_error"] = str(e)

    threading.Thread(target=_bg, daemon=True,
                     name="ThinkingRefreshManual").start()
    return {"state": "starting"}

def refresh_status():
    with _manual_lock:
        info = dict(_refresh_state)
    info["generated"] = generated_phrase_count()
    return info
