"""TTS-abort exits must clear the silence bar (G6).

Both `is_tts_playing()` abort exits leave the silence progress bar stale
(their sibling exits call the clear_bar() pair), pinning the spectrum's
orange silence palette through TTS playback + STANDBY. The second site —
inside `_transcribe_with_sherpa_onnx`'s duplicated recording loop — also
returns the TRUTHY tuple (None, 0) from a function whose contract is
None|dict, so `_transcribe_utterance`'s
`if self.post_utterance_callback and result:` fires and recursively
starts recording rounds while TTS is still playing.

All device access is stubbed; these run on macOS with no hardware
(pattern: test_stt_loop_failsafe.py).
"""
import unittest
from collections import deque
from unittest import mock

import numpy as np

from modules import module_stt
from modules import module_tts
from modules.module_state import TarsState


class _FakeUI:
    def __init__(self):
        self.silence = mock.MagicMock()


class _FakeMic:
    """Context-manager stand-in for ResamplingInputStream (full surface
    the recording loops touch: read() -> (data, overflowed), flush())."""
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        return np.full(n, 50, dtype=np.int16), False

    def flush(self):
        pass


def _mgr(**attrs):
    """STTManager via __new__ with exactly the attributes the abort paths
    touch before the first mic.read (mirrors test_stt_loop_failsafe.py's
    _minimal_manager). A missing attribute fails loudly as AttributeError,
    never silently skips."""
    mgr = module_stt.STTManager.__new__(module_stt.STTManager)
    mgr.DEBUG = False
    mgr.vadmethod = "rms"
    mgr.MAX_SILENT_FRAMES = 2
    mgr.MAX_RECORDING_FRAMES = 100
    mgr.smart_turn_session = None
    mgr.smart_turn_audio_buffer = deque()
    mgr._smart_turn_future = None
    mgr._smart_turn_last_buf_len = 0
    mgr._progress_bar_funcs = None   # defeat the closure cache: fresh bar per test
    mgr.ui_manager = _FakeUI()
    for k, v in attrs.items():
        setattr(mgr, k, v)
    return mgr


def _patches(tts_playing=True):
    return [
        mock.patch.object(module_stt, "ResamplingInputStream", _FakeMic),
        mock.patch.object(module_stt, "is_tts_playing", return_value=tts_playing),
        mock.patch.object(module_stt, "set_tars_state"),
        mock.patch.object(module_stt, "queue_message"),
        mock.patch.object(module_tts, "needs_mic_flush", lambda: False),
    ]


class TestTtsAbortClearsBar(unittest.TestCase):
    def test_tts_abort_clears_bar_and_returns_none_tuple(self):
        mgr = _mgr()
        for p in _patches(tts_playing=True):
            p.start()
            self.addCleanup(p.stop)
        result = mgr._record_audio_chunks()
        self.assertEqual((None, 0), result)
        mgr.ui_manager.silence.assert_called_once_with(0)
        module_stt.set_tars_state.assert_called_once_with(TarsState.STANDBY)

    def test_sherpa_tts_abort_clears_bar_and_returns_none(self):
        mgr = _mgr(sherpa_recognizer=object())
        for p in _patches(tts_playing=True):
            p.start()
            self.addCleanup(p.stop)
        result = mgr._transcribe_with_sherpa_onnx()
        self.assertIsNone(result)
        self.assertFalse(result)
        mgr.ui_manager.silence.assert_called_once_with(0)
        module_stt.set_tars_state.assert_called_once_with(TarsState.STANDBY)

    def test_pre_speech_timeout_still_clears_bar(self):
        mgr = _mgr()
        mgr._is_silence_detected_rms = mock.MagicMock(
            return_value=(True, False, 99))
        for p in _patches(tts_playing=False):
            p.start()
            self.addCleanup(p.stop)
        result = mgr._record_audio_chunks()
        self.assertEqual((None, 0), result)
        mgr.ui_manager.silence.assert_called_once_with(0)
