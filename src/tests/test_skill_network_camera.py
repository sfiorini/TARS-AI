"""M3-S1: network_camera credential sanitization (D20).

Six tests pin the sanitize-at-source fix: agentdvr error drops the
tried-URL list, http error replaces {e} with a generic category,
rtsp unchanged, _redact_url helper, handlers wrap sanitized err.

NO network, no real cameras, no secrets.
"""

import sys
import unittest
from unittest import mock

import requests as _requests


# -- Helpers -----------------------------------------------------------

def _import_nc_module():
    """Import skill_network_camera, clearing cached imports first."""
    for key in list(sys.modules.keys()):
        if "skill_network_camera" in key:
            del sys.modules[key]
    # Ensure requests is available for mocking
    import src.skills.skill_network_camera as nc
    return nc


def _req_exc(msg="connection failed"):
    """Shortcut for a requests.RequestException with the given message."""
    return _requests.RequestException(msg)


# -- Tests -------------------------------------------------------------

class TestNetworkCameraSanitization(unittest.TestCase):
    """S1 pin: returned content never carries :// or userinfo after the fix."""

    def setUp(self):
        self.nc = _import_nc_module()

    # ---- 1. agentdvr error drops the URL list -----------------------

    def test_agentdvr_error_has_no_urls(self):
        """_capture_agentdvr with all endpoints failing → camera name +
        endpoint count; NO :// and no userinfo."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = _req_exc()
            _, err = self.nc._capture_agentdvr(
                "http://user:pass@192.168.1.100:8090",
                "front_door",
            )
        self.assertIn("front_door", err)
        self.assertIn("tried 3 snapshot endpoints", err)
        self.assertNotIn("://", err)
        self.assertNotIn("user:pass", err)

    # ---- 2. http error drops the exception text ---------------------

    def test_http_error_has_no_exception_text(self):
        """_capture_http with a RequestException that embeds the full URL →
        generic category; no :// and no user:pass."""
        import requests as _requests
        exc = _requests.RequestException(
            "Failed to connect to http://user:pass@10.0.0.5:8080/snap.jpg"
        )
        with mock.patch("requests.get", side_effect=exc):
            _, err = self.nc._capture_http(
                "http://user:pass@10.0.0.5:8080/snap.jpg"
            )
        self.assertIn("connection failed or timed out", err)
        self.assertNotIn("://", err)
        self.assertNotIn("user:pass", err)

    # ---- 3. http status-code line kept ------------------------------

    def test_http_status_line_kept(self):
        """Non-200 response → status-code line preserved (no URL in it)."""
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 404
        with mock.patch("requests.get", return_value=fake_resp):
            _, err = self.nc._capture_http(
                "http://192.168.1.50/snap.jpg"
            )
        self.assertIn("status 404", err)

    # ---- 4. rtsp error unchanged ------------------------------------

    def test_rtsp_error_unchanged(self):
        """_capture_rtsp already generic — pin its message unchanged."""
        # _capture_rtsp imports cv2 internally; mock cv2.VideoCapture
        with mock.patch("cv2.VideoCapture") as mock_vc:
            mock_vc.return_value.read.return_value = (False, None)
            _, err = self.nc._capture_rtsp(
                "rtsp://user:pass@10.0.0.5:554/stream"
            )
        self.assertIn("Could not capture frame from RTSP stream", err)
        self.assertNotIn("://", err)

    # ---- 5. _redact_url helper --------------------------------------

    def test_redact_url_strips_userinfo(self):
        """_redact_url strips user:pass@, keeps scheme+host."""
        result = self.nc._redact_url(
            "http://admin:secret@192.168.1.100:8080/some/path"
        )
        self.assertEqual(result, "http://192.168.1.100:8080")
        self.assertNotIn("admin", result)
        self.assertNotIn("secret", result)

    # ---- 6. handlers wrap sanitized err -----------------------------

    def test_handlers_wrap_sanitized_err(self):
        """At execute level with a failing camera → RETURNED content has
        no :// (the leak path is content → follow-up LLM → speech)."""
        exc_with_url = _req_exc(
            "ConnectionError to http://user:pass@10.0.0.5:8080/snap.jpg"
        )
        with mock.patch("requests.get", side_effect=exc_with_url):
            result = self.nc.execute(
                {"camera": "driveway", "action": "snapshot"},
                {
                    "skill_config": {
                        "cameras": '[{"name":"driveway","url":"http://x:y@10.0.0.5:8080/snap.jpg","type":"http"}]'
                    },
                    "source": "voice",
                },
            )
        self.assertNotIn("://", result)


class TestSanitizationNotes(unittest.TestCase):
    """M3-S5: post-sanitization comment/docs note pins (D20 comment sweep).

    Two tests pin the updated rationale in module_skills.py and
    voice-pipeline.md — needles 'sanitized' + 'retained', stale claims
    absent from the relevant scoped window.
    """

    def _module_skills_comment_window(self):
        """Return the comment block above _SPEAKABLE_BUILTIN_SKILLS."""
        import pathlib
        nc_file = pathlib.Path(__file__).resolve().parents[1] / "modules" / "module_skills.py"
        text = nc_file.read_text()
        # window: from the G9 header line to the frozenset opening
        marker = "# G9: builtin skills whose result content is natural"
        close = "_SPEAKABLE_BUILTIN_SKILLS = frozenset({"
        start = text.index(marker)
        end = text.index(close, start)
        return text[start:end]

    def _voice_pipeline_followup_window(self):
        """Return the Follow-up failure paragraph from voice-pipeline.md."""
        import pathlib
        vp_file = pathlib.Path(__file__).resolve().parents[2] / "docs" / "voice-pipeline.md"
        text = vp_file.read_text()
        # window: the Follow-up failure section — start at the heading, end at
        # the next same-level heading or the Rollback section
        heading = "\n- **Follow-up failure**"
        start = text.index(heading)
        # end at next heading-level marker: "- **Rollback**" or end of file
        next_marker = text.find("\n- **Rollback**", start)
        end = next_marker if next_marker != -1 else len(text)
        return text[start:end]

    def test_module_skills_comment_has_sanitized_rationale(self):
        """The module_skills.py comment above _SPEAKABLE_BUILTIN_SKILLS
        contains 'sanitized' + 'retained' and does NOT contain the stale
        'fetch-failure paths' or 'tried camera URLs' claims."""
        window = self._module_skills_comment_window()
        self.assertIn("sanitized", window)
        self.assertIn("retained", window)
        self.assertNotIn("fetch-failure paths", window)
        self.assertNotIn("tried camera URLs", window)

    def test_voice_pipeline_followup_note_has_sanitized_rationale(self):
        """The voice-pipeline.md Follow-up failure paragraph contains
        'sanitized' + 'retained' in the network_camera note, and does
        NOT contain the stale 'fetch-failure paths' or 'tried camera URLs'
        claims within that window."""
        window = self._voice_pipeline_followup_window()
        self.assertIn("sanitized", window)
        self.assertIn("retained", window)
        self.assertNotIn("fetch-failure paths", window)
        self.assertNotIn("tried camera URLs", window)


if __name__ == "__main__":
    unittest.main()