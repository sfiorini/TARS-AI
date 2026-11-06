# Development

Repo layout, how to run the tests, how to author skills, how CI works, and how to contribute. Work directly on **`V3`** — it is the one and only branch of sfiorini/TARS-AI (`git branch -a` shows `remotes/origin/V3`; `upstream` = TARS-AI-Community, reference only).

## Repo layout

```
TARS-AI/
├── App-Start.py / App-Stop.py       # app lifecycle (App-Stop has TERM→KILL escalation)
├── tars-launcher.sh                 # venv + setcap cap_net_bind_service + App-Start.py (--reboot mode)
├── toggle-autostart.sh / lp.sh      # XDG autostart (~/.config/autostart/tars-ai.desktop)
├── launch_retropie.sh               # hand control to RetroPie
├── Install.sh                       # one-shot installer (per-Pi-model requirements, venv, config seed)
├── .env.template                    # secrets template (.env is gitignored)
├── mkdocs.yml / docs/ / requirements-docs.txt   # this docs site (GitHub Pages)
├── .github/workflows/docs.yml       # CI: pytest gate → mkdocs build --strict → Pages deploy
├── pytest.ini                       # testpaths = src/tests, pythonpath = src
├── 3d Printer Files/                # STL for the motorized build (A. Required/, B. Mods/, Tools/)
├── Static Version (no motors)/      # display-only statue files
├── Documentation/                   # wiring SVGs (copied into docs/assets/)
├── scripts/configure_tars_client.sh # point the robot at the mini server
├── src/
│   ├── .venv/                       # virtualenv created by Install.sh
│   ├── app.py                       # entrypoint: python app.py show_ui=true [key=value …]
│   ├── app-servotester.py           # pygame servo calibration GUI
│   ├── config.ini.template          # → src/config.ini
│   ├── custom_sequences.json        # web-UI movement sequences
│   ├── modules/                     # module_*.py engine: main, stt, llm, tts, servoctl,
│   │                                #   movements, movement_registry, battery, chatui, config,
│   │                                #   skills, vision, memory/hyperdb, messageQue (log), …
│   ├── skills/                      # skill_*.py auto-discovered skills
│   ├── tests/                       # pytest suite
│   └── character/ memory/ stt/ tts/ vision/ www/ logs/
└── TARS-AI_Server/                  # companion FastAPI server: app-server.py :5678, Dockerfile,
                                     #   docker-compose.yml, requirements-server.txt, www/, README.txt
```

## Running the tests

```bash
python3 -m pytest src/tests -q
```

On V3 this reports **62 passed** in about a second. `pytest.ini` sets `testpaths = src/tests` and `pythonpath = src`; the tests inject fake `board`/`busio` modules and bootstrap `src/config.ini` from the template, so no Pi and no hardware deps are needed — only `pip install pytest`. On the robot use the app venv: `src/.venv/bin/python -m pytest src/tests -q`.

Suite breakdown (5 files):

| File | Tests | Covers |
|---|---|---|
| `test_servo_watchdog.py` | 31 | MODE1/MODE2 health, stuck-MOVING, lazy re-init, repair + position re-apply, fake-I2C register IO |
| `test_execute_movement.py` | 13 | registry-driven skill prompt (all 33 ids), registry-only resolver, arm gating, `voicemovement` gate |
| `test_module_llm_delta.py` | 8 | `_delta_text()` GLM list-block normalization |
| `test_module_stt_elevenlabs.py` | 8 | Scribe STT request + external-server fallback |
| `test_module_vision_glm.py` | 2 | vision `message.content` list-block fix |

## Authoring skills

Skills are plain Python files **`src/skills/skill_*.py`**, auto-discovered by `module_skills.py` (glob `skills/skill_*.py`, imported as `skills.<name>`). Each defines a `SKILL` dict and an `execute()` function:

```python
SKILL = {
    "name": "my_skill",
    "required_params": ["topic"],
    "description": "What the LLM should use this for",
    "prompt": "Instructions shown to the LLM, incl. the parameter JSON format",
    "examples": ["Example - …\nUser: …\nResponse: {\"reply\": …, \"function_calls\": [{" +
                 "\"function\": \"my_skill\", \"parameters\": {…}}], \"new_memories\": []}"],
    # "config": {"field": {"type": "string", "default": "…"}}   # → [SKILL:my_skill] section
}

def execute(parameters, context):
    # context["config"] is the full load_config() dict; context["skill_config"] = [SKILL:name] values
    ...
    return None  # or a dict that modifies the LLM reply
```

A file missing `SKILL` or `execute` is skipped with a log line; duplicate names overwrite with a warning. Disable a skill via `[SKILL:my_skill] disabled = true` in `config.ini` or the web UI (`POST /toggle_skill`). Start from `src/skills/skill_example.py`; see `skill_execute_movement.py` for a registry-driven prompt ([Movements](movements.md)).

## Documentation and CI

- Docs: MkDocs + Material — `mkdocs.yml` (nav mirrors `docs/`), pages in `docs/*.md`, images in `docs/assets/`. Preview: `pip install -r requirements-docs.txt && mkdocs serve`; validate: `mkdocs build --strict`.
- `.github/workflows/docs.yml` runs on pushes to **V3** touching `docs/**`, `mkdocs.yml`, `requirements-docs.txt`, the workflow, `src/tests/**`, or `pytest.ini`. Jobs: **test** (`python -m pytest src/tests -q` on ubuntu-latest, py3.11) → **build** (`pip install -r requirements-docs.txt && mkdocs build --strict`) → **deploy** (upload-pages-artifact + deploy-pages to the `github-pages` environment) → published at `https://sfiorini.github.io/TARS-AI/`. A red test suite blocks the docs deploy.

## Contributing

1. Work on `V3`: branch from `origin/V3`, open a PR against `V3`.
2. Keep the gate green — add/extend tests under `src/tests/` for behavior changes (TDD is the norm on this branch; see the watchdog and movement suites above).
3. Update the matching page in `docs/` in the same PR.
4. Never commit `.env` or `src/config.ini` (both gitignored); secrets only via `.env`. `ai_plan/` and `.pi/` are local-only and gitignored too.
5. Community: [Discord](https://discord.gg/AmE2Gv9EUt).

## License

This fork is licensed **CC-BY-NC 4.0** — free for non-commercial use with attribution; commercial use (including selling robots, kits, or STL files) requires separate written permission. AtomikSpace-authored files are dual-licensed; retain the line "Contains contributions by Charles-Olivier Dion (AtomikSpace)" in source and docs. See [LICENSE](https://github.com/sfiorini/TARS-AI/blob/V3/LICENSE), [DUAL-LICENSE.md](https://github.com/sfiorini/TARS-AI/blob/V3/DUAL-LICENSE.md), and [ATTRIBUTION.md](https://github.com/sfiorini/TARS-AI/blob/V3/ATTRIBUTION.md).
