"""Endpoint parity (S-M7-02): every new Flask route must appear in
docs/web-ui.md.  The route strings are sourced from the constants defined
next to the route groups in module_chatui.py — adding a route without
documenting it fails CI.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]



def _webui_doc() -> str:
    return (REPO_ROOT / "docs" / "web-ui.md").read_text(encoding="utf-8")


def _route_constants():
    """AST-extract the route constants from module_chatui.py — the source
    of truth stays next to the route groups, and the test needs no Flask
    installed (module_chatui imports flask at module level)."""
    import ast
    chatui = (REPO_ROOT / "src" / "modules" / "module_chatui.py").read_text(
        encoding="utf-8")
    tree = ast.parse(chatui)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("MOVEMENT_ROUTES",
                                           "AGENT_SKILLS_ROUTES",
                                           "MCP_ROUTES") \
                and isinstance(node.value, (ast.List, ast.Tuple)):
            found[node.targets[0].id] = [
                elt.value for elt in node.value.elts
                if isinstance(elt, ast.Constant)]
    assert set(found) == {"MOVEMENT_ROUTES", "AGENT_SKILLS_ROUTES",
                          "MCP_ROUTES"}, f"route constants missing: {found}"
    return found["MOVEMENT_ROUTES"], found["AGENT_SKILLS_ROUTES"], \
        found["MCP_ROUTES"]


def test_every_new_route_is_documented():
    doc = _webui_doc()
    movements, agent_skills, mcp = _route_constants()
    for route in movements + agent_skills + mcp:
        assert route in doc, f"web-ui.md lost route '{route}'"


def test_changed_sequence_routes_documented():
    doc = _webui_doc()
    for route in ("/save_sequence", "/play_sequence", "/get_movement_steps/"):
        assert route in doc, f"web-ui.md lost route '{route}'"
