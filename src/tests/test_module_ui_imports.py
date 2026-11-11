"""Source-structure guard for module_ui.py's import hygiene (G3).

`queue_message` was called in UIManager.run()'s overlay error handler but
never imported — the NameError propagated to the render loop's broad
handler, which sets `self.running = False`: one overlay-load failure
killed the whole UI thread. This test AST-scans the module so the whole
bug class (any undefined name) is statically impossible to reintroduce,
WITHOUT importing module_ui (pygame/OpenGL are not installed on dev Macs).
"""
import ast
import builtins
import os
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MODULE_UI = os.path.join(_SRC, "modules", "module_ui.py")

# Exact names bound by the module's two star imports (OpenGL.GL / OpenGL.GLU
# at module top). Frozen by running the scan with an EMPTY set at HEAD
# 9e70e9d and taking (flagged set) - {"queue_message"}; every entry was
# verified against the real OpenGL exports. Exact-name (NOT pattern)
# exemption: a typo'd GL name still fails the scan.
_OPENGL_STAR_NAMES = frozenset({
    "GL_BLEND", "GL_COLOR_BUFFER_BIT", "GL_DEPTH_BUFFER_BIT", "GL_DEPTH_TEST",
    "GL_LINEAR", "GL_MODELVIEW", "GL_NO_ERROR", "GL_ONE_MINUS_SRC_ALPHA",
    "GL_PROJECTION", "GL_QUADS", "GL_RGBA", "GL_SRC_ALPHA", "GL_TEXTURE_2D",
    "GL_TEXTURE_MAG_FILTER", "GL_TEXTURE_MIN_FILTER", "GL_UNSIGNED_BYTE",
    "glBegin", "glBindTexture", "glBlendFunc", "glClear", "glClearColor",
    "glColor4f", "glDisable", "glEnable", "glEnd", "glGenTextures",
    "glGetError", "glLoadIdentity", "glMatrixMode", "glOrtho", "glPopMatrix",
    "glPushMatrix", "glTexImage2D", "glTexParameteri", "glTexCoord2f",
    "glVertex2f", "glViewport", "gluOrtho2D",
})

_BUILTINS = set(dir(builtins))


def _bound_names(tree):
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


def _loaded_names(tree):
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


class TestModuleUiImports(unittest.TestCase):
    def test_no_undefined_names_beyond_star_import_allowlist(self):
        with open(_MODULE_UI, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename="module_ui.py")
        undefined = (_loaded_names(tree) - _bound_names(tree)
                     - _BUILTINS - _OPENGL_STAR_NAMES)
        undefined = {n for n in undefined
                     if not (n.startswith("__") and n.endswith("__"))}
        self.assertEqual(
            set(), undefined,
            "module_ui.py references undefined names — an unimported name "
            "here is a latent UI-thread killer (G3)")

    def test_queue_message_is_bound_at_module_scope(self):
        # The generic scan unions bound names across ALL scopes, so a
        # function-local `from modules.module_messageQue import queue_message`
        # (or a local `queue_message = ...`) would still satisfy it while
        # leaving the module-scope Load at the overlay handler a live
        # NameError. Pin the specific name this guard exists to protect to
        # a top-level ImportFrom from modules.module_messageQue.
        with open(_MODULE_UI, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename="module_ui.py")
        for stmt in tree.body:
            if not isinstance(stmt, ast.ImportFrom):
                continue
            if stmt.module != "modules.module_messageQue":
                continue
            for alias in stmt.names:
                if (alias.asname or alias.name) == "queue_message":
                    return
        self.fail(
            "queue_message is not bound at module scope in module_ui.py — "
            "a function-local import cannot fix the overlay handler's "
            "NameError")
