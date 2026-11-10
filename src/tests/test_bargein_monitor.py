"""Barge-in monitor tests (S-M3-06 / S-M3-07).

The shared `_bargein_frame_loop` is driven directly with a scripted
(fake) mic reader and a stubbed `is_tts_playing` — no hardware, no
network, no real audio device.

Invariants under test:
- The EchoGate is the SOLE owner of the candidate streak: two consecutive
  above-gate frames trigger, and `stop_tts_playback` fires EXACTLY ONCE.
- Bleed-level frames while TTS plays NEVER trigger.
- The captured utterance is onset-trimmed (<= 4 pre-roll frames before the
  first above-gate frame of the streak).
- A veto discards the utterance and arms the gate's backoff; the ready
  Event is STILL set (with a None capture) so `take_bargein_utterance`
  returns promptly instead of blocking the main loop for the full
  timeout (review F1) — veto completion must be distinguishable from
  "still recording".
- EVERY monitor exit path signals completion — including the exception
  path, so a crashing monitor cannot hang the main loop for 8 s.
"""
import sys
import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

_SRC = __file__
if __package__ in (None, ""):
    sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

import modules.module_stt as module_stt  # noqa: E402
import modules.module_tts as module_tts  # noqa: E402
import modules.module_toolloop as module_toolloop  # noqa: E402
from modules.module_stt import STTManager  # noqa: E402


def frame(value):
    """One 125 ms mic frame (2000 samples @ 16 kHz) with constant amplitude."""
    return np.full((2000, 1), value, dtype=np.int16)


class FakeMic:
    """Scripted mic reader: yields the given frames; when exhausted, stops
    the monitor loop with a final quiet frame (loop exits normally)."""

    def __init__(self, frames, mgr):
        self._frames = list(frames)
        self._mgr = mgr
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def flush(self):
        pass

    def read(self, n):
        if self.reads < len(self._frames):
            data = self._frames[self.reads]
        else:
            self._mgr._bargein_active = False
            data = frame(0)
        self.reads += 1
        return data, False


def make_manager():
    """Minimal STTManager — no hardware, no models, no __init__ side effects.

    silence_threshold=1000 with rms_scale=1.0 and amp_gain=1.0 means:
    quiet frames (amplitude ~10) are below the gate, loud frames
    (amplitude ~3000) are above it.
    """
    mgr = STTManager.__new__(STTManager)
    mgr.DEBUG = False
    mgr.silence_threshold = 1000.0
    mgr._bargein_rms_scale = 1.0
    mgr.amp_gain = 1.0
    mgr._bargein_active = True
    mgr._bargein_thread = None
    mgr._bargein_gate = None
    mgr._bargein_captured = None
    mgr._bargein_captured_ready = threading.Event()
    mgr._bargein_tts_data = {'word_list': [], 'words_all': set()}
    mgr._bargein_transcribe = lambda buf: None
    return mgr


class BargeinMonitorTestCase(unittest.TestCase):
    """Patches the frame loop's collaborators (mic, TTS, toolloop)."""

    def setUp(self):
        self.mgr = make_manager()
        self.mic_frames = []

        def _mic_factory(**kwargs):
            return FakeMic(self.mic_frames, self.mgr)

        p = mock.patch.object(module_stt, "ResamplingInputStream", _mic_factory)
        p.start()
        self.addCleanup(p.stop)

        self.stop_calls = []
        p = mock.patch.object(module_tts, "stop_tts_playback",
                              side_effect=lambda: self.stop_calls.append(1))
        p.start()
        self.addCleanup(p.stop)

        self.tts_playing = False
        p = mock.patch.object(module_tts, "is_tts_playing",
                              side_effect=lambda: self.tts_playing)
        p.start()
        self.addCleanup(p.stop)

        self.cancel_calls = []
        p = mock.patch.object(module_toolloop, "cancel_pending_turns",
                              side_effect=lambda reason: self.cancel_calls.append(reason))
        p.start()
        self.addCleanup(p.stop)

    def run_loop(self, veto_fn):
        self.mgr._bargein_frame_loop(veto_fn)


class TestBargeinFrameLoop(BargeinMonitorTestCase):
    def test_bleed_frames_never_trigger(self):
        """Above-gate frames while TTS is playing are bleed — never a trigger."""
        self.tts_playing = True
        self.mic_frames = [frame(3000) for _ in range(20)]
        self.run_loop(veto_fn=lambda u: True)
        self.assertEqual([], self.stop_calls)
        self.assertEqual([], self.cancel_calls)
        self.assertFalse(self.mgr._bargein_captured_ready.is_set())
        self.assertIsNone(self.mgr._bargein_captured)

    def test_two_candidate_frames_trigger_stop_exactly_once(self):
        """Two consecutive above-gate frames trigger (min_candidates=2);
        stop_tts_playback fires EXACTLY ONCE — the gate's 'candidate'
        return IS the trigger, the loop keeps no counter."""
        self.mic_frames = (
            [frame(10), frame(10)]                       # quiet pre-roll
            + [frame(3000), frame(3001)]                 # streak -> candidate
            + [frame(3002), frame(3003)]                  # above gate, already triggered
            + [frame(11)] * 8)                            # ~1 s of silence
        self.run_loop(veto_fn=lambda u: True)
        self.assertEqual([1], self.stop_calls)
        self.assertEqual(["barge-in"], self.cancel_calls)
        self.assertTrue(self.mgr._bargein_captured_ready.is_set())
        self.assertIsNotNone(self.mgr._bargein_captured)
        # onset-trimmed pre-roll (q1,q2,L1) + trigger frame + 2 loud +
        # 8 silent frames = 14 x 2000 samples
        self.assertEqual(14 * 2000, self.mgr._bargein_captured.size)

    def test_no_retrigger_while_already_triggered(self):
        """Many above-gate frames after the trigger never call stop again."""
        self.mic_frames = (
            [frame(3000), frame(3001)]                   # trigger
            + [frame(3000)] * 12                         # post-trigger speech
            + [frame(11)] * 8)                           # silence
        self.run_loop(veto_fn=lambda u: True)
        self.assertEqual([1], self.stop_calls)
        self.assertEqual(["barge-in"], self.cancel_calls)

    def test_preroll_onset_trim(self):
        """The captured utterance starts <= 4 frames before the FIRST
        above-gate frame of the triggering streak (onset trim)."""
        self.mic_frames = (
            [frame(11), frame(12), frame(13), frame(14), frame(15), frame(16)]
            + [frame(3000), frame(3001)]                # streak -> candidate
            + [frame(21)] * 8)                           # silence
        self.run_loop(veto_fn=lambda u: True)
        audio = self.mgr._bargein_captured
        self.assertIsNotNone(audio)
        flat = audio.reshape(-1)
        # pre-roll at trigger time: [11,12,13,14,15,16,3000]; onset-trimmed
        # to the last 4 -> the utterance starts at 14 (3 frames before the
        # first above-gate frame, 3000).
        self.assertEqual(14, int(flat[0]))
        self.assertEqual(3000, int(flat[3 * 2000]))
        # 3 quiet pre-roll + first loud frame (kept via pre-roll) + trigger
        # frame + 8 silent = 13 frames
        self.assertEqual(13 * 2000, flat.size)

    def test_veto_discards_and_backs_off(self):
        """A veto discards the utterance (capture stays None) and arms the
        gate's backoff, but STILL signals monitor completion — the ready
        Event is set with a None capture so the take path returns promptly
        (review F1). TTS stays stopped (accepted trade-off)."""
        self.mic_frames = (
            [frame(3000), frame(3001)]
            + [frame(21)] * 8)
        self.run_loop(veto_fn=lambda u: False)
        self.assertTrue(self.mgr._bargein_captured_ready.is_set())
        self.assertIsNone(self.mgr._bargein_captured)
        self.assertIsNotNone(self.mgr._bargein_gate)
        self.assertTrue(self.mgr._bargein_gate.in_backoff())
        # the cancel already fired on the energy gate — TTS stays stopped
        self.assertEqual([1], self.stop_calls)
        self.assertEqual(["barge-in"], self.cancel_calls)

    def test_veto_take_returns_promptly(self):
        """Review F1: after a veto, `take_bargein_utterance` must return
        None PROMPTLY (monitor finished, nothing captured) — NOT block the
        full 8 s timeout like it does when the veto path forgets to signal
        completion."""
        self.mic_frames = (
            [frame(3000), frame(3001)]
            + [frame(21)] * 8)
        self.run_loop(veto_fn=lambda u: False)
        t0 = time.monotonic()
        taken = self.mgr.take_bargein_utterance(timeout=8.0)
        elapsed = time.monotonic() - t0
        self.assertIsNone(taken)
        self.assertLess(elapsed, 1.0,
                        f"take blocked {elapsed:.1f}s after a veto — the "
                        "veto path did not signal monitor completion")
        # the completion signal is consumed, not left set for a later round
        self.assertFalse(self.mgr._bargein_captured_ready.is_set())

    def test_monitor_exception_signals_completion(self):
        """Review F1: a crashing monitor must still signal completion —
        an exception inside the frame loop must not leave the main loop
        hanging on take_bargein_utterance for the full timeout."""
        class BombMic:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def flush(self):
                pass

            def read(self, n):
                raise RuntimeError("mic exploded")

        p = mock.patch.object(module_stt, "ResamplingInputStream",
                              lambda **kwargs: BombMic())
        p.start()
        self.addCleanup(p.stop)
        self.run_loop(veto_fn=lambda u: True)
        self.assertTrue(self.mgr._bargein_captured_ready.is_set())
        self.assertIsNone(self.mgr._bargein_captured)
        t0 = time.monotonic()
        taken = self.mgr.take_bargein_utterance(timeout=8.0)
        elapsed = time.monotonic() - t0
        self.assertIsNone(taken)
        self.assertLess(elapsed, 1.0,
                        f"take blocked {elapsed:.1f}s after a monitor "
                        "crash — the exception path did not signal completion")

    def test_ten_second_cap_ends_recording(self):
        """Post-trigger recording caps at 80 frames (10 s) even without silence."""
        self.mic_frames = (
            [frame(3000), frame(3001)]
            + [frame(3000)] * 100)
        self.run_loop(veto_fn=lambda u: True)
        self.assertTrue(self.mgr._bargein_captured_ready.is_set())
        # first loud frame (pre-roll) + trigger frame + 78 more = 80 frames
        self.assertEqual(80 * 2000, self.mgr._bargein_captured.size)


class TestTakeBargeinUtterance(unittest.TestCase):
    """S-M3-07: the monitor-thread -> main-loop handoff API."""

    def setUp(self):
        self.mgr = make_manager()

    def test_take_returns_audio_once_then_none(self):
        audio = np.zeros(2000, dtype=np.int16)
        self.mgr._bargein_captured = audio
        self.mgr._bargein_captured_ready.set()
        taken = self.mgr.take_bargein_utterance(timeout=0.1)
        self.assertIsNotNone(taken)
        np.testing.assert_array_equal(audio, taken)
        self.assertIsNone(self.mgr._bargein_captured)
        # second take: nothing left -> None (cleared)
        self.assertIsNone(self.mgr.take_bargein_utterance(timeout=0.1))

    def test_take_timeout_returns_none(self):
        self.assertIsNone(self.mgr.take_bargein_utterance(timeout=0.05))

    def test_transcribe_bargein_audio_returns_dict(self):
        self.mgr._bargein_transcribe = lambda buf: "hello there"
        audio = np.zeros(2000, dtype=np.int16)
        self.assertEqual({"text": "hello there"},
                         self.mgr.transcribe_bargein_audio(audio))

    def test_transcribe_bargein_audio_none_and_empty(self):
        self.assertIsNone(self.mgr.transcribe_bargein_audio(None))
        self.assertIsNone(self.mgr.transcribe_bargein_audio(
            np.zeros(0, dtype=np.int16)))
        # empty transcript -> None too
        self.mgr._bargein_transcribe = lambda buf: ""
        self.assertIsNone(self.mgr.transcribe_bargein_audio(
            np.zeros(2000, dtype=np.int16)))


if __name__ == "__main__":
    unittest.main()
