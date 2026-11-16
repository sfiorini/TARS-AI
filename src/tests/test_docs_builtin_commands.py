"""Built-in Commands initiative — docs and UI source pins.

Mutation-kill-accepted; house idiom: whole-line heading anchors + line-prefix
needles + load-bearing claim needles + scoped bans.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_JS = (REPO_ROOT / "src" / "www" / "static" / "js" / "main.js").read_text()
DOCS = REPO_ROOT / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^{re.escape(heading)}[ \t]*$", text, re.MULTILINE)
    assert match, f"document lost the section heading '{heading}'"
    return text[match.end():].split("\n## ", 1)[0]


def _line_starting_with(section: str, prefix: str) -> str:
    for line in section.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starts with '{prefix}'")


class TestUiRenamePins:
    def test_section_label_is_builtin_commands(self):
        assert "'SKILLS':'Built-in Commands'" in MAIN_JS
        assert not re.search(r"SKILLS\'\s*:\s*\'Skills\'", MAIN_JS, re.IGNORECASE)

    def test_loading_commands_count_is_two(self):
        # </span> adjacency kills substring extensions (Loading commands...x)
        assert MAIN_JS.count('Loading commands\u2026</span>') == 2
        assert not re.search(r'Loading skills', MAIN_JS, re.IGNORECASE)

    def test_no_commands_discovered(self):
        # </div> adjacency kills substring extensions (No commands discoveredx)
        assert 'No commands discovered</div>' in MAIN_JS
        assert not re.search(r'No skills discovered', MAIN_JS, re.IGNORECASE)

    def test_toggle_toast_line_prefix_and_underscore_replacement(self):
        line = _line_starting_with(
            MAIN_JS,
            '              showToast(`"${name.replace(/_/g,\' \')}"',
        )
        assert "${enabled?'enabled':'disabled'}" in line
        assert not re.search(r'Skill "\${', MAIN_JS, re.IGNORECASE)

    def test_failed_to_toggle_command(self):
        assert 'Failed to toggle command' in MAIN_JS
        assert not re.search(r'Failed to toggle skill', MAIN_JS, re.IGNORECASE)

    def test_failed_to_load_commands(self):
        assert 'Failed to load commands' in MAIN_JS
        assert not re.search(r'Failed to load skills', MAIN_JS, re.IGNORECASE)


class TestDocsRenamePins:
    """M1-S3: combined docs page sweep — per-page pins + keep-list pins."""

    def test_development_builtin_commands_section(self):
        dev = _read("development.md")
        window = _section(dev, "### Built-in Commands")
        assert _line_starting_with(
            window, "Built-in commands are plain Python files"
        )
        assert "skill_*.py" in window
        assert "auto-discovered" in window
        assert not re.search(r"Builtin tools \(skills\)", dev, re.IGNORECASE)

    def test_development_authoring_template_points_at_dice_roll(self):
        dev = _read("development.md")
        # Table row: line-prefix + claim needle
        line = _line_starting_with(dev, "| **Built-in command**")
        assert "src/agent_skills_defaults/dice-roll/" in line
        # Inline skeleton in the section window
        window = _section(dev, "### Built-in Commands")
        assert "SKILL = {" in window
        assert "def execute(parameters, context):" in window
        # Ban the old pointer
        assert "skill_example.py" not in dev

    def test_webui_skills_row_says_builtin_command(self):
        webui = _read("web-ui.md")
        line = _line_starting_with(webui, "| `GET /get_skills`")
        assert "built-in command" in line
        # Route names frozen
        assert "GET /get_skills" in webui
        assert "POST /toggle_skill" in webui
        assert "POST /save_skill_config" in webui

    def test_voice_pipeline_definitions_and_speakable_list(self):
        vp = _read("voice-pipeline.md")
        assert "built-in command definitions" in vp
        # Follow-up failure line: speakable list
        line = _line_starting_with(vp, "- **Follow-up failure**")
        assert "web_search" in line
        assert "tars_radio" in line
        assert "capture_camera_view" in line
        assert not re.search(r"example", line, re.IGNORECASE)

    def test_movements_voice_heading_command(self):
        mov = _read("movements.md")
        assert re.search(
            r"^## Voice: the `execute_movement` command[ \t]*$",
            mov, re.MULTILINE
        )
        window = _section(mov, "## Voice: the `execute_movement` command")
        assert window.count("command prompt") == 1
        # "command prompt" also appears in the leg-wave note outside this window
        assert mov.count("command prompt") >= 2
        assert "the `execute_movement` skill" not in mov

    def test_configuration_per_command_sections(self):
        cfg = _read("configuration.md")
        assert re.search(
            r"^## Built-in command sections[ \t]*$",
            cfg, re.MULTILINE
        )
        window = _section(cfg, "### `[SKILL:<name>]`")
        assert _line_starting_with(window, "Per-command config")
        assert "a built-in command defines" in cfg
        # Ban the old heading text within THIS section only (standard pool keeps
        # "Per-skill config" for [SKILL_STD:*] — legitimate).
        assert not re.search(r"Per-skill config", window, re.IGNORECASE)
        assert not re.search(r"## Skills sections", cfg, re.IGNORECASE)

    def test_configuration_tension_note(self):
        cfg = _read("configuration.md")
        window = _section(cfg, "### `[SKILL:<name>]`")
        assert _line_starting_with(
            window, "    The web UI and docs call this pool"
        )
        assert "Built-in Commands" in window
        assert "`[SKILL:<name>]`" in window
        assert "internal" in window
        assert "/get_skills" in window
        assert "skill_*.py" in window

    def test_quickstart_next_steps_renamed(self):
        qs = _read("quickstart.md")
        line = _line_starting_with(qs, "- [Configuration](configuration.md)")
        assert "built-in commands & agent skills" in line

    def test_mcp_intro_renamed(self):
        mcp = _read("mcp.md")
        assert "built-in commands and agent skills" in mcp
        assert not re.search(r"builtin skills", mcp, re.IGNORECASE)

    def test_keep_list_agent_skills_survives(self):
        # skills.md still says "Agent Skills"
        skills = _read("skills.md")
        assert "Agent Skills" in skills
        # mkdocs.yml nav has skills.md
        yml = (REPO_ROOT / "mkdocs.yml").read_text()
        assert "- Agent Skills: skills.md" in yml
        # configuration.md [STANDARD_SKILLS]
        cfg = _read("configuration.md")
        assert "### `[STANDARD_SKILLS]`" in cfg
        # index.md:35 doc-map Agent Skills line
        idx = _read("index.md")
        assert "- [Agent Skills](skills.md)" in idx
        # web-ui.md:51 agent_skills row
        webui = _read("web-ui.md")
        line = _line_starting_with(
            webui, "| `GET /agent_skills`"
        )
        assert "Agent Skills pool" in line
        # development.md:65 Agent-skill row
        dev = _read("development.md")
        dev_line = _line_starting_with(dev, "| **Agent skill**")
        assert "Agent Skills" in dev_line
        # configuration.md:21 skill_discord filename reference
        assert "`skill_discord`" in cfg


class TestEntryPointPins:
    """M1-S4: entry points — index delta table + doc-map + flow line + README."""

    def test_index_delta_table_builtin_commands_row(self):
        idx = _read("index.md")
        window = _section(idx, "## What V3 adds over upstream")
        line = _line_starting_with(window, "| Built-in Commands:")
        assert "relabeled" in line
        assert "`[SKILL:*]`" in line
        assert "`example`" in line

    def test_index_doc_map_development_line(self):
        idx = _read("index.md")
        line = _line_starting_with(idx, "- [Development](development.md)")
        assert "authoring built-in commands & agent skills" in line

    def test_index_conversation_flow_builtin_commands(self):
        idx = _read("index.md")
        assert "built-in commands, e.g." in idx
        assert "skills, e.g. `execute_movement`" not in idx

    def test_readme_execute_movement_command(self):
        readme = (REPO_ROOT / "README.md").read_text()
        line = _line_starting_with(
            readme,
            "- **Full movement registry exposed to the LLM**",
        )
        assert "`execute_movement` command" in line
        assert "`execute_movement` skill" not in line


class TestExampleRemoval:

    def test_example_skill_file_absent(self):
        import os
        assert not os.path.exists(
            str(REPO_ROOT / "src" / "skills" / "skill_example.py")
        )

    def test_speakable_set_lacks_example(self):
        from src.modules.module_skills import _SPEAKABLE_BUILTIN_SKILLS
        assert "example" not in _SPEAKABLE_BUILTIN_SKILLS
        assert "capture_camera_view" in _SPEAKABLE_BUILTIN_SKILLS
        assert "tars_radio" in _SPEAKABLE_BUILTIN_SKILLS
        assert "web_search" in _SPEAKABLE_BUILTIN_SKILLS

    def test_thinking_keywords_dice_roll_not_skill_example(self):
        thinking = (REPO_ROOT / "src" / "modules" / "module_thinking.py").read_text()
        # KEYWORDS dict line for "skill" (around line 211)
        # Contains: "skill": ("skill example", ...) or after fix: "dice roll"
        kw_start = thinking.index('KEYWORDS = {')
        # Find the matching closing brace: count nesting
        depth = 0
        kw_end = kw_start
        for i, ch in enumerate(thinking[kw_start:], start=kw_start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    kw_end = i + 1
                    break
        kw_block = thinking[kw_start:kw_end]
        assert '"dice roll"' in kw_block
        assert '"skill example"' not in kw_block

    def test_thinking_suppression_dice_roll_not_skill_example(self):
        thinking = (REPO_ROOT / "src" / "modules" / "module_thinking.py").read_text()
        sup_start = thinking.index('SUPPRESSION_TRIGGERS = {')
        depth = 0
        sup_end = sup_start
        for i, ch in enumerate(thinking[sup_start:], start=sup_start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    sup_end = i + 1
                    break
        sup_block = thinking[sup_start:sup_end]
        assert '"dice roll"' in sup_block
        assert '"skill example"' not in sup_block

    def test_no_example_builtin_descriptor(self):
        import sys
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from modules.module_skills import SkillManager
        mgr = SkillManager()
        mgr.discover()  # the real src/skills — without this the test is vacuous
        mgr._disabled.clear()  # every loaded skill yields a descriptor
        descriptors = list(mgr.iter_descriptors())
        names = [d.name for d in descriptors]
        assert "example" not in names