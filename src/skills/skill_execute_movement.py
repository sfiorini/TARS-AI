"""Skill: execute_movement — Send movement commands to servo motors."""

import threading

from modules.module_movement_registry import MOVEMENTS, LEGS_ONLY, HAS_ARMS


def _build_prompt():
    """Build the LLM prompt from the movement registry.

    Iterates MOVEMENTS so registry additions appear in the prompt automatically.
    """
    legs = [
        f'     * "{mid}" - {entry["name"]}'
        for mid, entry in MOVEMENTS.items() if entry["type"] == LEGS_ONLY
    ]
    arms = [
        f'     * "{mid}" - {entry["name"]}'
        for mid, entry in MOVEMENTS.items() if entry["type"] == HAS_ARMS
    ]
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
   Do NOT infer or guess movement from suggestions or questions
   Parameters: {{"movements": ["<id>", ...]}}
   Example: {{"function": "execute_movement", "parameters": {{"movements": ["wave_left"]}}}}
   Example: {{"function": "execute_movement", "parameters": {{"movements": ["left_hi"]}}}}"""


SKILL = {
    "name": "execute_movement",
    "required_params": ["movements"],
    "description": "Perform physical movements and gestures",
    "prompt": _build_prompt(),
    "examples": [
        """Example - Movement command:
User: "Walk forward and then turn left"
Response: {{"reply": "Moving now.", "function_calls": [{{"function": "execute_movement", "parameters": {{"movements": ["step_forward", "turn_left_slow"]}}}}], "new_memories": []}}""",
    ],
}


def _resolve_movement(movement_id, arms_present):
    """Resolve a registry movement id to its callable in module_movements.

    Registry-only dispatch: getattr is ONLY called on ids present in MOVEMENTS —
    never on raw caller/LLM strings.

    Returns (callable, None) on success, or (None, reason) where reason is
    "unknown" (id not in the registry) or "arm_gated" (arm move without arms).
    """
    entry = MOVEMENTS.get(movement_id)
    if entry is None:
        return None, "unknown"
    if entry["type"] == HAS_ARMS and not arms_present:
        return None, "arm_gated"

    from modules import module_movements
    function = getattr(module_movements, movement_id, None)
    if not callable(function):
        return None, "unknown"
    return function, None


def _execute_movement(movements, arms_present):
    """Execute a sequence of movements in a separate thread."""
    from modules.module_messageQue import queue_message

    # Fail fast if the servo stack is unavailable (no hardware / import error).
    try:
        from modules import module_movements  # noqa: F401
    except ImportError:
        queue_message("[ERROR] Servo control module not available.")
        return

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
