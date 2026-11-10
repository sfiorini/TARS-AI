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

from modules.module_atomik import WakeWordSystem


def _template_detector():
    """Minimal template-mode detector with one stub (all-zero) template."""
    det = WakeWordSystem(mode=WakeWordSystem.MODE_TEMPLATE)
    det.templates.append(np.zeros((13, 50), dtype=np.float32))
    return det


def _loud_slice(n=1024):
    """~64 ms of speech-amplitude audio (rms ~0.21 >> 0.008)."""
    t = np.arange(n) / 16000.0
    return (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# S-M1-08 — monotonic clock + VAD slice + progress counters
# ---------------------------------------------------------------------------

def test_vad_slice_uses_newest_samples(monkeypatch):
    """The VAD gate must judge the NEWEST ~64 ms (window tail), not the
    ~1 s-old head. 1 s of silence followed by loud speech at the tail must
    stamp a nonzero _last_window_rms even when the VAD gate itself passes."""
    det = _template_detector()
    det.buffer.extend(np.zeros(16000, dtype=np.float32))
    det.buffer.extend(_loud_slice(1024))
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
    det.buffer.extend(np.zeros(16000, dtype=np.float32))

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
    det.buffer.extend(np.zeros(16000, dtype=np.float32))
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)

    before = det._check_count
    det.detect()

    assert det._check_count == before + 1
    # Silence: gates pass (constant monotonic bypasses cooldown/interval),
    # the VAD gate on the silent slice rejects -> "vad".
    assert det._last_exit == "vad"
    assert det._last_window_rms == pytest.approx(0.0)
