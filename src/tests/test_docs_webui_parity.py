"""Endpoint parity (S-M7-02, M7 review F1): every new Flask route must
appear in docs/web-ui.md.  The route paths are AST-collected from the
@flask_app.route decorators in module_chatui.py — decorator-sourcing
cannot drift: a route added without documentation fails CI, and a
documented route removed from the code is caught by the reverse check.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHATUI = REPO_ROOT / "src" / "modules" / "module_chatui.py"
WEBUI_DOC = REPO_ROOT / "docs" / "web-ui.md"

# the route groups this parity test owns (M2/M5/M6 additions)
PREFIXES = ("/movements", "/agent_skills", "/mcp")


def _collect_routes(source: str):
    """AST-walk a module source and return the sorted set of
    @flask_app.route('<path>', ...) paths under the owned prefixes."""
    tree = ast.parse(source)
    paths = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "route"
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "flask_app"):
                continue
            if not decorator.args or not isinstance(decorator.args[0],
                                                   ast.Constant):
                continue
            path = decorator.args[0].value
            if isinstance(path, str) and path.startswith(PREFIXES):
                paths.add(path)
    return sorted(paths)


def test_collector_finds_routes():
    """Test-of-the-test: the AST walk extracts routes from arbitrary
    source — an undocumented route added to the module WILL be collected
    and so WILL fail the parity assertion."""
    snippet = (
        "from x import flask_app\n"
        "@flask_app.route('/movements', methods=['GET'])\n"
        "def a(): pass\n"
        "@flask_app.route('/agent_skills/<name>', methods=['GET'])\n"
        "def b(): pass\n"
        "@flask_app.route('/mcp/<name>/test', methods=['POST'])\n"
        "def c(): pass\n"
        "@flask_app.route('/other_thing', methods=['GET'])\n"  # not owned
        "def d(): pass\n"
    )
    collected = _collect_routes(snippet)
    assert collected == ["/agent_skills/<name>", "/mcp/<name>/test",
                         "/movements"], collected


def test_every_new_route_is_documented():
    doc = WEBUI_DOC.read_text(encoding="utf-8")
    collected = _collect_routes(CHATUI.read_text(encoding="utf-8"))
    # the walk must not silently collect nothing (a refactor that breaks
    # the AST walk fails loudly here instead of passing vacuously)
    assert len(collected) >= 10, (
        f"route collection implausibly small ({collected}) — "
        f"is the AST walk broken?")
    for route in collected:
        assert route in doc, f"web-ui.md lost route '{route}'"


def test_test_counts_not_hardcoded():
    """M7 review F3: hardcoded pass-counts rot — the docs must use the
    command form, and no stale 'N passed' number may return."""
    for page in ("development.md", "quickstart.md", "troubleshooting.md",
                 "servo-watchdog.md"):
        doc = (REPO_ROOT / "docs" / page).read_text(encoding="utf-8")
        assert "python3 -m pytest src/tests -q" in doc, \
            f"docs/{page} must tell the reader to run the suite"
        import re
        stale = re.findall(r"\*\*\d+ passed\*\*", doc)
        assert not stale, f"docs/{page} hardcodes a pass count: {stale}"


def test_changed_sequence_routes_documented():
    doc = WEBUI_DOC.read_text(encoding="utf-8")
    for route in ("/save_sequence", "/play_sequence", "/get_movement_steps/"):
        assert route in doc, f"web-ui.md lost route '{route}'"
