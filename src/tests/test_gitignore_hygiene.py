"""C10: .gitignore must not blanket-ignore markdown or persona-prefixed files.

The old `*.md` rule silently untracked every new docs page (each needed a
forced `git add -f`), and the broad `persona*` rule ignored any file whose
name starts with "persona" anywhere in the tree. Ignore rules must be
explicit: CLAUDE.md, todo*.md, ai_plan/**/*.md and the actual persona
paths — while README.md and docs/*.md stay normally trackable.
"""
import os
import subprocess
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _ignored(path):
    """True if git ignores the (possibly nonexistent) path in the repo."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=_REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


class TestGitignoreMarkdown(unittest.TestCase):
    def test_docs_markdown_not_ignored(self):
        self.assertFalse(_ignored("docs/some-new-page.md"),
                         "docs/*.md must be trackable without git add -f")

    def test_readme_not_ignored(self):
        self.assertFalse(_ignored("README.md"))

    def test_claude_md_ignored(self):
        self.assertTrue(_ignored("CLAUDE.md"))

    def test_todo_markdown_ignored(self):
        self.assertTrue(_ignored("todo.md"))
        self.assertTrue(_ignored("todo-phase-b.md"))

    def test_ai_plan_markdown_ignored(self):
        # local planning artifacts, never committed
        self.assertTrue(_ignored("ai_plan/2026-11-07-code-docs-audit/plan.md"))


class TestGitignorePersona(unittest.TestCase):
    def test_character_persona_ini_ignored(self):
        self.assertTrue(_ignored("src/character/NEWCHARACTER/persona.ini"))

    def test_arbitrary_persona_prefixed_file_not_ignored(self):
        # the old broad `persona*` rule ignored any such file anywhere
        self.assertFalse(_ignored("personal-notes.txt"))


class TestLegacySequencesFile(unittest.TestCase):
    """src/custom_sequences.json is gone (D5: the step_forward_test entry
    was discarded, not migrated — builder sequences live in the store)."""

    def test_custom_sequences_gone(self):
        self.assertFalse(
            os.path.exists(os.path.join(_REPO, "src", "custom_sequences.json")))


class TestGitignoreAgentSkills(unittest.TestCase):
    """The live agent-skills pool is per-robot state (seeded from the
    tracked defaults on first start); only the .gitkeep marker is tracked."""

    def test_agent_skills_pool_ignored(self):
        self.assertTrue(_ignored("src/agent_skills/dice-roll/SKILL.md"))

    def test_agent_skills_gitkeep_gone(self):
        # M5 review F3: the tracked .gitkeep made the pool dir always
        # exist, so first-start seeding never fired on a fresh clone —
        # the marker is gone and the dir is absent until seeded
        self.assertFalse(
            os.path.exists(os.path.join(_REPO, "src", "agent_skills",
                                        ".gitkeep")))

    def test_defaults_tracked(self):
        self.assertFalse(
            _ignored("src/agent_skills_defaults/dice-roll/SKILL.md"))


class TestGitignoreMcp(unittest.TestCase):
    """The live MCP registry is per-robot state; the template is tracked."""

    def test_mcp_servers_json_ignored(self):
        self.assertTrue(_ignored("src/mcp_servers.json"))

    def test_mcp_template_tracked(self):
        self.assertFalse(_ignored("src/mcp_servers.json.template"))


class TestGitignoreLiveState(unittest.TestCase):
    """Per-robot live state: src/movements.json is gitignored (seeded from
    the tracked src/movements_defaults.json on first start, D5)."""

    def test_movements_json_ignored(self):
        self.assertTrue(_ignored("src/movements.json"))

    def test_movements_defaults_tracked(self):
        self.assertFalse(_ignored("src/movements_defaults.json"))


class TestFinalLiveStateSplit(unittest.TestCase):
    """S-M7-03: the complete final tracked-vs-ignored split, one
    self-documenting table — a new state file is one line to add."""

    _SPLIT = [
        # (path, ignored?)
        ("src/movements.json", True),
        ("src/movements_defaults.json", False),
        ("src/mcp_servers.json", True),
        ("src/mcp_servers.json.template", False),
        ("src/agent_skills/dice-roll/SKILL.md", True),
        ("src/agent_skills_defaults/dice-roll/SKILL.md", False),
        ("docs/some-new-page.md", False),
    ]

    def test_all_new_live_state_files(self):
        for path, ignored in self._SPLIT:
            self.assertEqual(_ignored(path), ignored,
                             f"{path} must be "
                             f"{'ignored' if ignored else 'trackable'}")

    def test_custom_sequences_gone(self):
        self.assertFalse(
            os.path.exists(os.path.join(_REPO, "src",
                                        "custom_sequences.json")))


if __name__ == "__main__":
    unittest.main()
