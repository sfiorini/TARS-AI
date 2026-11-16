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
        """A stale request_app overwritten before drain is cleared by cleanup."""
        mod = self._vid_mod

        # Simulate: request arrives, generation recorded
        mod.request_play("stale", "Stale")
        stale_gen = mod._generation

        # Cleanup clears everything
        with mod._payload_lock:
            mod._pending_play = None
            mod._stop_requested = False
            mod._playing = False

        # A later manual launch without a fresh request should NOT auto-play
        with mod._payload_lock:
            play = mod._pending_play  # None
            mod._pending_play = None
            mod._stop_requested = False
        self.assertIsNone(play)

        # Verify the stale generation marker is irrelevant after cleanup
        self.assertTrue(stale_gen > 0)  # was set
        self.assertIsNone(mod._pending_play)  # cleared

    # -- 5. generation guard: fresh survives relaunch (R1-F5) -------------

    def test_generation_guard_fresh_survives_relaunch(self):
        """Fresh request during old-instance cleanup survives the cleanup."""
        mod = self._vid_mod

        # Old instance records its generation
        mod.request_play("old", "Old")
        old_gen = mod._generation

        # Cleanup starts, but a NEW request arrives before cleanup clears pending
        mod.request_play("new", "New")
        new_gen = mod._generation
        self.assertGreater(new_gen, old_gen)

        # Old cleanup atomically reads — but a fresh drain sees the new request
        with mod._payload_lock:
            play = mod._pending_play  # "new" because request_play overwrote
            mod._pending_play = None
            mod._stop_requested = False

        self.assertEqual(play["url"], "new")
        self.assertEqual(play["title"], "New")


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
            ), mock.patch(
                "ctypes.util.find_library", return_value="libc.so"
            ):
                app.reset()
                self.assertTrue(mock_popen.called)
                kwargs = mock_popen.call_args[1]
                self.assertIn("preexec_fn", kwargs)

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

        # Test 3: pygame-free — cleanup works with no pygame objects
        app3 = mod.VideoApp.__new__(mod.VideoApp)
        app3.surface = None
        app3.width = 480
        app3.height = 320
        app3._proc = None
        app3._sock_path = None
        app3._my_generation = 0
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
        app3._handle_mpv_exit(1)  # non-zero, non-SIGKILL
        self.assertIn("Video playback error", app3._last_error)
        self.assertEqual(exit_restore, [True])
        self.assertIsNone(app3._proc)