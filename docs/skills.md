# Agent Skills

Agent Skills are **spec-pure `SKILL.md` folders** — the standard skills format. Each skill is a directory in `src/agent_skills/` containing a `SKILL.md` (frontmatter + Markdown body) and optional `scripts/`. The pool ships with one working example (`dice-roll`) and you can install your own from a folder or a `.zip`, create and edit them in the web UI, or manage them with `python3 src/app-skills.py`.

## The six frontmatter fields (and only these)

```yaml
---
name: dice-roll
description: Roll dice for the user. Use when the user asks to roll a die or dice...
license: MIT
compatibility: ...
metadata:
  tars_parameters: '{"properties": {...}, "required": []}'
allowed-tools: ...
---
```

| Field | Constraint |
|---|---|
| `name` | 1–64 chars, `[a-z0-9-]` only, no leading/trailing/consecutive hyphens, **must equal the parent directory name** |
| `description` | 1–1024 chars — this is what the AI sees in the system prompt |
| `license` | e.g. `MIT` |
| `compatibility` | optional compatibility note |
| `metadata` | the TARS extension channel (below) |
| `allowed-tools` | optional tool declaration |

**Any other top-level key is rejected at validation.** Claude Code hard-errors on unknown top-level frontmatter keys, so a skill that works here but smuggles in extra fields would break the moment you reuse it elsewhere. Validation happens on install, on create, and in `app-skills.py validate`.

### The `metadata:` extension (TARS data, spec-pure)

TARS-specific data cannot be a top-level field, so it lives under `metadata:` as **JSON-encoded strings**:

- `tars_parameters` — a JSON schema string (`{"properties": {...}, "required": [...]}`). A skill with `tars_parameters` becomes **directly callable in one hop**: its line in the prompt carries a compact `Params: {...}` spec, and calling it renders the body's `run_skill_script` line with your arguments.
- `tars_config` — a config schema string; persisted values live in `[SKILL_STD:<name>]` in `config.ini`.

Both values must parse as JSON or the skill is rejected at validation.

## Progressive disclosure

The system prompt carries **only each skill's name and description** (plus the compact param spec for parameterized skills) — never full bodies. When the AI needs more:

1. `load_skill` — fetches the skill's SKILL.md body (truncated to `[STANDARD_SKILLS] body_char_limit`, default 4000 chars; truncation is logged).
2. `run_skill_script` — runs one of the skill's scripts and returns its stdout.

The agent-skills prompt block is bounded by `[STANDARD_SKILLS] prompt_budget_chars` (default 3000). On overflow, descriptions are shortened first (200→100→50 chars), then whole entries are dropped alphabetically — **every dropped name is logged**, and the web UI panel shows an "in prompt" badge per skill.

## The script execution contract

```text
python3 <skill_dir>/scripts/<script> <args...>
```

- **Exit 0** → stdout is the tool result, fed back to the AI.
- **Non-zero exit** → error result carrying the script's stderr.
- **Timeout** (past `[LLM] tool_call_timeout`) → error result.
- `script` must be a bare filename — paths with `..` or separators are rejected before anything executes.

!!! danger "Security — read this before importing anything"
    **Imported scripts run with the app's privileges. There is no sandbox.**
    A skill's script can do anything the TARS process can do — read and
    write files, reach the network, drive the servos. Only install skills
    from sources you trust, and read the scripts before you import them.

## Install, create, edit, delete, reset

- **Install from a folder or `.zip`** — web UI **skills** tab (`POST /agent_skills/install` with a path on the robot and an overwrite checkbox) or `python3 src/app-skills.py install <path> [--force]`. Zips are extracted to a temp dir and validated before anything is moved into the pool; entries with absolute paths or `..` traversal are rejected.
- **Create** in the UI or via the API (`POST /agent_skills` with `name`, `description`, `body`) — the SKILL.md is written for you with spec-pure frontmatter.
- **Edit** in the UI (`PUT /agent_skills/<name>`) — description and body.
- **Delete** — UI or `python3 src/app-skills.py uninstall <name>`.
- **`reset`** — wipes the pool and re-copies from the shipped defaults (`src/agent_skills_defaults/`). **User-created skills are deleted by reset** — that is the locked meaning. CLI: `python3 src/app-skills.py reset` (prompts `y/N` unless `--yes`).

Config values a skill declares in `tars_config` persist under `[SKILL_STD:<name>]` sections in `config.ini`.

## The complete worked example: `dice-roll`

The shipped skill at `src/agent_skills_defaults/dice-roll/` — install it, call it, delete it, reset it back.

**`SKILL.md`** (the whole file):

```yaml
---
name: dice-roll
description: Roll dice for the user. Use when the user asks to roll a die or dice, e.g. "roll a d20", "roll 3 dice", "give me a random number between 1 and 6".
license: MIT
metadata:
  tars_parameters: '{"properties": {"count": {"type": "number", "description": "How many dice to roll (1-10)", "default": 1}, "sides": {"type": "number", "description": "Sides per die (2-100)", "default": 6}}, "required": []}'
---
# Dice Roll

Roll dice by running the script:

    run_skill_script: {"skill": "dice-roll", "script": "roll_dice.py", "args": ["<count>", "<sides>"]}

The script prints one line like "Rolled 3d6: 4, 2, 5 (total 11)". Speak that result
to the user. If the user says "roll a d20", use sides=20. Defaults: count=1, sides=6.
```

**`scripts/roll_dice.py`** (stdlib only):

```python
#!/usr/bin/env python3
"""roll_dice.py [count] [sides] — prints e.g. 'Rolled 2d6: 4, 1 (total 5)'."""
import random
import sys


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    sides = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    if not (1 <= count <= 10) or not (2 <= sides <= 100):
        print("count must be 1-10 and sides must be 2-100", file=sys.stderr)
        sys.exit(1)                      # exercises the error contract
    rolls = [random.randint(1, sides) for _ in range(count)]
    print(f"Rolled {count}d{sides}: {', '.join(map(str, rolls))} (total {sum(rolls)})")


if __name__ == "__main__":
    main()
```

### Verify it yourself

```bash
python3 src/app-skills.py list
# dice-roll — Roll dice for the user... [params]

python3 src/agent_skills/dice-roll/scripts/roll_dice.py 2 20
# Rolled 2d20: 14, 3 (total 17)
```

Then by voice: say **"roll me a d20"** — TARS speaks the rolled number. (If the model calls `load_skill` first instead of the direct call, that is also valid — both paths speak a result.)

And the delete/reset round-trip:

```bash
python3 src/app-skills.py uninstall dice-roll
python3 src/app-skills.py list          # dice-roll gone
python3 src/app-skills.py reset --yes
python3 src/app-skills.py list          # dice-roll is back
```

## Where things live

| Path | What |
|---|---|
| `src/agent_skills/` | the live pool (gitignored, seeded from defaults on first start) |
| `src/agent_skills_defaults/` | the tracked shipped skills (`dice-roll`) |
| `src/modules/module_agent_skills.py` | parse/validate, store, `load_skill`/`run_skill_script`, descriptors |
| `src/app-skills.py` | the CLI |
| web UI **skills** tab | the panel (`/agent_skills/*` endpoints) |
