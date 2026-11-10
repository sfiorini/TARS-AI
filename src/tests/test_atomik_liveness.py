"""Atomik wake-word liveness tests (S-M1-08..S-M1-10).

Covers the three liveness fixes on the atomik detect() path:
  - S-M1-08: monotonic clock (no wall-clock latch), VAD gate on the NEWEST
    ~64 ms of the window, and the _check_count/_last_exit/_last_window_rms
    progress counters consumed by the bounded listener.
  - S-M1-09: chunk-deque buffer + _window()/last_window() accessors.
  - S-M1-10: bounded listenForWakeWord (check-advance deadline, L1 loop,
    AudioStallError).

All device access is stubbed; these tests run on macOS with no hardware.
"""
import time

import numpy as np
import pytest

import modules.module_atomik as atomik
import modules.module_mic as module_mic
from modules.module_atomik import WakeWordSystem
from modules.module_mic import AudioStallError


def _template_detector():
    """Minimal template-mode detector with one stub (all-zero) template."""
    det = WakeWordSystem(mode=WakeWordSystem.MODE_TEMPLATE)
    det.templates.append(np.zeros((13, 50), dtype=np.float32))
    return det


def _loud_slice(n=1024):
    """~64 ms of speech-amplitude audio (rms ~0.21 >> 0.008)."""
    t = np.arange(n) / 16000.0
    return (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


def _append_chunk(det, arr):
    """Append one chunk to the detector's chunk-deque buffer the way the
    RT callback does (S-M1-09)."""
    det.buffer.append(np.asarray(arr, dtype=np.float32))
    det._buffered_samples += len(arr)


# ---------------------------------------------------------------------------
# S-M1-08 — monotonic clock + VAD slice + progress counters
# ---------------------------------------------------------------------------

def test_vad_slice_uses_newest_samples(monkeypatch):
    """The VAD gate must judge the NEWEST ~64 ms (window tail), not the
    ~1 s-old head. 1 s of silence followed by loud speech at the tail must
    stamp a nonzero _last_window_rms even when the VAD gate itself passes."""
    det = _template_detector()
    _append_chunk(det, np.zeros(16000, dtype=np.float32))
    _append_chunk(det, _loud_slice(1024))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)

    det._detect_template()

    # Old code gated on audio_window[:1024] (the zeros) and returned before
    # stamping rms; the tail slice is loud, so rms must be well above the
    # VAD energy threshold.
    assert det._last_window_rms > 0.008


def test_monotonic_no_clock_latch(monkeypatch):
    """A backward wall-clock (NTP) step must not latch detection off.
    detect() must read time.monotonic(), never time.time()."""
    det = _template_detector()
    _append_chunk(det, np.zeros(16000, dtype=np.float32))

    # Wall clock jumps backward between reads (NTP step): 100 -> 50.
    wall = {"calls": 0}

    def fake_time():
        wall["calls"] += 1
        return 100.0 if wall["calls"] == 1 else 50.0

    monkeypatch.setattr(time, "time", fake_time)

    # A detection 10 ms ago in monotonic time. Ruling: the story pins
    # cooldown=1.5 by default, which would correctly suppress a retrigger
    # 10 ms after a detection — so cooldown is zeroed here to make the
    # story's own assertion (_last_exit != "cooldown") satisfiable.
    det.cooldown = 0.0
    det.last_detection_time = time.monotonic() - 0.01

    before = det._check_count
    det.detect()
    det.detect()

    assert det._check_count == before + 2
    assert det._last_exit != "cooldown"


def test_counters_advance(monkeypatch):
    """Every detect() call increments _check_count before any gate and
    stamps _last_exit with the reason it exited."""
    det = _template_detector()
    _append_chunk(det, np.zeros(16000, dtype=np.float32))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)

    before = det._check_count
    det.detect()

    assert det._check_count == before + 1
    # Silence: gates pass (constant monotonic bypasses cooldown/interval),
    # the VAD gate on the silent slice rejects -> "vad".
    assert det._last_exit == "vad"
    assert det._last_window_rms == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Audit F2 — a raising detect pass must freeze _check_count
# ---------------------------------------------------------------------------

class _ExplodingModel:
    """Stand-in for a corrupt ONNX model: predict() raises on every call."""

    def predict(self, feat):
        raise RuntimeError("corrupt model")


def test_detect_model_throwing_predict_freezes_check_count(monkeypatch):
    """F2: a detect pass that RAISES must not advance _check_count (the
    listenForWakeWord/hub liveness signal) — otherwise a permanently
    broken detector looks alive and the L1/L2 ladder never trips. The
    raise counts into _check_error_count instead."""
    det = WakeWordSystem(mode=WakeWordSystem.MODE_MODEL)
    det.model = _ExplodingModel()
    _append_chunk(det, _loud_slice(16000))     # 1 s of loud speech-band audio
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)

    before = det._check_count
    with pytest.raises(RuntimeError):
        det.detect()

    assert det._check_count == before          # frozen despite the raise
    assert det._check_error_count == 1


def test_detect_model_normal_false_still_advances_check_count():
    """F2 counterpart: a normal (non-raising) False pass still advances
    _check_count and does not touch _check_error_count."""
    det = WakeWordSystem(mode=WakeWordSystem.MODE_MODEL)
    det.model = None                            # early (False, 0.0) return

    before = det._check_count
    detected, score = det.detect()

    assert detected is False
    assert det._check_count == before + 1
    assert det._check_error_count == 0


def test_detect_template_throwing_freezes_check_count(monkeypatch):
    """F2 applies to the template path too: a raising _detect_template pass
    must freeze _check_count and count into _check_error_count."""
    det = _template_detector()
    _append_chunk(det, _loud_slice(16000))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(det.mfcc_extractor, "extract_mfcc",
                        lambda w: (_ for _ in ()).throw(RuntimeError("mfcc boom")))

    before = det._check_count
    with pytest.raises(RuntimeError):
        det.detect()

    assert det._check_count == before
    assert det._check_error_count == 1


def test_listen_escalates_when_detect_raises_every_chunk(monkeypatch):
    """F2 end-to-end: chunks keep arriving but every detect() raises (the
    hub swallows it like _on_audio does). _check_count must stay frozen so
    the stall deadline expires and listenForWakeWord escalates to
    AudioStallError — instead of resetting the deadline forever."""
    det = WakeWordSystem(mode=WakeWordSystem.MODE_MODEL)
    det.model = _ExplodingModel()
    det.cooldown = 0.0          # let every pass reach the throwing predict()
    det.check_interval = 0.0    # (the deadline itself uses real monotonic time)
    state = {"enters": 0, "callback": None}

    def deliver(state_):
        cb = state_["callback"]
        if cb is not None:
            for _ in range(5):                 # 5 chunks delivered per registration
                try:
                    cb(_loud_slice(16000), 16000, None, None)
                except Exception:
                    pass                       # _on_audio swallows consumer raises

    monkeypatch.setattr(atomik, "open_native_stream", _fake_open_factory(state, deliver))
    monkeypatch.setattr(atomik, "make_resampling_callback", lambda cb: cb)
    counter = {"chunk_count": 0}

    def fake_stats():
        counter["chunk_count"] += 1
        return counter

    invalidated = []
    _patch_hub(monkeypatch, fake_stats, invalidated)
    frozen_check = det._check_count

    with pytest.raises(AudioStallError):
        det.listenForWakeWord(stall_timeout=0.2, max_recoveries=1)

    assert det._check_count == frozen_check   # throwing detector froze it
    assert det._check_error_count >= 1
    assert state["enters"] == 1


# ---------------------------------------------------------------------------
# Audit F9 — model mode must stamp _last_window_rms
# ---------------------------------------------------------------------------

class _StubModel:
    """Stand-in ONNX model: predict() always returns a below-threshold score."""

    def predict(self, feat):
        return 0.0


def test_detect_model_stamps_window_rms(monkeypatch):
    """F9: the model-mode detect path computes the window rms but never
    stored it, so the hub watchdog's rms= field and the WAKE stall log
    always read 0.0 in model mode — misleading the digital-silence vs
    detect-stalled discrimination. After a model-mode detect() pass on
    non-zero audio, _last_window_rms must be > 0."""
    det = WakeWordSystem(mode=WakeWordSystem.MODE_MODEL)
    det.model = _StubModel()
    _append_chunk(det, _loud_slice(16000))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)

    detected, score = det.detect()

    assert detected is False
    assert det._last_window_rms > 0.0


# ---------------------------------------------------------------------------
# Audit F10 — cooldown/interval gates must run before the _window() build
# ---------------------------------------------------------------------------

def test_detect_model_cooldown_gate_skips_window(monkeypatch):
    """F10: in model mode the 1 s _window() build (~16k float32 concat)
    ran on EVERY RT callback before the cheap cooldown/interval early
    returns — a steady allocation stream on the real-time thread. With the
    cooldown active, detect() must exit at the cooldown gate WITHOUT
    calling _window (spy counter), and still advance _check_count (a
    normal False pass)."""
    det = WakeWordSystem(mode=WakeWordSystem.MODE_MODEL)
    det.model = _StubModel()
    _append_chunk(det, _loud_slice(16000))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    det.last_detection_time = 99.5      # 0.5 s ago — cooldown (1.5 s) active

    window_calls = []
    real_window = det._window

    def spy_window(seconds=1.0):
        window_calls.append(seconds)
        return real_window(seconds)

    det._window = spy_window

    before = det._check_count
    detected, score = det.detect()

    assert window_calls == []           # cooldown held -> no window build
    assert det._last_exit == "cooldown"
    assert detected is False
    assert det._check_count == before + 1  # normal False pass still advances


def test_detect_model_interval_gate_skips_window(monkeypatch):
    """F10 counterpart: the interval gate must also short-circuit before
    the _window() build."""
    det = WakeWordSystem(mode=WakeWordSystem.MODE_MODEL)
    det.model = _StubModel()
    _append_chunk(det, _loud_slice(16000))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    det.last_check_time = 99.95         # 0.05 s ago — interval (0.1 s) active

    window_calls = []
    real_window = det._window

    def spy_window(seconds=1.0):
        window_calls.append(seconds)
        return real_window(seconds)

    det._window = spy_window

    det.detect()

    assert window_calls == []
    assert det._last_exit == "interval"


# ---------------------------------------------------------------------------
# Audit B7 — template path: the cooldown/interval gates must also run
# before the _window() build (the robot runs atomik_mode = template, so
# this is the live RT-callback path).
# ---------------------------------------------------------------------------

def test_detect_template_cooldown_gate_skips_window(monkeypatch):
    """B7: in template mode the 1 s _window() build (~16k float32 concat)
    ran on EVERY RT callback before the cheap cooldown/interval early
    returns — a steady allocation stream on the real-time thread. With the
    cooldown active, detect() must exit at the cooldown gate WITHOUT
    calling _window (spy counter), and still advance _check_count (a
    normal False pass)."""
    det = _template_detector()
    _append_chunk(det, _loud_slice(16000))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    det.last_detection_time = 99.5      # 0.5 s ago — cooldown (1.5 s) active

    window_calls = []
    real_window = det._window

    def spy_window(seconds=1.0):
        window_calls.append(seconds)
        return real_window(seconds)

    det._window = spy_window

    before = det._check_count
    detected, score = det.detect()

    assert window_calls == []           # cooldown held -> no window build
    assert det._last_exit == "cooldown"
    assert detected is False
    assert det._check_count == before + 1  # normal False pass still advances


def test_detect_template_interval_gate_skips_window(monkeypatch):
    """B7 counterpart: the interval gate must also short-circuit before
    the _window() build in template mode."""
    det = _template_detector()
    _append_chunk(det, _loud_slice(16000))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    det.last_check_time = 99.95         # 0.05 s ago — interval (0.1 s) active

    window_calls = []
    real_window = det._window

    def spy_window(seconds=1.0):
        window_calls.append(seconds)
        return real_window(seconds)

    det._window = spy_window

    det.detect()

    assert window_calls == []
    assert det._last_exit == "interval"


def test_detect_template_non_gated_still_builds_window(monkeypatch):
    """B7: a non-gated call must behave identically to before — the
    window IS built exactly once and detection proceeds past the gates."""
    det = _template_detector()
    _append_chunk(det, _loud_slice(16000))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    det.cooldown = 0.0
    det.last_detection_time = 0.0       # no cooldown
    det.last_check_time = 0.0            # no interval gate

    window_calls = []
    real_window = det._window

    def spy_window(seconds=1.0):
        window_calls.append(seconds)
        return real_window(seconds)

    det._window = spy_window

    det.detect()

    assert window_calls == [1.0]         # exactly one 1 s window build
    assert det._last_exit not in ("cooldown", "interval")


# ---------------------------------------------------------------------------
# S-M1-09 — chunk-deque buffer + _window()/last_window() accessors
# ---------------------------------------------------------------------------

def test_window_returns_trailing_seconds():
    """_window(1.0) returns exactly the trailing 1 s of buffered audio."""
    det = _template_detector()
    for i in range(3):
        _append_chunk(det, np.full(16000, i + 1, dtype=np.float32))

    w = det._window(1.0)

    assert w is not None
    assert len(w) == 16000
    np.testing.assert_array_equal(w, np.full(16000, 3, dtype=np.float32))


def test_window_none_when_short():
    """Fewer than the requested seconds buffered -> None (gates hold)."""
    det = _template_detector()
    _append_chunk(det, np.zeros(320, dtype=np.float32))

    assert det._window(1.0) is None


def test_buffered_samples():
    """buffered_samples equals the sum of buffered chunk lengths."""
    det = _template_detector()
    _append_chunk(det, np.zeros(1000, dtype=np.float32))
    _append_chunk(det, np.zeros(500, dtype=np.float32))

    assert det.buffered_samples == 1500


def test_last_window_short_returns_all():
    """last_window(2.0) with <2 s buffered returns everything concatenated."""
    det = _template_detector()
    _append_chunk(det, np.full(16000, 7.0, dtype=np.float32))

    w = det.last_window(2.0)

    assert len(w) == 16000
    np.testing.assert_array_equal(w, np.full(16000, 7.0, dtype=np.float32))


def test_detect_works_with_chunk_buffer(monkeypatch):
    """Detection is NOT permanently deaf with a chunk-count len(buffer):
    100 chunks of 320 samples (len(buffer) == 100, far below the old
    16000-sample gate) must still reach the similarity comparison."""
    det = _template_detector()
    t = np.arange(320 * 100) / 16000.0
    speech = (0.1 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    for i in range(100):
        _append_chunk(det, speech[i * 320:(i + 1) * 320])
    assert len(det.buffer) == 100

    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(det.vad, "is_speech", lambda w: True)

    before = det._check_count
    detected, confidence = det.detect()

    # The stub template (all zeros) has zero norm -> similarity 0.0 -> the
    # detect path ran to the similarity comparison and exited there.
    assert det._check_count == before + 1
    assert det._last_exit == "below-threshold"
    assert detected is False


# ---------------------------------------------------------------------------
# S-M1-10 — bounded listenForWakeWord (check-advance deadline + L1 + raise)
# ---------------------------------------------------------------------------

class _FakeStream:
    """Context-manager stand-in for the shared callback stream. `on_enter`
    runs inside __enter__ (used to simulate a revived hub delivering a
    chunk through the captured callback)."""

    def __init__(self, state, on_enter=None):
        self._state = state
        self._on_enter = on_enter

    def __enter__(self):
        self._state["enters"] += 1
        if self._on_enter is not None:
            self._on_enter(self._state)
        return self

    def __exit__(self, *exc):
        return False


def _fake_open_factory(state, on_enter=None):
    def fake_open_native_stream(**kwargs):
        state["callback"] = kwargs.get("callback")
        return _FakeStream(state, on_enter)
    return fake_open_native_stream


def _patch_hub(monkeypatch, stats_fn, invalidated):
    monkeypatch.setattr(module_mic, "audio_hub_stats", stats_fn)
    monkeypatch.setattr(module_mic, "invalidate_audio_hub", lambda: invalidated.append(1))


def test_listen_raises_after_stalls(monkeypatch):
    """No chunks ever arrive -> each registration stalls -> L1 loop
    re-registers max_recoveries times, then raises AudioStallError."""
    det = _template_detector()
    state = {"enters": 0, "callback": None}
    monkeypatch.setattr(atomik, "open_native_stream", _fake_open_factory(state))
    monkeypatch.setattr(atomik, "make_resampling_callback", lambda cb: cb)
    invalidated = []
    _patch_hub(monkeypatch, lambda: {"chunk_count": 0}, invalidated)

    with pytest.raises(AudioStallError):
        det.listenForWakeWord(stall_timeout=0.2, max_recoveries=3)

    assert state["enters"] == 3          # 3 L1 re-registers
    assert len(invalidated) == 3          # hub invalidated before each retry


def test_listen_stalls_when_chunks_advance_but_detect_frozen(monkeypatch):
    """Mode-(c) discriminator: hub chunk_count advances every poll while
    detect() never runs (_check_count frozen) — chunk-advance alone must
    NOT reset the stall deadline; the listener must escalate."""
    det = _template_detector()
    state = {"enters": 0, "callback": None}
    monkeypatch.setattr(atomik, "open_native_stream", _fake_open_factory(state))
    monkeypatch.setattr(atomik, "make_resampling_callback", lambda cb: cb)
    counter = {"chunk_count": 0}

    def fake_stats():
        counter["chunk_count"] += 1     # chunks keep advancing...
        return counter

    invalidated = []
    _patch_hub(monkeypatch, fake_stats, invalidated)
    frozen_check = det._check_count      # ...but detect() never runs

    with pytest.raises(AudioStallError):
        det.listenForWakeWord(stall_timeout=0.2, max_recoveries=1)

    assert det._check_count == frozen_check
    assert state["enters"] == 1


def test_listen_recovers_when_hub_revives(monkeypatch):
    """The hub starts delivering after the first re-register: the captured
    callback is invoked with a chunk, detect() advances _check_count and
    returns True -> listenForWakeWord returns True with exactly 2
    registrations."""
    det = _template_detector()
    state = {"enters": 0, "callback": None}

    def revive(state_):
        if state_["enters"] >= 2 and state_["callback"] is not None:
            state_["callback"](np.zeros(512, dtype=np.float32), 512, None, None)

    monkeypatch.setattr(atomik, "open_native_stream", _fake_open_factory(state, revive))
    monkeypatch.setattr(atomik, "make_resampling_callback", lambda cb: cb)
    counter = {"chunk_count": 0}

    def fake_stats():
        counter["chunk_count"] += 1
        return counter

    invalidated = []
    _patch_hub(monkeypatch, fake_stats, invalidated)

    def fake_detect():
        det._check_count += 1             # advances the check counter...
        return True, 0.9                  # ...and detects

    det.detect = fake_detect

    assert det.listenForWakeWord(stall_timeout=0.2, max_recoveries=3) is True
    assert state["enters"] == 2
    assert len(invalidated) == 1


def test_listen_publishes_detector_to_watchdog(monkeypatch):
    """F2: while a listener is live it publishes itself to the hub
    watchdog (check counter + window RMS for the mode-(c) discriminator)
    and unpublishes on every exit path — including the AudioStallError
    raise."""
    det = _template_detector()
    state = {"enters": 0, "callback": None}
    monkeypatch.setattr(atomik, "open_native_stream", _fake_open_factory(state))
    monkeypatch.setattr(atomik, "make_resampling_callback", lambda cb: cb)
    invalidated = []
    _patch_hub(monkeypatch, lambda: {"chunk_count": 0}, invalidated)
    published = []
    monkeypatch.setattr(module_mic, "publish_listener_diagnostics",
                        lambda d: published.append(d))

    with pytest.raises(AudioStallError):
        det.listenForWakeWord(stall_timeout=0.1, max_recoveries=1)

    assert published[0] is det            # published at listen entry
    assert published[-1] is None          # cleared on the raise path
    assert module_mic._active_listener is None


def test_stall_log_reports_current_chunk_count(monkeypatch):
    """F3: in mode (c) (chunks advance, _check_count frozen) the WAKE stall
    line must report the CURRENT hub chunk count at the moment of the stall
    decision — not the stale value captured when the check counter last
    advanced. Otherwise mode (c) is indistinguishable from mode (a) in the
    log."""
    from unittest import mock

    det = _template_detector()
    state = {"enters": 0, "callback": None}
    monkeypatch.setattr(atomik, "open_native_stream", _fake_open_factory(state))
    monkeypatch.setattr(atomik, "make_resampling_callback", lambda cb: cb)
    counter = {"chunk_count": 0}

    def fake_stats():
        counter["chunk_count"] += 1     # chunks keep advancing (mode c)...
        return counter

    invalidated = []
    _patch_hub(monkeypatch, fake_stats, invalidated)
    frozen_check = det._check_count      # ...but detect() never runs

    with mock.patch.object(atomik, "queue_message") as qmsg:
        with pytest.raises(AudioStallError):
            det.listenForWakeWord(stall_timeout=0.2, max_recoveries=1)

    assert det._check_count == frozen_check
    stalls = [c.args[0] for c in qmsg.call_args_list
              if c.args and str(c.args[0]).startswith("WARNING: WAKE stall")]
    assert len(stalls) == 1, stalls
    current = counter["chunk_count"]
    assert f"hub_chunk={current}" in stalls[0], (
        f"stall line must report the current chunk count ({current}), got: {stalls[0]}")
    # And it must not be the stale first-poll value (mode c keeps advancing).
    assert "hub_chunk=1}" not in stalls[0]
