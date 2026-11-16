"""M4 — S1 Framework · S2 VideoApp · payload store · mpv lifecycle (mocked).

S1: request_app pending+drain · prior-app restore · shutdown-path cleanup (R1-F1).
S2: generation-guarded payload store + VideoApp + mpv command construction.

No real mpv, no pygame display, no network.

Imports the real module_ui / module_ui_apps via stubbed sys.modules to provide
minimal stubs ONLY during import.  Instances are constructed via __new__
(bypassing the heavy __init__ that needs CameraModule, sounddevice, etc.).
"""
import os
import sys
import types
import threading
import tempfile
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
        setattr(pg, attr, 1)  # real pygame re-exports constants
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
    # CI runners have no Pillow (local Mac does) — module_ui.py:30 and
    # module_ui_particles.py:23 do `from PIL import Image, ImageDraw,
    # ImageFilter` at import time. Stub only when absent (cv2 hotfix
    # precedent f26a6528: suite must be environment-independent).
    pil = types.ModuleType("PIL")
    pil.Image = mock.MagicMock()
    pil.ImageDraw = mock.MagicMock()
    pil.ImageFilter = mock.MagicMock()
    stubs["PIL"] = pil
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

    # -- 0b. QUIT guard during video transitions (T-28 bench defect) ------

    def test_quit_guard_video_active(self):
        """T-28: mpv open/close under labwc delivers spurious QUIT events
        that silently headless the appliance. While the video app is
        active, QUIT must be ignored."""
        ui = self._make_ui_manager()
        ui.app_manager = types.SimpleNamespace(current_app_name="video")
        ui._video_exit_ticks = None
        self.assertTrue(ui._quit_during_video_transition())

    def test_quit_guard_recent_video_exit(self):
        ui = self._make_ui_manager()
        ui.app_manager = types.SimpleNamespace(current_app_name=None)
        ui._video_exit_ticks = 1000
        mod = self._ui_mod
        with mock.patch.object(mod.pygame.time, "get_ticks",
                               return_value=2000):
            self.assertTrue(ui._quit_during_video_transition())
        with mock.patch.object(mod.pygame.time, "get_ticks",
                               return_value=5000):
            self.assertFalse(ui._quit_during_video_transition())

    def test_quit_guard_idle_false(self):
        ui = self._make_ui_manager()
        ui.app_manager = None
        ui._video_exit_ticks = None
        self.assertFalse(ui._quit_during_video_transition())

    def test_run_loop_quit_guard_and_event_logging_wired(self):
        """Source pins: run() consults the guard before honoring QUIT,
        logs ignored QUITs, and logs window events (T-28 diagnosis)."""
        import inspect
        src = inspect.getsource(self._ui_mod.UIManager.run)
        self.assertIn("_quit_during_video_transition()", src)
        self.assertIn("ignoring QUIT during video transition", src)
        self.assertIn("UI: window event", src)
        self.assertIn("UI: run loop exited", src)
        # IA benchfix M5/M6/M7 pin hardening: tick recording, QUIT log,
        # and the actual exit action must all stay wired.
        self.assertIn("_video_exit_ticks = pygame.time.get_ticks()", src)
        self.assertIn("UI: QUIT received", src)
        self.assertIn(
            'queue_message("UI: QUIT received — exiting run loop")\n'
            "                            self.running = False", src)

    # -- 0c. video-exit re-stack (T-28: labwc buries the surface) ---------

    def test_video_exit_restack_activates_own_window(self):
        """T-28 bench-proven: after mpv closes, the TARS X window is
        alive + VIEWABLE (_NET_WM_PID-verified) but buried under the
        desktop; SDL gets no events. XRaiseWindow/XSetInputFocus and
        fullscreen toggle were bench-DISPROVEN; the EWMH
        _NET_ACTIVE_WINDOW client message restored the UI live."""
        ui = self._make_ui_manager()
        mod = self._ui_mod
        ui._schedule_video_exit_restack()
        self.assertEqual(ui._restack_pending, 1)
        # pygame returns the Display* as a PyCapsule (bench-proven
        # TypeError with raw ints-only stubs) — use a REAL capsule.
        import ctypes
        pyapi = ctypes.pythonapi
        pyapi.PyCapsule_New.restype = ctypes.py_object
        pyapi.PyCapsule_New.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
        cap = pyapi.PyCapsule_New(ctypes.c_void_p(12345), None, None)
        disp = types.SimpleNamespace(
            get_wm_info=lambda: {"display": cap, "window": 67890})
        fake_x = mock.MagicMock()
        fake_x.XDefaultRootWindow.return_value = 1
        fake_x.XInternAtom.return_value = 999
        with mock.patch.object(mod.pygame, "display", disp, create=True), \
             mock.patch("ctypes.CDLL", return_value=fake_x):
            ui._apply_video_exit_restack()
            ui._apply_video_exit_restack()
        self.assertEqual(ui._restack_pending, 0)
        fake_x.XInternAtom.assert_called_once_with(
            12345, b"_NET_ACTIVE_WINDOW", 0)
        self.assertEqual(fake_x.XSendEvent.call_count, 1)
        args = fake_x.XSendEvent.call_args[0]
        self.assertEqual(args[0], 12345)      # display
        self.assertEqual(args[1], 1)          # root window
        self.assertEqual(args[3], (1 << 19) | (1 << 20))
        self.assertTrue(fake_x.XFlush.called)

    def test_run_loop_restack_and_driver_log_wired(self):
        import inspect
        src = inspect.getsource(self._ui_mod.UIManager.run)
        self.assertIn("_schedule_video_exit_restack()", src)
        self.assertIn("_apply_video_exit_restack()", src)
        self.assertIn("SDL driver=", src)

    # -- 1. request_app pending under lock --------------------------------

    def test_request_app_pending_under_lock(self):
        ui = self._make_ui_manager()

        # Verify request_app USES the lock, not just that the attribute
        # exists (plan-acknowledged behavioral race isn't
        # deterministically testable; the lock-acquisition pin carries it).
        with mock.patch.object(
            ui, "_app_request_lock", wraps=ui._app_request_lock
        ) as mock_lock:

            def _from_thread():
                ui.request_app("video")

            t = threading.Thread(target=_from_thread)
            t.start()
            t.join()

            self.assertEqual(ui._pending_app_request, "video")
            mock_lock.__enter__.assert_called()

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
        # ESC arm uses the REAL call form — exit_app() with NO argument,
        # exactly as the ESC site in module_ui.py calls it. This pins the
        # restore_prior=False DEFAULT: flipping the default would regress
        # ESC to prior-restoration (orchestrator close-sweep hardening).
        ui.exit_app()
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

        # ORDER pin (audit IB-F3/IA-F3): deactivate must precede os._exit —
        # presence-only let a reorder survive.
        with mock.patch("os._exit",
                        side_effect=lambda *a: cleanup_log.append("os_exit")
                        ) as mock_exit:
            ui.exit_program()

        self.assertIn("deactivate", cleanup_log)
        mock_exit.assert_called_once_with(0)
        self.assertLess(cleanup_log.index("deactivate"),
                        cleanup_log.index("os_exit"))

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

        with mock.patch("os._exit",
                        side_effect=lambda *a: cleanup_log.append("os_exit")
                        ) as mock_exit, mock.patch(
            "subprocess.Popen"
        ) as mock_popen:
            ui.initiate_shutdown()

        self.assertIn("deactivate", cleanup_log)
        mock_exit.assert_called_once_with(0)
        self.assertLess(cleanup_log.index("deactivate"),
                        cleanup_log.index("os_exit"))

    # -- 8b. run loop wires the video exit-drain (audit IB-F4) ------------

    def test_run_loop_wires_exit_drain(self):
        """Source pin: run() must CALL drain_exit_request — removing the
        whole exit-drain block previously survived the suite. Pin the
        exact call line (the import alone would satisfy a name needle)."""
        import inspect
        src = inspect.getsource(self._ui_mod.UIManager.run)
        self.assertIn("exit_now, restore = drain_exit_request()", src)
        self.assertIn("exit_app", src)


# -----------------------------------------------------------------------
# S2: Payload store import helper
# -----------------------------------------------------------------------

def _import_payload_store():
    """Import module_app_video's payload store under pygame stub.

    Returns the module so callers can access the module-level globals
    (_payload_lock, _pending_play, _stop_requested, _playing, _generation,
    _should_exit, _restore_prior) and functions.

    Saves and restores sys.modules state for pygame, module_messageQue, and
    module_app_video so other test files aren't affected.
    """
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    src = os.path.join(repo, "src")

    # Save state we'll overwrite
    saved = {}
    for key in ("pygame", "pygame.locals", "modules.module_messageQue",
                 "modules.UI.apps.module_app_video"):
        saved[key] = sys.modules.get(key)

    try:
        # Install pygame stub
        pg, pg_locals = _build_pygame_stub()
        sys.modules["pygame"] = pg
        sys.modules["pygame.locals"] = pg_locals

        # Ensure package hierarchy
        for pkg_name in ("modules", "modules.UI", "modules.UI.apps"):
            if pkg_name not in sys.modules:
                m = types.ModuleType(pkg_name)
                m.__path__ = [os.path.join(src, *pkg_name.split("."))]
                sys.modules[pkg_name] = m
                saved[f"_pkg_{pkg_name}"] = None  # mark for cleanup

        # Stub module_messageQue
        mq = types.ModuleType("modules.module_messageQue")
        mq.queue_message = lambda *a, **kw: None
        sys.modules["modules.module_messageQue"] = mq

        # Force re-import of module_app_video under current stubs
        sys.modules.pop("modules.UI.apps.module_app_video", None)

        if src not in sys.path:
            sys.path.insert(0, src)
            src_added = True
        else:
            src_added = False

        mod = __import__(
            "modules.UI.apps.module_app_video", fromlist=[""]
        )
        return mod, src
    finally:
        if src_added:
            sys.path.remove(src)
        # Restore saved state
        for key, orig in saved.items():
            if key.startswith("_pkg_"):
                pkg = key[len("_pkg_"):]
                if orig is None:
                    sys.modules.pop(pkg, None)
            elif orig is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = orig


def _reset_payload_globals(mod):
    """Reset module-level payload globals between tests."""
    with mod._payload_lock:
        mod._pending_play = None
        mod._stop_requested = False
        mod._playing = False
        mod._generation = 0
        mod._should_exit = False
        mod._restore_prior = True


# -----------------------------------------------------------------------
# S2: TestPayloadStore
# -----------------------------------------------------------------------


class TestPayloadStore(unittest.TestCase):
    """S2: generation-guarded payload store thread safety and semantics."""

    @classmethod
    def setUpClass(cls):
        cls._vid_mod, cls._src = _import_payload_store()

    def setUp(self):
        _reset_payload_globals(self._vid_mod)

    # -- 1. request_play drains atomically ---------------------------------

    def test_request_play_drain_atomic(self):
        """Thread writes url/title; reset drains atomically with generation."""
        mod = self._vid_mod

        # Write from a thread
        written = []

        def _writer():
            mod.request_play("https://example.com/video.mp4", "Test Video")
            written.append(True)

        t = threading.Thread(target=_writer)
        t.start()
        t.join()

        self.assertTrue(written)
        with mod._payload_lock:
            self.assertEqual(
                mod._pending_play["url"], "https://example.com/video.mp4"
            )
            self.assertEqual(mod._pending_play["title"], "Test Video")
            gen_after_write = mod._generation
            self.assertGreater(gen_after_write, 0)

        # Drain atomically by simulating reset()'s drain
        with mod._payload_lock:
            play = mod._pending_play
            mod._pending_play = None
            mod._stop_requested = False
            my_gen = mod._generation

        self.assertEqual(play["url"], "https://example.com/video.mp4")
        self.assertEqual(play["title"], "Test Video")
        self.assertEqual(my_gen, gen_after_write)
        with mod._payload_lock:
            self.assertIsNone(mod._pending_play)

    # -- 2. stop flag and is_playing --------------------------------------

    def test_stop_flag_and_is_playing(self):
        mod = self._vid_mod

        self.assertFalse(mod.is_playing())
        mod.request_play("url", "")
        self.assertFalse(mod.is_playing())  # not yet drained

        with mod._payload_lock:
            mod._playing = True
        self.assertTrue(mod.is_playing())

        mod.request_stop()
        with mod._payload_lock:
            self.assertTrue(mod._stop_requested)
            self.assertTrue(mod._playing)  # stop doesn't clear playing

    # -- 3. last-wins overwrite before drain -----------------------------

    def test_last_wins_overwrite(self):
        mod = self._vid_mod

        mod.request_play("first", "First")
        gen1 = mod._generation
        mod.request_play("second", "Second")
        gen2 = mod._generation
        self.assertEqual(gen2, gen1 + 1)

        with mod._payload_lock:
            self.assertEqual(mod._pending_play["url"], "second")
            self.assertEqual(mod._pending_play["title"], "Second")
            self.assertEqual(mod._generation, gen2)

    # -- 4. generation guard: stale request cleared (R1-F5) ---------------

    def test_generation_guard_stale_cleared(self):
        """R1-F5 pin, BEHAVIORAL (orchestrator rewrite — the delivered test
        simulated the store inline and never called cleanup/reset): an
        instance that drained generation N cleans up with NO newer request
        → residual payload/stop cleared; a later manual reset() does NOT
        auto-play. Kill: cleanup that never clears fails this test."""
        mod = self._vid_mod
        app = mod.VideoApp.__new__(mod.VideoApp)  # bypass pygame __init__
        app._proc = None
        app._sock_path = None
        app._my_generation = 0
        app._title = ""
        app._last_error = ""

        # Instance drains a real request (gen N) and "plays" it.
        mod.request_play("first", "First")
        with mock.patch.object(app, "_spawn_mpv") as m_spawn:
            app.reset()
        m_spawn.assert_called_once_with("first")
        self.assertEqual(app._my_generation, mod._generation)

        # Residual stop flag at the SAME generation is cleared by cleanup.
        mod.request_stop()
        app.cleanup()
        self.assertFalse(mod._stop_requested)
        self.assertIsNone(mod._pending_play)
        self.assertFalse(mod.is_playing())

        # Later MANUAL launch (no fresh request): reset() must NOT auto-play.
        app2 = mod.VideoApp.__new__(mod.VideoApp)
        app2._proc = None
        app2._sock_path = None
        app2._my_generation = 0
        app2._title = ""
        app2._last_error = ""
        with mock.patch.object(app2, "_spawn_mpv") as m_spawn2:
            app2.reset()
        m_spawn2.assert_not_called()

    # -- 5. generation guard: fresh survives relaunch (R1-F5) -------------

    def test_generation_guard_fresh_survives_relaunch(self):
        """R1-F5 pin, BEHAVIORAL (orchestrator rewrite — the delivered test
        simulated the store inline): a fresh request (newer generation)
        during relaunch SURVIVES the old instance's cleanup (launch cleans
        BEFORE construct); the old instance's update() quits mpv for the
        replay; the NEW instance's reset() drains and plays it.
        Kills: unguarded cleanup (clears fresh replays) AND removing the
        generation check from update() both fail this test."""
        mod = self._vid_mod
        old = mod.VideoApp.__new__(mod.VideoApp)
        old._proc = mock.MagicMock()
        old._proc.poll.return_value = None  # still "playing"
        old._sock_path = None
        old._my_generation = 0
        old._title = ""
        old._last_error = ""

        # Old instance drains gen N.
        mod.request_play("old", "Old")
        with mock.patch.object(old, "_spawn_mpv"):
            old.reset()
        old_gen = old._my_generation

        # Fresh request arrives (gen N+1) — the relaunch scenario.
        mod.request_play("new", "New")
        self.assertGreater(mod._generation, old_gen)

        # Old instance's update() must quit mpv for the newer-gen replay
        # (generation-check pin — removing the check fails here).
        with mock.patch.object(old, "_send_ipc_quit") as m_quit:
            old.update()
        m_quit.assert_called_once()

        # Old cleanup must NOT clear the newer-generation payload.
        old.cleanup()
        self.assertIsNotNone(mod._pending_play)
        self.assertEqual(mod._pending_play["url"], "new")

        # New instance drains and plays it.
        new = mod.VideoApp.__new__(mod.VideoApp)
        new._proc = None
        new._sock_path = None
        new._my_generation = 0
        new._title = ""
        new._last_error = ""
        with mock.patch.object(new, "_spawn_mpv") as m_spawn:
            new.reset()
        m_spawn.assert_called_once_with("new")

    # -- 6. reset clears a stale exit request (audit IB-F8) ---------------

    def test_reset_clears_stale_exit_request(self):
        """An EOF-exit request left by a prior instance must not kill a
        freshly relaunched video (same-tick race)."""
        mod = self._vid_mod
        with mod._payload_lock:
            mod._should_exit = True
            mod._restore_prior = False
        app = mod.VideoApp.__new__(mod.VideoApp)
        app._proc = None
        app._sock_path = None
        app._input_conf_path = None
        app._my_generation = 0
        app._title = ""
        app._last_error = ""
        mod.request_play("fresh", "Fresh")
        with mock.patch.object(app, "_spawn_mpv"):
            app.reset()
        should_exit, restore = mod.drain_exit_request()
        self.assertFalse(should_exit)
        self.assertTrue(restore)

    # -- 7. manual launch without payload is NOT playing (audit IA-F5) ----

    def test_manual_launch_not_playing(self):
        """reset() with no payload must leave _playing False — stop says
        'No video is playing.', not 'Stopped the video.'"""
        mod = self._vid_mod
        app = mod.VideoApp.__new__(mod.VideoApp)
        app._proc = None
        app._sock_path = None
        app._input_conf_path = None
        app._my_generation = 0
        app._title = ""
        app._last_error = ""
        with mock.patch.object(app, "_spawn_mpv") as m_spawn:
            app.reset()
        m_spawn.assert_not_called()
        self.assertFalse(mod.is_playing())

    def test_manual_launch_idle_screen_and_tap_exit(self):
        """Bench defect 2026-11-16: manual launch showed 'Loading...'
        forever and tap was a no-op (no mpv → _send_ipc_quit early
        return) — a dead-end black screen. The idle render must give a
        helpful hint, and a tap with no mpv must exit back to the prior
        app."""
        mod = self._vid_mod
        app = mod.VideoApp.__new__(mod.VideoApp)
        app._proc = None
        app._sock_path = None
        app._input_conf_path = None
        app._my_generation = 0
        app._title = ""
        app._last_error = ""
        app._font = mock.MagicMock()
        app.surface = mock.MagicMock()
        with mock.patch.object(app, "_spawn_mpv"):
            app.reset()
        app.render()
        rendered = app._font.render.call_args[0][0]
        self.assertNotEqual(rendered, "Loading...")
        self.assertIn("YouTube", rendered)
        evt = mock.MagicMock()
        evt.type = mod.pygame.MOUSEBUTTONDOWN
        self.assertTrue(app.handle_event(evt))
        should_exit, restore = mod.drain_exit_request()
        self.assertTrue(should_exit)
        self.assertTrue(restore)


# -----------------------------------------------------------------------
# S2: TestVideoApp
# -----------------------------------------------------------------------


class TestVideoApp(unittest.TestCase):
    """S2: VideoApp — mpv command construction, lifecycle, cleanup."""

    @classmethod
    def setUpClass(cls):
        cls._vid_mod, cls._src = _import_payload_store()

    def setUp(self):
        _reset_payload_globals(self._vid_mod)

    def _make_surface(self):
        """Minimal pygame surface stub for VideoApp construction."""
        surf = types.SimpleNamespace()
        surf.blit = lambda *a, **kw: None
        surf.fill = lambda *a, **kw: None
        surf.get_width = lambda: 480
        surf.get_height = lambda: 320
        return surf

    # -- 6. mpv command construction (D14 verbatim) -----------------------

    def test_mpv_command_construction(self):
        """All D14 flags verbatim: fullscreen, avc1/720p policy, IPC socket,
        tap-to-stop input-conf (MBTN_LEFT quit), URL last, handle_event pin."""
        mod = self._vid_mod

        with mock.patch("subprocess.Popen") as mock_popen, mock.patch(
            "os.unlink"
        ) as mock_unlink:
            mock_popen.return_value.poll.return_value = None
            mock_popen.return_value.wait.return_value = None

            mod.request_play("https://youtube.com/watch?v=test", "Test")
            app = mod.VideoApp(self._make_surface(), 480, 320)
            app.reset()

            self.assertTrue(mock_popen.called)
            argv = mock_popen.call_args[0][0]

            # Mandatory D14 flags
            self.assertIn("--fullscreen", argv)
            self.assertIn("--force-window=immediate", argv)
            self.assertIn("--really-quiet", argv)

            # IPC socket
            ipc_arg = [a for a in argv if a.startswith("--input-ipc-server=")]
            self.assertEqual(len(ipc_arg), 1)
            self.assertIn("tars_mpv_", ipc_arg[0])
            self.assertIn(".sock", ipc_arg[0])

            # Format policy — vcodec^=avc1 and height<=720
            fmt_arg = [a for a in argv if a.startswith("--ytdl-format=")]
            self.assertEqual(len(fmt_arg), 1)
            self.assertIn("vcodec^=avc1", fmt_arg[0])
            self.assertIn("height<=720", fmt_arg[0])

            # Input conf (tap-to-stop)
            conf_arg = [a for a in argv if a.startswith("--input-conf=")]
            self.assertEqual(len(conf_arg), 1)

            # URL is the last positional
            self.assertEqual(argv[-1], "https://youtube.com/watch?v=test")

            # Verify input-conf file content: MBTN_LEFT quit
            conf_path = conf_arg[0].split("=", 1)[1]
            with open(conf_path) as f:
                conf_content = f.read()
            self.assertIn("MBTN_LEFT quit", conf_content)
            os.unlink(conf_path)

            # Tap-to-stop: handle_event on mouse event sends IPC quit
            quit_calls = []
            app._send_ipc_quit = lambda: quit_calls.append(True)
            event = types.SimpleNamespace(type=1)  # MOUSEBUTTONDOWN
            result = app.handle_event(event)
            self.assertTrue(result)
            self.assertEqual(quit_calls, [True])

    # -- 6b. ALSA audio device (bench defect 2026-11-16) ------------------

    def test_usb_alsa_card_parser(self):
        """Parse the first USB-Audio card from /proc/asound/cards text."""
        mod = self._vid_mod
        cards = (
            " 0 [vc4hdmi0       ]: vc4_hdmi - vc4-hdmi-0\n"
            "                      MAI PCM i2s-hifi-0\n"
            " 1 [vc4hdmi1       ]: vc4_hdmi - vc4-hdmi-1\n"
            "                      MAI PCM i2s-hifi-0\n"
            " 2 [Device         ]: USB-Audio - USB PnP Audio Device\n"
            "                      USB PnP Audio Device at usb-0000:01:00.0-1.2\n"
        )
        self.assertEqual(mod._usb_alsa_card_number(cards), 2)
        self.assertIsNone(mod._usb_alsa_card_number(
            " 0 [vc4hdmi0       ]: vc4_hdmi - vc4-hdmi-0\n"))
        self.assertIsNone(mod._usb_alsa_card_number(""))

    def test_spawn_uses_alsa_device_on_linux_usb(self):
        """Bench defect: mpv's pipewire ao stalls against the ALSA-held
        USB card (audio-pts frozen -> static frame, robot-proven; live
        IPC switch to alsa/plughw:2,0 unfroze playback). On Linux with a
        USB card, spawn must pass --audio-device=alsa/plughw:<n>,0."""
        mod = self._vid_mod
        with mock.patch("subprocess.Popen") as mock_popen, \
             mock.patch.object(mod, "_alsa_playback_device",
                               return_value="alsa/plughw:2,0"), \
             mock.patch("ctypes.CDLL"), \
             mock.patch("ctypes.util.find_library", return_value="libc.so"):
            mock_popen.return_value.poll.return_value = None
            mod.request_play("url", "")
            app = mod.VideoApp(self._make_surface(), 480, 320)
            with mock.patch("sys.platform", "linux"):
                app.reset()
            argv = mock_popen.call_args[0][0]
            self.assertIn("--audio-device=alsa/plughw:2,0", argv)
            self.assertLess(argv.index("--audio-device=alsa/plughw:2,0"),
                            argv.index("url"))
            app.cleanup()

    def test_spawn_no_audio_device_flag_without_usb(self):
        """No USB card (or non-Linux) -> no --audio-device flag; mpv
        keeps its default ao choice."""
        mod = self._vid_mod
        with mock.patch("subprocess.Popen") as mock_popen, \
             mock.patch.object(mod, "_alsa_playback_device",
                               return_value=None), \
             mock.patch("ctypes.CDLL"), \
             mock.patch("ctypes.util.find_library", return_value="libc.so"):
            mock_popen.return_value.poll.return_value = None
            mod.request_play("url", "")
            app = mod.VideoApp(self._make_surface(), 480, 320)
            with mock.patch("sys.platform", "linux"):
                app.reset()
            argv = mock_popen.call_args[0][0]
            self.assertFalse(
                [a for a in argv if a.startswith("--audio-device=")])
            app.cleanup()

    # -- 7. PDEATHSIG preexec Linux-guarded -------------------------------

    def test_pdeathsig_preexec_linux_guarded(self):
        """PDEATHSIG preexec_fn only on Linux; skipped otherwise."""
        mod = self._vid_mod

        with mock.patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            mock_popen.return_value.wait.return_value = None

            mod.request_play("url", "")
            app = mod.VideoApp(self._make_surface(), 480, 320)

            with mock.patch("sys.platform", "linux"), mock.patch(
                "ctypes.CDLL"
            ) as mock_cdll, mock.patch(
                "ctypes.util.find_library", return_value="libc.so"
            ):
                app.reset()
                self.assertTrue(mock_popen.called)
                kwargs = mock_popen.call_args[1]
                self.assertIn("preexec_fn", kwargs)
                # Behavioral pin (close-sweep S2-3 hardening): the preexec
                # must ACTUALLY set PR_SET_PDEATHSIG=1 to SIGTERM=15 —
                # presence-only let a dropped prctl call survive.
                kwargs["preexec_fn"]()
                mock_cdll.return_value.prctl.assert_called_once_with(1, 15)

            # On non-Linux, no preexec_fn — fresh request needed since
            # the payload was drained by the first reset()
            mock_popen.reset_mock()
            mod.request_play("url2", "")
            app2 = mod.VideoApp(self._make_surface(), 480, 320)
            with mock.patch("sys.platform", "darwin"):
                app2.reset()
                self.assertTrue(mock_popen.called)
                kwargs2 = mock_popen.call_args[1]
                self.assertNotIn("preexec_fn", kwargs2)

    # -- 8. cleanup kills proc, unlinks socket, pygame-free ---------------

    def test_cleanup_kills_proc_unlinks_sock(self):
        """Cleanup: IPC quit → wait → terminate/kill escalation → unlink;
        also callable with NO pygame objects (thread-safe)."""
        mod = self._vid_mod

        # Test 1: normal cleanup path with wait
        sock_dir = tempfile.mkdtemp()
        sock_path = os.path.join(sock_dir, "test.sock")
        with open(sock_path, "w") as f:
            f.write("")

        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.wait.return_value = None
            mock_popen.return_value = mock_proc

            mod.request_play("url", "")
            app = mod.VideoApp(self._make_surface(), 480, 320)
            app._sock_path = sock_path
            app._proc = mock_proc

            cleanup_log = []

            def fake_send_quit():
                cleanup_log.append("ipc_quit")

            app._send_ipc_quit = fake_send_quit
            app.cleanup()

            self.assertIn("ipc_quit", cleanup_log)
            mock_proc.wait.assert_called_with(timeout=2)
            # Socket UNLINK pin (close-sweep S2-6 hardening — the test
            # name promised it but never asserted it).
            self.assertFalse(os.path.exists(sock_path))

        # Test 2: terminate/kill escalation
        sock_path2 = os.path.join(sock_dir, "test2.sock")
        with open(sock_path2, "w") as f:
            f.write("")
        subp = __import__("subprocess")

        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc2 = mock.MagicMock()
            mock_proc2.poll.return_value = None
            mock_proc2.wait.side_effect = [
                subp.TimeoutExpired("cmd", 2),
                subp.TimeoutExpired("cmd", 1),
                None,
            ]
            mock_popen.return_value = mock_proc2

            app2 = mod.VideoApp(self._make_surface(), 480, 320)
            app2._sock_path = sock_path2
            app2._proc = mock_proc2
            app2._send_ipc_quit = lambda: None
            app2.cleanup()

            mock_proc2.terminate.assert_called_once()
            mock_proc2.kill.assert_called_once()
            self.assertFalse(os.path.exists(sock_path2))

        # Test 3: pygame-free — cleanup works with no pygame objects.
        # The instance matches the CURRENT generation (it "drained" the
        # requests above), so the D15 guard clears the residual payload.
        app3 = mod.VideoApp.__new__(mod.VideoApp)
        app3.surface = None
        app3.width = 480
        app3.height = 320
        app3._proc = None
        app3._sock_path = None
        app3._my_generation = mod._generation
        app3._title = ""
        app3._last_error = ""
        app3._font = None
        app3.cleanup()

        with mod._payload_lock:
            self.assertFalse(mod._playing)
            self.assertIsNone(mod._pending_play)
            self.assertFalse(mod._stop_requested)

    # -- 9. stale socket unlinked before spawn ---------------------------

    def test_stale_socket_unlinked_before_spawn(self):
        """Pre-existing pid-unique socket is unlinked before Popen."""
        mod = self._vid_mod

        pid_sock = os.path.join(
            tempfile.gettempdir(), f"tars_mpv_{os.getpid()}.sock"
        )
        with open(pid_sock, "w") as f:
            f.write("stale")
        self.assertTrue(os.path.exists(pid_sock))

        with mock.patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            mock_popen.return_value.wait.return_value = None

            mod.request_play("url", "")
            app = mod.VideoApp(self._make_surface(), 480, 320)
            app.reset()

            self.assertFalse(os.path.exists(pid_sock))

    # -- 10. update polls proc, drains stop/replay/end-file/crash ---------

    def test_update_polls_and_drains_ipc(self):
        """update(): poll per frame; stop → IPC quit; replay → IPC quit;
        proc crash → error + exit request with restore_prior=True."""
        mod = self._vid_mod

        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            mod.request_play("url", "Test Title")
            app = mod.VideoApp(self._make_surface(), 480, 320)
            app.reset()

            # Running — poll returns None
            app.update()
            self.assertIsNotNone(app._proc)

            # Stop flag → IPC quit
            quit_calls = []
            app._send_ipc_quit = lambda: quit_calls.append(True)
            mod.request_stop()
            app.update()
            self.assertEqual(quit_calls, [True])

        # New instance: replay with newer generation
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_proc2 = mock.MagicMock()
            mock_proc2.poll.return_value = None
            mock_popen.return_value = mock_proc2

            mod.request_play("url1", "Old")
            app2 = mod.VideoApp(self._make_surface(), 480, 320)
            app2.reset()

            quit_calls2 = []
            app2._send_ipc_quit = lambda: quit_calls2.append(True)
            mod.request_play("url2", "New")
            app2.update()
            self.assertEqual(quit_calls2, [True])

        # Proc crash → error + exit request with restore=True
        exit_restore = []
        app3 = mod.VideoApp.__new__(mod.VideoApp)
        app3.surface = self._make_surface()
        app3.width = 480
        app3.height = 320
        app3._proc = None
        app3._sock_path = None
        app3._my_generation = 0
        app3._title = ""
        app3._last_error = ""
        app3._font = type(
            "Font", (), {"render": lambda *a, **kw: types.SimpleNamespace()},
        )()
        app3._request_exit = lambda restore=True: exit_restore.append(restore)
        with mock.patch.object(mod, "_speak_in_background"):
            app3._handle_mpv_exit(1)  # non-zero, non-SIGKILL
        self.assertIn("Video playback error", app3._last_error)
        self.assertEqual(exit_restore, [True])
        self.assertIsNone(app3._proc)

    # -- 11. input-conf survives spawn, unlinked at cleanup (IB-F1/IA-F2) -

    def test_input_conf_survives_spawn_unlinked_at_cleanup(self):
        """The tap-to-stop conf must EXIST after spawn (mpv reads it after
        exec — the delivered code unlinked it immediately, killing the
        affordance) and be gone only after cleanup()."""
        mod = self._vid_mod
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = None
            mock_popen.return_value.wait.return_value = None
            mod.request_play("url", "")
            app = mod.VideoApp(self._make_surface(), 480, 320)
            app.reset()
            conf = app._input_conf_path
            self.assertIsNotNone(conf)
            self.assertTrue(os.path.exists(conf),
                            "input-conf deleted before mpv could read it")
            with open(conf) as f:
                self.assertIn("MBTN_LEFT quit", f.read())
            app.cleanup()
            self.assertFalse(os.path.exists(conf))
        # Source-order pin: no immediate unlink of the conf in _spawn_mpv.
        import inspect
        spawn_src = inspect.getsource(mod.VideoApp._spawn_mpv)
        self.assertNotIn("os.unlink(input_conf)", spawn_src)

    # -- 12. failure speaks the error line; EOF/stop silent (IB-F2/IA-F1) -

    def test_failure_speaks_error_line_eof_silent(self):
        """D14: a playback FAILURE is spoken via background TTS; EOF (0)
        and user-stop (-9) stay silent."""
        mod = self._vid_mod
        app = mod.VideoApp.__new__(mod.VideoApp)
        app._proc = None
        app._sock_path = None
        app._input_conf_path = None
        app._my_generation = 0
        app._title = ""
        app._last_error = ""
        with mock.patch.object(mod, "_speak_in_background") as m_speak:
            app._handle_mpv_exit(2)
            m_speak.assert_called_once_with("Video playback error (code 2)")
            m_speak.reset_mock()
            app._handle_mpv_exit(0)   # EOF
            m_speak.assert_not_called()
            app._handle_mpv_exit(-9)  # SIGKILL from stop
            m_speak.assert_not_called()
        # The speaker uses the eyes precedent (background thread + TTS).
        import inspect
        speak_src = inspect.getsource(mod._speak_in_background)
        self.assertIn("play_audio_chunks", speak_src)
        self.assertIn("Thread", speak_src)

    # -- 13. IPC quit bytes + end-file drain, real AF_UNIX (IB-F5/IA-F4) --

    def test_ipc_quit_bytes_and_end_file_drain(self):
        """Drive the REAL _send_ipc_quit/_drain_ipc against a fake mpv IPC
        server: exact quit payload bytes; end-file event triggers quit."""
        import socket as sk
        import time
        mod = self._vid_mod
        sock_dir = tempfile.mkdtemp()
        path = os.path.join(sock_dir, "ipc.sock")
        srv = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
        srv.bind(path)
        srv.listen(2)
        srv.settimeout(3)
        app = mod.VideoApp.__new__(mod.VideoApp)
        app._proc = None
        app._sock_path = path
        app._input_conf_path = None
        app._my_generation = 0
        app._title = ""
        app._last_error = ""

        # Real mpv queues events ASYNCHRONOUSLY (data is buffered before
        # the drain connects). A fake server can only send after accept,
        # which the drain's non-blocking recv would outrace — so the
        # socket wrapper yields 50ms after connect, letting the server
        # emit first (models the async event stream deterministically).
        real_socket = sk.socket

        class EventStreamSocket:
            def __init__(self, *a, **kw):
                self._s = real_socket(*a, **kw)

            def connect(self, addr):
                self._s.connect(addr)
                time.sleep(0.05)

            def __getattr__(self, name):
                return getattr(self._s, name)

        try:
            # 1) exact quit payload
            with mock.patch("socket.socket", EventStreamSocket):
                app._send_ipc_quit()
            conn, _ = srv.accept()
            data = conn.recv(4096)
            conn.close()
            self.assertEqual(data, b'{"command":["quit"]}\n')

            # 2) end-file event → quit. The server emits the event on
            # EVERY connection (like mpv's async event stream); the drain
            # is polled per-frame as in the real run loop — a single
            # non-blocking recv can miss data that arrives mid-frame.
            received = []

            def server():
                while True:
                    try:
                        c, _ = srv.accept()
                    except OSError:
                        return
                    data = b""
                    try:
                        try:
                            c.sendall(b'{"event":"end-file","reason":"eof"}\n')
                        except OSError:
                            pass  # drain may close before we send
                        c.settimeout(0.5)
                        try:
                            data = c.recv(4096)
                        except OSError:
                            data = b""
                    finally:
                        c.close()
                    if data:
                        received.append(data)
                        return

            t = threading.Thread(target=server, daemon=True)
            t.start()
            deadline = time.time() + 2
            with mock.patch("socket.socket", EventStreamSocket):
                while not received and time.time() < deadline:
                    app._drain_ipc()
                    time.sleep(0.05)
            t.join(timeout=1)
            self.assertEqual(received, [b'{"command":["quit"]}\n'])
        finally:
            srv.close()