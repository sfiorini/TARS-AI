"""Stop-and-listen wiring tests (S-M3-08).

Drives the REAL ``module_main.utterance_callback`` through the preemptive
path with ``play_audio_chunks`` stubbed to return True (interrupted), the
same harness pattern as test_voice_side_effect_dispatch.py — no hardware,
no network, no real TTS.

Invariants under test:
- A captured barge-in utterance re-enters the SAME round entry point the
  normal STT path uses (the module-global ``utterance_callback``) with a
  pure ``{"text": ...}`` JSON payload — no wake word, no beep flags.
- ``stop_bargein_monitor()`` runs AFTER ``take_bargein_utterance()``
  returns (the Event + 3 s join release the mic reader before the
  re-entered round opens its own).
- No capture (veto / empty transcript) -> existing behavior: LISTENING,
  wake word required.
- ``bargein_listen_after_interrupt = False`` (kill switch) ->
  ``take_bargein_utterance`` is never called.
- Review F2: an interrupted filler / deferred follow-up consumes its
  captured utterance through the SAME wiring (take -> transcribe ->
  re-enter the round entry point) — the capture is never dropped and
  never leaks stale into a later round.
"""
import json
import os
import sys
import types
import unittest
from unittest import mock

import numpy as np

# Fake Pi-only hardware so module_servoctl (via module_main) imports on
# any dev machine — same pattern as test_voice_side_effect_dispatch.py.
board = types.ModuleType("board")
board.SCL = "SCL"
board.SDA = "SDA"
busio = types.ModuleType("busio")
busio.I2C = mock.MagicMock()
pca9685 = types.ModuleType("adafruit_pca9685")
pca9685.PCA9685 = mock.MagicMock()
for _name, _mod in (("board", board), ("busio", busio),
                    ("adafruit_pca9685", pca9685)):
    sys.modules.setdefault(_name, _mod)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import modules.module_main as module_main  # noqa: E402
import modules.module_toolloop as module_toolloop  # noqa: E402
from test_voice_side_effect_dispatch import _FakeRunner, _VoiceTurnTestCase  # noqa: E402
from modules.module_state import TarsState  # noqa: E402


class FakeBargeinStt:
    """Stands in for the STT manager on the interrupted path."""

    def __init__(self, audio=None, transcript=None, take_results=None):
        self.calls = []
        self._audio = audio
        self._transcript = transcript
        self._take_results = list(take_results) if take_results is not None else None
        self._next_run_id = 0

    def start_bargein_monitor(self, tts_text=""):
        # audit F1: the real manager returns a per-run generation token;
        # the caller must capture it and hand it back to the matching stop
        self._next_run_id += 1
        self.calls.append(("start", tts_text, self._next_run_id))
        return self._next_run_id

    def take_bargein_utterance(self, timeout=8.0):
        self.calls.append(("take", timeout))
        if self._take_results is not None:
            return self._take_results.pop(0) if self._take_results else None
        return self._audio

    def transcribe_bargein_audio(self, audio):
        self.calls.append(("transcribe", audio))
        return self._transcript

    def stop_bargein_monitor(self, run_id=None):
        self.calls.append(("stop", run_id))

    def call_names(self):
        return [c[0] for c in self.calls]


class StopAndListenTestCase(_VoiceTurnTestCase):
    """Interrupted-round harness: play_audio_chunks -> True (interrupted),
    the module-global utterance_callback replaced by a recorder so the
    barge-in re-entry is observable without running a second real round."""

    def setUp(self):
        super().setUp()
        # interrupted playback: was_interrupted = True
        p = mock.patch.object(module_main, "play_audio_chunks",
                              new=mock.AsyncMock(return_value=True))
        p.start()
        self.addCleanup(p.stop)

        # the round entry point the barge-in transcript must re-enter
        # (module-global lookup at call time -> the recorder sees it)
        self._real_cb = module_main.utterance_callback
        self.round_calls = []
        p = mock.patch.object(module_main, "utterance_callback",
                              side_effect=lambda msg: self.round_calls.append(msg))
        p.start()
        self.addCleanup(p.stop)

    def _run_interrupted_turn(self, fake_stt):
        saved = module_main.stt_manager
        module_main.stt_manager = fake_stt
        self.addCleanup(setattr, module_main, "stt_manager", saved)
        p = mock.patch.object(module_toolloop, "get_loop_runner",
                              return_value=_FakeRunner(started=False))
        p.start()
        self.addCleanup(p.stop)
        message = json.dumps({
            "text": "tell me something long",
            "preemptive_llm_result": {"reply": "A long reply.",
                                      "function_calls": []},
        })
        self._real_cb(message)

    def _states_set(self):
        return [c.args[0] for c in module_main.set_tars_state.call_args_list]


class TestStopAndListen(StopAndListenTestCase):
    def test_captured_audio_feeds_round(self):
        """Captured audio -> the round handler is invoked with the
        transcribed text, no wake word / beep flags in the payload."""
        audio = np.zeros(16000, dtype=np.int16)
        fake = FakeBargeinStt(audio=audio, transcript={"text": "tell me about x"})
        self._run_interrupted_turn(fake)

        # exactly one re-entry, pure {"text": ...} payload
        self.assertEqual(1, len(self.round_calls))
        payload = json.loads(self.round_calls[0])
        self.assertEqual({"text": "tell me about x"}, payload)

        # monitor consulted, then stopped AFTER the take
        self.assertIn("take", fake.call_names())
        self.assertEqual(1, fake.call_names().count("stop"))
        self.assertLess(fake.call_names().index("take"),
                        fake.call_names().index("stop"))

        # the interrupted round returned early: the re-entered round owns
        # the LISTENING transition, not this frame
        self.assertNotIn(TarsState.LISTENING, self._states_set())

    def test_no_capture_falls_back(self):
        """No capture (veto) -> the handler is NOT called; the monitor is
        consulted and released; existing LISTENING fallback preserved."""
        fake = FakeBargeinStt(audio=None, transcript=None)
        self._run_interrupted_turn(fake)

        self.assertEqual([], self.round_calls)
        # the wiring must still consult the monitor (waited, got None)
        self.assertIn("take", fake.call_names())
        self.assertIn("stop", fake.call_names())
        self.assertIn(TarsState.LISTENING, self._states_set())

    def test_kill_switch_off_never_takes(self):
        """bargein_listen_after_interrupt = False -> take_bargein_utterance
        is never called; existing behavior (LISTENING, wake word)."""
        with mock.patch.dict(module_main.CONFIG["STT"],
                             {"bargein_listen_after_interrupt": False}):
            fake = FakeBargeinStt(audio=np.zeros(16000, dtype=np.int16),
                                  transcript={"text": "should not be used"})
            self._run_interrupted_turn(fake)

        self.assertEqual([], self.round_calls)
        self.assertNotIn("take", fake.call_names())
        self.assertIn(TarsState.LISTENING, self._states_set())


class _CallbackCaptureRunner:
    """Captures the TurnCallbacks handed to start_turn (started=True) so
    the REAL _speak_followup can be driven directly in the test — the
    deferred follow-up speaks through callbacks.speak on the tool loop's
    turn thread in production."""

    def __init__(self):
        self.callbacks = None

    def start_turn(self, parsed, user_text, source, callbacks):
        self.callbacks = callbacks
        return True


class TestFollowupStopAndListen(StopAndListenTestCase):
    """Review F2: an interrupted filler/deferred follow-up must consume
    the captured utterance — same stop-and-listen wiring as the main
    round — instead of silently dropping it (and leaking a stale capture
    into a later round via the never-cleared ready Event)."""

    def _run_and_get_speak(self, fake_stt):
        saved = module_main.stt_manager
        module_main.stt_manager = fake_stt
        self.addCleanup(setattr, module_main, "stt_manager", saved)
        runner = _CallbackCaptureRunner()
        p = mock.patch.object(module_toolloop, "get_loop_runner",
                              return_value=runner)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(module_toolloop, "fire_and_forget_calls",
                              return_value=[])
        p.start()
        self.addCleanup(p.stop)
        message = json.dumps({
            "text": "tell me something long",
            "preemptive_llm_result": {"reply": "A long reply.",
                                      "function_calls": []},
        })
        self._real_cb(message)
        self.assertIsNotNone(runner.callbacks,
                             "start_turn never received the callbacks — "
                             "_speak_followup was not built")
        return runner.callbacks.speak

    def test_interrupted_followup_consumes_capture(self):
        """The real _speak_followup, interrupted: the captured utterance is
        taken, transcribed, and re-enters the round entry point as a pure
        {"text": ...} payload — exactly the main-round wiring."""
        audio = np.zeros(16000, dtype=np.int16)
        # take #1 = the main reply round (no capture there); take #2 = the
        # interrupted follow-up -> the captured audio
        fake = FakeBargeinStt(take_results=[None, audio],
                              transcript={"text": "tell me about x"})
        speak = self._run_and_get_speak(fake)

        speak("Here is the follow-up.")

        # exactly one re-entry, pure {"text": ...} payload
        self.assertEqual(1, len(self.round_calls))
        self.assertEqual({"text": "tell me about x"},
                         json.loads(self.round_calls[0]))
        # the follow-up consulted the monitor: take BEFORE stop
        names = fake.call_names()
        self.assertEqual(2, names.count("start"))
        self.assertEqual(2, names.count("take"))
        self.assertEqual(2, names.count("stop"))
        self.assertLess(names.index("take"), names.index("stop"))

    def test_followup_stops_own_run_id(self):
        """Audit F1: every stop names ITS OWN run id — the main round's
        stop (run 1) must not tear down the follow-up's monitor (run 2):
        each start/stop pair is matched, never crossed."""
        audio = np.zeros(16000, dtype=np.int16)
        fake = FakeBargeinStt(take_results=[None, audio],
                              transcript={"text": "tell me about x"})
        speak = self._run_and_get_speak(fake)

        speak("Here is the follow-up.")

        start_ids = [c[2] for c in fake.calls if c[0] == "start"]
        stop_ids = [c[1] for c in fake.calls if c[0] == "stop"]
        self.assertEqual([1, 2], start_ids)
        self.assertEqual([1, 2], stop_ids,
                         "each stop must name its own run id — a stop "
                         "naming None (or the other run's id) can tear "
                         "down a monitor it does not own")

    def test_followup_kill_switch_off_never_takes(self):
        """bargein_listen_after_interrupt = False: the interrupted follow-up
        never takes — no capture consumed, no re-entry, LISTENING."""
        with mock.patch.dict(module_main.CONFIG["STT"],
                             {"bargein_listen_after_interrupt": False}):
            fake = FakeBargeinStt(take_results=[],
                                  transcript={"text": "should not be used"})
            speak = self._run_and_get_speak(fake)
            speak("Here is the follow-up.")

        self.assertEqual([], self.round_calls)
        self.assertNotIn("take", fake.call_names())
        self.assertIn(TarsState.LISTENING, self._states_set())


if __name__ == "__main__":
    unittest.main()
