"""Config-key parity (S-M6-12): every shipped [LLM] / [STANDARD_SKILLS] /
[MCP] key must be documented in docs/configuration.md — any future rename
fails CI.  (This test could only exist once all keys ship; judgement
call: landed here, not M4.)
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

ALL_KEYS = [
    # [LLM] — deferred reply loop (M4)
    "tool_loop_max_hops",
    "tool_call_timeout",
    "request_timeout",
    "deferred_loop_enabled",
    # [STANDARD_SKILLS] — agent skills (M5)
    "enabled",
    "body_char_limit",
    "prompt_budget_chars",
    # [MCP] — MCP client (M6)
    "health_interval_s",
    "restart_max_attempts",
]


def test_configuration_documents_every_new_key():
    doc = (DOCS / "configuration.md").read_text(encoding="utf-8")
    for key in ALL_KEYS:
        assert key in doc, f"configuration.md lost key '{key}'"


def test_configuration_documents_sections():
    doc = (DOCS / "configuration.md").read_text(encoding="utf-8")
    assert "[STANDARD_SKILLS]" in doc or "STANDARD_SKILLS" in doc
    assert "[MCP]" in doc or "`MCP`" in doc


def test_config_template_matches_documented_keys():
    template = (REPO_ROOT / "src" / "config.ini.template").read_text(
        encoding="utf-8")
    for key in ALL_KEYS:
        assert key in template, f"config.ini.template lost key '{key}'"
