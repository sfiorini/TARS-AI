"""Docs parity (S-M5-11): docs/skills.md must document the Agent Skills
feature as shipped.  These tests read the page and fail whenever a
documented field/endpoint/contract drifts from the code again.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_skills_doc_has_all_six_frontmatter_fields():
    doc = _read("skills.md")
    for field in ("name", "description", "license", "compatibility",
                  "metadata", "allowed-tools"):
        assert field in doc, f"frontmatter field '{field}' missing"


def test_skills_doc_has_metadata_extension_encoding():
    doc = _read("skills.md")
    assert "tars_parameters" in doc
    assert "tars_config" in doc
    # the Claude-Code hard-error rationale must be stated
    assert "hard-error" in doc.lower() or "hard error" in doc.lower()


def test_skills_doc_has_meta_tools_and_cli():
    doc = _read("skills.md")
    assert "load_skill" in doc
    assert "run_skill_script" in doc
    assert "app-skills.py" in doc


def test_skills_doc_has_install_and_endpoints():
    doc = _read("skills.md")
    assert "/agent_skills/" in doc
    assert "install" in doc


def test_skills_doc_has_security_warning():
    doc = _read("skills.md")
    assert "privileges" in doc
    assert "no sandbox" in doc.lower()


def test_skills_doc_has_dice_roll_example():
    doc = _read("skills.md")
    assert "dice-roll" in doc
    assert "roll_dice.py" in doc
    # exact verification commands
    assert "python3 src/app-skills.py list" in doc
    assert "roll_dice.py 2 20" in doc
    # the voice test and the reset round-trip
    assert "roll me a d20" in doc
    assert "reset" in doc


def test_skills_doc_in_nav():
    nav = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "skills.md" in nav, "skills.md must be in the mkdocs nav (1:1)"
