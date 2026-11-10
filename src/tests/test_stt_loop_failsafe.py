"""STT loop fail-safe tests (S-M1-11, S-M1-12).

Covers:
  - S-M1-11: `_stt_processing_loop` wraps each iteration in try/except with
    exponential backoff (0.5 s doubling, cap 5 s, reset on success) — the STT
    thread must survive repeated exceptions instead of dying permanently.
  - S-M1-12: `_detect_wake_word_atomik` catches `AudioStallError` from the
    bounded listener and escalates to L2 (`restart_sound_server`), rate-limited
    to once per 60 s via the STTManager-scoped `_last_l2_mono`.

All device access is stubbed; these tests run on macOS with no hardware.
"""
import threading
import time
from unittest import mock

import numpy as np

from modules import module_stt
from modules.module_mic import AudioStallError


def _minimal_manager(**attrs):
    """STTManager without running __init__ (no models, no mic, no UI)."""
    mgr = module_stt.STTManager.__new__(module_stt.STTManager)
    mgr.running = True
    mgr.shutdown_event = threading.Event()
    mgr.DEBUG = False
    mgr.sherpa_vad = None
    mgr.is_paused = lambda: False
    mgr.WAKE_WORD = "hey tar"
    mgr.MODEL_RATE = 16000
    mgr._last_l2_mono = 0.0        # mirrors STTManager.__init__ (S-M1-12)
    for k, v in attrs.items():
        setattr(mgr, k, v)
    return mgr


# ---------------------------------------------------------------------------
# S-M1-11 — processing loop fail-safe
# ---------------------------------------------------------------------------

class TestSttLoopFailSafe:

    def test_loop_survives_exceptions(self, monkeypatch):
        """`_detect_wake_word` raising twice must NOT kill the loop: it logs,
        backs off, and keeps calling until the stub ends the loop."""
        mgr = _minimal_manager()
        calls = {"n": 0}
        sleeps = []

        def fake_detect():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("boom")
            mgr.running = False
            return False

        monkeypatch.setattr(mgr, "_detect_wake_word", fake_detect)
        # Backoff is observed via the interruptible wait, not time.sleep
        # (M2/F4: the loop must not sleep blind through shutdown).
        monkeypatch.setattr(mgr.shutdown_event, "wait",
                            lambda timeout: (sleeps.append(timeout), False)[1])
        with mock.patch.object(module_stt, "queue_message") as qmsg:
            t = threading.Thread(target=mgr._stt_processing_loop, daemon=True)
            t.start()
            t.join(5.0)

        assert not t.is_alive(), "STT loop thread died on an exception"
        assert calls["n"] == 3, "loop must retry after exceptions, got %d calls" % calls["n"]
        assert sleeps == [0.5, 1.0], "exponential backoff 0.5s doubling, got %s" % sleeps
        errors = [c.args[0] for c in qmsg.call_args_list
                  if c.args and str(c.args[0]).startswith("ERROR: STT loop iteration failed")]
        assert len(errors) == 2, "each exception must be logged, got %s" % errors

    def test_backoff_caps_at_5s(self, monkeypatch):
        """Backoff doubles 0.5 -> 1 -> 2 -> 4 -> 5 (cap) -> 5 ..."""
        mgr = _minimal_manager()
        calls = {"n": 0}
        sleeps = []

        def fake_detect():
            calls["n"] += 1
            if calls["n"] >= 7:
                mgr.running = False
                return False
            raise RuntimeError("boom")

        monkeypatch.setattr(mgr, "_detect_wake_word", fake_detect)
        # Backoff is observed via the interruptible wait, not time.sleep
        # (M2/F4: the loop must not sleep blind through shutdown).
        monkeypatch.setattr(mgr.shutdown_event, "wait",
                            lambda timeout: (sleeps.append(timeout), False)[1])
        with mock.patch.object(module_stt, "queue_message"):
            mgr._stt_processing_loop()          # run inline; loop must terminate

        assert sleeps == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0], sleeps


# ---------------------------------------------------------------------------
# S-M1-12 — atomik stall -> L2 escalation (rate-limited 60 s, instance-scoped)
# ---------------------------------------------------------------------------

class _StallDetector:
    """Fake WakeWordSystem whose listener always stalls (S-M1-10 contract)."""

    def __init__(self, *args, **kwargs):
        pass

    def createModel(self):
        pass

    def listenForWakeWord(self):
        raise AudioStallError("dual-signal stall: no chunk/check advance")

    def last_window(self, seconds=2.0):
        return np.zeros(16000, dtype=np.float32)


class TestAtomikStallEscalatesToL2:

    @staticmethod
    def _patch_clock(monkeypatch, start=1000.0):
        """Controlled monotonic clock. The rate limit compares against uptime
        (time.monotonic), which on the robot is >> 60 s by the time a stall
        happens — but near 0 in a fresh test process, which would make the
        FIRST L2 look rate-limited. Start the fake clock past the limit."""
        clock = {"now": start}
        monkeypatch.setattr(module_stt.time, "monotonic", lambda: clock["now"])
        return clock

    def test_atomik_stall_escalates_to_l2(self, monkeypatch):
        """AudioStallError from the bounded listener -> restart_sound_server
        called once, returns False (processing loop retries with backoff)."""
        mgr = _minimal_manager()
        self._patch_clock(monkeypatch)
        monkeypatch.setattr(module_stt, "WakeWordSystem", _StallDetector)
        monkeypatch.setattr(module_stt, "is_tts_playing", lambda: False)
        import modules.module_audio_recovery as recovery
        with mock.patch.object(recovery, "restart_sound_server") as restart, \
             mock.patch.object(module_stt, "queue_message"):
            result = mgr._detect_wake_word_atomik()

        assert result is False
        assert restart.call_count == 1

    def test_l2_rate_limited_across_calls(self, monkeypatch):
        """Two stalls within 60 s -> exactly ONE restart (instance-scoped
        `_last_l2_mono`); after 60 s the limit re-arms -> second restart."""
        mgr = _minimal_manager()
        clock = self._patch_clock(monkeypatch)
        monkeypatch.setattr(module_stt, "WakeWordSystem", _StallDetector)
        monkeypatch.setattr(module_stt, "is_tts_playing", lambda: False)
        import modules.module_audio_recovery as recovery
        with mock.patch.object(recovery, "restart_sound_server") as restart, \
             mock.patch.object(module_stt, "queue_message") as qmsg:
            assert mgr._detect_wake_word_atomik() is False      # fresh L2
            assert restart.call_count == 1
            clock["now"] += 5.0                                 # still within 60 s
            assert mgr._detect_wake_word_atomik() is False
            assert restart.call_count == 1, "L2 must be rate-limited to 1/60s"
            skipped = [c.args[0] for c in qmsg.call_args_list
                       if c.args and "rate-limited" in str(c.args[0])]
            assert skipped, "the skipped restart must be logged"

            # Re-arm: pretend the last L2 was 61 s ago.
            mgr._last_l2_mono = clock["now"] - 61.0
            assert mgr._detect_wake_word_atomik() is False
            assert restart.call_count == 2

    def test_stall_skips_l2_when_recovery_in_progress(self, monkeypatch):
        """F1: the STT catch consults audio_hub_stats()['recovery_in_progress']
        before calling restart_sound_server — if a recovery is already in
        flight (hub watchdog L2 / TTS), it must not even attempt."""
        mgr = _minimal_manager()
        self._patch_clock(monkeypatch)
        monkeypatch.setattr(module_stt, "WakeWordSystem", _StallDetector)
        monkeypatch.setattr(module_stt, "is_tts_playing", lambda: False)
        import modules.module_mic as mic
        monkeypatch.setattr(mic, "audio_hub_stats",
                            lambda: {"recovery_in_progress": True})
        import modules.module_audio_recovery as recovery
        with mock.patch.object(recovery, "restart_sound_server") as restart, \
             mock.patch.object(module_stt, "queue_message") as qmsg:
            result = mgr._detect_wake_word_atomik()

        assert result is False
        restart.assert_not_called()          # not even attempted
        skipped = [c.args[0] for c in qmsg.call_args_list
                   if c.args and "already in progress" in str(c.args[0])]
        assert skipped, "the skipped L2 must be logged"
        # The 60 s rate-limit anchor is NOT stamped when skipping — the next
        # stall re-checks instead of waiting a full 60 s for nothing.
        assert mgr._last_l2_mono == 0.0


# ---------------------------------------------------------------------------
# F4 — backoff must reset after ANY exception-free iteration
# ---------------------------------------------------------------------------

class TestBackoffResetsOnCleanIteration:

    def test_backoff_resets_after_clean_no_detection_iteration(self, monkeypatch):
        """F4: a clean iteration that detects nothing must also reset the
        backoff, otherwise idle listening (nothing detected) keeps the
        backoff ratcheted at the 5 s cap after one transient error.
        raise -> clean False -> raise: the second sleep must be 0.5, not 1.0."""
        mgr = _minimal_manager()
        calls = {"n": 0}
        sleeps = []

        def fake_detect():
            calls["n"] += 1
            if calls["n"] in (1, 3):
                raise RuntimeError("boom")
            if calls["n"] >= 4:
                mgr.running = False
            return False        # clean iteration, no wake word detected

        monkeypatch.setattr(mgr, "_detect_wake_word", fake_detect)
        # Backoff is observed via the interruptible wait, not time.sleep
        # (M2/F4: the loop must not sleep blind through shutdown).
        monkeypatch.setattr(mgr.shutdown_event, "wait",
                            lambda timeout: (sleeps.append(timeout), False)[1])
        with mock.patch.object(module_stt, "queue_message"):
            mgr._stt_processing_loop()      # run inline; loop must terminate

        assert sleeps == [0.5, 0.5], (
            "backoff must reset after the clean iteration (second sleep 0.5, "
            "not 1.0), got %s" % sleeps)


class TestSttBackoffInterruptible:
    """M2 review F4: the backoff sleep must not delay shutdown by up to 5 s.
    `_stt_processing_loop` runs on a daemon thread that `stop()` joins with a
    3 s timeout — an uninterruptible 5 s sleep means the join times out and
    the thread outlives stop()."""

    def test_backoff_breaks_on_shutdown_event(self):
        mgr = _minimal_manager()
        calls = {"n": 0}

        def fake_detect():
            calls["n"] += 1
            mgr.running = False        # bound the loop: one iteration only
            raise RuntimeError("boom")  # force the backoff path

        def fake_wait(timeout):
            mgr.shutdown_event.set()
            return True                 # "the event became set"

        with mock.patch.object(mgr, "_detect_wake_word", fake_detect), \
             mock.patch.object(mgr.shutdown_event, "wait", fake_wait), \
             mock.patch.object(module_stt.time, "sleep") as sleep_mock:
            mgr._stt_processing_loop()

        assert calls["n"] == 1, "must not retry after shutdown was signalled"
        # RED before the fix: the loop used time.sleep(backoff), a blind wait
        # that ignores shutdown_event entirely.
        sleep_mock.assert_not_called()


class TestL2AnchorSentinel:
    """Audit B/F2: `_last_l2_mono = 0.0` is a false "last L2" anchor — on a
    fresh process (uptime < 60 s) `now - 0.0 >= 60` is False, so the STT-side
    L2 escalation is silently dead for the first minute of uptime, which is
    exactly when a servo power-on spike wedges the USB card."""

    def test_first_stall_early_in_uptime_escalates(self, monkeypatch):
        mgr = _minimal_manager()
        mgr._last_l2_mono = None          # None sentinel = never issued
        clock = {"now": 30.0}             # only 30 s of uptime
        monkeypatch.setattr(module_stt.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(module_stt, "WakeWordSystem", _StallDetector)
        monkeypatch.setattr(module_stt, "is_tts_playing", lambda: False)
        import modules.module_audio_recovery as recovery
        with mock.patch.object(recovery, "restart_sound_server") as restart, \
             mock.patch.object(module_stt, "queue_message"):
            result = mgr._detect_wake_word_atomik()
        assert result is False
        assert restart.call_count == 1, "first stall at 30 s uptime must escalate"
        assert mgr._last_l2_mono == 30.0
