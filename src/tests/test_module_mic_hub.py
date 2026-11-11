"""Unit tests (A3): _AudioHub.invalidate() in module_mic.py.

_recover_sound_server() in module_tts.py restarts the user sound server and
calls sd._terminate()/sd._initialize(), which invalidates every live
PortAudio stream — including the mic hub's shared InputStream. Without an
explicit invalidate the hub keeps a dangling stream and the mic stays deaf
until reboot. invalidate() must stop+close the stream and the hub must
lazily re-create it on the next read.
"""
import os
import sys
import unittest
from unittest import mock


from modules import module_mic  # noqa: E402


class TestAudioHubInvalidate(unittest.TestCase):
    def _hub_with_stream(self):
        hub = module_mic._AudioHub()
        stream = mock.MagicMock()
        hub._stream = stream
        return hub, stream

    def test_invalidate_stops_and_closes_stream(self):
        hub, stream = self._hub_with_stream()
        hub.invalidate()
        stream.stop.assert_called_once()
        stream.close.assert_called_once()
        self.assertIsNone(hub._stream)

    def test_invalidate_safe_without_stream(self):
        hub = module_mic._AudioHub()
        hub.invalidate()  # no live stream — must be a no-op, not an error
        self.assertIsNone(hub._stream)

    def test_stream_lazily_recreated_after_invalidate(self):
        hub, old_stream = self._hub_with_stream()
        with mock.patch.object(module_mic, "get_device_info", return_value=(0, 48000)), \
             mock.patch.object(module_mic.sd, "query_devices",
                               return_value=[{"index": 0, "name": "usb",
                                              "max_input_channels": 2}]), \
             mock.patch.object(module_mic.sd, "InputStream", return_value=mock.MagicMock()):
            hub.invalidate()
            self.assertIsNone(hub._stream)
            rid = hub.register_reader()      # next consumer re-creates the stream
            self.assertIsNotNone(hub._stream)
            self.assertIsNot(hub._stream, old_stream)
            hub.unregister_reader(rid)

    def test_invalidate_cancels_pending_grace_shutdown(self):
        hub, _ = self._hub_with_stream()
        timer = mock.MagicMock()
        hub._grace_timer = timer
        hub.invalidate()
        timer.cancel.assert_called_once()
        self.assertIsNone(hub._grace_timer)


if __name__ == "__main__":
    unittest.main()
