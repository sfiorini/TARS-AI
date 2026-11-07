"""Tests: audio output device resolution survives a busy/unavailable device.

Regression test: after a reboot the sound server (PipeWire) holds the raw USB
PCM, so opening the hw device directly fails with PaErrorCode -9985
("Device unavailable") and TARS spoke no audio. The resolver must validate
candidates by opening a stream and fall back to the system default (None),
and playback must retry once on the default if the cached device dies.
"""
import os
import sys
import unittest
from unittest import mock

SRC = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(SRC))

from modules import module_tts as tts  # noqa: E402


def _dev(name, outputs=2):
    return {"name": name, "max_output_channels": outputs, "max_input_channels": 0}


class TestResolveOutputDevice(unittest.TestCase):
    def setUp(self):
        tts._output_device = None
        tts._output_device_resolved = False

    def _run(self, devices, opens):
        """opens: set of device indices that successfully open a stream."""
        with mock.patch.object(tts.sd, "query_devices", return_value=devices), \
             mock.patch.object(tts, "_device_opens", side_effect=lambda i: i in opens):
            tts._resolve_output_device()
        return tts._output_device

    def test_prefers_openable_usb_device(self):
        devices = [_dev("USB PnP Audio Device"), _dev("USB other")]
        self.assertEqual(self._run(devices, {0, 1}), 0)

    def test_skips_busy_usb_falls_back_to_hardware(self):
        devices = [_dev("USB PnP Audio Device"), _dev("Headphones")]
        self.assertEqual(self._run(devices, {1}), 1)

    def test_falls_back_to_system_default_when_nothing_opens(self):
        devices = [_dev("USB PnP Audio Device"), _dev("vc4-hdmi-0")]  # hdmi skipped anyway
        self.assertIsNone(self._run(devices, set()))

    def test_skips_hdmi(self):
        devices = [_dev("vc4-hdmi-0 HDMI"), _dev("Speaker")]
        self.assertEqual(self._run(devices, {0, 1}), 1)

    def test_prefers_pulse_sound_server_over_usb(self):
        # Desktop session: PipeWire owns the raw USB card; the pulse route must win.
        devices = [_dev("USB PnP Audio Device"), _dev("PulseAudio Sound Server")]
        self.assertEqual(self._run(devices, {0, 1}), 1)

    def test_pulse_skipped_when_unopenable_falls_back_to_usb(self):
        devices = [_dev("PulseAudio Sound Server"), _dev("USB PnP Audio Device")]
        self.assertEqual(self._run(devices, {1}), 1)


class TestPlayFallback(unittest.TestCase):
    """sd.play raising PortAudioError must trigger sound-server recovery and retry."""

    def test_play_recovers_and_retries(self):
        calls = []

        def fake_play(data, samplerate, device=None):
            calls.append(device)
            if len(calls) == 1:
                raise tts.sd.PortAudioError("Device unavailable [PaErrorCode -9985]")

        tts._output_device = 2
        tts._output_device_resolved = True
        with mock.patch.object(tts.sd, "play", side_effect=fake_play), \
             mock.patch.object(tts, "_recover_sound_server") as recover, \
             mock.patch.object(tts, "_resolve_output_device") as resolve, \
             mock.patch.object(tts.sd, "query_devices", return_value=[_dev("USB PnP")]), \
             mock.patch.object(tts, "_device_opens", return_value=True):
            try:
                tts.sd.play(b"x", 16000, device=tts._output_device)
            except tts.sd.PortAudioError:
                tts._recover_sound_server()
                tts._reset_output_device_resolution()
                tts._resolve_output_device()
                tts.sd.play(b"x", 16000, device=tts._output_device)
        recover.assert_called_once()
        self.assertEqual(len(calls), 2)  # failed on 2, succeeded on re-resolved device
        self.assertEqual(calls[0], 2)

    def test_final_fallback_to_default_when_retry_also_fails(self):
        calls = []

        def fake_play(data, samplerate, device=None):
            calls.append(device)
            raise tts.sd.PortAudioError("Device unavailable [PaErrorCode -9985]")

        tts._output_device = 2
        tts._output_device_resolved = True
        with mock.patch.object(tts.sd, "play", side_effect=fake_play), \
             mock.patch.object(tts, "_recover_sound_server"), \
             mock.patch.object(tts.sd, "query_devices", return_value=[]), \
             mock.patch.object(tts, "_resolve_output_device", side_effect=lambda: setattr(tts, "_output_device", None) or setattr(tts, "_output_device_resolved", True)):
            try:
                tts.sd.play(b"x", 16000, device=tts._output_device)
            except tts.sd.PortAudioError:
                tts._recover_sound_server()
                tts._reset_output_device_resolution()
                tts._resolve_output_device()
                try:
                    tts.sd.play(b"x", 16000, device=tts._output_device)
                except tts.sd.PortAudioError:
                    tts._fallback_to_default_output()
                    tts.sd.play(b"x", 16000, device=None)
        self.assertEqual(calls, [2, None, None])
        self.assertIsNone(tts._output_device)


if __name__ == "__main__":
    unittest.main()
