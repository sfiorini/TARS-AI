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
├── scripts/configure_tars_client.sh # robot client config: STT/TTS/vision/embeddings + the TTS chain
├── scripts/setup_tts_voice.sh       # optional: clone your own Fish.audio voice (CC BY 4.0 sample)
├── src/
│   ├── .venv/                       # virtualenv created by Install.sh
│   ├── app.py                       # entrypoint: python app.py show_ui=true [key=value …]
│   ├── app-servotester.py           # pygame servo calibration GUI
│   ├── app-movements.py            # movements CLI: list/show/edit/reset/prune/export/import/validate
│   ├── app-skills.py               # agent-skills CLI: list/install/uninstall/validate/reset
│   ├── app-mcp.py                  # MCP CLI: list/add/remove/enable/disable/test
│   ├── mcp_example_server.py       # the shipped example MCP server (copy this)
│   ├── movements_defaults.json     # tracked movement defaults (live movements.json is gitignored)
│   ├── mcp_servers.json.template   # tracked MCP registry template (live mcp_servers.json is gitignored)
│   ├── agent_skills_defaults/      # tracked shipped skills (live agent_skills/ is gitignored)
│   ├── config.ini.template          # → src/config.ini
│   ├── modules/                     # module_*.py engine: main, stt, llm, tts, servoctl,
│   │                                #   movementstore (movement JSON store), toolregistry +
│   │                                #   toolloop (unified tool dispatch + deferred reply),
│   │                                #   agent_skills (SKILL.md pool), mcp (MCP client),
│   │                                #   battery, chatui, config, skills, vision,
│   │                                #   memory/hyperdb, messageQue (log), …
│   ├── skills/                      # skill_*.py auto-discovered BUILTIN tools
│   ├── tests/                       # pytest suite
│   └── character/ memory/ stt/ tts/ vision/ www/ logs/
└── TARS-AI_Server/                  # companion FastAPI server: app-server.py :5678, Dockerfile,
                                     #   docker-compose.yml, requirements-server.txt, www/, README.txt
```

## Running the tests

```bash
python3 -m pytest src/tests -q
```

On V3 the whole suite is green in a few seconds — run `python3 -m pytest src/tests -q` for the current count (the number grows with every feature; the docs deliberately do not hardcode it). `pytest.ini` sets `testpaths = src/tests` and `pythonpath = src`; the tests inject fake `board`/`busio` modules and bootstrap `src/config.ini` from the template, so no Pi and no hardware deps are needed — only `pip install pytest`. On the robot use the app venv: `src/.venv/bin/python -m pytest src/tests -q`.

The suite covers the servo watchdog, the movement store (including a golden structural test on `movements_defaults.json`), the tool registry and deferred reply loop, the agent-skills pool (the shipped `dice-roll` script runs via real subprocesses), the MCP client (raw JSON-RPC over a real pipe to the shipped example server), the TTS/STT providers, and docs-parity tests that fail when a page drifts from the code. Run `python3 -m pytest src/tests -q` for the full list and count.

## Authoring tools — the three kinds

The LLM can call three kinds of tools, all dispatched through one registry (`module_toolregistry.py`) and the deferred reply loop (`module_toolloop.py`):

| Kind | What it is | How to add one | Template to copy |
|---|---|---|---|
| **Builtin tool** | In-process Python code, enable/disable only, addable/modifiable only in code (D2) | Drop a `skill_*.py` in `src/skills/` (auto-discovered) — see below | `src/skills/skill_example.py` |
| **Agent skill** | A spec-pure `SKILL.md` folder with optional scripts — installable from folder/zip at runtime, no restart | Create in the web UI **skills** tab, `app-skills.py install`, or drop a folder in `src/agent_skills/` | `src/agent_skills_defaults/dice-roll/` — see [Agent Skills](skills.md) |
| **MCP tool** | An external server process (stdio or Streamable HTTP) — tools appear as `mcp_<server>_<tool>` | Add a registry entry via the web UI **MCP** tab or `app-mcp.py add` | `src/mcp_example_server.py` — see [MCP](mcp.md) |

Movements are data, not code: the 33 shipped movements live in `src/movements_defaults.json` — the authoritative, hand-edited source of truth (`scripts/export_movement_defaults.py` is retained only as historical provenance for how the file was first derived and can no longer regenerate it — its input functions were deleted when the store took over). Add or edit movements in the web UI **movements** tab or `app-movements.py` — see [Movements](movements.md).

### Builtin tools (skills)

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

A file missing `SKILL` or `execute` is skipped with a log line; duplicate names overwrite with a warning. Disable a skill via `[SKILL:my_skill] disabled = true` in `config.ini` or the web UI (`POST /toggle_skill`). Start from `src/skills/skill_example.py`; see `skill_execute_movement.py` for a store-driven per-request prompt ([Movements](movements.md)). Skills with `"followup": True` are result-bearing: their results flow through the deferred reply loop ([Voice Pipeline](voice-pipeline.md)).

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
