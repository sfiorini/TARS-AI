"""M3-S2: sandbox_exec dunder-allowlist hardening (D21).

Remove __class__ from the allowlist — defense-in-depth. Tests are
VALIDATOR-LEVEL only: call _validate_ast on parsed candidates.
Payload code is NEVER executed.

NO pins on T-14 string-mediated vectors' CURRENT acceptance (that
would cement the hole).
"""

import ast
import unittest


# -- Helpers -----------------------------------------------------------

def _import_sb_module():
    """Import skill_sandbox_exec, clearing cached imports first."""
    import sys
    for key in list(sys.modules.keys()):
        if "skill_sandbox_exec" in key:
            del sys.modules[key]
    import src.skills.skill_sandbox_exec as sb
    return sb


# -- Tests -------------------------------------------------------------

class TestSandboxClassRemoval(unittest.TestCase):
    """S2 pin: __class__ rejected, __name__/__doc__ kept, existing
    defenses for getattr/ctypes/classic-chain preserved."""

    def setUp(self):
        self.sb = _import_sb_module()

    # ---- 1. literal __class__ access rejected -----------------------

    def test_literal_class_access_rejected(self):
        """().__class__ as AST → _SafetyError 'Forbidden dunder access: __class__'."""
        tree = ast.parse("().__class__")
        with self.assertRaises(self.sb._SafetyError) as ctx:
            self.sb._validate_ast(tree)
        self.assertIn("__class__", str(ctx.exception))

    # ---- 2. __name__ / __doc__ still allowed ------------------------

    def test_name_and_doc_still_allowed(self):
        """def f(): pass + f.__name__ / f.__doc__ validate clean."""
        code = "def f():\n    pass\n\nf.__name__\nf.__doc__"
        tree = ast.parse(code)
        try:
            self.sb._validate_ast(tree)
        except self.sb._SafetyError as e:
            self.fail(f"__name__/__doc__ should be allowed but got: {e}")

    # ---- 3. classic chain rejected ----------------------------------

    def test_classic_chain_rejected(self):
        """().__class__.__bases__[0].__subclasses__() → rejected
        (at __class__ now; __bases__/__subclasses__ were never allowed)."""
        code = "().__class__.__bases__[0].__subclasses__()"
        tree = ast.parse(code)
        with self.assertRaises(self.sb._SafetyError) as ctx:
            self.sb._validate_ast(tree)
        err = str(ctx.exception)
        self.assertTrue(
            "__class__" in err or "__subclasses__" in err,
            f"expected forbidden dunder in error, got: {err}",
        )

    # ---- 4. getattr rejected ----------------------------------------

    def test_getattr_rejected(self):
        """getattr((), '__class__') → rejected (_FORBIDDEN_NAMES)."""
        code = "getattr((), '__class__')"
        tree = ast.parse(code)
        with self.assertRaises(self.sb._SafetyError) as ctx:
            self.sb._validate_ast(tree)
        self.assertIn("getattr", str(ctx.exception))

    # ---- 5. ctypes import rejected ----------------------------------

    def test_ctypes_import_rejected(self):
        """import ctypes → rejected (_FORBIDDEN_MODULES)."""
        code = "import ctypes"
        tree = ast.parse(code)
        with self.assertRaises(self.sb._SafetyError) as ctx:
            self.sb._validate_ast(tree)
        self.assertIn("ctypes", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()