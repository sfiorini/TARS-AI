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
    mgr._bargein_lock = threading.Lock()
    mgr._bargein_generation = 1
    mgr._bargein_gate = None
    mgr._bargein_backoff = None
    mgr._bargein_captured = None
    mgr._bargein_captured_ready = threading.Event()
    mgr._bargein_tts_data = {'word_list': [], 'words_all': set()}
    mgr._bargein_enabled = True
    mgr._bargein_mode = 'fuzzy'
    mgr.sherpa_recognizer = object()   # truthy: fuzzy mode passes its check
    mgr.fastrtc_model = None
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
        self.mgr._bargein_frame_loop(self.mgr._bargein_generation, veto_fn)


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

    def test_backoff_persists_across_monitor_runs(self):
        """Review F3: the 60 s raised bar must survive the per-run EchoGate.
        A veto on one monitor run keeps the NEXT run's fresh gate raised
        (min_candidates=3, in_backoff True) until the window expires —
        the same two loud frames that triggered run 1 must NOT trigger
        during the backoff window, and must again after it expires."""
        trigger_frames = [frame(3000), frame(3001)] + [frame(21)] * 8

        # run 1: trigger + veto -> backoff recorded at manager level
        self.mic_frames = list(trigger_frames)
        self.run_loop(veto_fn=lambda u: False)
        self.assertTrue(self.mgr._bargein_gate.in_backoff())
        self.assertIsNotNone(self.mgr._bargein_backoff,
                              "the veto did not persist the raised bar at "
                              "manager level — it dies with the per-run gate")
        # consume run 1's veto completion signal, as the main loop's
        # take_bargein_utterance does in production
        self.assertIsNone(self.mgr.take_bargein_utterance(timeout=0.1))

        # run 2: a FRESH gate is built; the raised bar must still apply.
        self.mgr._bargein_active = True
        self.stop_calls.clear()
        self.mic_frames = list(trigger_frames)
        self.run_loop(veto_fn=lambda u: True)   # accept if it wrongly fires
        self.assertEqual([], self.stop_calls,
                         "the raised bar did not survive into the second "
                         "monitor run — a fresh gate re-armed the old "
                         "false-positive trigger")
        self.assertFalse(self.mgr._bargein_captured_ready.is_set())
        self.assertTrue(self.mgr._bargein_gate.in_backoff())
        self.assertEqual(3, self.mgr._bargein_gate.min_candidates)

        # after the window expires the original bar is restored
        self.mgr._bargein_backoff['until'] = time.monotonic() - 0.1
        self.mgr._bargein_active = True
        self.stop_calls.clear()
        self.mic_frames = list(trigger_frames)
        self.run_loop(veto_fn=lambda u: True)
        self.assertEqual([1], self.stop_calls,
                         "the backoff never expired — the raised bar is "
                         "permanent instead of a 60 s window")
        self.assertTrue(self.mgr._bargein_captured_ready.is_set())

    def test_stale_publish_after_stop_does_not_set_event(self):
        """Audit F3: a monitor whose run is no longer current must NEVER
        publish — the stop's clear must be the last writer. Here the run
        is stopped while the veto runs (e.g. the round's stop lands during
        the transcribe): the end-of-speech publish must be dropped instead
        of writing the capture / setting the ready Event after the clear."""
        def _veto(utterance):
            # the round is stopped while the veto transcribes — the
            # generation moves on without this run
            self.mgr._bargein_generation = 2
            return True
        self.mic_frames = (
            [frame(3000), frame(3001)]
            + [frame(21)] * 8)
        self.run_loop(veto_fn=_veto)
        self.assertFalse(self.mgr._bargein_captured_ready.is_set(),
                         "a stale monitor set the ready Event after its "
                         "run ended — the stop's clear was not the last "
                         "writer")
        self.assertIsNone(self.mgr._bargein_captured,
                          "a stale monitor wrote the capture after its "
                          "run ended")

    def test_ten_second_cap_ends_recording(self):
        """Post-trigger recording caps at 80 frames (10 s) even without silence."""
        self.mic_frames = (
            [frame(3000), frame(3001)]
            + [frame(3000)] * 100)
        self.run_loop(veto_fn=lambda u: True)
        self.assertTrue(self.mgr._bargein_captured_ready.is_set())
        # first loud frame (pre-roll) + trigger frame + 78 more = 80 frames
        self.assertEqual(80 * 2000, self.mgr._bargein_captured.size)


class TestBargeinRunOwnership(BargeinMonitorTestCase):
    """Audit F1/F3: per-run generation tokens — a monitor can only be
    stopped and can only publish on behalf of ITS OWN run.

    Uses the REAL start_bargein_monitor / stop_bargein_monitor with a
    scripted mic — no hardware, no network."""

    def test_stale_stop_is_noop(self):
        """A stop naming a superseded run id is a NO-OP: the newer run's
        monitor stays alive and can still trigger a cancel."""
        class _ModeMic:
            # shared across mic instances: 'quiet' -> endless quiet frames,
            # 'loud' -> endless above-gate frames
            mode = ['quiet']

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def flush(self):
                pass

            def read(self, n):
                if _ModeMic.mode[0] == 'loud':
                    return frame(3000), False
                return frame(0), False

        p = mock.patch.object(module_stt, "ResamplingInputStream",
                              lambda **kwargs: _ModeMic())
        p.start()
        self.addCleanup(p.stop)

        run_a = self.mgr.start_bargein_monitor("")
        self.assertIsNotNone(run_a, "start_bargein_monitor must return "
                                 "the run id that owns the monitor")
        run_b = self.mgr.start_bargein_monitor("")
        self.assertNotEqual(run_a, run_b,
                            "each start must mint a FRESH run id")

        # the stale stop (run A) must be a no-op — run B owns the monitor
        self.mgr.stop_bargein_monitor(run_id=run_a)
        self.assertTrue(self.mgr._bargein_active,
                        "a stale stop tore down the newer run's monitor")
        self.assertEqual(run_b, self.mgr._bargein_generation)
        self.assertIsNotNone(self.mgr._bargein_thread)
        self.assertTrue(self.mgr._bargein_thread.is_alive(),
                        "a stale stop killed the newer run's monitor thread")

        # ...and run B's monitor can still trigger a cancel
        _ModeMic.mode[0] = 'loud'
        deadline = time.monotonic() + 5.0
        while not self.stop_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([1], self.stop_calls,
                         "the surviving monitor never fired the cancel")
        self.assertEqual(["barge-in"], self.cancel_calls)

        self.mgr.stop_bargein_monitor(run_id=run_b)
        self.assertFalse(self.mgr._bargein_active)

    def test_stale_thread_not_rearmed(self):
        """Audit F3: after a stop whose join times out (the thread sits in
        a slow mic read), a subsequent start must NOT re-arm the stale
        thread — when its read finally returns it exits, leaving exactly
        one live monitor."""
        release = threading.Event()
        state = {'slow': True}

        class _SlowMic:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def flush(self):
                pass

            def read(self, n):
                if state['slow']:
                    # simulate the hub's evt.wait(timeout=5.0) stall —
                    # test-controlled so no real 5 s block is needed
                    release.wait(timeout=30)
                return frame(0), False

        p = mock.patch.object(module_stt, "ResamplingInputStream",
                              lambda **kwargs: _SlowMic())
        p.start()
        self.addCleanup(p.stop)

        run_a = self.mgr.start_bargein_monitor("")
        thread_a = self.mgr._bargein_thread
        self.assertTrue(thread_a.is_alive())

        # a stop whose join times out while the thread is stuck in read:
        # shrink the join so the test doesn't wait the full 6 s — the
        # thread is GENUINELY still alive, which is the F3 precondition
        real_join = thread_a.join
        thread_a.join = lambda timeout=None: real_join(0.05)
        self.mgr.stop_bargein_monitor(run_id=run_a)
        self.assertTrue(thread_a.is_alive(),
                        "precondition broken: the thread was not stuck")
        self.assertIsNone(self.mgr._bargein_thread)

        # a subsequent start must NOT re-arm the stale thread
        state['slow'] = False
        run_b = self.mgr.start_bargein_monitor("")
        thread_b = self.mgr._bargein_thread
        self.assertIsNot(thread_b, thread_a)
        self.assertTrue(thread_b.is_alive())

        # release the stale thread's read: the generation mismatch must
        # make it exit instead of resuming the loop
        release.set()
        del thread_a.join  # restore the real join for the assertion below
        thread_a.join(timeout=5)
        self.assertFalse(thread_a.is_alive(),
                         "the stale thread was re-armed by the new start "
                         "— two live monitors")
        self.assertTrue(thread_b.is_alive())
        self.assertEqual([], self.stop_calls,
                         "the stale thread triggered a cancel after its "
                         "run was stopped")

        self.mgr.stop_bargein_monitor(run_id=run_b)


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

    def test_stop_clears_unconsumed_capture(self):
        """Review F2: stop_bargein_monitor must clear a capture that was
        never consumed — an unconsumed capture (e.g. an interrupted
        filler/follow-up nobody took) must NOT leak into a later round as
        stale audio."""
        self.mgr._bargein_captured = np.zeros(2000, dtype=np.int16)
        self.mgr._bargein_captured_ready.set()
        self.mgr.stop_bargein_monitor()
        self.assertFalse(self.mgr._bargein_captured_ready.is_set(),
                         "stop left the ready Event set — a later take "
                         "would return stale audio")
        self.assertIsNone(self.mgr._bargein_captured)
        t0 = time.monotonic()
        taken = self.mgr.take_bargein_utterance(timeout=0.5)
        self.assertIsNone(taken,
                          "a subsequent take returned the stale capture")
        self.assertLess(time.monotonic() - t0, 1.0)

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
