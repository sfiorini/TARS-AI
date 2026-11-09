"""Docs parity (S-M6-12): docs/mcp.md must document the MCP client as
shipped.  These tests read the page and fail whenever a documented
protocol detail, command, or filename drifts from the code again.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_mcp_doc_protocol_era():
    doc = _read("mcp.md")
    assert "initialize" in doc
    assert "notifications/initialized" in doc
    assert "MCP-Session-Id" in doc
    assert "MCP-Protocol-Version" in doc
    assert "nextCursor" in doc


def test_mcp_doc_transports():
    doc = _read("mcp.md")
    assert "stdio" in doc
    assert "Streamable HTTP" in doc


def test_mcp_doc_registry_and_cli():
    doc = _read("mcp.md")
    assert "mcp_servers.json" in doc
    assert "mcp_servers.json.template" in doc
    assert "app-mcp.py" in doc


def test_mcp_doc_iserror_and_example():
    doc = _read("mcp.md")
    assert "isError" in doc
    assert "mcp_example_server.py" in doc
    assert "mcp_example_get_time" in doc


def test_mcp_doc_in_nav():
    nav = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "mcp.md" in nav
