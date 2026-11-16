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
        assert MAIN_JS.count('Loading commands\u2026') == 2
        assert not re.search(r'Loading skills', MAIN_JS, re.IGNORECASE)

    def test_no_commands_discovered(self):
        assert 'No commands discovered' in MAIN_JS
        assert 'No skills discovered' not in MAIN_JS

    def test_toggle_toast_line_prefix_and_underscore_replacement(self):
        line = _line_starting_with(
            MAIN_JS,
            '              showToast(`"${name.replace(/_/g,\' \')}"',
        )
        assert "${enabled?'enabled':'disabled'}" in line
        assert not re.search(r'Skill "\${', MAIN_JS, re.IGNORECASE)

    def test_failed_to_toggle_command(self):
        assert 'Failed to toggle command' in MAIN_JS
        assert 'Failed to toggle skill' not in MAIN_JS

    def test_failed_to_load_commands(self):
        assert 'Failed to load commands' in MAIN_JS
        assert 'Failed to load skills' not in MAIN_JS


class TestExampleRemoval:
    """M1-S2: example built-in removed — file absent, frozenset updated,
    thinking triggers replaced, no descriptor."""

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
        descriptors = list(mgr.iter_descriptors())
        names = [d.name for d in descriptors]
        assert "example" not in names