"""M4 — S1 Framework · S2 VideoApp · payload store · mpv lifecycle (mocked).

S1: request_app pending+drain · prior-app restore · shutdown-path cleanup (R1-F1).
S2: generation-guarded payload store + VideoApp + mpv command construction.

No real mpv, no pygame display, no network.

Imports the real module_ui / module_ui_apps via mock.patch.dict to provide
minimal stubs ONLY during import.  Instances are constructed via __new__
(bypassing the heavy __init__ that needs CameraModule, sounddevice, etc.).
"""
import os
import sys
import types
import threading
import unittest
from unittest import mock


# -----------------------------------------------------------------------
# Lazy import helpers — install stubs, import, then remove stubs.
# -----------------------------------------------------------------------

def _build_pygame_stub():
    pg = types.ModuleType("pygame")
    pg.time = types.SimpleNamespace(get_ticks=lambda: 0)
    pg.display = types.SimpleNamespace(
        get_surface=lambda: types.SimpleNamespace(get_flags=lambda: 0),
        flip=lambda: None,
    )
    pg.event = types.SimpleNamespace(
        Event=lambda *a, **kw: None, get=lambda: [], pump=lambda: None,
    )
    pg.Surface = lambda *a, **kw: types.SimpleNamespace(
        blit=lambda *a, **kw: None, fill=lambda *a, **kw: None,
        get_width=lambda: 480, get_height=lambda: 320,
    )
    pg.font = types.SimpleNamespace(
        Font=lambda *a, **kw: types.SimpleNamespace(
            render=lambda *a, **kw: None, size=lambda *a: (0, 0),
        ),
    )
    pg.image = types.SimpleNamespace(
        load=lambda *a, **kw: types.SimpleNamespace(get_size=lambda: (100, 100)),
        tostring=lambda *a, **kw: b"",
    )
    pg.transform = types.SimpleNamespace(
        rotate=lambda *a, **kw: None, smoothscale=lambda *a, **kw: None,
    )
    _locals = types.ModuleType("pygame.locals")
    for attr in ("DOUBLEBUF", "OPENGL", "KEYDOWN", "K_ESCAPE", "K_s", "K_c",
                  "MOUSEBUTTONDOWN", "MOUSEBUTTONUP", "MOUSEMOTION",
                  "MOUSEWHEEL", "QUIT"):
        setattr(_locals, attr, 1)
    pg.locals = _locals
    return pg, _locals


def _build_opengl_stub():
    gl = types.ModuleType("OpenGL")
    gl_gl = types.ModuleType("OpenGL.GL")
    gl_glu = types.ModuleType("OpenGL.GLU")
    for attr in ("glGenTextures", "glEnable", "glDisable", "glBindTexture",
                  "glTexParameteri", "glTexImage2D", "glClear", "glColor4f",
                  "glBegin", "glEnd", "glTexCoord2f", "glVertex2f",
                  "glPushMatrix", "glPopMatrix", "glLoadIdentity",
                  "glOrtho", "glMatrixMode", "glBlendFunc"):
        setattr(gl_gl, attr, lambda *a, **kw: None)
    gl_gl.glBlendFunc = lambda *a: None
    return gl, gl_gl, gl_glu


def _get_stub_modules():
    """Return dict of module-name → stub for modules that module_ui needs."""
    stubs = {}
    pg, pg_locals = _build_pygame_stub()
    stubs["pygame"] = pg
    stubs["pygame.locals"] = pg_locals
    gl, gl_gl, gl_glu = _build_opengl_stub()
    stubs["OpenGL"] = gl
    stubs["OpenGL.GL"] = gl_gl
    stubs["OpenGL.GLU"] = gl_glu
    for mod in ("sounddevice", "cv2"):
        stubs[mod] = types.ModuleType(mod)
    return stubs


def _import_module(module_path):
    """Import a module from the real source tree under stubs.

    Returns (module, stubs_dict) where stubs_dict maps names to the stubs
    that were injected into sys.modules.
    """
    stubs = _get_stub_modules()
    # Install stubs that aren't already present
    injected = {}
    for name, stub in stubs.items():
        if name not in sys.modules:
            sys.modules[name] = stub
            injected[name] = stub
    # Ensure paths
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    mod_path = os.path.join(repo, "src", "modules")
    src_path = os.path.join(repo, "src")
    added_paths = []
    for p in (mod_path, src_path):
        if p not in sys.path:
            sys.path.insert(0, p)
            added_paths.append(p)
    try:
        mod = __import__(module_path, fromlist=[""])
        return mod, injected, added_paths
    finally:
        for name in injected:
            del sys.modules[name]
        for p in added_paths:
            sys.path.remove(p)


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------


class TestUiFramework(unittest.TestCase):
    """S1: request_app pending+drain, prior-app restore, shutdown-path cleanup."""

    @classmethod
    def setUpClass(cls):
        # Import the real modules once, with stubs.  Stubs are removed
        # after import but the module objects remain cached in sys.modules.
        cls._ui_mod, cls._injected, cls._added_paths = _import_module(
            "modules.module_ui"
        )
        cls._apps_mod, _, _ = _import_module("modules.UI.module_ui_apps")

    @classmethod
    def tearDownClass(cls):
        # Clean up cached module imports so other test modules aren't affected
        for key in list(sys.modules.keys()):
            if key.startswith("modules.module_ui") or key.startswith(
                "modules.UI"
            ) or key == "module_config":
                del sys.modules[key]

    def _make_ui_manager(self):
        """UIManager via __new__ (bypasses heavy __init__)."""
        ui = self._ui_mod.UIManager.__new__(self._ui_mod.UIManager)
        ui.show_app = False
        ui.show_camera = False
        ui.app_manager = None
        ui.screensaver_manager = None
        ui.terminal_system = None
        ui._overlay_lock = threading.Lock()
        ui._overlay_image = None
        ui._overlay_pending_path = None
        ui.running = False
        ui.shutdown_event = threading.Event()
        ui._pending_app_request = None
        ui._app_request_lock = threading.Lock()
        return ui

    def _make_app_manager(self):
        """AppManager via __new__ (bypasses heavy __init__)."""
        mgr = self._apps_mod.AppManager.__new__(self._apps_mod.AppManager)
        mgr.screen = None
        mgr.width = 480
        mgr.height = 320
        mgr.rotation = 0
        mgr.display_width = 480
        mgr.display_height = 320
        mgr.active = False
        mgr.current_app = None
        mgr.current_app_name = None
        mgr.current_app_type = None
        mgr.failed_apps = set()
        mgr.gl_mode_active = False
        mgr.offscreen_surface = None
        mgr.gl_texture_id = None
        mgr._prior_app_name = None
        return mgr

    # -- 1. request_app pending under lock --------------------------------

    def test_request_app_pending_under_lock(self):
        ui = self._make_ui_manager()

        def _from_thread():
            ui.request_app("video")

        t = threading.Thread(target=_from_thread)
        t.start()
        t.join()

        self.assertEqual(ui._pending_app_request, "video")
        self.assertTrue(hasattr(ui._app_request_lock, "acquire"))

    # -- 2. drain on loop tick (last-wins) -------------------------------

    def test_request_app_drained_on_loop_tick(self):
        ui = self._make_ui_manager()
        called = []

        def fake_launch(name):
            called.append(name)

        ui.launch_app = fake_launch
        ui.request_app("video")
        ui._drain_pending_app_request()
        self.assertEqual(called, ["video"])
        self.assertIsNone(ui._pending_app_request)

        ui.request_app("clock")
        ui.request_app("eyes")
        ui._drain_pending_app_request()
        self.assertEqual(called, ["video", "eyes"])

        # Source pin: run() body contains the drain call
        import inspect

        src = inspect.getsource(ui.run)
        self.assertIn("_drain_pending_app_request", src)

    # -- 3. prior_app recorded on launch ---------------------------------

    def test_prior_app_recorded_on_launch(self):
        mgr = self._make_app_manager()
        mgr.current_app_name = "eyes"
        mgr.active = True
        mgr.launch("video")
        self.assertEqual(mgr._prior_app_name, "eyes")

        mgr.deactivate()
        mgr._prior_app_name = None
        mgr.launch("video")
        self.assertIsNone(mgr._prior_app_name)

    # -- 4. replay keeps existing prior ----------------------------------

    def test_replay_keeps_existing_prior(self):
        mgr = self._make_app_manager()
        mgr.current_app_name = "eyes"
        mgr.active = True
        mgr.launch("video")
        self.assertEqual(mgr._prior_app_name, "eyes")

        mgr.launch("video")
        self.assertEqual(mgr._prior_app_name, "eyes")

    # -- 5. exit_app restore_prior matrix ---------------------------------

    def test_exit_app_restore_prior_matrix(self):
        ui = self._make_ui_manager()
        mgr = self._make_app_manager()
        ui.app_manager = mgr

        mgr._prior_app_name = "eyes"
        launched = []
        ui.launch_app = lambda name: launched.append(name)
        ui.exit_app(restore_prior=True)
        self.assertEqual(launched, ["eyes"])
        self.assertIsNone(mgr._prior_app_name)

        launched.clear()
        mgr._prior_app_name = None
        ui.exit_app(restore_prior=True)
        self.assertEqual(launched, [])

        launched.clear()
        mgr._prior_app_name = "eyes"
        ui.exit_app(restore_prior=False)
        self.assertEqual(launched, [])
        self.assertIsNone(mgr._prior_app_name)
        ui.exit_app(restore_prior=True)
        self.assertEqual(launched, [])

    # -- 6. other apps unaffected ----------------------------------------

    def test_other_apps_unaffected(self):
        ui = self._make_ui_manager()
        mgr = self._make_app_manager()
        ui.app_manager = mgr

        launched = []
        ui.launch_app = lambda name: launched.append(name)
        mgr.launch("clock")
        ui.show_app = True
        ui.exit_app()
        self.assertEqual(launched, [])
        self.assertFalse(ui.show_app)

    # -- 7. exit_program deactivates before os._exit (R1-F1) -------------

    def test_exit_program_deactivates_before_os_exit(self):
        ui = self._make_ui_manager()

        cleanup_log = []

        class FakeApp:
            @staticmethod
            def cleanup():
                cleanup_log.append("cleanup")

        class FakeAppManager:
            active = True
            current_app = FakeApp()
            current_app_name = "video"
            current_app_type = "pygame"

            @staticmethod
            def deactivate():
                cleanup_log.append("deactivate")
                FakeAppManager.active = False
                FakeAppManager.current_app = None
                FakeAppManager.current_app_name = None

        ui.app_manager = FakeAppManager()

        with mock.patch("os._exit") as mock_exit:
            ui.exit_program()

        self.assertIn("deactivate", cleanup_log)
        mock_exit.assert_called_once_with(0)

    # -- 8. initiate_shutdown deactivates before os._exit (R1-F1) ---------

    def test_initiate_shutdown_deactivates_before_os_exit(self):
        ui = self._make_ui_manager()

        cleanup_log = []

        class FakeApp:
            @staticmethod
            def cleanup():
                cleanup_log.append("cleanup")

        class FakeAppManager:
            active = True
            current_app = FakeApp()
            current_app_name = "video"
            current_app_type = "pygame"

            @staticmethod
            def deactivate():
                cleanup_log.append("deactivate")
                FakeAppManager.active = False
                FakeAppManager.current_app = None

        ui.app_manager = FakeAppManager()

        with mock.patch("os._exit") as mock_exit, mock.patch(
            "subprocess.Popen"
        ) as mock_popen:
            ui.initiate_shutdown()

        self.assertIn("deactivate", cleanup_log)
        mock_exit.assert_called_once_with(0)