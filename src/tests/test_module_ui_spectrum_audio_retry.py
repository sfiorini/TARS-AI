"""The face-UI spectrum must survive a transient mic-hub open failure.

module_ui_spectrum opens its audio with
``module_mic.open_native_stream(callback=...)``, which is *hub-backed*: it
registers a callback consumer on the single shared mic stream (see
module_mic._AudioHub — "One PortAudio stream, many consumers"). The hub opens
the PortAudio device lazily on the first registration.

At boot that device open races the background-noise calibration stream being
torn down (module_stt._measure_background_noise holds a hub reader and releases
it ~2 s before the UI builds its spectrum). The robot logged exactly one such
collision per boot:

    Failed to start audio stream: Error opening InputStream:
    Device unavailable [PaErrorCode -9985]

``start_audio_stream()`` ran exactly once, from ``__init__``, and on failure set
``audio_running = False`` for good: every style (bars / wave / sinewave /
circular / spectrogram) stayed blank for the whole session, and switching the
style from the menu changed nothing, because ``update_spectrum()`` returns early
when ``audio_running`` is False and nothing ever tried again.

Registration is cheap — the hub reuses one stream — so a failed start must be
retried instead of being terminal. It must NOT be retried after an intentional
``stop_audio_stream()`` (called on UI shutdown), or the spectrum would
resurrect itself as a hub consumer that never unregisters.
"""
import importlib
import sys
import types
import unittest
from unittest import mock

# Pre-import numpy: the module under test imports it, and a native extension
# first imported here stays resident (see _import_spectrum_with_stub).
import numpy  # noqa: F401

_DEVICE_UNAVAILABLE = (
    "Error opening InputStream: Device unavailable [PaErrorCode -9985]"
)
_MODULE = "modules.UI.module_ui_spectrum"


def _make_pygame_stub():
    """Minimal pygame stand-in.

    pygame is not installed on dev Macs, and on the robot a real
    ``Surface(...).convert_alpha()`` needs a video mode. The spectrum's audio
    registration path touches only ``pygame.Surface`` at construction time, so a
    stub keeps this test about the mic hub instead of about SDL.
    """
    pygame = types.ModuleType("pygame")

    class _Surface:
        def __init__(self, size, flags=0):
            self.size = size
            self.flags = flags

        def convert_alpha(self):
            return self

        def fill(self, *args, **kwargs):
            pass

        def blit(self, *args, **kwargs):
            pass

    pygame.Surface = _Surface
    pygame.HWSURFACE = 0x00000000
    pygame.SRCALPHA = 0x00010000
    return pygame


def _import_spectrum_with_stub():
    """Import the module under test against the stub, without side effects.

    Only the "pygame" key is swapped and restored. Do NOT wrap this import in
    mock.patch.dict(sys.modules, ...): patch.dict restores sys.modules by
    clearing it back to a snapshot taken at entry, which drops every module the
    import pulled in — including native extensions (soxr, sounddevice) that
    module_mic imports. Re-importing a native extension on the robot aborts the
    interpreter ("Fatal Python error: Aborted" in soxr/__init__.py), and it
    orphans the queue threads module_messageQue starts at import.
    """
    stub = _make_pygame_stub()
    previous = sys.modules.get("pygame")
    sys.modules["pygame"] = stub
    try:
        sys.modules.pop(_MODULE, None)
        return importlib.import_module(_MODULE)
    finally:
        if previous is None:
            sys.modules.pop("pygame", None)
        else:
            sys.modules["pygame"] = previous


spectrum_mod = _import_spectrum_with_stub()


class TestSpectrumAudioRetry(unittest.TestCase):
    def _construct(self):
        """Build a SpectrumSystem at a pinned monotonic time.

        The caller holds the open_native_stream (and get_native_rate) patches for
        the whole scenario: the retry happens on a later update_spectrum(), so a
        patch scoped to construction alone would let the real hub register.
        """
        with mock.patch.object(spectrum_mod.time, "monotonic", return_value=1000.0):
            return spectrum_mod.SpectrumSystem(480, 800, style="wave")

    def test_retries_and_recovers_after_a_transient_boot_failure(self):
        """A failed first registration is retried on a later frame."""
        stream = mock.MagicMock()
        attempts = []

        def factory(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise RuntimeError(_DEVICE_UNAVAILABLE)
            return stream

        rate = mock.patch("modules.module_mic.get_native_rate", return_value=16000)
        opener = mock.patch.object(spectrum_mod, "open_native_stream",
                                   side_effect=factory)
        with rate, opener:
            spectrum = self._construct()
            # Boot: the hub lost the race, exactly as logged on the robot.
            self.assertFalse(spectrum.audio_running)
            self.assertEqual(len(attempts), 1)

            # Same instant: a retry is not due yet — must not hammer the device
            # (each hub open attempt can sleep ~3 s inside _ensure_stream).
            with mock.patch.object(spectrum_mod.time, "monotonic", return_value=1000.0):
                spectrum.update_spectrum()
            self.assertFalse(spectrum.audio_running)
            self.assertEqual(len(attempts), 1)

            # Past the retry interval the next frame recovers the visualizer.
            with mock.patch.object(spectrum_mod.time, "monotonic", return_value=1003.0):
                spectrum.update_spectrum()
            self.assertTrue(spectrum.audio_running)
            self.assertEqual(len(attempts), 2)
            stream.start.assert_called_once()
            self.assertIs(spectrum.stream, stream)

    def test_never_retries_after_an_intentional_stop(self):
        """stop_audio_stream() (UI shutdown) is terminal — no resurrection."""
        stream = mock.MagicMock()
        rate = mock.patch("modules.module_mic.get_native_rate", return_value=16000)
        opener = mock.patch.object(spectrum_mod, "open_native_stream",
                                   return_value=stream)
        with rate, opener:
            spectrum = self._construct()
            self.assertTrue(spectrum.audio_running)

            spectrum.stop_audio_stream()
            self.assertFalse(spectrum.audio_running)

            with mock.patch.object(spectrum_mod.time, "monotonic", return_value=1e9):
                spectrum.update_spectrum()

            self.assertFalse(spectrum.audio_running)
            stream.start.assert_called_once()  # only the original start


if __name__ == "__main__":
    unittest.main()