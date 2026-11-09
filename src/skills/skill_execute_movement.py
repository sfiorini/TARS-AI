"""Skill: execute_movement — Send movement commands to servo motors."""

import threading

from modules.module_movement_registry import MOVEMENTS, LEGS_ONLY, HAS_ARMS


def _render_prompt(legs, arms):
    """Render the movement prompt from pre-built leg/arm id lines."""
    return f"""execute_movement
   Triggers: Use ONLY when user explicitly commands a movement or gesture
     * "walk forward", "turn left", "step back", "move backward", "go forward", "turn right"
     * waving, greeting, dancing, bowing, posing, pointing, poking, laughing, excitement
     * "say hi", "monster", "tilt", "swing legs"
   Movements are executed by their exact id from the list below.
   Valid movements:
     Legs (always available):
{chr(10).join(legs)}
     Arms (only if arms installed):
{chr(10).join(arms)}
   Note: wave_left and wave_right wave the LEGS; left_hi, right_hi and the shy/open waves are ARM gestures. If the user asks to wave or say hi with a leg, use wave_left or wave_right.
   Do NOT infer or guess movement from suggestions or questions
   Parameters: {{"movements": ["<id>", ...]}}
   Example: {{"function": "execute_movement", "parameters": {{"movements": ["wave_left"]}}}}
   Example: {{"function": "execute_movement", "parameters": {{"movements": ["left_hi"]}}}}"""


PROMPT_BUDGET_CHARS = 4000  # bound for the movement prompt (D5)


def get_prompt():
    """Build the movement prompt from the store at call time (D5), bounded.

    The prompt is assembled per request (never at import time), so a
    movement created via the UI or CLI is LLM-callable without a restart.
    Descriptions are truncated (200 -> 100 -> 50 chars), then entries are
    dropped alphabetically until the prompt fits the budget; every dropped
    id is logged.
    """
    from modules.module_movement_registry import (MOVEMENTS, LEGS_ONLY,
                                                  HAS_ARMS, reload_from_store)
    from modules.module_messageQue import queue_message
    reload_from_store()

    def _lines(desc_limit):
        mk = lambda mid, e: (f'     * "{mid}" - {e["name"]} '
                             f'({e.get("description", "")[:desc_limit]})')
        legs = [mk(m, e) for m, e in sorted(MOVEMENTS.items())
                if e["type"] == LEGS_ONLY]
        arms = [mk(m, e) for m, e in sorted(MOVEMENTS.items())
                if e["type"] == HAS_ARMS]
        return legs, arms

    legs, arms = _lines(200)
    for limit in (100, 50):                      # 1) truncate descriptions
        if len(_render_prompt(legs, arms)) <= PROMPT_BUDGET_CHARS:
            break
        legs, arms = _lines(limit)
    dropped = []
    for pool in (legs, arms):                    # 2) drop alphabetically
        while pool and len(_render_prompt(legs, arms)) > PROMPT_BUDGET_CHARS:
            dropped.append(pool.pop(0).split('"')[1])
    for mid in dropped:
        queue_message(f"MOVEMENTS: '{mid}' dropped from skill prompt "
                      f"(budget {PROMPT_BUDGET_CHARS} chars)")
    return _render_prompt(legs, arms)


SKILL = {
    "name": "execute_movement",
    "required_params": ["movements"],
    "description": "Perform physical movements and gestures",
    "examples": [
        """Example - Movement command:
User: "Walk forward and then turn left"
Response: {"reply": "Moving now.", "function_calls": [{"function": "execute_movement", "parameters": {"movements": ["step_forward", "turn_left_slow"]}}], "new_memories": []}""",
    ],
}


def _dispatch_via_store(mid):
    """Execute a movement through the store (lazy import so tests can
    monkeypatch modules.module_movementstore.get_movement_store)."""
    from modules.module_movementstore import get_movement_store
    return get_movement_store().execute(mid)


def _resolve_movement(movement_id, arms_present):
    """Resolve a registry movement id to a store-dispatch callable.

    Registry-only dispatch: the store is only ever asked for ids present
    in MOVEMENTS — never on raw caller/LLM strings.

    Returns (callable, None) on success, or (None, reason) where reason is
    "unknown" (id not in the registry) or "arm_gated" (arm move without arms).
    """
    entry = MOVEMENTS.get(movement_id)
    if entry is None:
        return None, "unknown"
    if entry["type"] == HAS_ARMS and not arms_present:
        return None, "arm_gated"
    return lambda: _dispatch_via_store(movement_id), None


def _execute_movement(movements, arms_present):
    """Execute a sequence of movements in a separate thread."""
    from modules.module_messageQue import queue_message

    def movement_task():
        try:
            total = len(movements)
            for i, move_id in enumerate(movements, start=1):
                function, reason = _resolve_movement(move_id, arms_present)
                if function is not None:
                    queue_message(f"[INFO] Executing movement {i}/{total}: {move_id}")
                    function()
                elif reason == "arm_gated":
                    queue_message(f"[INFO] Skipping {move_id} - arm servos not installed")
                else:
                    valid_ids = ", ".join(MOVEMENTS.keys())
                    queue_message(f"[ERROR] Unknown movement '{move_id}'. Valid movements: {valid_ids}")
        except Exception as e:
            queue_message(f"[ERROR] Unexpected error while executing movements: {e}")
        finally:
            queue_message(f"[DEBUG] Thread completed for movements: {movements}")

    thread = threading.Thread(target=movement_task, daemon=True)
    thread.start()
    return thread


def execute(parameters, context):
    """Execute servo movement commands. Returns None (no reply modification)."""
    config = context.get("config", {})
    if not config.get("CONTROLS", {}).get("voicemovement"):
        return None

    arms_present = bool(config.get("SERVO", {}).get("arms_present", False))

    movements = parameters.get("movements", [])
    if movements:
        _execute_movement(movements, arms_present)
    return None
