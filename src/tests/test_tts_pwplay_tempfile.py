"""C7: the pw-play fallback must never leak its temp wav.

_play_via_pwplay writes a temp wav and hands it to a pw-play subprocess; the
caller unlinks it after playback. Two leaks existed: (1) if Popen raises after
sf.write, the temp file was never removed; (2) the caller unlinked only on the
happy path — an exception during the poll loop left the file behind. The temp
path must be tracked on the proc and unlinked in a finally.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np

from modules import module_tts as tts  # noqa: E402  (pytest.ini pythonpath=src)


class TestPwPlayTempCleanup(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="tars-pwplay-test-")
        # The tracking test deliberately leaves its wav behind; rmtree handles both.
        self.addCleanup(lambda: shutil.rmtree(self._tmpdir, ignore_errors=True))
        self._created = []

    def _spy_named_temp_file(self):
        """Patch tempfile.NamedTemporaryFile with a spy on the real one."""
        real_ntf = tempfile.NamedTemporaryFile

        def spy(*args, **kwargs):
            kwargs["dir"] = self._tmpdir
            f = real_ntf(*args, **kwargs)
            self._created.append(f.name)
            return f

        return mock.patch("tempfile.NamedTemporaryFile", side_effect=spy)

    def _data(self):
        return np.zeros(100, dtype="float32")

    def test_success_tracks_temp_path_on_proc(self):
        with self._spy_named_temp_file(), \
             mock.patch.object(tts.shutil, "which", return_value="/usr/bin/pw-play"), \
             mock.patch("subprocess.Popen", return_value=mock.MagicMock()):
            proc = tts._play_via_pwplay(self._data(), 16000)
        self.assertIsNotNone(proc)
        self.assertEqual(len(self._created), 1)
        self.assertEqual(getattr(proc, "tars_tmp_wav", None), self._created[0])
        self.assertTrue(os.path.exists(self._created[0]))

    def test_temp_removed_after_playback(self):
        with self._spy_named_temp_file(), \
             mock.patch.object(tts.shutil, "which", return_value="/usr/bin/pw-play"), \
             mock.patch("subprocess.Popen", return_value=mock.MagicMock()):
            proc = tts._play_via_pwplay(self._data(), 16000)
        path = proc.tars_tmp_wav
        self.assertTrue(os.path.exists(path))
        tts._cleanup_pwplay_proc(proc)   # caller's finally after playback
        self.assertFalse(os.path.exists(path))
        # Idempotent / safe to call again
        tts._cleanup_pwplay_proc(proc)

    def test_temp_removed_when_popen_raises(self):
        with self._spy_named_temp_file(), \
             mock.patch.object(tts.shutil, "which", return_value="/usr/bin/pw-play"), \
             mock.patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            proc = tts._play_via_pwplay(self._data(), 16000)
        self.assertIsNone(proc)
        self.assertEqual(len(self._created), 1)
        self.assertFalse(os.path.exists(self._created[0]), "temp wav leaked when Popen raised")

    def test_temp_removed_when_sf_write_raises(self):
        with self._spy_named_temp_file(), \
             mock.patch.object(tts.shutil, "which", return_value="/usr/bin/pw-play"), \
             mock.patch.object(tts.sf, "write", side_effect=OSError("disk full")), \
             mock.patch("subprocess.Popen") as popen:
            proc = tts._play_via_pwplay(self._data(), 16000)
        self.assertIsNone(proc)
        popen.assert_not_called()
        self.assertFalse(os.path.exists(self._created[0]), "temp wav leaked when sf.write raised")

    def test_cleanup_safe_for_plain_proc_and_none(self):
        # Procs that never went through _play_via_pwplay (old mocks) and None
        # must not raise — cleanup is best-effort.
        tts._cleanup_pwplay_proc(None)
        tts._cleanup_pwplay_proc(mock.MagicMock(spec=subprocess.Popen))


if __name__ == "__main__":
    unittest.main()
