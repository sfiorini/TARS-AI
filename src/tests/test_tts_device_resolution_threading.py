"""B6: output device resolution must be thread-safe.

_resolve_output_device() is called from playback coroutines (asyncio.to_thread)
and from the UI thread. The old code set _output_device_resolved=True BEFORE
the scan ran, so a concurrent caller returned immediately with
_output_device=None (silent system-default routing). Resolution must run
under a module-level lock, with the flag set only AFTER the scan completes;
_reset_output_device_resolution / _fallback_to_default_output must serialize
with an in-flight scan the same way.
"""
import threading
import time
import unittest
from unittest import mock

from modules import module_tts as tts  # noqa: E402  (pytest.ini pythonpath=src)


def _dev(name, outputs=2):
    return {"name": name, "max_output_channels": outputs, "max_input_channels": 0}


class TestConcurrentDeviceResolution(unittest.TestCase):
    def setUp(self):
        tts._output_device = None
        tts._output_device_resolved = False

    def _slow_scan_patches(self, started, release):
        """query_devices returns one USB device; _device_opens blocks on release."""
        def slow_opens(idx):
            started.set()
            release.wait(2)
            return True

        return (
            mock.patch.object(tts.sd, "query_devices", return_value=[_dev("USB PnP")]),
            mock.patch.object(tts, "_device_opens", side_effect=slow_opens),
        )

    def test_flag_not_set_until_resolution_completes(self):
        started, release = threading.Event(), threading.Event()
        qd, _opens = self._slow_scan_patches(started, release)
        with qd, _opens:
            t = threading.Thread(target=tts._resolve_output_device)
            t.start()
            self.assertTrue(started.wait(2))          # scan is mid-flight
            self.assertFalse(tts._output_device_resolved)
            release.set()
            t.join(2)
            self.assertFalse(t.is_alive())
        self.assertTrue(tts._output_device_resolved)
        self.assertEqual(tts._output_device, 0)

    def test_concurrent_callers_all_get_the_resolved_device(self):
        started, release = threading.Event(), threading.Event()
        qd, _opens = self._slow_scan_patches(started, release)
        results = []

        def resolver():
            tts._resolve_output_device()
            results.append(tts._output_device)

        with qd as qd_mock, _opens:
            t1 = threading.Thread(target=resolver)
            t1.start()
            self.assertTrue(started.wait(2))          # t1 is mid-scan
            t2 = threading.Thread(target=resolver)   # t2 must not see a half-resolved state
            t2.start()
            time.sleep(0.05)
            release.set()
            t1.join(2)
            t2.join(2)
            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
        self.assertEqual(results, [0, 0])             # neither caller got None
        self.assertEqual(qd_mock.call_count, 1)       # exactly one scan ran
        self.assertTrue(tts._output_device_resolved)

    def test_reset_during_resolution_waits_for_scan(self):
        started, release = threading.Event(), threading.Event()
        qd, _opens = self._slow_scan_patches(started, release)
        with qd, _opens:
            t1 = threading.Thread(target=tts._resolve_output_device)
            t1.start()
            self.assertTrue(started.wait(2))
            tts._reset_output_device_resolution()      # must serialize with the scan
            release.set()
            t1.join(2)
            self.assertFalse(t1.is_alive())
        # The reset must not be lost to the finishing scan.
        self.assertFalse(tts._output_device_resolved)
        self.assertIsNone(tts._output_device)

    def test_fallback_during_resolution_wins(self):
        started, release = threading.Event(), threading.Event()
        qd, _opens = self._slow_scan_patches(started, release)
        with qd, _opens:
            t1 = threading.Thread(target=tts._resolve_output_device)
            t1.start()
            self.assertTrue(started.wait(2))
            tts._fallback_to_default_output()         # must serialize with the scan
            release.set()
            t1.join(2)
            self.assertFalse(t1.is_alive())
        # The fallback (last writer, under the lock) must win over the scan result.
        self.assertTrue(tts._output_device_resolved)
        self.assertIsNone(tts._output_device)


if __name__ == "__main__":
    unittest.main()
