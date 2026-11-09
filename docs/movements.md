# Movements

Physical motion is driven by the **movement store**: two JSON files with one schema.

- **`src/movements_defaults.json`** — tracked in git, hand-editable, generated once from the original movement code by `scripts/export_movement_defaults.py` (an AST extractor; run it to re-derive the file). This is the source of truth for the shipped movements.
- **`src/movements.json`** — the **live store**, gitignored per-robot state. On first app start it is **seeded from the defaults** (one log line: `MOVEMENTS: seeded movements.json from defaults`). Every load validates it; a corrupt or invalid file never stops the robot: the offending entry is logged (`MOVEMENTS: invalid entry '<id>': <reason>`) and the defaults content is used instead.

Playback goes through `module_movementstore.py`: `execute(id)` picks the right branch (`steps_arms` when arm servos are present, else `steps`), honours the `swap_turn_directions` config for the four turn ids, and plays the step list with `play_steps()`. The MOVING flag is claimed/released through the locked helpers (`acquire_moving()` / `set_moving()`), so a second movement is refused while one is running.

## Entry and step schema

```json
"bow": {
  "name": "Bow",
  "type": "legs_only",
  "description": "bow forward politely",
  "steps": [ { "left_height": 50, "right_height": 50,
               "left_leg": 50, "right_leg": 50,
               "left_main": null, "left_forearm": null, "left_hand": null,
               "right_main": null, "right_forearm": null, "right_hand": null,
               "speed": 0.4, "hold_time": 0.0, "movement": null } ],
  "steps_arms": null
}
```

- `id` — lowercase letters, digits, underscores (`[a-z0-9_]+`).
- `type` — `legs_only` or `has_arms`. `has_arms` entries are skipped with an INFO log (`Skipping <id> - arm servos not installed`) when `[SERVO] arms_present = False`.
- Each **step** has exactly the builder fields: `left_height`, `right_height`, `left_leg`, `right_leg`, `left_main`, `left_forearm`, `left_hand`, `right_main`, `right_forearm`, `right_hand`, `speed`, `hold_time`, `movement`.
    - `null` leg/arm value = **skip** that servo.
    - `-1` arm value = **HOLD** (re-poke the last position).
    - `speed` — 0–1 (higher is faster); `hold_time` — seconds to sleep after the step (>= 0).
    - `arm_speed` — optional; the arm call's own speed on a merged leg+arm step (absent/`null` = the step's `speed` — files written before this key existed keep working).
    - `movement` — `null`, or the id of another store entry: the step **composes** that movement's steps inline.
- `steps` plays when arms are absent (or the entry has no arms variant); `steps_arms` (may be `null`) plays when arm servos are installed.

## Reset vs prune (exact semantics)

- **reset** — for every id in the defaults: delete the store entry and re-insert fresh from defaults. **Ids absent from the defaults are untouched** (user-created movements survive; edits to shipped movements are lost — that is the locked meaning).
- **prune** — reset **plus** delete store ids that no longer exist in the defaults.

## CLI: `src/app-movements.py`

```bash
python3 src/app-movements.py list        # id<tab>type<tab>name per line (33 shipped)
python3 src/app-movements.py show bow    # pretty JSON of one entry
python3 src/app-movements.py edit bow    # open $EDITOR; validates on save
python3 src/app-movements.py reset       # "Restored 33 movements from defaults, kept N user movement(s)"
python3 src/app-movements.py prune       # "Restored 33 movements from defaults, pruned N orphan(s)"
python3 src/app-movements.py export /tmp/backup.json   # "Exported 33 movements to /tmp/backup.json"
python3 src/app-movements.py import /tmp/backup.json   # all-or-nothing; errors name the bad entry
python3 src/app-movements.py validate    # "movements.json is valid" (exit 0) or per-entry errors (exit 1)
```

`validate` never touches the file. `import` validates every entry first — one invalid entry means **nothing** is imported and the error names the offending id. `edit` exits non-zero with the reason on stderr when the edited entry fails validation. The `TARS_MOVEMENTS_STORE` env var overrides the live store path (used by tests and offline tooling); `TARS_CONFIG_PATH` is respected by the config bootstrap.

## Web UI

- The **movements** tab lists the whole pool (fetched from `GET /movements/`), with per-entry Play / Edit / Delete buttons, a create form, and Reset / Prune buttons. Edits are validated on save; the server rejects invalid entries with the reason.
- The **builder** tab still builds step sequences graphically; saving now writes a movement store entry (id = name, so ids are lowercase/digits/underscores). Sequences from the legacy builder file (`src/custom_sequences.json`) are **not** migrated; recreate them in the panel.
- The **motion** tab dropdown executes movements through the store; a movement already in progress returns `409 Robot is already moving`.

## Voice: the `execute_movement` skill

`src/skills/skill_execute_movement.py` resolves ids against the registry (rebuilt from the store after every mutation) and dispatches through `movementstore.execute(id)` — never `getattr` on raw LLM strings. The skill prompt is **built from the store at call time and bounded** (`PROMPT_BUDGET_CHARS = 4000`): descriptions are truncated, then entries are dropped alphabetically, and every dropped id is logged. A movement created via the UI or CLI is LLM-callable without a restart.

Example LLM call: `{"function": "execute_movement", "parameters": {"movements": ["step_forward", "turn_left_slow"]}}`.

## Shipped movement reference

Generated from `src/movements_defaults.json` (33 entries — 19 `legs_only` + 14 `has_arms`):

| ID | Name | Type | Description |
|---|---|---|---|
| `bow` | Bow | legs_only | bow forward politely |
| `excited` | Excited | legs_only | excited leg bounce/fidget |
| `happy_dance` | Happy Dance | has_arms | full happy dance with arms and legs |
| `laugh` | Laugh | legs_only | body laugh - shake with amusement |
| `left_hi` | Left Hi | has_arms | raise the left ARM in a hi greeting |
| `left_pezz_dispenser` | Left Pezz Dispenser | has_arms | left-arm Pez-dispenser gag gesture |
| `left_point` | Left Point | has_arms | point with the left ARM |
| `left_poke` | Left Poke | has_arms | poke gesture with the left ARM |
| `left_shy_wave` | Left Shy Wave | has_arms | small shy wave with the left ARM |
| `left_wave_open` | Left Wave Open | has_arms | big open-armed wave with the left ARM |
| `monster` | Monster | has_arms | raise both ARMS up like a monster |
| `neutral_legs` | Neutral Legs | legs_only | return the legs to the neutral standing position |
| `pose` | Pose | legs_only | strike a heroic pose |
| `right_hi` | Right Hi | has_arms | raise the right ARM in a hi greeting |
| `right_pezz_dispenser` | Right Pezz Dispenser | has_arms | right-arm Pez-dispenser gag gesture |
| `right_point` | Right Point | has_arms | point with the right ARM |
| `right_poke` | Right Poke | has_arms | poke gesture with the right ARM |
| `right_shy_wave` | Right Shy Wave | has_arms | small shy wave with the right ARM |
| `right_wave_open` | Right Wave Open | has_arms | big open-armed wave with the right ARM |
| `side_side` | Side Side | legs_only | sway side to side |
| `step_backward` | Step Backward | legs_only | take one step backward |
| `step_forward` | Step Forward | legs_only | take one step forward |
| `swing_legs` | Swing Legs | legs_only | swing the legs back and forth playfully |
| `tilt_left` | Tilt Left | legs_only | lean/tilt the body to the left |
| `tilt_right` | Tilt Right | legs_only | lean/tilt the body to the right |
| `turn_left` | Turn Left | legs_only | turn the body to the left |
| `turn_left_slow` | Turn Left Slow | legs_only | turn the body to the left slowly |
| `turn_right` | Turn Right | legs_only | turn the body to the right |
| `turn_right_slow` | Turn Right Slow | legs_only | turn the body to the right slowly |
| `walk_backward` | Walk Backward | legs_only | walk backward several steps |
| `walk_forward` | Walk Forward | legs_only | walk forward several steps |
| `wave_left` | Wave Left | legs_only | wave a greeting with the left LEG |
| `wave_right` | Wave Right | legs_only | wave a greeting with the right LEG |

!!! note "Leg wave vs arm wave"
    `wave_left` / `wave_right` wave the **legs**; `left_hi`, `right_hi` and the shy/open waves are **arm** gestures. The skill prompt says this explicitly so the LLM picks the right id.

## Servo channels and calibration

Channels 0–3 = legs (0 left height, 1 right height, 2 left swing, 3 right swing); 4–9 = arms (4/5/6 left main/forearm/hand, 7/8/9 right). Calibration keys in `[SERVO]`: leg ranges + `perfect*Offset` trims; arm `*Min/Max/Offset` per joint. Positions persist to `~/.servo_positions.json` and are re-applied after watchdog repairs ([servo-watchdog.md](servo-watchdog.md)).

## Servo tester GUI

`src/app-servotester.py` is a pygame GUI for the Pi's display: per-channel sliders (0–600 pulse), registry movement buttons (dispatched through the store), position save/load. Run on the robot:

```bash
cd src && source .venv/bin/activate && python app-servotester.py
```
