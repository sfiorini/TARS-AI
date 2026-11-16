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

    def test_skill_examples_present_and_callable_format(self):
        """Bench defect 2026-11-16: video was the ONLY builtin without
        'examples' — the LLM never saw a call exemplar and answered with
        text instead of calling the skill (6/6 voice turns, zero
        TOOLLOOP). Every builtin ships examples in the house
        doubled-brace Response-JSON format; video must cover play-query,
        play-url and stop."""
        import json as _json
        examples = self._SKILL.get("examples")
        self.assertTrue(examples, "video SKILL must ship examples")
        self.assertGreaterEqual(len(examples), 3)
        rendered = []
        for ex in examples:
            text = ex.format()
            self.assertIn("Example - ", text)
            self.assertIn("User:", text)
            payload = _json.loads(text.split("Response: ", 1)[1])
            calls = payload["function_calls"]
            self.assertEqual(calls[0]["function"], "video")
            rendered.append(calls[0]["parameters"])
        actions = [p.get("action") for p in rendered]
        self.assertIn("play", actions)
        self.assertIn("stop", actions)
        plays = [p for p in rendered if p.get("action") == "play"]
        self.assertTrue(any("query" in p for p in plays))
        self.assertTrue(any("url" in p for p in plays))

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

            # Enabled defaults play (close-sweep S3-1 hardening: the
            # matrix previously covered only unknown/disabled/live arms,
            # so deleting an enabled site's gate entry survived).
            r = execute(
                {"action": "play", "url": "https://www.youtube.com/watch?v=x",
                 "_config": {}}, None,
            )
            self.assertIn("Now playing", r)
            r = execute(
                {"action": "play", "url": "https://vimeo.com/12345",
                 "_config": {}}, None,
            )
            self.assertIn("Now playing", r)

            # Opt-in sites are disabled by default.
            r = execute(
                {"action": "play",
                 "url": "https://www.tiktok.com/@user/video/123",
                 "_config": {}}, None,
            )
            self.assertIn("playback is disabled", r)
            self.assertIn("TikTok", r)
            r = execute(
                {"action": "play", "url": "https://www.bbc.co.uk/iplayer/episode/x",
                 "_config": {}}, None,
            )
            self.assertIn("playback is disabled", r)
            self.assertIn("BBC iPlayer", r)

            # Remaining enabled defaults + canonical clip host (IB-F6:
            # dailymotion/youtu.be arms were missing; clips.twitch.tv was
            # absent from the map AND live-rejected without /clip/).
            r = execute(
                {"action": "play",
                 "url": "https://www.dailymotion.com/video/x123",
                 "_config": {}}, None,
            )
            self.assertIn("Now playing", r)
            r = execute(
                {"action": "play", "url": "https://youtu.be/abc123",
                 "_config": {}}, None,
            )
            self.assertIn("Now playing", r)
            r = execute(
                {"action": "play",
                 "url": "https://clips.twitch.tv/SomeClipSlug",
                 "_config": {}}, None,
            )
            self.assertIn("Now playing", r)

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
        # yt-dlp must sit INSIDE the pi4||pi5-gated MEDIA heredoc — a
        # whole-file needle let an ungated relocation survive (IB-F7).
        gate = ('if [[ "$PI_VERSION" == "pi5" || '
                '"$PI_VERSION" == "pi4" ]]; then')
        self.assertIn(gate, src)
        rest = src[src.index(gate):]
        body = rest[rest.index("<< 'MEDIA'"):].split("\nMEDIA\n", 1)[0]
        self.assertIn("yt-dlp", body)

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


class TestYoutubeSearchResolution(unittest.TestCase):
    """Bench defect 2026-11-16 (robot, yt-dlp 2026.10.26): with
    extract_flat, a BARE query + default_search returns a _type=url
    redirect with NO entries — the skill silently found nothing
    ('Couldn't find a video for that search'). The explicit
    'ytsearch1:' prefix returns a playlist with entries. Prior tests
    mocked _resolve_youtube_search wholesale, so the real yt-dlp
    interaction was never exercised (stub-fidelity gap)."""

    @classmethod
    def setUpClass(cls):
        _ensure_modules()
        cls._skill_mod = _import_skill_video()

    @classmethod
    def tearDownClass(cls):
        _restore_modules()

    def _fake_ytdlp(self, entries, capture):
        class _FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                capture.append(url)
                if url.startswith("ytsearch1:"):
                    return {"_type": "playlist", "entries": entries}
                # bench-faithful flat redirect: NO entries key
                return {"_type": "url",
                        "url": "https://www.youtube.com/watch?v=abc123",
                        "webpage_url":
                            "https://www.youtube.com/watch?v=abc123"}
        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = _FakeYDL
        return fake

    def test_search_resolves_via_explicit_ytsearch_prefix(self):
        capture = []
        fake = self._fake_ytdlp(
            [{"id": "abc123", "title": "Lofi Radio"}], capture)
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            url, title = self._skill_mod._resolve_youtube_search(
                "lofi hip hop")
        self.assertEqual(url, "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(title, "Lofi Radio")
        self.assertEqual(capture, ["ytsearch1:lofi hip hop"])

    def test_search_handles_generator_entries(self):
        capture = []
        gen = (e for e in [{"id": "gen1", "title": "Gen"}])
        fake = self._fake_ytdlp(gen, capture)
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            url, title = self._skill_mod._resolve_youtube_search("q")
        self.assertEqual(url, "https://www.youtube.com/watch?v=gen1")
        self.assertEqual(title, "Gen")

    def test_search_empty_results_returns_none(self):
        capture = []
        fake = self._fake_ytdlp([], capture)
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            url, title = self._skill_mod._resolve_youtube_search("q")
        self.assertIsNone(url)
        self.assertEqual(title, "")

    def test_search_fallback_rejects_non_youtube_url(self):
        """IA benchfix O-1: no downstream host gate on the search branch
        — the raw-url fallback must stay YouTube-only."""
        capture = []
        fake = self._fake_ytdlp(
            [{"url": "https://evil.example.com/x", "title": "T"}], capture)
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            url, title = self._skill_mod._resolve_youtube_search("q")
        self.assertIsNone(url)
        self.assertEqual(title, "")

    def test_search_fallback_accepts_youtube_url(self):
        capture = []
        fake = self._fake_ytdlp(
            [{"url": "https://www.youtube.com/watch?v=xyz", "title": "T"}],
            capture)
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            url, title = self._skill_mod._resolve_youtube_search("q")
        self.assertEqual(url, "https://www.youtube.com/watch?v=xyz")
        self.assertEqual(title, "T")
