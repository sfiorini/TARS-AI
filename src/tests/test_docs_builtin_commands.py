"""Built-in Commands initiative — docs and UI source pins.

Mutation-kill-accepted; house idiom: whole-line heading anchors + line-prefix
needles + load-bearing claim needles + scoped bans.
"""
import os
import re
import unittest
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
        # T-26 accuracy: the old "no restart" claim was disproven on the
        # bench — directly-callable skills need a restart after install.
        assert "restart" in dev_line, \
            "agent-skill row lost the restart caveat (T-26)"
        assert "no restart" not in dev_line, \
            "disproven 'no restart' claim returned (T-26)"
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


class TestHaRemovalPins:
    """M2-S2: HA built-in removal surface (D7 rows 1-10)."""

    def test_ha_builtin_file_absent(self):
        ha_file = REPO_ROOT / "src" / "skills" / "skill_home_assistant.py"
        assert not ha_file.exists(), f"{ha_file} still exists"

    def test_legacy_map_lacks_home_assistant(self):
        ms_source = (REPO_ROOT / "src" / "modules" / "module_skills.py").read_text()
        # LEGACY_MAP block
        lm_start = ms_source.index("LEGACY_MAP = {")
        depth = 0
        lm_end = lm_start
        for i, ch in enumerate(ms_source[lm_start:], start=lm_start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    lm_end = i + 1
                    break
        lm_block = ms_source[lm_start:lm_end]
        assert "'home_assistant'" not in lm_block, "LEGACY_MAP still has home_assistant"
        assert "'discord'" in lm_block, "discord entry gone from LEGACY_MAP"
        # Docstring line above LEGACY_MAP
        doc_win = ms_source[max(0, lm_start - 500):lm_start]
        assert "[HOME_ASSISTANT]" not in doc_win, "docstring still mentions HOME_ASSISTANT"

    def test_thinking_map_home_assistant_hyphenated(self):
        import sys
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from modules.module_thinking import TOOL_CATEGORY_MAP
        assert TOOL_CATEGORY_MAP.get("home-assistant") == "smart_home", \
            "home-assistant not mapped to smart_home"
        assert "home_assistant" not in TOOL_CATEGORY_MAP, \
            "old home_assistant key still in TOOL_CATEGORY_MAP"

    def test_no_home_assistant_builtin_descriptor(self):
        import sys
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from modules.module_skills import SkillManager
        mgr = SkillManager()
        mgr.discover()
        mgr._disabled.clear()
        descriptors = list(mgr.iter_descriptors())
        names = [d.name for d in descriptors]
        assert "home_assistant" not in names, \
            f"home_assistant descriptor still present: {names}"

    def test_env_template_keeps_ha_token_adds_ha_url_comment(self):
        env_tmpl = (REPO_ROOT / ".env.template").read_text()
        assert "HA_TOKEN" in env_tmpl, "HA_TOKEN missing from .env.template"
        assert "# HA_URL" in env_tmpl or "#HA_URL" in env_tmpl or \
               any("HA_URL" in line and line.strip().startswith("#")
                   for line in env_tmpl.splitlines()), \
            "HA_URL comment line missing from .env.template"

    def test_template_lacks_ha_section(self):
        cfg_tmpl = (REPO_ROOT / "src" / "config.ini.template").read_text()
        assert "[SKILL:home_assistant]" not in cfg_tmpl, \
            "[SKILL:home_assistant] still in config.ini.template"
        assert "[SKILL:discord]" in cfg_tmpl, \
            "other disabled sections missing from template"


class TestHaDocsPins:
    """M2-S3: HA docs + entry points + shipped-example counts."""

    def test_skills_md_home_assistant_walkthrough(self):
        text = _read("skills.md")
        # Heading
        heading = "## The `home-assistant` shipped example"
        win = _section(text, heading)
        assert "python3 src/app-skills.py install" in win, \
            "missing install command"
        assert "[SKILL_STD:home-assistant]" in win, \
            "missing config section reference"
        assert "HA_TOKEN" in win, "missing token reference"
        assert "HA_URL" in win, "missing HA_URL reference"
        assert "list_entities" in win, "missing action docs"
        assert "call_service" in win, "missing action docs"
        assert "uninstall" in win, "missing uninstall escape docs"
        # D11 safety guidance + D8 UX delta (M2 audit IA-F3/IB-F3: these
        # paragraphs were deletable with the pins green)
        assert "Start with read-only actions" in win, \
            "missing read-only-first safety guidance (D11)"
        assert "user present" in win, \
            "missing attended-first-call guidance (D11)"
        assert "canned fallback" in win, "missing UX-delta note (D8)"
        # fresh-install seeding behaviour (user question 2026-11-16)
        assert "fresh install" in win, "missing fresh-install seeding note"
        assert "seeded automatically" in win, \
            "missing auto-seed claim for fresh installs"
        # T-26: runtime install needs a restart for direct dispatch
        assert "restart the app" in win, \
            "missing post-install restart guidance (T-26)"

    def test_configuration_ha_token_repointed(self):
        text = _read("configuration.md")
        token_row = _line_starting_with(text, "| `HA_TOKEN` |")
        assert "home-assistant" in token_row, \
            f"HA_TOKEN row not repointed: {token_row}"
        assert not re.search(r"skill_home_assistant", token_row,
                             re.IGNORECASE), \
            f"skill_home_assistant still in HA_TOKEN row: {token_row}"

    def test_configuration_disabled_lists_lack_ha(self):
        text = _read("configuration.md")
        for needle in ("discord", "generate_image", "generate_music",
                       "network_camera", "launch_retropie"):
            assert needle in text, \
                f"expected disabled section '{needle}' gone from config docs"
        # Only check the disabled-list sentences, not the debris note
        # (case-insensitive per §0.3.6 — M2 audit IA-F7)
        win1 = _section(text, "### `[SKILL:<name>]`")
        assert not re.search(r"home_assistant", win1, re.IGNORECASE), \
            "home_assistant still in first disabled list"
        # The second disabled list is the "## Built-in command sections"
        # intro paragraph (up to the next ### heading). The url-note +
        # debris subsection now lives AFTER [STANDARD_SKILLS] (audit
        # IB-F7 relocation), so it is outside this window by construction.
        win2 = text.split("## Built-in command sections")[1].split("\n###")[0]
        assert not re.search(r"home_assistant", win2, re.IGNORECASE), \
            "home_assistant still in second disabled list"

    def test_configuration_std_ha_url_note(self):
        text = _read("configuration.md")
        assert "[SKILL_STD:home-assistant]" in text, \
            "missing [SKILL_STD:home-assistant] url note"
        assert "HA_URL" in text, "missing HA_URL env reference in url note"
        # Placement (audit IB-F7): the note belongs AFTER the
        # [STANDARD_SKILLS] subsection, BEFORE [MCP] — not at EOF under
        # Built-in command sections.
        note_i = text.index("### `[SKILL_STD:home-assistant]` url")
        assert note_i > text.index("### `[STANDARD_SKILLS]`"), \
            "url note must sit after the [STANDARD_SKILLS] subsection"
        assert note_i < text.index("### `[MCP]`"), \
            "url note must sit before [MCP] (plan §0.4 placement)"
        # Content needles (audit IA-F6): hand-edit guidance + the
        # preserved/harmless debris claim, scoped to the note window.
        win = text[note_i:].split("\n### ")[0].split("\n## ")[0]
        assert "Hand-edit" in win, "url note lost the hand-edit guidance"
        assert "preserved" in win, "url note lost the debris-preserved claim"
        assert "harmless" in win, "url note lost the debris-harmless claim"

    def test_voice_pipeline_ha_historical(self):
        text = _read("voice-pipeline.md")
        assert "(the former home_assistant built-in)" in text, \
            "missing historical clarification in voice-pipeline.md"

    def test_entry_points_ha(self):
        index_text = _read("index.md")
        # Shipped example count
        assert "two shipped examples" in index_text, \
            "index.md still says one shipped example"
        assert "home-assistant" in index_text, \
            "home-assistant missing from index.md"
        # Quickstart count + HA membership on the count line (audit
        # IA-F5/IB-F3: the membership was deletable with the pin green)
        qs_text = _read("quickstart.md")
        assert "Three shipped examples" in qs_text, \
            "quickstart.md count not updated"
        qs_line = _line_starting_with(qs_text, "Three shipped examples")
        assert "home-assistant" in qs_line, \
            "quickstart count line lost the home-assistant membership"
        # README bullet
        readme = (REPO_ROOT / "README.md").read_text()
        assert "Home Assistant Agent Skill" in readme, \
            "HA bullet missing from README"
        # HA capability delta row: line-prefix + load-bearing claim needles
        ha_row = _line_starting_with(index_text, "| Home Assistant Agent Skill:")
        assert "REST" in ha_row, "HA delta row lost the REST claim"
        assert "HA_TOKEN" in ha_row, "HA delta row lost the token claim"
        assert "conversation-API" in ha_row, \
            "HA delta row lost the replaces-the-built-in claim"
        # Initiative row carries the HA clause
        init_row = _line_starting_with(index_text, "| Built-in Commands:")
        assert "Home Assistant moved to an Agent Skill" in init_row, \
            "initiative row lost the HA clause"
        # README bullet line-prefix (not just a substring somewhere)
        readme_line = _line_starting_with(
            readme, "- **Home Assistant Agent Skill**")
        assert "REST" in readme_line, "README HA bullet lost the REST claim"
class TestVideoDocsPins(unittest.TestCase):
    """S5: Video docs - configuration subsection, index/README entry points."""

    @classmethod
    def setUpClass(cls):
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        docs = os.path.join(repo, "docs")
        with open(os.path.join(docs, "configuration.md")) as f:
            cls._config = f.read()
        with open(os.path.join(docs, "index.md")) as f:
            cls._index = f.read()
        with open(os.path.join(repo, "README.md")) as f:
            cls._readme = f.read()

    def test_configuration_video_subsection(self):
        c = self._config
        # House idiom (close-sweep S5-1 hardening): whole-line heading
        # anchor -> SECTION WINDOW -> claim needles. Whole-file assertIn
        # let a hollowed/relocated subsection pass on elsewhere-satisfied
        # needles.
        m = re.search(r"^### Video playback \(`video` command\)[ \t]*$",
                      c, re.MULTILINE)
        self.assertIsNotNone(
            m, "video subsection heading missing or re-leveled")
        win = c[m.end():].split("\n## ", 1)[0].split("\n### ", 1)[0]
        for needle in ("site_youtube", "mpv", "yt-dlp", "tap",
                       "previous app", "extractor", "Live streams",
                       # bench-verified behaviour (2026-11-16): tap is the
                       # reliable stop; TARS deaf/mute while mpv holds audio;
                       # display auto-restores after video.
                       "deaf", "audio device", "automatically"):
            self.assertIn(needle, win,
                          f"video subsection lost claim: {needle}")

    def test_entry_points_video(self):
        idx = self._index
        lines = idx.split(chr(10))
        video_line = [l for l in lines if l.startswith("| Internal video app:")]
        self.assertEqual(len(video_line), 1, "video row missing or duplicated")
        self.assertIn("mpv", video_line[0])
        self.assertIn("YouTube", video_line[0])
        self.assertIn("prior-app", video_line[0])

        initiative = [l for l in lines if l.startswith("| Built-in Commands:")]
        self.assertEqual(len(initiative), 1)
        self.assertIn("video", initiative[0].lower())

        self.assertIn("video playback", idx.lower())

        readme_lines = self._readme.split("\n")
        video_bullet = [l for l in readme_lines if "video" in l.lower() and l.strip().startswith("- **Internal video")]
        self.assertEqual(len(video_bullet), 1, "README video bullet missing")
        self.assertIn("mpv", video_bullet[0])

    def test_initiative_row_complete(self):
        idx = self._index
        lines = idx.split(chr(10))
        initiative = [l for l in lines if l.startswith("| Built-in Commands:")]
        self.assertEqual(len(initiative), 1)
        row = initiative[0]
        self.assertIn("relabeled", row)
        self.assertIn("example", row)
        self.assertIn("Home Assistant", row)
        self.assertIn("video", row.lower())

    def test_video_not_startup_app_option(self):
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        config_path = os.path.join(repo, "src", "modules", "module_config.py")
        with open(config_path) as f:
            cfg = f.read()
        self.assertNotIn("video", cfg)
