"""Back-button cooldown must not be poisoned by misses or the launch tap (G7).

At HEAD, `handle_app_click` stamps `last_click_time` BEFORE the hit test,
on EVERY tap in app view — misses on the small 90x24 button and taps on
app content both consume the 400 ms window, and the app-LAUNCH tap
(stamped by `handle_click` on the same attribute) poisons the first
400 ms of app view. A deliberate corrective tap was silently dropped.

All tests fake the clock at base 1000, NEVER 0: with stamps initialized
to 0 and a clock also at 0, `0 - 0 < 400` is True and the FIRST event is
always dropped — at HEAD and post-fix alike (the 0-stamp trap).
"""
import ast
import os
import sys
import types
import unittest
from unittest import mock

_STUBBED_PYGAME = False
try:
    import pygame  # noqa: F401
except ImportError:
    # Install a stub ONLY when real pygame is unavailable, BEFORE importing
    # the module under test (it does `import pygame` at its top level);
    # popped in tearDownModule so it can never shadow the real package
    # (convention: test_llm_content_text.py:26-59).
    stub = types.ModuleType("pygame")
    stub.time = types.SimpleNamespace(get_ticks=lambda: 0)
    sys.modules["pygame"] = stub
    _STUBBED_PYGAME = True

from modules.UI import module_ui_terminal  # noqa: E402

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def tearDownModule():
    if _STUBBED_PYGAME:
        sys.modules.pop("pygame", None)


class _FakeRect:
    def __init__(self, x, y, w, h):
        self._x0, self._y0 = x, y
        self._x1, self._y1 = x + w, y + h

    def collidepoint(self, pos):
        px, py = pos
        return self._x0 <= px < self._x1 and self._y0 <= py < self._y1


def _terminal(**attrs):
    t = module_ui_terminal.TerminalSystem.__new__(
        module_ui_terminal.TerminalSystem)
    t.click_cooldown = 400
    t.last_click_time = 0
    t._back_click_time = 0
    t.back_button_rect = _FakeRect(0, 0, 90, 24)
    t.on_app_back = mock.MagicMock()
    for k, v in attrs.items():
        setattr(t, k, v)
    return t


def _clock(*ticks):
    """Patch pygame.time.get_ticks to return the given values in order.

    Post-fix, get_ticks is called ONLY on a back-button HIT, so a miss
    consumes no tick; at HEAD every call consumes one. Supply at least as
    many ticks as the HEAD path needs — leftover ticks are fine; a call
    beyond the list raises StopIteration and fails the test loudly."""
    return mock.patch.object(module_ui_terminal.pygame.time, "get_ticks",
                             side_effect=list(ticks))


class TestBackButtonDebounce(unittest.TestCase):
    def test_miss_then_hit_within_cooldown_still_fires(self):
        t = _terminal()
        with _clock(1000, 1100):
            self.assertFalse(t.handle_app_click((500, 500)))  # miss
            self.assertTrue(t.handle_app_click((45, 12)))      # hit, +100 ms
        t.on_app_back.assert_called_once()

    def test_launch_tap_does_not_poison_back_button(self):
        t = _terminal()
        t.last_click_time = 1000   # handle_click stamped the launch tap
        with _clock(1100):
            self.assertTrue(t.handle_app_click((45, 12)))     # +100 ms
        t.on_app_back.assert_called_once()

    def test_rapid_double_tap_on_back_button_debounced(self):
        t = _terminal()
        with _clock(1000, 1100):
            self.assertTrue(t.handle_app_click((45, 12)))
            self.assertFalse(t.handle_app_click((45, 12)))    # +100 ms
        t.on_app_back.assert_called_once()

    def test_none_back_rect_returns_false(self):
        t = _terminal(back_button_rect=None)
        with _clock(1000):
            self.assertFalse(t.handle_app_click((45, 12)))

    def test_none_on_app_back_returns_true(self):
        t = _terminal(on_app_back=None)
        with _clock(1000):
            self.assertTrue(t.handle_app_click((45, 12)))


class TestBackClickTimeInit(unittest.TestCase):
    def test_back_click_time_initialized_in_init(self):
        path = os.path.join(_REPO, "src", "modules", "UI",
                            "module_ui_terminal.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        init = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "TerminalSystem":
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef) and sub.name == "__init__":
                        init = sub
        self.assertIsNotNone(init, "TerminalSystem.__init__ not found")
        stamps = [n for n in ast.walk(init)
                  if isinstance(n, ast.Assign)
                  and len(n.targets) == 1
                  and isinstance(n.targets[0], ast.Attribute)
                  and n.targets[0].attr == "_back_click_time"
                  and isinstance(n.value, ast.Constant)
                  and n.value.value == 0]
        self.assertTrue(
            stamps,
            "__init__ must initialize self._back_click_time = 0 — the back "
            "button needs its own stamp, not the shared last_click_time")
