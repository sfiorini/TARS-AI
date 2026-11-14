"""Docs parity (S-M2-15): docs/movements.md must document the movement store.

The page was rewritten around the JSON store (defaults-vs-live split,
reset/prune semantics, CLI/UI CRUD).  These tests fail whenever the doc
drifts back to the old hardcoded-functions framing or loses a key term.
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_movements_doc_documents_the_store():
    doc = _read("movements.md")
    for needle in ("movements.json", "movements_defaults.json",
                   "app-movements.py", "reset", "prune", "export",
                   "import", "validate", "/movements/", "left_height",
                   "hold_time", "movement"):
        assert needle in doc, f"movements.md lost '{needle}'"


def test_movements_doc_old_framing_gone():
    doc = _read("movements.md")
    assert "33 hardcoded" not in doc
    assert "module_movement_registry.py defines" not in doc


def test_movements_doc_id_table_lists_every_movement():
    doc = _read("movements.md")
    defaults = json.loads(
        (REPO_ROOT / "src" / "movements_defaults.json").read_text())
    for mid, entry in defaults["movements"].items():
        assert f"`{mid}`" in doc, f"movement id '{mid}' missing from the doc"
        assert entry["name"] in doc, f"name for '{mid}' missing from the doc"


def test_web_ui_documents_movements_routes():
    doc = _read("web-ui.md")
    for endpoint in ("/movements", "/movements/reset"):
        assert endpoint in doc, f"web-ui.md lost endpoint '{endpoint}'"


def _section(text: str, heading: str) -> str:
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def test_readme_fork_delta_lists_movement_studio():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = _section(readme, "## What this fork adds (V3)")
    assert "Movement Studio" in section, \
        "README's fork-delta list lost the Movement Studio entry"


def test_docs_home_documents_movement_studio():
    index = _read("index.md")
    table = _section(index, "## What V3 adds over upstream")
    assert "Movement Studio" in table, \
        "docs home page delta table lost the Movement Studio row"
    assert "the builder" not in index, \
        "docs home page still advertises the removed Builder tab"
