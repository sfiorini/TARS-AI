"""M1 audio-hub liveness tests (S-M1-01 .. S-M1-07, S-M1-13).

Covers the single-writer RT counters in `_AudioHub._on_audio`, the `stats()`
snapshot, `register_callback(name=...)` threading, `read()` stall accounting,
the `_ensure_stream` health check + bounded retry, `AudioStallError` /
`request_l1_recovery`, the hub watchdog daemon ladder, and watchdog start
idempotency.

All tests are off-robot: sounddevice streams are never opened against real
hardware — `_ensure_stream` is stubbed or `sd.InputStream` is faked, and the
watchdog is driven tick-by-tick with a fake monotonic clock.
"""
import threading
import time
from unittest import mock

import numpy as np

from modules import module_mic
from modules.module_mic import _AudioHub


def _chunk(n=512, value=1.0):
    return np.full((n, 1), value, dtype=np.float32)


class TestOnAudioCounters:
    """S-M1-01: single-writer counters stamped by the RT callback."""

    def test_on_audio_counters_advance(self):
        hub = _AudioHub()
        hub._on_audio(_chunk(), 512, None, None)
        hub._on_audio(_chunk(), 512, None, None)
        assert hub._chunk_count == 2
        assert hub._last_chunk_mono > 0
        assert hub._last_nonzero_mono > 0

    def test_on_audio_silence_not_stamped(self):
        hub = _AudioHub()
        hub._on_audio(_chunk(value=1.0), 512, None, None)   # loud first
        before = hub._last_nonzero_mono
        hub._on_audio(np.zeros((512, 1), dtype=np.float32), 512, None, None)
        assert hub._last_nonzero_mono == before      # silence does not stamp
        assert hub._chunk_count == 2                 # but the chunk counter does

    def test_consumer_exception_counted(self):
        hub = _AudioHub()

        def raiser(chunk, frames, time_info, status):
            raise RuntimeError("boom")

        hub._callbacks[0] = raiser
        hub._rebuild_snapshots()
        hub._on_audio(_chunk(), 512, None, None)     # must not raise
        assert hub._consumer_exc_count == 1
