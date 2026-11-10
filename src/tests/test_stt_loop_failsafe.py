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
        monkeypatch.setattr(module_stt.time, "sleep", lambda s: sleeps.append(s))
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
        monkeypatch.setattr(module_stt.time, "sleep", lambda s: sleeps.append(s))
        with mock.patch.object(module_stt, "queue_message"):
            mgr._stt_processing_loop()          # run inline; loop must terminate

        assert sleeps == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0], sleeps
