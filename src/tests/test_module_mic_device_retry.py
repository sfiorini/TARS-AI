"""C8: mic detection failure must not be cached forever.

get_device_info() retries 4x at boot for late-enumerating USB mics, but the
old code then cached (None, MODEL_RATE) for the process lifetime — a mic that
appears 30s after boot stayed dead until reboot. Like servoctl's
_try_lazy_pca_init, a failed detection must be retried on later calls, rate-
limited so callers don't spin.
"""
import unittest
from unittest import mock

from modules import module_mic  # noqa: E402  (pytest.ini pythonpath=src)


class TestDeviceRetryAfterBootFailure(unittest.TestCase):
    def setUp(self):
        self._old_info = module_mic._device_info
        self._old_probe = getattr(module_mic, "_last_device_probe", 0.0)
        module_mic._device_info = None
        module_mic._last_device_probe = self._old_probe
        self.addCleanup(self._restore)

    def _restore(self):
        module_mic._device_info = self._old_info
        module_mic._last_device_probe = self._old_probe

    def _get(self, **kwargs):
        """get_device_info with the PortAudio re-init side effects stubbed."""
        with mock.patch.object(module_mic.sd, "_terminate"), \
             mock.patch.object(module_mic.sd, "_initialize"):
            return module_mic.get_device_info(**kwargs)

    def test_failure_not_cached_forever(self):
        # Boot: the USB mic has not enumerated yet — all attempts fail.
        with mock.patch.object(module_mic, "_find_input_device",
                               side_effect=RuntimeError("no device")) as find:
            info1 = self._get(retries=2, backoff=0.0)
            self.assertEqual(info1, (None, module_mic.MODEL_RATE))
            self.assertEqual(find.call_count, 2)

        # Immediately after: rate-limited, no re-probe.
        with mock.patch.object(module_mic, "_find_input_device") as find2:
            info2 = self._get(retries=2, backoff=0.0)
            find2.assert_not_called()
        self.assertEqual(info2, (None, module_mic.MODEL_RATE))

        # After the retry window: the mic has enumerated — it must be found.
        module_mic._last_device_probe -= getattr(module_mic, "_DEVICE_RETRY_INTERVAL", 5.0) + 1.0
        with mock.patch.object(module_mic, "_find_input_device",
                               return_value=(7, 48000)) as find3:
            info3 = self._get(retries=2, backoff=0.0)
            find3.assert_called_once()
        self.assertEqual(info3, (7, 48000))
        self.assertEqual(module_mic._device_info, (7, 48000))

    def test_success_is_still_cached(self):
        with mock.patch.object(module_mic, "_find_input_device",
                               return_value=(3, 44100)) as find:
            first = self._get(retries=2, backoff=0.0)
            second = self._get(retries=2, backoff=0.0)
        self.assertEqual(first, (3, 44100))
        self.assertEqual(second, (3, 44100))
        self.assertEqual(find.call_count, 1)  # cached — no re-probe


if __name__ == "__main__":
    unittest.main()
