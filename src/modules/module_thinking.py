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
