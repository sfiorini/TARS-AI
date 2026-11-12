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

# [F3] TEST SEAM ONLY — production code NEVER reads this. Tests
# patch this attribute and stub load_config to return it; the fallback
# chain and the refresh loop read config via load_config() at CALL
# time (web-UI edits are picked up live). A module-level load_config()
# would force a full config parse plus load_config's process-wide
# os.chdir() at import, and sys.exit(1) on a missing config file —
# a BaseException no except-Exception armor can catch.
CONFIG = None

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

# [F2] HIGH-PRECISION suppression triggers. KEYWORDS is high-RECALL on
# purpose (a mis-picked phrase pool is cosmetic), but the pre-LLM
# suppression in module_llm._maybe_play_thinking_response speaks
# NOTHING on a hit — a suppressed turn with an empty reply-0 is dead
# air — so its gate must be high-PRECISION. KEYWORDS contains bare
# nouns ("volume", "convert", "dancing", "python", "lights",
# "camera", "bow", "wave"), which silenced 8 of 13 ordinary
# questions measured by the audit. Only imperative/action patterns
# live here: a bare noun NEVER suppresses.
SUPPRESSION_TRIGGERS = {
    "search": ("search for", "search the web", "look up", "google",
        "find me", "can you search", "what's the weather", "the weather",
        "what's in the news", "latest headlines", "the news",
        "check the news"),
    "vision": ("take a photo", "take a picture", "take my picture",
        "take a pic", "take a photograph", "snap a picture",
        "capture a photo", "what do you see", "look around",
        "describe your surroundings", "what's around you", "can you see",
        "look at me"),
    "movement": ("walk forward", "turn left", "turn right", "step back",
        "move backward", "go forward", "do a dance", "dance for me",
        "dance move", "wave at me", "wave to me", "wave hello",
        "wave goodbye", "give me a wave", "take a bow", "swing your legs"),
    "music": ("play some", "play a song", "play a track", "play music",
        "play my", "play that", "start the radio", "the radio",
        "stop the music", "turn off the music", "next song", "skip this",
        "pause the music", "download this song",
        "add this to my library", "list my music", "what's in my library",
        "what's playing", "what song is this", "make me a song",
        "generate music", "compose a track", "create a beat",
        "write a song", "make some music", "produce a track"),
    "creative": ("generate a photo of", "draw me", "draw a", "draw an",
        "create an image", "make a picture", "generate artwork",
        "paint me", "create a portrait", "generate an image",
        "make an image", "create art", "generate a picture"),
    "smart_home": ("turn on the", "turn off the", "open the garage",
        "close the garage", "garage door", "lock the front door",
        "unlock the front door", "set the thermostat", "shopping list",
        "dim the lights", "brighten the lights", "lights on",
        "lights off"),
    "system": ("exit the program", "quit the program", "close the program",
        "stop the program", "exit tars", "quit tars", "shut down",
        "shutdown", "power off", "restart", "reboot", "volume up",
        "volume down", "set the volume", "set volume",
        "turn the volume",
        "turn up the volume", "turn down the volume", "open retropie",
        "launch retropie", "start retropie", "open youtube",
        "watch the video", "open the website", "go to the website",
        "open the browser", "open a browser"),
    "compute": ("calculate", "do the math", "do the maths",
        "run this code", "run the code", "run that code",
        "execute this code", "execute the code", "run this script",
        "run the script", "run this python", "compute this",
        "convert this for me", "convert that for me", "can you convert",
        "please convert"),
    "reminder": ("remind me", "set a timer", "set a reminder",
        "set an alarm", "set my alarm", "alarm in", "what reminders",
        "show my timers", "show my reminders", "cancel my reminder",
        "cancel the reminder", "cancel my timers", "cancel the timer",
        "cancel my timer", "stop the timer",
        "delete the timer", "cancel reminder", "timer for",
        "snooze the alarm"),
    "identity": ("my name is", "call me", "be more", "be less",
        "your humor", "your sarcasm", "your verbosity",
        "remember my name", "adjust your persona", "change your persona",
        "your persona"),
    "skill": ("run skill", "load skill", "skill example", "on discord",
        "send a message", "send a message on discord", "send a discord"),
    "mcp": (),
    "general": (),
}

# [F4] Numeric tool phrasings ("volume 50", "convert 20 dollars to
# euros") cannot be a plain substring trigger without either overfitting
# to the sample utterance ("volume 50" suppresses nothing else) or
# reintroducing the bare-noun regression ("volume" / "convert" silenced
# the ordinary questions in TestSuppressionPrecision). The DIGIT is
# what makes the phrasing an imperative tool request rather than a
# question about the concept — so the trigger is the noun followed by a
# number. Checked by likely_tool_turn exactly like the substrings above.
SUPPRESSION_REGEX_TRIGGERS = {
    "system": (re.compile(r"\bvolume\s*\d"),),
    "compute": (re.compile(r"\bconvert\s+\d"),),
    # [F4] "add milk to my shopping list": the noun ("shopping list")
    # sits mid-utterance, so no startswith trigger can reach it — the
    # VERB + object shape is the tool request; matched against the
    # imperative head (and the original text) like the substrings.
    "smart_home": (re.compile(r"add .+ to my shopping list"),),
}

# [F1-REGRESSION] IMPERATIVE POSITION. The substring triggers above
# silenced 9 of 12 measured ordinary questions ("why does my cat
# look at me like that", "what is volume 2 of the encyclopedia
# about", "how do I convert 100 dollars into euros when
# travelling", ...) - a suppressed turn with an empty reply-0
# speaks NOTHING, i.e. dead air for the whole LLM turn. A tool
# request is an IMPERATIVE: it starts with the verb. An ordinary
# question starts with why/how/what/does/is. So a trigger matches
# only at the START of the utterance, after stripping this short,
# explicit prefix allowlist (politeness/address lead-ins, plus
# "turn the " so "turn the lights on" reaches its "lights on"
# trigger).
_SUPPRESSION_PREFIXES = (
    "please ", "hey tars ", "tars ", "can you ", "could you ",
    "would you ", "will you ", "turn the ",
)

def _imperative_head(text):
    """[F1-REGRESSION] Strip the prefix allowlist (repeatedly -
    "hey tars please lights on" and "please turn the lights on"
    both reduce to "lights on"). [F3] An optional comma after each
    prefix is accepted too ("hey tars, lights off", "please, turn
    the lights off") — the natural vocative comma. Pure string work,
    no I/O."""
    stripped = text
    changed = True
    while changed:
        changed = False
        for prefix in _SUPPRESSION_PREFIXES:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                changed = True
                continue
            # [F3] comma variant: the prefix word + a comma (then any
            # spaces) instead of the plain trailing space
            stem = prefix[:-1]  # the prefix without its trailing space
            if stripped.startswith(stem + ","):
                stripped = stripped[len(stem) + 1:].lstrip(" ")
                changed = True
    return stripped

def likely_tool_turn(user_text):
    """[F2] High-precision pre-LLM gate: True only when the user's text
    STARTS with a SUPPRESSION_TRIGGERS pattern (after the prefix
    allowlist strip) - an imperative/action phrase — an imperative/action phrase
    that reliably precedes a tool call (so reply-0 can hold the line).
    Bare-noun KEYWORDS hits do NOT count, and neither does a trigger
    buried mid-sentence: suppressing an ordinary question ("what is
    volume 2 of the encyclopedia about") speaks nothing at all
    (dead air). Pure, no I/O; tool-name signals are handled by the
    caller (tool_names is not None disables the gate entirely)."""
    text = (user_text or "").lower()
    if not text:
        return False
    head = _imperative_head(text)
    if not head:
        return False
    for category in CATEGORY_ORDER:
        for trigger in SUPPRESSION_TRIGGERS.get(category, ()):
            if head.startswith(trigger):
                return True
        for pattern in SUPPRESSION_REGEX_TRIGGERS.get(category, ()):
            if pattern.match(head):
                return True
    # [F2] SECONDARY check against the ORIGINAL text: triggers that
    # themselves BEGIN with an allowlist prefix ("can you see",
    # "can you search", "can you convert", "please convert",
    # "turn the volume") can never match the stripped head — the strip
    # removes the very prefix they start with, leaving dead literals
    # in the config and regressing the phrasings they were added for
    # ("can you see what's on the table", "please convert this").
    # Match the full phrase against the original text instead: still
    # imperative position (startswith / match at position 0), so no
    # bare-noun or mid-sentence trigger becomes reachable.
    for category in CATEGORY_ORDER:
        for trigger in SUPPRESSION_TRIGGERS.get(category, ()):
            if text.startswith(trigger):
                return True
        for pattern in SUPPRESSION_REGEX_TRIGGERS.get(category, ()):
            if pattern.match(text):
                return True
    return False

_pool_lock = threading.Lock()
_refreshed = {}  # category -> list of LLM-refreshed phrases (M4; in-memory only)

_MISSING = object()  # sentinel: key ABSENT vs explicitly empty []

def _config_thinking_responses():
    """Read CHAR.thinking_responses FRESH (a web-UI edit is picked up
    without restart; a config failure never raises into the voice turn).
    Returns (present, valid): present is False when the key is missing,
    the read fails, or the value is not a JSON list (no provable user
    intent -> the bank fallback serves); valid is the list of non-empty
    string phrases. A MISSING key and an explicit [] are DISTINCT:
    [] is a user's promise of silence, a missing key is not."""
    try:
        raw = load_config().get("CHAR", {}).get(
            "thinking_responses", _MISSING)
    except Exception:
        return False, []
    if raw is _MISSING:
        return False, []
    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False, []
    if not isinstance(cfg, list):
        return False, []
    valid = [p for p in cfg if isinstance(p, str) and p.strip()]
    return True, valid

def get_filler_phrase(category):
    """The fallback chain. Returns a phrase string or None.

    1. refreshed in-memory pool for the category (M4; empty until a
       successful refresh — generated set wins while it exists)
    2. curated PHRASE_BANK[category] (specific categories only)
    3. a SPECIFIC category with an empty bank: the user's
       CHAR.thinking_responses list (universal fallback, read FRESH)
       -> curated PHRASE_BANK['general'] -> None
    4. the 'general' category: CHAR.thinking_responses IS the pool —
       a non-empty list wins, an explicit [] is SILENCE (None, the
       tooltip's promise), a missing key / config failure / non-list
       value falls back to the curated PHRASE_BANK['general'] -> None
    5. None -> the caller speaks HOLDING_LINE (module_toolloop.py:38)
    """
    with _pool_lock:
        pool = _refreshed.get(category) or []
    if pool:
        return random.choice(pool)
    bank = PHRASE_BANK.get(category) or ()
    if category != "general":
        if bank:
            return random.choice(bank)
        present, valid = _config_thinking_responses()
        if valid:
            return random.choice(valid)
        general = PHRASE_BANK.get("general") or ()
        if general:
            return random.choice(general)
        return None
    # 'general': the user's list IS the pool
    present, valid = _config_thinking_responses()
    if not present:
        general = PHRASE_BANK.get("general") or ()
        if general:
            return random.choice(general)
        return None
    if not valid:
        return None  # explicit [] -> silence, never the bank's voice
    return random.choice(valid)


_played_lock = threading.Lock()
# [F1] PER-SOURCE dedup state: source -> epoch of THAT source's last
# turn that played. A single global slot was clobbered by any
# concurrent source's mark (a voice turn's filler, then a webui turn's
# own socketio thread, left the voice turn's check seeing the webui
# key -> not-played -> HOLDING_LINE spoken on top of the filler).
_last_played_epochs = {}

def _turn_epoch(source):
    """The toolloop epoch for this source. Lazy import (module_toolloop
    is Mac-safe, but keep the pattern uniform and the failure armor
    returns a stable epoch)."""
    try:
        from modules.module_toolloop import _current_epoch
        return _current_epoch(source)
    except Exception:
        return None

def already_played_this_turn(source):
    """[F1] Consults ONLY this source's own entry — another source's
    mark must never clobber this source's dedup state."""
    with _played_lock:
        epoch = _turn_epoch(source)
        return (source in _last_played_epochs
                and _last_played_epochs[source] == epoch)

def mark_played_this_turn(source):
    with _played_lock:
        _last_played_epochs[source] = _turn_epoch(source)

def reset_dedup():
    """Test hook — clear the dedup markers between tests."""
    with _played_lock:
        _last_played_epochs.clear()


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

def _refresh_all_categories(respect_idle=True):
    """One full refresh cycle over every category. Survives a raising
    category; advances _last_refresh after EVERY cycle (success or not —
    no hammering on persistent failure). Checks the stop event BETWEEN
    categories: a full cycle can take minutes (13 x 60 s), so a stop()
    whose 5 s join timed out must not leave the daemon making LLM calls
    for the rest of the cycle. [F3] Re-checks the IDLE gate between
    categories too: the daemon samples it only once per cycle, so a
    user waking TARS mid-cycle would otherwise leave the remaining
    categories firing LLM calls on the same API key, competing with the
    user's turn for the rate limit. [F1] The gate applies ONLY to
    the daemon (respect_idle=True, the default): the MANUAL web-UI
    refresh passes respect_idle=False — explicit user intent bypasses
    the idle gate for EVERY category (request_manual_refresh's
    docstring and docs/web-ui.md promise exactly that). The FIRST
    category is not gated for the daemon either (it has just checked
    _is_idle itself). A partial cycle still advances _last_refresh
    (correct: don't hammer).
    [F4] At most one cycle runs at a time (daemon OR manual — the two
    used to guard independently, so a web-UI click mid-daemon-cycle
    stacked a second loop: up to 26 concurrent raw_complete_llm calls
    on the same API key). The shared non-blocking guard returns None
    ('a cycle is already in flight') immediately instead of stacking —
    [F3] distinct from False ('every category failed'), so the manual
    path never paints an error badge for a refresh that IS running."""
    global _last_refresh, _cycle_seq, _running_cycle_id
    if not _cycle_lock.acquire(blocking=False):
        return None  # another cycle (daemon or manual) is in flight
    # [F2] capture the cycle's IDENTITY inside the lock: the settle in
    # the finally may write ONLY a state whose marker names THIS cycle.
    with _cycle_id_lock:
        _cycle_seq += 1
        cycle_id = _cycle_seq
        _running_cycle_id = cycle_id
    try:
        ok_any = False
        for index, category in enumerate(CATEGORIES):
            if _stop_event.is_set():
                break
            # [F1] the gate applies ONLY to the daemon (respect_idle=True,
            # its default) — the manual web-UI refresh passes False
            # (explicit user intent bypasses the idle gate for EVERY
            # category; the old index-0-only exemption refreshed 1 of
            # 13 categories on any machine where _is_idle() is
            # permanently False, while reporting 'done')
            if respect_idle and index and not _is_idle():
                break  # user woke TARS mid-cycle — stop burning the API key
            try:
                if _refresh_once(category):
                    ok_any = True
            except Exception as e:
                queue_message(f"WARNING: thinking refresh error "
                              f"({category}): {e}")
        with _meta_lock:
            _last_refresh = time.time()
        return ok_any
    finally:
        # [F2] clear the identity BEFORE releasing the lock: a click in
        # the post-clear window can never piggy-back on a cycle whose
        # settle is about to run (it takes the spawn branch instead,
        # and the id-guarded settle below cannot touch a state the
        # spawned _bg owns). The clear and every piggy-back mark are
        # serialized by _cycle_id_lock, so a mark can never land after
        # this cycle's settle already ran (no stranded 'running').
        with _cycle_id_lock:
            _running_cycle_id = None
        _cycle_lock.release()
        # [F1] settle a piggy-backed 'running' state AFTER the release:
        # while the cycle ran the state was 'running', so a manual
        # click took the piggy-back branch and named THIS cycle —
        # only that named state is written ([F2] identity guard).
        _settle_refresh_state(ok_any, cycle_id)


# --- M4: idle-gated background refresh daemon (S-M4-04) -----------------

_stop_event = threading.Event()
_thread = None
_start_lock = threading.Lock()
_meta_lock = threading.Lock()  # guards _last_refresh (daemon + manual)
_cycle_lock = threading.Lock()  # [F4] one refresh cycle at a time (daemon + manual)
_cycle_id_lock = threading.Lock()  # [F2] guards the cycle identity below
_cycle_seq = 0                    # [F2] monotonic cycle identity
_running_cycle_id = None          # [F2] id of the cycle currently holding _cycle_lock, else None
# [F1] BOUND on the manual refresh's busy-lock retry: _bg may not
# spin forever waiting for _cycle_lock — a thread that cannot get the
# lock gives up after this many seconds and reports the already-
# running state (it must never outlive the caller's intent).
_MANUAL_CYCLE_WAIT_S = 2.0
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
        # [F2] An ABSENT sensor leaves CPUTempModule.temperature at its
        # 0.0 initialiser, so the status reports 0.0 — NOT None — and
        # 0.0 >= 70.0 is False: the gate would pass while the CPU is
        # hot. A temperature that is not a positive number cannot
        # prove idle (a real reading, e.g. 45 C, still passes).
        if (isinstance(temp, bool) or not isinstance(temp, (int, float))
                or temp <= 0 or temp >= _IDLE_TEMP_C):
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
            with _meta_lock:
                last = _last_refresh
            if time.time() - last < _refresh_interval_s():
                continue
            if not _is_idle():
                continue
            _refresh_all_categories(respect_idle=True)
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
    """Stop the daemon. The 5 s join routinely times out mid-cycle (one
    cycle can take minutes), so _thread is nulled ONLY if it actually
    exited — otherwise a later start() would not see it alive, would
    clear _stop_event, and double-spawn a second permanent daemon."""
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
        if not _thread.is_alive():
            _thread = None

def generated_phrase_count():
    with _pool_lock:
        return sum(len(v) for v in _refreshed.values())

_manual_lock = threading.Lock()
_refresh_state = {"state": "idle", "last_error": None, "cycle": None}

def _settle_refresh_state(ok, cycle_id):
    """[F1] Terminal write for a manual refresh state left 'running'
    by a click that piggy-backed on an in-flight cycle: that branch
    of request_manual_refresh sets 'running' but spawns NO _bg, so
    only the cycle's completion can settle it — and the daemon used to
    never write _refresh_state, so the web UI polled
    /api/thinking/refresh/status and showed 'Refreshing…' forever.
    Called from _refresh_all_categories' finally, AFTER the cycle
    lock releases.
    [F2] IDENTITY GUARD: the settle runs after the lock is released,
    and a click landing in that window sees a free lock and spawns a
    REAL _bg cycle — whose 'running' state this verdict must not
    touch (main.js clears its poller on the first non-running state,
    so a clobbered badge stays wrong for the whole session). The
    piggy-backed click NAMED this cycle (_refresh_state['cycle'] ==
    cycle_id); a spawned _bg's state carries cycle None, which no
    settle matches. Invariant: a settle writes only the state its own
    cycle was named to settle."""
    with _manual_lock:
        if _refresh_state["state"] != "running":
            return
        if _refresh_state.get("cycle") != cycle_id:
            return  # not this cycle's state — never clobber another owner
        _refresh_state["state"] = "done" if ok else "error"
        _refresh_state["cycle"] = None
        if ok:
            _refresh_state["last_error"] = None
        else:
            _refresh_state["last_error"] = "no category refreshed"

def request_manual_refresh():
    """Manual (web-UI) trigger — copies the /api/tunnel/start pattern:
    idempotent, daemon thread, returns immediately. Bypasses the idle
    and interval gates (explicit user intent) but never blocks a turn.
    [F4] Shares the cycle guard with the daemon: a click while a cycle
    is already in flight REPORTS 'running' instead of stacking a second
    loop (the web UI polls status anyway)."""
    with _manual_lock:
        if _refresh_state["state"] == "running":
            return {"state": "running"}
    # [F2] piggy-back decision: read the RUNNING CYCLE'S identity under
    # the id lock (atomic with the cycle's identity-clear) — NOT
    # _cycle_lock.locked(): a cycle between its identity-clear and its
    # lock release must not be piggy-backed on (its settle is about to
    # run and would strand the state), and a cycle that has not yet
    # taken an identity cannot be named.
    with _cycle_id_lock:
        running = _running_cycle_id
        if running is not None:
            with _manual_lock:
                # a cycle (typically the daemon's) is already in flight.
                # [F2] The web UI ignores the POST body and POLLS /status,
                # so the STATE must say 'running' too — the old early
                # return left the previous terminal value
                # ('idle'/'done'/'error') in place and the badge showed it
                # while the refresh ran.
                # [F1] No _bg is spawned here, so the in-flight cycle
                # itself settles this 'running' when it ends
                # (_settle_refresh_state — the daemon path used to never
                # write the state, and the badge showed 'Refreshing…'
                # forever). [F2] NAME the cycle: only ITS settle may
                # write this state (identity guard).
                if _refresh_state["state"] == "running":
                    return {"state": "running"}
                _refresh_state["state"] = "running"
                _refresh_state["cycle"] = running
                return {"state": "running"}
    with _manual_lock:
        if _refresh_state["state"] == "running":
            return {"state": "running"}
        _refresh_state["state"] = "running"
        # [F2] marker None: this 'running' state is owned by OUR _bg —
        # no cycle's settle matches None, so a previous cycle's settle
        # (still in flight after its lock release) can never clobber it.
        _refresh_state["cycle"] = None
        _refresh_state["last_error"] = None

    def _bg():
        try:
            # [F1] explicit user intent: bypass the idle gate for every
            # category (the daemon passes respect_idle=True)
            # [F1] BOUNDED retry: the loop below re-attempts the
            # acquire while the lock is busy, but a thread that cannot
            # get the lock must TERMINATE, not spin — an unbounded
            # spin outlives the caller's intent (and, in the test
            # suite, the test's cleanups: the leaked thread reacquired
            # the REAL lock and ran a full 13-category cycle against
            # the live LLM). On expiry: do NOT run a cycle — report the
            # already-running state and return (the daemon's cycle
            # will settle it, or the user polls again). The deadline
            # check is at the TOP of the loop so an expired thread
            # never makes another acquire attempt.
            give_up_at = time.monotonic() + _MANUAL_CYCLE_WAIT_S
            while True:
                if time.monotonic() >= give_up_at:
                    return
                ok = _refresh_all_categories(respect_idle=False)
                if ok is not None:
                    break
                # [F3] a cycle is already in flight (daemon or manual):
                # already-running is NOT an error — the refresh IS
                # running. [F2] hand OUR 'running' state's settle to
                # THAT cycle (name it) so its verdict settles us; the
                # read+name is atomic with the cycle's identity-clear
                # under _cycle_id_lock, so the name can never land on a
                # cycle whose settle already ran.
                with _cycle_id_lock:
                    running = _running_cycle_id
                    if running is not None:
                        with _manual_lock:
                            if (_refresh_state["state"] == "running"
                                    and _refresh_state["cycle"] is None):
                                _refresh_state["cycle"] = running
                        return
                # the in-flight cycle ended in the window between our
                # failed acquire and the read: nothing can settle our
                # state — retry our OWN cycle (the lock is free)
                time.sleep(0.05)
            with _manual_lock:
                if (_refresh_state["state"] == "running"
                        and _refresh_state["cycle"] is None):
                    _refresh_state["state"] = "done" if ok else "error"
                    # [F2] self-consistent terminal write: 'done' must
                    # never ship beside a stale last_error (mirrors
                    # _settle_refresh_state)
                    if ok:
                        _refresh_state["last_error"] = None
                    else:
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
