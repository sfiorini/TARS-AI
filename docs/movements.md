# Movements

Physical motion is driven by a **registry**: `src/modules/module_movement_registry.py` defines `MOVEMENTS` with **33 entries — 19 `legs_only` + 14 `has_arms`**. The LLM skill, the web UI motion tab, and the voice-command gate all read this registry, so one edit propagates everywhere. The motion functions themselves live in `src/modules/module_movements.py` on top of `move_legs()` / `move_arm()` in `module_servoctl.py`.

## The registry

**Legs (`legs_only`, 19):** `step_forward`, `walk_forward`, `step_backward`, `walk_backward`, `turn_right`, `turn_right_slow`, `turn_left`, `turn_left_slow`, `pose`, `bow`, `tilt_right`, `tilt_left`, `side_side`, `wave_right`, `wave_left` (note: these two wave the **legs**), `neutral_legs`, `excited`, `laugh`, `swing_legs`.

**Arms (`has_arms`, 14):** `right_hi`, `left_hi`, `left_pezz_dispenser`, `right_pezz_dispenser`, `monster`, `left_point`, `right_point`, `left_poke`, `right_poke`, `left_wave_open`, `right_wave_open`, `left_shy_wave`, `right_shy_wave`, `happy_dance`.

Each entry is `{"name": <display>, "type": LEGS_ONLY|HAS_ARMS, "description": <short>}` with helper accessors `get_all()`, `get_by_type()`, `get_legs_only()`, `get_has_arms()`, `get_names()`, `get_names_by_type()`.

## Arm gating

`[SERVO] arms_present = False` in the template (legs-only builds). Every `has_arms` movement is **skipped with an INFO log** (`Skipping X - arm servos not installed`) and the batch continues. The web UI reports the flag via `GET /get_arms_status` and hides arm movements.

## Voice-triggered movement: the `execute_movement` skill

`src/skills/skill_execute_movement.py` exposes `execute_movement` to the LLM. Its prompt is **built at import time by iterating `MOVEMENTS`** (grouped Legs/Arms), so new registry entries appear in the prompt automatically. Dispatch is registry-only: `_resolve_movement()` does `getattr(module_movements, id)` **only for ids present in `MOVEMENTS`** — never on raw LLM strings (unknown ids get an ERROR with the valid id list). `execute(parameters, context)` requires `[CONTROLS] voicemovement = True`, passes `arms_present` from config, and runs the movement list in a daemon thread, logging each step (`Executing movement 1/3: …`).

Example LLM call: `{"function": "execute_movement", "parameters": {"movements": ["step_forward", "turn_left_slow"]}}`.

## Custom sequences

`src/custom_sequences.json` stores named step sequences created in the web UI **builder** tab:

```json
"step_forward_test": {
  "type": "movement",
  "quick": false,
  "steps": [
    {"movement": null, "left_height": 50, "right_height": 50,
     "left_leg": 50, "right_leg": 50,
     "left_main": null, "left_forearm": null, "left_hand": null,
     "right_main": null, "right_forearm": null, "right_hand": null,
     "speed": 0.9, "hold_time": 0}
  ]
}
```

Each step: leg percentages (1–100; 0/None = skip), arm percentages (1–100, -1 = hold), an optional registry `movement` id, `speed` (0–1) and `hold_time` (seconds). Endpoints: `POST /save_sequence`, `GET /get_saved_sequences`, `POST /play_saved_sequence`, `POST /delete_saved_sequence`, `POST /play_sequence`.

## Servo channels and calibration

Channels 0–3 = legs (0 left height, 1 right height, 2 left swing, 3 right swing); 4–9 = arms (4/5/6 left main/forearm/hand, 7/8/9 right). Calibration keys in `[SERVO]`: leg ranges + `perfect*Offset` trims; arm `*Min/Max/Offset` per joint. Positions persist to `~/.servo_positions.json` and are re-applied after watchdog repairs ([servo-watchdog.md](servo-watchdog.md)).

## Servo tester GUI

`src/app-servotester.py` is a pygame GUI for the Pi's display: per-channel sliders (0–600 pulse), registry movement buttons, position save/load. Run on the Pi:

```bash
cd src && source .venv/bin/activate && python app-servotester.py
```

Use it to find your `[SERVO]` min/max values and offsets before walking.

## How to add a movement

1. Add a function to `src/modules/module_movements.py` (build it from `move_legs()` / `move_arm()` / `move_servos_synchronized()` in `module_servoctl.py`).
2. Add one entry to `MOVEMENTS` in `module_movement_registry.py` with the **same id**: `"my_move": {"name": "My Move", "type": LEGS_ONLY, "description": "…"}`.
3. Done — the skill prompt (`_build_prompt()` iterates the registry), `/get_movements`, and `_resolve_movement()` dispatch pick it up automatically.
4. Extend `src/tests/test_execute_movement.py` (prompt contains all ids, resolver legs/arms/arm-gated/unknown cases).

## Full registry reference (33 entries)

| ID | Name | Type | Description |
|---|---|---|---|
| `step_forward` | Step Forward | legs_only | take one step forward |
| `walk_forward` | Walk Forward | legs_only | walk forward several steps |
| `step_backward` | Step Backward | legs_only | take one step backward |
| `walk_backward` | Walk Backward | legs_only | walk backward several steps |
| `turn_right` | Turn Right | legs_only | turn the body to the right |
| `turn_right_slow` | Turn Right Slow | legs_only | turn the body to the right slowly |
| `turn_left` | Turn Left | legs_only | turn the body to the left |
| `turn_left_slow` | Turn Left Slow | legs_only | turn the body to the left slowly |
| `pose` | Pose | legs_only | strike a heroic pose |
| `bow` | Bow | legs_only | bow forward politely |
| `tilt_right` | Tilt Right | legs_only | lean/tilt the body to the right |
| `tilt_left` | Tilt Left | legs_only | lean/tilt the body to the left |
| `side_side` | Side Side | legs_only | sway side to side |
| `wave_right` | Wave Right | legs_only | wave a greeting with the right LEG |
| `wave_left` | Wave Left | legs_only | wave a greeting with the left LEG |
| `neutral_legs` | Neutral Legs | legs_only | return the legs to the neutral standing position |
| `excited` | Excited | legs_only | excited leg bounce/fidget |
| `laugh` | Laugh | legs_only | body laugh - shake with amusement |
| `swing_legs` | Swing Legs | legs_only | swing the legs back and forth playfully |
| `right_hi` | Right Hi | has_arms | raise the right ARM in a hi greeting |
| `left_hi` | Left Hi | has_arms | raise the left ARM in a hi greeting |
| `left_pezz_dispenser` | Left Pezz Dispenser | has_arms | left-arm Pez-dispenser gag gesture |
| `right_pezz_dispenser` | Right Pezz Dispenser | has_arms | right-arm Pez-dispenser gag gesture |
| `monster` | Monster | has_arms | raise both ARMS up like a monster |
| `left_point` | Left Point | has_arms | point with the left ARM |
| `right_point` | Right Point | has_arms | point with the right ARM |
| `left_poke` | Left Poke | has_arms | poke gesture with the left ARM |
| `right_poke` | Right Poke | has_arms | poke gesture with the right ARM |
| `left_wave_open` | Left Wave Open | has_arms | big open-armed wave with the left ARM |
| `right_wave_open` | Right Wave Open | has_arms | big open-armed wave with the right ARM |
| `left_shy_wave` | Left Shy Wave | has_arms | small shy wave with the left ARM |
| `right_shy_wave` | Right Shy Wave | has_arms | small shy wave with the right ARM |
| `happy_dance` | Happy Dance | has_arms | full happy dance with arms and legs |

!!! note "Leg wave vs arm wave"
    `wave_left` / `wave_right` wave the **legs**; `left_hi`, `right_hi` and the shy/open waves are **arm** gestures. The skill prompt says this explicitly so the LLM picks the right id.
