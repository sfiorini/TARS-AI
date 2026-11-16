"""M4 - S3 skill_video / browser removal / thinking surface / STT/UI ban pins."""
import os
import sys
import types
import unittest
from unittest import mock

# Module-level saved state for restore after test class
_saved_modules = {}


def _ensure_modules():
    """Set up package hierarchy and stub modules for test isolation.
    Saves original state for restoration in tearDownClass."""
    global _saved_modules
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    src = os.path.join(repo, "src")

    # Save state
    for key in ("modules.module_main", "modules.module_messageQue",
                 "modules.UI.apps.module_app_video"):
        _saved_modules[key] = sys.modules.get(key)

    # Create package hierarchy
    for pkg_name in ("modules", "modules.UI", "modules.UI.apps"):
        m = sys.modules.get(pkg_name)
        if m is None:
            m = types.ModuleType(pkg_name)
        m.__path__ = [os.path.join(src, *pkg_name.split("."))]
        sys.modules[pkg_name] = m

    # Stubs
    if "modules.module_main" not in sys.modules:
        mm = types.ModuleType("modules.module_main")
        mm.ui_manager = None
        sys.modules["modules.module_main"] = mm

    if "modules.module_messageQue" not in sys.modules:
        mq = types.ModuleType("modules.module_messageQue")
        mq.queue_message = lambda *a, **kw: None
        sys.modules["modules.module_messageQue"] = mq

    av = types.ModuleType("modules.UI.apps.module_app_video")
    av.request_play = lambda url, title="": None
    av.request_stop = lambda: None
    av.is_playing = lambda: False
    av.drain_exit_request = lambda: (False, True)
    sys.modules["modules.UI.apps.module_app_video"] = av


def _restore_modules():
    """Restore sys.modules to pre-test state."""
    for key, orig in _saved_modules.items():
        if orig is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = orig


def _import_skill_video():
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    src = os.path.join(repo, "src")
    src_added = False
    try:
        if src not in sys.path:
            sys.path.insert(0, src)
            src_added = True
        return __import__("skills.skill_video", fromlist=[""])
    finally:
        if src_added:
            sys.path.remove(src)


class TestSkillVideo(unittest.TestCase):
    """S3: skill_video.py - contract, gates, verbatim responses, source pins."""

    @classmethod
    def setUpClass(cls):
        _ensure_modules()
        cls._skill_mod = _import_skill_video()
        cls._SKILL = cls._skill_mod.SKILL

    @classmethod
    def tearDownClass(cls):
        _restore_modules()

    def setUp(self):
        self._base_params = {"action": "play", "_config": {}}

    def _make_ui(self, running=True):
        ui = mock.MagicMock()
        ui.running = running
        ui.request_app = mock.MagicMock()
        return ui

    # -- 1. SKILL dict contract ------------------------------------------

    def test_skill_dict_contract(self):
        self.assertEqual(self._SKILL["name"], "video")
        self.assertEqual(self._SKILL["required_params"], ["action"])
        self.assertNotIn("followup", self._SKILL)
        config = self._SKILL["config"]
        self.assertEqual(len(config), 7)
        self.assertTrue(config["site_youtube"]["default"])
        self.assertTrue(config["site_vimeo"]["default"])
        self.assertTrue(config["site_dailymotion"]["default"])
        self.assertTrue(config["site_twitch"]["default"])
        self.assertFalse(config["site_us_news"]["default"])
        self.assertFalse(config["site_tiktok"]["default"])
        self.assertFalse(config["site_bbc_iplayer"]["default"])

    # -- 2. site gate matrix ---------------------------------------------

    def test_site_gate_matrix(self):
        execute = self._skill_mod.execute
        ui = self._make_ui()

        with mock.patch.object(
            sys.modules["modules.module_main"], "ui_manager", ui
        ), mock.patch(
            "shutil.which", return_value="/usr/bin/mpv"
        ), mock.patch.object(
            self._skill_mod, "_check_ytdlp", return_value=None
        ):
            r = execute(
                {"action": "play", "url": "https://unknown.example.com/v",
                 "_config": {}}, None,
            )
            self.assertIn("isn't supported", r)

            r = execute(
                {"action": "play", "url": "https://cnn.com/video",
                 "_config": {}}, None,
            )
            self.assertIn("playback is disabled", r)
            self.assertIn("US News", r)

            r = execute(
                {"action": "play", "url": "https://twitch.tv/somechannel",
                 "_config": {}}, None,
            )
            self.assertIn("Live streams", r)

    # -- 3. search gated by site_youtube ---------------------------------

    def test_search_gated_by_site_youtube(self):
        execute = self._skill_mod.execute
        ui = self._make_ui()

        with mock.patch.object(
            sys.modules["modules.module_main"], "ui_manager", ui
        ), mock.patch(
            "shutil.which", return_value="/usr/bin/mpv"
        ), mock.patch.object(
            self._skill_mod, "_check_ytdlp", return_value=None
        ):
            r = execute(
                {"action": "play", "query": "cats",
                 "_config": {"site_youtube": False}}, None,
            )
            self.assertIn("playback is disabled", r)
            self.assertIn("YouTube", r)

    # -- 4. mpv missing guard --------------------------------------------

    def test_mpv_missing_guard_message(self):
        execute = self._skill_mod.execute
        ui = self._make_ui()

        with mock.patch.object(
            sys.modules["modules.module_main"], "ui_manager", ui
        ), mock.patch(
            "shutil.which", return_value=None
        ):
            r = execute(
                {"action": "play", "query": "cats", "_config": {}}, None,
            )
            self.assertIn("Video playback needs mpv", r)
            self.assertIn("rerun Install.sh", r)

    # -- 5. yt-dlp missing guard -----------------------------------------

    def test_ytdlp_missing_guard_message(self):
        execute = self._skill_mod.execute
        ui = self._make_ui()

        with mock.patch.object(
            sys.modules["modules.module_main"], "ui_manager", ui
        ), mock.patch(
            "shutil.which", return_value="/usr/bin/mpv"
        ), mock.patch.object(
            self._skill_mod, "_check_ytdlp",
            return_value="Video playback needs yt-dlp - install it in the virtual environment.",
        ):
            r = execute(
                {"action": "play", "query": "cats", "_config": {}}, None,
            )
            self.assertIn("yt-dlp", r)

    # -- 6. UI absent guard ----------------------------------------------

    def test_ui_absent_guard_message(self):
        execute = self._skill_mod.execute

        with mock.patch.object(
            sys.modules["modules.module_main"], "ui_manager", None
        ):
            r = execute(
                {"action": "play", "query": "cats", "_config": {}}, None,
            )
            self.assertIn("on-device display", r)

    # -- 7. stop idle message --------------------------------------------

    def test_stop_idle_message(self):
        execute = self._skill_mod.execute
        ui = self._make_ui()

        with mock.patch.object(
            sys.modules["modules.module_main"], "ui_manager", ui
        ), mock.patch(
            "shutil.which", return_value="/usr/bin/mpv"
        ):
            r = execute({"action": "stop", "_config": {}}, None)
            self.assertEqual(r, "No video is playing.")

    # -- 8. play returns now-playing verbatim ----------------------------

    def test_play_returns_now_playing_verbatim(self):
        execute = self._skill_mod.execute
        ui = self._make_ui()

        with mock.patch.object(
            sys.modules["modules.module_main"], "ui_manager", ui
        ), mock.patch(
            "shutil.which", return_value="/usr/bin/mpv"
        ), mock.patch.object(
            self._skill_mod, "_check_ytdlp", return_value=None
        ), mock.patch.object(
            self._skill_mod, "_resolve_youtube_search",
            return_value=("https://youtube.com/watch?v=abc", "Cat Video"),
        ):
            r = execute(
                {"action": "play", "query": "cats", "_config": {}}, None,
            )
            self.assertEqual(r, "Now playing: Cat Video.")
            ui.request_app.assert_called_with("video")

    # -- 9. stop returns stopped verbatim ---------------------------------

    def test_stop_returns_stopped_verbatim(self):
        execute = self._skill_mod.execute
        ui = self._make_ui()

        with mock.patch.object(
            sys.modules["modules.module_main"], "ui_manager", ui
        ), mock.patch(
            "shutil.which", return_value="/usr/bin/mpv"
        ), mock.patch.object(
            sys.modules["modules.UI.apps.module_app_video"],
            "is_playing", return_value=True,
        ):
            r = execute({"action": "stop", "_config": {}}, None)
            self.assertEqual(r, "Stopped the video.")

    # -- 10. search failure error line -----------------------------------

    def test_search_failure_error_line(self):
        execute = self._skill_mod.execute
        ui = self._make_ui()

        with mock.patch.object(
            sys.modules["modules.module_main"], "ui_manager", ui
        ), mock.patch(
            "shutil.which", return_value="/usr/bin/mpv"
        ), mock.patch.object(
            self._skill_mod, "_check_ytdlp", return_value=None
        ), mock.patch.object(
            self._skill_mod, "_resolve_youtube_search",
            return_value=(None, ""),
        ):
            r = execute(
                {"action": "play", "query": "xyzzy_no_match", "_config": {}}, None,
            )
            self.assertIn("Couldn't find a video", r)

    # -- 11. request_app not launch_app source pin -----------------------

    def test_skill_uses_request_app_not_launch_app(self):
        repo = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        skill_path = os.path.join(repo, "src", "skills", "skill_video.py")
        with open(skill_path) as f:
            src = f.read()
        self.assertIn("request_app", src)
        self.assertNotIn("launch_app(", src)
        self.assertNotIn("pygame.display", src)
        self.assertNotIn("pygame.Surface", src)

    # -- 12. STT/UI ban pins ---------------------------------------------

    def test_no_stt_pause_in_video_source(self):
        repo = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        skill_path = os.path.join(repo, "src", "skills", "skill_video.py")
        app_path = os.path.join(
            repo, "src", "modules", "UI", "apps", "module_app_video.py"
        )
        with open(skill_path) as f:
            skill_src = f.read().lower()
        with open(app_path) as f:
            app_src = f.read().lower()
        self.assertNotIn("stt.pause", skill_src)
        self.assertNotIn("stt.pause", app_src)
        self.assertNotIn("_close_ui", skill_src)
        self.assertNotIn("ui_manager.running = false", skill_src)
        self.assertNotIn("ui_manager.running = false", app_src)

    # -- 13. browser removal + thinking surface --------------------------

    def test_browser_removed_and_thinking_surface(self):
        repo = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        browser_path = os.path.join(repo, "src", "skills", "skill_browser.py")
        self.assertFalse(os.path.exists(browser_path))

        skill_path = os.path.join(repo, "src", "skills", "skill_video.py")
        with open(skill_path) as f:
            skill_src = f.read()
        self.assertNotIn("open_url", skill_src)

        thinking_path = os.path.join(
            repo, "src", "modules", "module_thinking.py"
        )
        with open(thinking_path) as f:
            thinking_src = f.read()

        self.assertIn('"video": "system"', thinking_src)
        self.assertNotIn('"browser": "system"', thinking_src)
        self.assertNotIn('"open the website"', thinking_src)
        self.assertNotIn('"go to the website"', thinking_src)
        self.assertNotIn(', "browser"', thinking_src)
        self.assertIn('"on youtube"', thinking_src)
        self.assertIn('"watch the video"', thinking_src)
        self.assertIn('"play a video"', thinking_src)
        self.assertIn('"play video"', thinking_src)
        self.assertNotIn('"open the browser"', thinking_src)
        self.assertNotIn('"open a browser"', thinking_src)
        self.assertIn('"open youtube"', thinking_src)

class TestInstallMedia(unittest.TestCase):
    """S4: Install.sh - mpv pi4+pi5 apt lines, yt-dlp pi4+pi5 MEDIA block, pandas Pi5-only."""

    @classmethod
    def setUpClass(cls):
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        cls._install_path = os.path.join(repo, "Install.sh")
        with open(cls._install_path) as f:
            cls._src = f.read()

    def test_mpv_in_pi4_and_pi5_apt_lines(self):
        src = self._src
        lines = src.split("\n")
        # apt lines that contain both "apt install" and "mpv"
        apt_mpv = [l for l in lines if "apt install" in l and "mpv" in l]
        self.assertEqual(len(apt_mpv), 2, f"expected 2 apt mpv lines, got {len(apt_mpv)}")
        # pi5 line (has swig)
        pi5 = [l for l in apt_mpv if "swig" in l]
        self.assertEqual(len(pi5), 1, "pi5 apt mpv line missing")
        # pi4 line (has python3-libcamera without swig)
        pi4 = [l for l in apt_mpv if "python3-libcamera" in l and "swig" not in l]
        self.assertEqual(len(pi4), 1, "pi4 apt mpv line missing")

    def test_ytdlp_in_pi4_pi5_block(self):
        src = self._src
        # Gate is: $"PI_VERSION" == "pi5" || "$PI_VERSION" == "pi4"
        self.assertIn('"pi5" ||', src)
        self.assertIn("yt-dlp", src)
        self.assertIn("MEDIA", src)

    def test_pandas_still_pi5_only(self):
        src = self._src
        lines = src.split(chr(10))
        # Find line range of YOUTUBE heredoc
        in_block = False
        block_lines = []
        for l in lines:
            if "<< 'YOUTUBE'" in l:
                in_block = True
            elif in_block and l.strip() == "YOUTUBE":
                break
            elif in_block:
                block_lines.append(l)
        block_src = chr(10).join(block_lines)
        self.assertIn("pandas", block_src)
        self.assertNotIn("yt-dlp", block_src)
