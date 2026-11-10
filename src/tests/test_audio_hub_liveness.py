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


_STAT_KEYS = {
    "chunk_count", "last_chunk_age_s", "last_nonzero_age_s",
    "consumer_exc_count", "consecutive_read_stalls",
    "stream_open", "stream_active", "consumers",
    "callback_names", "recovery_in_progress",
}


class TestStatsSnapshot:
    """S-M1-02: stats() snapshot + register_callback(name=...)."""

    def test_stats_snapshot_keys(self):
        hub = _AudioHub()
        hub._ensure_stream = lambda: None
        hub.register_callback(lambda chunk: None, name="atomik")
        s = hub.stats()
        assert set(s.keys()) == _STAT_KEYS
        assert s["callback_names"] == ["atomik"]
        assert s["consumers"] == 1
        assert s["stream_open"] is False
        assert s["recovery_in_progress"] is False

    def test_unregister_removes_name(self):
        hub = _AudioHub()
        hub._ensure_stream = lambda: None
        rid = hub.register_callback(lambda chunk: None, name="atomik")
        assert hub.stats()["callback_names"] == ["atomik"]
        hub.unregister_callback(rid)
        assert hub.stats()["callback_names"] == []
        assert 0 not in hub._callback_names


class TestCallbackNameThreading:
    """S-M1-02b: name= threaded through open_native_stream/_SharedCallbackStream."""

    def test_callback_names_threaded(self):
        hub = _AudioHub()
        hub._ensure_stream = lambda: None
        with mock.patch.object(module_mic, "_hub", hub):
            s1 = module_mic._SharedCallbackStream(lambda chunk: None, name="atomik")
            s2 = module_mic._SharedCallbackStream(lambda chunk: None, name="spectrum")
            s1.start()
            s2.start()
            names = hub.stats()["callback_names"]
        assert names == ["atomik", "spectrum"]

    def test_unnamed_falls_back(self):
        hub = _AudioHub()
        hub._ensure_stream = lambda: None
        with mock.patch.object(module_mic, "_hub", hub):
            s3 = module_mic._SharedCallbackStream(lambda chunk: None)   # no name
            s3.start()
            names = hub.stats()["callback_names"]
        assert len(names) == 1
        assert names[0].startswith("cb-")

    def test_open_native_stream_forwards_name(self):
        hub = _AudioHub()
        hub._ensure_stream = lambda: None
        with mock.patch.object(module_mic, "_hub", hub):
            stream = module_mic.open_native_stream(callback=lambda chunk: None,
                                                    blocksize=512, name="atomik")
            with stream:
                pass
        assert hub.stats()["callback_names"] == []   # closed again
        # re-open and inspect while live
        with mock.patch.object(module_mic, "_hub", hub):
            with module_mic.open_native_stream(callback=lambda chunk: None,
                                                blocksize=512, name="atomik"):
                assert hub.stats()["callback_names"] == ["atomik"]


class TestReadStallAccounting:
    """S-M1-03: read() timeout increments the stall counter; data resets it."""

    def test_read_stall_counter(self):
        hub = _AudioHub()
        hub._ensure_stream = lambda: None
        rid = hub.register_reader()
        # Replace the reader's Event with one whose wait() returns False
        # immediately — no real 5 s block, no stream needed.
        buf, evt, dtype = hub._readers[rid]
        fake_evt = mock.MagicMock()
        fake_evt.wait.return_value = False
        hub._readers[rid] = (buf, fake_evt, dtype)

        data, overflow = hub.read(rid, 2000)
        assert overflow is True                      # silence-padded timeout
        assert data.shape == (2000, 1)
        assert hub.stats()["consecutive_read_stalls"] == 1

        # Feed one chunk -> the next read produces data and resets the counter
        buf.append(np.ones((2000, 1), dtype=np.float32))
        data, overflow = hub.read(rid, 2000)
        assert overflow is False
        assert data.shape == (2000, 1)
        assert hub.stats()["consecutive_read_stalls"] == 0


class _FakeInputStream:
    """Fake sd.InputStream recording instantiations and start() calls."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.active = True
        self.stopped = False
        _FakeInputStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.active = False

    def close(self):
        self.active = False


class TestEnsureStreamHealthRetry:
    """S-M1-04: health check + bounded retry, sleeping outside the lock."""

    def test_ensure_stream_recreates_stopped_stream(self):
        hub = _AudioHub()
        dead = mock.MagicMock()
        dead.active = False
        dead.stopped = False
        hub._stream = dead
        _FakeInputStream.instances = []
        with mock.patch.object(module_mic, "get_device_info", return_value=(0, 16000)), \
             mock.patch.object(module_mic.sd, "InputStream", _FakeInputStream), \
             mock.patch.object(module_mic.time, "sleep"):
            hub._ensure_stream()
        assert len(_FakeInputStream.instances) == 1      # a new stream was created
        assert _FakeInputStream.instances[0].started
        assert hub._stream is _FakeInputStream.instances[0]
        dead.stop.assert_called_once()
        dead.close.assert_called_once()

    def test_ensure_stream_reuses_healthy_stream(self):
        hub = _AudioHub()
        healthy = mock.MagicMock()
        healthy.active = True
        healthy.stopped = False
        hub._stream = healthy
        _FakeInputStream.instances = []
        with mock.patch.object(module_mic.sd, "InputStream", _FakeInputStream):
            hub._ensure_stream()
        assert hub._stream is healthy
        assert _FakeInputStream.instances == []           # no churn on the healthy path

    def test_ensure_stream_bounded_retry_raises(self):
        hub = _AudioHub()
        calls = []

        def failing_resolver():
            calls.append(1)
            raise RuntimeError("no device")

        with mock.patch.object(module_mic, "get_device_info", side_effect=failing_resolver), \
             mock.patch.object(module_mic.time, "sleep"):
            try:
                hub._ensure_stream()
            except RuntimeError as e:
                assert "no device" in str(e)
            else:
                raise AssertionError("_ensure_stream did not raise on exhaustion")
        assert len(calls) == 3                            # exactly 3 attempts
        assert hub._stream is None

    def test_ensure_stream_retry_sleeps_without_lock(self):
        hub = _AudioHub()
        lock_states = []

        def fake_sleep(s):
            # From the sleeping thread the hub lock must be FREE
            lock_states.append(hub._lock.acquire(blocking=False))
            if lock_states[-1]:
                hub._lock.release()

        with mock.patch.object(module_mic, "get_device_info", side_effect=RuntimeError("no device")), \
             mock.patch.object(module_mic.time, "sleep", side_effect=fake_sleep):
            with mock.patch.object(module_mic, "_hub", hub):
                try:
                    hub._ensure_stream()
                except RuntimeError:
                    pass
        assert lock_states and all(lock_states), "backoff slept while holding _lock"

    def test_register_callback_not_deadlocked_on_retry(self):
        hub = _AudioHub()
        entered_sleep = threading.Event()
        release = threading.Event()

        def fake_sleep(s):
            # PARK until the main thread releases us, so the register thread
            # is genuinely inside the backoff sleep (lock-free) while the
            # main thread inspects the hub.
            entered_sleep.set()
            release.wait(5.0)

        errors = []

        def register():
            try:
                hub.register_callback(lambda chunk: None)
            except Exception as e:
                errors.append(e)

        with mock.patch.object(module_mic, "get_device_info", side_effect=RuntimeError("no device")), \
             mock.patch.object(module_mic.time, "sleep", side_effect=fake_sleep):
            t = threading.Thread(target=register)
            t.start()
            assert entered_sleep.wait(5.0), "register never reached backoff sleep"
            # While the register thread is parked in backoff (outside the
            # lock), stats() must complete and see the registration.
            s = hub.stats()
            assert s["consumers"] == 1                  # registration visible
            release.set()
            t.join(5.0)
        assert errors and isinstance(errors[0], RuntimeError)
        # Rollback: the failed registration was removed after retries exhausted
        assert hub.stats()["consumers"] == 0
        assert hub._callbacks == {}
