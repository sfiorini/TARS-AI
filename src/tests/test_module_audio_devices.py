"""C9: shared audio device-candidate logic (module_audio_devices).

module_mic._find_input_device and module_tts._resolve_output_device duplicated
device discovery with different priorities. The shared module keeps both
behaviors exactly: output = pulse > USB > hardware > virtual (HDMI skipped,
every candidate open-validated); input = system default if real hardware,
else pulse, else real hardware, else any input, else the PortAudio C default
(never stream-validated — opening the mic would race the audio hub).
"""
import unittest
from unittest import mock

from modules import module_audio_devices as ad  # noqa: E402  (pytest.ini pythonpath=src)


def _odev(name, outputs=2):
    return {"name": name, "max_output_channels": outputs, "max_input_channels": 0,
            "default_samplerate": 48000}


def _idev(name, inputs=1, rate=48000):
    return {"name": name, "max_input_channels": inputs, "max_output_channels": 0,
            "default_samplerate": rate}


def make_query(devices):
    """sd.query_devices() -> list; sd.query_devices(idx, kind=...) -> dict."""
    def query(*args, **kwargs):
        if args:
            return devices[args[0]]
        return devices
    return query


class TestFindOutputDevice(unittest.TestCase):
    def _find(self, devices, opens, log=None, **kwargs):
        with mock.patch.object(ad.sd, "query_devices", side_effect=make_query(devices)), \
             mock.patch.object(ad, "_opens", side_effect=lambda i, kind: i in opens):
            return ad.find_device(kind="output", log=log, **kwargs)

    def test_pulse_preferred_over_usb(self):
        idx, dev, label = self._find([_odev("USB PnP"), _odev("PulseAudio Sound Server")], {0, 1})
        self.assertEqual((idx, label), (1, "pulse"))

    def test_usb_over_hardware(self):
        idx, dev, label = self._find([_odev("USB PnP"), _odev("Headphones")], {0, 1})
        self.assertEqual((idx, label), (0, "USB"))

    def test_hardware_over_virtual(self):
        idx, dev, label = self._find([_odev("dmix"), _odev("Headphones")], {0, 1})
        self.assertEqual((idx, label), (1, "hardware"))

    def test_hdmi_outputs_skipped(self):
        idx, dev, label = self._find([_odev("vc4-hdmi-0 HDMI"), _odev("Speaker")], {0, 1})
        self.assertEqual((idx, label), (1, "hardware"))

    def test_unopenable_candidate_skipped_with_warning(self):
        logs = []
        idx, dev, label = self._find([_odev("USB busy"), _odev("Speaker")], {1}, log=logs.append)
        self.assertEqual(idx, 1)
        self.assertTrue(any("USB busy" in m and "unavailable" in m for m in logs),
                        f"expected per-candidate skip warning, got {logs}")

    def test_nothing_opens_returns_none(self):
        idx, dev, label = self._find([_odev("USB PnP")], set())
        self.assertEqual((idx, dev, label), (None, None, None))

    def test_validate_false_skips_open_check(self):
        with mock.patch.object(ad.sd, "query_devices", side_effect=make_query([_odev("USB PnP")])), \
             mock.patch.object(ad, "_opens") as opens:
            idx, dev, label = ad.find_device(kind="output", validate=False)
        self.assertEqual(idx, 0)
        opens.assert_not_called()

    def test_custom_validator_used(self):
        calls = []

        def validator(i):
            calls.append(i)
            return False

        idx, dev, label = self._find([_odev("USB PnP")], set(), validator=validator)
        self.assertEqual(calls, [0])  # module_tts passes its own _device_opens
        self.assertEqual((idx, dev, label), (None, None, None))


class TestFindInputDevice(unittest.TestCase):
    def _find(self, devices, default_in=None, pa_default=None):
        default = mock.MagicMock()
        default.device = [default_in, -1]
        with mock.patch.object(ad.sd, "query_devices", side_effect=make_query(devices)), \
             mock.patch.object(ad.sd, "default", default), \
             mock.patch.object(ad.sd, "_lib") as lib:
            lib.Pa_GetDefaultInputDevice.return_value = pa_default if pa_default is not None else -1
            return ad.find_device(kind="input")

    def test_system_default_when_real_hardware(self):
        # default (idx 0) is a real USB mic (1-32 input channels)
        idx, info, _label = self._find([_idev("USB PnP", inputs=1)], default_in=0)
        self.assertEqual(idx, 0)
        self.assertEqual(info["default_samplerate"], 48000)

    def test_alsa_plugin_default_skipped_pulse_preferred(self):
        # plugin "default" reports 128 channels -> not real hardware
        devices = [_idev("default", inputs=128), _idev("PulseAudio Sound Server")]
        idx, info, _label = self._find(devices, default_in=0)
        self.assertEqual(idx, 1)

    def test_real_hardware_after_pulse(self):
        devices = [_idev("dmix", inputs=128), _idev("USB PnP")]
        idx, info, _label = self._find(devices, default_in=-1)
        self.assertEqual(idx, 1)

    def test_any_input_device_as_fallback(self):
        devices = [_idev("default", inputs=128)]
        idx, info, _label = self._find(devices, default_in=-1)
        self.assertEqual(idx, 0)

    def test_portaudio_c_default_as_last_resort(self):
        devices = [_idev("default", inputs=128)]
        idx, info, _label = self._find(devices, default_in=-1, pa_default=0)
        self.assertEqual(idx, 0)

    def test_no_input_device_returns_none(self):
        idx, info, _label = self._find([], default_in=-1, pa_default=-1)
        self.assertEqual((idx, info), (None, None))

    def test_input_is_never_stream_validated(self):
        # Opening the mic would race the audio hub's exclusive InputStream.
        with mock.patch.object(ad.sd, "query_devices", side_effect=make_query([_idev("USB PnP")])), \
             mock.patch.object(ad.sd, "default", mock.MagicMock(device=[-1, -1])), \
             mock.patch.object(ad, "_opens") as opens:
            ad.find_device(kind="input", validate=True)
        opens.assert_not_called()


if __name__ == "__main__":
    unittest.main()
