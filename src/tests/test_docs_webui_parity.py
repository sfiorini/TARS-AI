"""Endpoint parity (S-M7-02, M7 review F1): every Flask route — METHOD AND
PATH — must appear in docs/web-ui.md, and every documented route must exist
in the code.  The (method, path) pairs are AST-collected from the
@flask_app.route decorators in module_chatui.py — decorator-sourcing
cannot drift: a route (or method) added without documentation fails CI,
and a documented route removed from the code is caught by the reverse
check.
"""
import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHATUI = REPO_ROOT / "src" / "modules" / "module_chatui.py"
WEBUI_DOC = REPO_ROOT / "docs" / "web-ui.md"

# the route groups this parity test owns (M2/M5/M6 additions)
PREFIXES = ("/movements", "/agent_skills", "/mcp")
_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
# doc rows look like: | `GET /movements`, `POST /movements`, … | … |
_DOC_PAIR_RE = re.compile(
    r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_<>/.-]+)")


def _decorator_pairs(decorator):
    """(method, path) pairs for one @flask_app.route decorator."""
    if not (isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "route"
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "flask_app"):
        return []
    if not decorator.args or not isinstance(decorator.args[0],
                                            ast.Constant):
        return []
    path = decorator.args[0].value
    if not (isinstance(path, str) and path.startswith(PREFIXES)):
        return []
    # methods=[...] keyword; Flask defaults to GET when omitted
    methods = ["GET"]
    for keyword in decorator.keywords:
        if keyword.arg == "methods" and isinstance(keyword.value, ast.List):
            methods = [elt.value for elt in keyword.value.elts
                       if isinstance(elt, ast.Constant)
                       and isinstance(elt.value, str)]
    return [(method, path) for method in methods]


def _collect_route_pairs(source: str):
    """AST-walk a module source and return the sorted set of
    (METHOD, path) pairs for @flask_app.route decorators under the
    owned prefixes."""
    tree = ast.parse(source)
    pairs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                pairs.update(_decorator_pairs(decorator))
    return sorted(pairs)


def _documented_pairs(doc: str):
    """(method, path) pairs the doc lists under the owned prefixes."""
    pairs = set()
    for match in _DOC_PAIR_RE.finditer(doc):
        method, path = match.group(1), match.group(2)
        if path.startswith(PREFIXES):
            pairs.add((method, path))
    return pairs


def test_collector_is_method_aware():
    """Test-of-the-test: the AST walk extracts (method, path) PAIRS —
    a method added to a route without documentation WILL be collected
    (and so fail the parity assertion), and a methods-less decorator
    defaults to GET per Flask semantics."""
    snippet = (
        "from x import flask_app\n"
        "@flask_app.route('/movements', methods=['GET', 'POST'])\n"
        "def a(): pass\n"
        "@flask_app.route('/agent_skills/<name>')\n"      # defaults to GET
        "def b(): pass\n"
        "@flask_app.route('/other_thing', methods=['GET'])\n"  # not owned
        "def d(): pass\n"
    )
    collected = _collect_route_pairs(snippet)
    assert collected == [("GET", "/agent_skills/<name>"),
                         ("GET", "/movements"),
                         ("POST", "/movements")], collected


def test_every_route_method_is_documented():
    doc = WEBUI_DOC.read_text(encoding="utf-8")
    collected = _collect_route_pairs(CHATUI.read_text(encoding="utf-8"))
    # the walk must not silently collect nothing (a refactor that breaks
    # the AST walk fails loudly here instead of passing vacuously)
    assert len(collected) >= 15, (
        f"route-pair collection implausibly small ({collected}) — "
        f"is the AST walk broken?")
    for method, path in collected:
        # the METHOD must directly precede the path in the doc — a path
        # documented with only OTHER methods does not count (a bare
        # substring check would match the method of a different route
        # on the same table row)
        # the char after the path must not continue it (else "POST /mcp"
        # would match inside "POST /mcp/<name>/test"); no trailing \b —
        # paths end in ">" (non-word), where \b never matches
        pair_re = re.compile(
            rf"\b{method}\s+{re.escape(path)}(?![\w/<])")
        assert pair_re.search(doc), (
            f"web-ui.md does not document {method} {path}")


def test_documented_routes_exist_in_code():
    """Reverse check: a route the doc advertises must exist in the code —
    the doc must not advertise endpoints that were removed."""
    doc = WEBUI_DOC.read_text(encoding="utf-8")
    collected = set(_collect_route_pairs(CHATUI.read_text(encoding="utf-8")))
    documented = _documented_pairs(doc)
    assert documented, "doc parsing found no owned routes — broken regex?"
    for method, path in sorted(documented):
        assert (method, path) in collected, (
            f"web-ui.md documents {method} {path} but the code has no "
            f"such route")


def test_changed_sequence_routes_documented():
    doc = WEBUI_DOC.read_text(encoding="utf-8")
    for route in ("/save_sequence", "/play_sequence", "/get_movement_steps/"):
        assert route in doc, f"web-ui.md lost route '{route}'"


def test_test_counts_not_hardcoded():
    """M7 review F3: hardcoded pass-counts rot — the docs must use the
    command form, and no stale 'N passed' number may return."""
    for page in ("development.md", "quickstart.md", "troubleshooting.md",
                 "servo-watchdog.md"):
        doc = (REPO_ROOT / "docs" / page).read_text(encoding="utf-8")
        assert "python3 -m pytest src/tests -q" in doc, \
            f"docs/{page} must tell the reader to run the suite"
        stale = re.findall(r"\*\*\d+ passed\*\*", doc)
        assert not stale, f"docs/{page} hardcodes a pass count: {stale}"
