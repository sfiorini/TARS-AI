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