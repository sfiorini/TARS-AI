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
import re
import threading
import time
from pathlib import Path
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

    def test_first_chunk_mono_set_once(self):
        """F6: _first_chunk_mono is stamped on the FIRST chunk only — the
        age of the never-nonzero silence case is measured from it."""
        hub = _AudioHub()
        hub._on_audio(_chunk(), 512, None, None)
        first = hub._first_chunk_mono
        assert first > 0, "first chunk must stamp _first_chunk_mono"
        hub._on_audio(_chunk(), 512, None, None)
        assert hub._first_chunk_mono == first   # set once, never restamped

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
    "stream_opened_age_s", "first_chunk_age_s",
    "consumer_exc_count", "consecutive_read_stalls",
    "stream_open", "stream_active", "consumers", "readers",
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

    def test_first_chunk_age_s_exposed(self):
        """F6: stats() exposes first_chunk_age_s — None before the first
        chunk ever arrives, then the age of the first chunk."""
        hub = _AudioHub()
        hub._ensure_stream = lambda: None
        assert hub.stats()["first_chunk_age_s"] is None
        hub._first_chunk_mono = 100.0
        with mock.patch.object(module_mic.time, "monotonic", lambda: 130.0):
            assert hub.stats()["first_chunk_age_s"] == 30.0

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


class TestEnsureStreamCounterAwareHealth:
    """F2: the health check consults the S-M1-1 counters — a stream that is
    active+not-stopped but delivering nothing is unhealthy and must be
    recreated; a fresh stream must not be churned."""

    def test_recent_chunk_reuses_stream(self):
        hub = _AudioHub()
        healthy = mock.MagicMock()
        healthy.active = True
        healthy.stopped = False
        hub._stream = healthy
        hub._chunk_count = 5
        hub._last_chunk_mono = time.monotonic()      # delivered just now
        _FakeInputStream.instances = []
        with mock.patch.object(module_mic.sd, "InputStream", _FakeInputStream):
            hub._ensure_stream()
        assert hub._stream is healthy
        assert _FakeInputStream.instances == []       # no churn on healthy path

    def test_stale_chunk_recreates_stream(self):
        hub = _AudioHub()
        silent = mock.MagicMock()                     # active but silent — the
        silent.active = True                          # exact stall this feature targets
        silent.stopped = False
        hub._stream = silent
        hub._chunk_count = 5
        hub._last_chunk_mono = time.monotonic() - 3600.0   # long-stalled
        _FakeInputStream.instances = []
        with mock.patch.object(module_mic, "get_device_info", return_value=(0, 16000)), \
             mock.patch.object(module_mic.sd, "InputStream", _FakeInputStream), \
             mock.patch.object(module_mic.time, "sleep"):
            hub._ensure_stream()
        assert len(_FakeInputStream.instances) == 1   # silent stream reopened
        assert hub._stream is _FakeInputStream.instances[0]
        silent.stop.assert_called_once()
        silent.close.assert_called_once()

    def test_never_delivered_chunk_reuses_stream(self):
        hub = _AudioHub()
        fresh = mock.MagicMock()                      # just created — legitimately
        fresh.active = True                           # silent for a moment
        fresh.stopped = False
        hub._stream = fresh
        hub._chunk_count = 0                          # no chunk ever arrived
        hub._last_chunk_mono = 0.0
        _FakeInputStream.instances = []
        with mock.patch.object(module_mic.sd, "InputStream", _FakeInputStream):
            hub._ensure_stream()
        assert hub._stream is fresh
        assert _FakeInputStream.instances == []        # no thrash on fresh stream

    def test_freshly_recreated_stream_reuses_stream(self):
        # After L1 recovery the cumulative _chunk_count is >0 and the stale
        # _last_chunk_mono belongs to the OLD stream — the NEW stream must
        # still get its silence grace window, or recovery thrashes.
        hub = _AudioHub()
        fresh = mock.MagicMock()
        fresh.active = True
        fresh.stopped = False
        hub._stream = fresh
        hub._chunk_count = 5                          # chunks from the old stream
        hub._last_chunk_mono = time.monotonic() - 3600.0
        hub._stream_opened_mono = time.monotonic()    # new stream, just opened
        _FakeInputStream.instances = []
        with mock.patch.object(module_mic.sd, "InputStream", _FakeInputStream):
            hub._ensure_stream()
        assert hub._stream is fresh
        assert _FakeInputStream.instances == []        # no recreate churn


class TestRequestL1Recovery:
    """S-M1-05: AudioStallError + request_l1_recovery (rate-limited reopen)."""

    def test_audio_stall_error_is_runtime_error(self):
        from modules.module_mic import AudioStallError
        assert issubclass(AudioStallError, RuntimeError)

    def test_request_l1_recovery_rate_limited(self):
        hub = _AudioHub()
        calls = {"invalidate": 0, "ensure": 0}

        def fake_invalidate():
            calls["invalidate"] += 1

        def fake_ensure():
            calls["ensure"] += 1

        with mock.patch.object(hub, "invalidate", side_effect=fake_invalidate), \
             mock.patch.object(hub, "_ensure_stream", side_effect=fake_ensure):
            first = hub.request_l1_recovery()
            second = hub.request_l1_recovery()
        assert first is True
        assert second is False                    # rate limit: 1 per 10 s
        assert calls["invalidate"] == 1
        assert calls["ensure"] == 1

    def test_request_l1_recovery_no_deadlock_real_invalidate(self):
        # invalidate() takes _lock itself; request_l1_recovery must NOT hold
        # _lock across the invalidate/_ensure_stream calls (would deadlock).
        hub = _AudioHub()
        hub._stream = mock.MagicMock()            # real invalidate closes it
        with mock.patch.object(hub, "_ensure_stream") as ensure:
            done = threading.Event()

            def run():
                assert hub.request_l1_recovery() is True
                done.set()

            t = threading.Thread(target=run, daemon=True)
            t.start()
            assert done.wait(5.0), "request_l1_recovery deadlocked on _lock"
            assert hub._stream is None            # invalidate really ran
            assert ensure.called

    def test_request_l1_recovery_returns_false_on_ensure_failure(self):
        hub = _AudioHub()
        with mock.patch.object(hub, "invalidate"), \
             mock.patch.object(hub, "_ensure_stream", side_effect=RuntimeError("boom")), \
             mock.patch.object(module_mic, "queue_message") as qm:
            assert hub.request_l1_recovery() is False
            assert qm.called                      # failure logged, not raised


class _FakeClock:
    """Controllable monotonic clock injected into module_mic.time."""

    def __init__(self, start=1000.0):
        self.now = start

    def advance(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


def _wd_reset(clock):
    """Fresh watchdog state (None sentinels — never backdate l1/l2 in tests)."""
    module_mic._wd_state = {"heartbeat": 0.0, "l1": None, "l2": None,
                            "invalidate": clock.monotonic()}


def _wd_hub(clock, stale=True, digital_silence=False):
    """Stubbed hub swapped in as module_mic._hub for watchdog tests."""
    hub = _AudioHub()
    hub._ensure_stream = lambda: None          # never open a real stream
    hub.register_callback(lambda chunk: None, name="test")
    hub._stream = mock.MagicMock(active=True)  # stream_open/stream_active True
    hub._chunk_count = 1   # real invariant: _last_chunk_mono > 0 implies a
                           # chunk was delivered (_on_audio stamps both)
    hub._last_chunk_mono = clock.now - (10.0 if stale else 0.0)
    hub._last_nonzero_mono = clock.now - (61.0 if digital_silence
                                          else (10.0 if stale else 0.0))
    return hub


def _wd_boot_hub(clock, opened_age_s, chunk_age=None):
    """F1 boot-window hub: stream opened (stamped _stream_opened_mono) but
    the first PortAudio callback has not fired yet (_last_chunk_mono 0.0,
    chunk_count 0). With chunk_age given, simulates a stream WITH chunk
    history (the pre-F1 stale contract must be preserved for it)."""
    hub = _AudioHub()
    hub._ensure_stream = lambda: None          # never open a real stream
    hub.register_callback(lambda chunk: None, name="test")
    hub._stream = mock.MagicMock(active=True)  # stream_open/stream_active True
    hub._stream_opened_mono = clock.now - opened_age_s
    if chunk_age is None:
        hub._chunk_count = 0
        hub._last_chunk_mono = 0.0     # no callback has fired yet
        hub._last_nonzero_mono = 0.0
    else:
        hub._chunk_count = 5
        hub._last_chunk_mono = clock.now - chunk_age
        hub._last_nonzero_mono = clock.now - chunk_age
    return hub


class TestHubWatchdogLadder:
    """S-M1-07: escalation ladder L1 -> (10 s) -> L2 -> (60 s) -> L2 repeat."""

    def test_watchdog_first_stale_is_l1_not_l2(self):
        clock = _FakeClock()
        hub = _wd_hub(clock)
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_called_once()
        l2.assert_not_called()
        assert module_mic._wd_state["l1"] == clock.now
        assert module_mic._wd_state["l2"] is None   # None-sentinel contract

    def test_watchdog_ladder_reaches_l2(self):
        clock = _FakeClock()
        hub = _wd_hub(clock)
        _wd_reset(clock)
        fired_at = []
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server",
                        side_effect=lambda: fired_at.append(clock.now)) as l2:
            for _ in range(6):                     # t=1002..1012, 2 s ticks
                clock.advance(2)
                module_mic._hub_watchdog_tick()
        assert l1_req.call_count == 1              # L1 fired at t=1002 only
        assert l2.call_count == 1                  # L2 reached — not stuck at L1
        assert fired_at == [1012.0]                 # exactly 10 s after the L1 stamp
        assert module_mic._wd_state["l2"] == 1012.0

    def test_watchdog_ladder_resets_after_recovery(self):
        clock = _FakeClock()
        hub = _wd_hub(clock)
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()         # stale -> L1
            assert l1_req.call_count == 1
            # Recover: fresh chunks -> healthy tick clears the ladder
            hub._last_chunk_mono = clock.now
            hub._last_nonzero_mono = clock.now
            clock.advance(2)
            module_mic._hub_watchdog_tick()
            assert module_mic._wd_state["l1"] is None
            assert module_mic._wd_state["l2"] is None
            # Long healthy gap, then a NEW stale episode: must get a fresh
            # cheap L1 — never an immediate L2 off a backdated l1 stamp.
            clock.advance(30)
            hub._last_chunk_mono = clock.now - 10
            hub._last_nonzero_mono = clock.now - 10
            module_mic._hub_watchdog_tick()
            assert l1_req.call_count == 2          # FRESH L1
            l2.assert_not_called()
        assert module_mic._wd_state["l2"] is None

    def test_watchdog_l2_repeat_60s_cadence(self):
        clock = _FakeClock()
        hub = _wd_hub(clock)
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            for _ in range(6):                     # drive to L2 (t=1012)
                clock.advance(2)
                module_mic._hub_watchdog_tick()
            assert l2.call_count == 1
            clock.advance(58)                       # 58 s after L2 — too early
            module_mic._hub_watchdog_tick()
            assert l2.call_count == 1               # no early repeat
            clock.advance(2)                        # exactly 60 s after L2
            module_mic._hub_watchdog_tick()
            assert l2.call_count == 2               # 60 s cadence honoured
            assert module_mic._wd_state["l2"] == clock.now

    def test_watchdog_digital_silence_escalates_l2(self):
        clock = _FakeClock()
        # Fresh chunks but 61 s of digital silence — the silence signal must
        # enter the ladder on its own, independent of the chunk-age signal.
        hub = _wd_hub(clock, stale=False, digital_silence=True)
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            for _ in range(6):                     # L1 at t=1002, L2 at t=1012
                clock.advance(2)
                module_mic._hub_watchdog_tick()
        assert l1_req.call_count == 1              # L1 DESPITE fresh chunk age
        assert l2.call_count == 1                  # silence alone reached L2

    def test_watchdog_no_consumers_noop(self):
        clock = _FakeClock()
        hub = _AudioHub()                          # zero consumers — no register
        hub._stream = mock.MagicMock(active=True)
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_not_called()
        l2.assert_not_called()

    def test_watchdog_stream_none_with_consumers_reopens(self):
        clock = _FakeClock()
        hub = _wd_hub(clock)
        hub._ensure_stream = mock.MagicMock()
        hub._stream = None                         # the invalidate() hole
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(3)                       # > 2 s since invalidate anchor
            module_mic._hub_watchdog_tick()
            assert hub._ensure_stream.call_count == 1
            assert module_mic._wd_state["invalidate"] == clock.now  # throttle advances
            module_mic._hub_watchdog_tick()         # immediate re-tick: throttled
            assert hub._ensure_stream.call_count == 1
        l1_req.assert_not_called()
        l2.assert_not_called()

    def test_watchdog_recovery_in_progress_noop(self):
        clock = _FakeClock()
        hub = _wd_hub(clock)
        hub._recovery_in_progress = True            # ~22 s L2 in flight
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_not_called()
        l2.assert_not_called()


class TestWatchdogBootGrace:
    """F1: "never delivered a chunk" is not "stopped delivering". A freshly
    opened stream gets the same grace _is_chunk_stream_healthy grants (no
    spurious L1 at boot), but past the grace window a stream that NEVER
    delivered is a real failure and must still be recovered — no permanent
    boot blindness. The _chunk_count > 0 contract is unchanged."""

    def test_boot_within_grace_no_l1(self):
        clock = _FakeClock()
        hub = _wd_boot_hub(clock, opened_age_s=1.0)   # opened 1 s ago
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_not_called()
        l2.assert_not_called()
        assert module_mic._wd_state["l1"] is None   # healthy tick reset ladder

    def test_boot_never_delivered_past_grace_fires_l1(self):
        clock = _FakeClock()
        hub = _wd_boot_hub(clock, opened_age_s=11.0)  # > _STALE_CHUNK_PERIOD
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_called_once()
        l2.assert_not_called()

    def test_never_delivered_escalates_to_l2_not_l1_loop(self):
        """Regression: the boot grace must not make L2 unreachable.

        A device that opens but NEVER delivers is not recovered by reopening
        it. The escalation clock must survive the reopen (which resets the
        grace anchor) so L2 -- restart_sound_server, the rung that actually
        fixes a wedged device -- stays reachable instead of looping L1
        reopens forever."""
        clock = _FakeClock()
        hub = _wd_boot_hub(clock, opened_age_s=11.0)   # past grace -> stale
        _wd_reset(clock)

        def _reopen_resets_anchor():
            # _ensure_stream() stamps _stream_opened_mono = now on every reopen
            hub._stream_opened_mono = clock.monotonic()

        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery",
                               side_effect=_reopen_resets_anchor) as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            for _ in range(8):          # 8 x 2 s = 16 s > the 10 s L1->L2 window
                module_mic._hub_watchdog_tick()
                clock.advance(2)

        assert l1_req.call_count == 1, "L1 must not storm while escalating"
        l2.assert_called_once()

    def test_within_grace_does_not_clear_stamped_ladder(self):
        """A tick that is merely within the boot grace (no chunks yet) is NOT
        evidence of recovery -- it must not wipe an in-flight escalation."""
        clock = _FakeClock()
        hub = _wd_boot_hub(clock, opened_age_s=1.0)    # within grace
        _wd_reset(clock)
        module_mic._wd_state["l1"] = clock.now - 5.0   # mid-escalation
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        assert module_mic._wd_state["l1"] is not None
        l1_req.assert_not_called()
        l2.assert_not_called()

    def test_delivered_healthy_clears_ladder(self):
        """Genuine delivery DOES clear the ladder -- a healthy stream must
        never be re-escalated from stale state."""
        clock = _FakeClock()
        hub = _wd_boot_hub(clock, opened_age_s=60.0, chunk_age=1.0)
        _wd_reset(clock)
        module_mic._wd_state["l1"] = clock.now - 3.0
        module_mic._wd_state["l2"] = clock.now - 3.0
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        assert module_mic._wd_state["l1"] is None
        assert module_mic._wd_state["l2"] is None
        l1_req.assert_not_called()
        l2.assert_not_called()

    def test_delivered_recent_chunk_no_l1(self):
        clock = _FakeClock()
        hub = _wd_boot_hub(clock, opened_age_s=60.0, chunk_age=1.0)
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_not_called()
        l2.assert_not_called()

    def test_delivered_stale_30s_fires_l1(self):
        clock = _FakeClock()
        hub = _wd_boot_hub(clock, opened_age_s=60.0, chunk_age=30.0)
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_called_once()
        l2.assert_not_called()
class TestDigitalSilenceFirstChunk:
    """F6: a device delivering all-zero chunks from the VERY first chunk
    keeps _last_nonzero_mono at 0 -> stats() last_nonzero_age_s is None.
    That "never non-zero" case must obey the SAME >= 60 s threshold as the
    sustained case (docs: ">= 60 s of all-zero audio") — a muted or
    echo-cancelled source legitimately yields exact-zero float32 during
    silence, and the first ~2 s of it are not a dead digital path.
    Zero chunks at all is the stale rule's business, never the silence
    guard's."""

    def test_all_zero_from_first_chunk_under_60s_no_escalation(self):
        clock = _FakeClock()
        hub = _wd_hub(clock, stale=False)       # chunks arrive (age fresh)
        hub._chunk_count = 100
        hub._last_nonzero_mono = 0.0            # never a non-zero chunk -> nz None
        hub._first_chunk_mono = clock.now - 30.0   # 30 s of all-zero audio
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_not_called()             # < 60 s: not yet digital silence
        l2.assert_not_called()

    def test_all_zero_from_first_chunk_60s_escalates(self):
        clock = _FakeClock()
        hub = _wd_hub(clock, stale=False)       # chunks arrive (age fresh)
        hub._chunk_count = 100
        hub._last_nonzero_mono = 0.0            # never a non-zero chunk -> nz None
        hub._first_chunk_mono = clock.now - 61.0   # >= 60 s of all-zero audio
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server"), \
             mock.patch.object(module_mic, "queue_message") as qm:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_called_once()            # the silence guard fired
        assert "mode=digital-silence" in qm.call_args.args[0]

    def test_zero_chunks_is_stale_not_digital_silence(self):
        clock = _FakeClock()
        hub = _wd_hub(clock, stale=False)
        hub._chunk_count = 0
        hub._last_chunk_mono = 0.0             # no chunk ever -> age None
        hub._last_nonzero_mono = 0.0           # nz None
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server"), \
             mock.patch.object(module_mic, "queue_message") as qm:
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        l1_req.assert_called_once()            # stale rule (age None) — by design
        assert "mode=callbacks-stopped" in qm.call_args.args[0]   # NOT silence


class _FakeDetector:
    """Stand-in for the published live wake listener: the watchdog reads
    _check_count (RT single-writer) and _last_window_rms from it."""

    def __init__(self, check_count=0, rms=0.0):
        self._check_count = check_count
        self._last_window_rms = rms


class TestWatchdogDiscriminatorLogging:
    """F2: the heartbeat and STALE lines must carry the fields that
    distinguish the four failure modes on the next real hang:
    healthy: rd, rate, last_nonzero, check_rate, exc, stalls
    stale: mode= (callbacks-stopped | detect-stalled | digital-silence)
           + chunk_age, check_rate, rms, exc
    """

    def _hub_full(self, clock, chunk_age=0.01, nz_age=0.4, exc=0):
        """Hub with 2 callbacks (atomik, spectrum) + 1 reader, stream open."""
        hub = _AudioHub()
        hub._ensure_stream = lambda: None
        hub.register_callback(lambda chunk: None, name="atomik")
        hub.register_callback(lambda chunk: None, name="spectrum")
        hub.register_reader()
        hub._stream = mock.MagicMock(active=True)
        # Real invariant: _on_audio stamps _chunk_count and _last_chunk_mono
        # together, so a fresh chunk age with chunk_count == 0 is impossible.
        # The boot-grace rule keys off chunk_count == 0, so the fixture must
        # model a hub that HAS delivered (63/s over 2 s => 126 per tick).
        hub._chunk_count = 1000
        hub._stream_opened_mono = clock.now - 60.0
        hub._first_chunk_mono = clock.now - 60.0
        hub._last_chunk_mono = clock.now - chunk_age
        hub._last_nonzero_mono = clock.now - nz_age
        hub._consumer_exc_count = exc
        return hub

    def _seed_prev(self, clock, hub, det, check_delta, chunk_delta=126):
        """Seed the previous-tick anchors so the NEXT tick (after a 2 s
        clock advance) computes rates over exactly 2 s."""
        module_mic._wd_state.update({
            "prev_time": clock.now,
            "prev_chunks": hub._chunk_count - chunk_delta,
            "prev_check": None if det is None else det._check_count - check_delta,
        })

    @staticmethod
    def _stamp(hub, clock, chunk_age, nz_age):
        """Re-stamp the hub's mono anchors relative to the (advanced) clock."""
        hub._last_chunk_mono = clock.now - chunk_age
        hub._last_nonzero_mono = clock.now - nz_age

    def test_heartbeat_includes_discriminator_fields(self):
        clock = _FakeClock()
        hub = self._hub_full(clock)
        det = _FakeDetector(check_count=1000, rms=0.013)
        _wd_reset(clock)
        self._seed_prev(clock, hub, det, check_delta=125)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server"), \
             mock.patch.object(module_mic, "queue_message") as qm:
            module_mic._active_listener = det
            try:
                clock.advance(2)
                self._stamp(hub, clock, 0.01, 0.4)
                module_mic._hub_watchdog_tick()
            finally:
                module_mic._active_listener = None
        assert qm.called, "healthy heartbeat must fire (anchor 0.0)"
        line = qm.call_args.args[0]
        for field in ("cb=2(atomik,spectrum)", "rd=1", "stream=open/active",
                      "rate=63.0/s", "last_chunk=0.01s", "last_nonzero=0.4s",
                      "check_rate=62.5/s", "exc=0", "stalls=0",
                      "state=HEALTHY"):
            assert field in line, f"heartbeat missing {field!r}: {line}"

    def test_stale_mode_callbacks_stopped(self):
        """Mode (a): chunk age grows, no detection progress."""
        clock = _FakeClock()
        hub = self._hub_full(clock, chunk_age=7.2, nz_age=7.2, exc=3)
        det = _FakeDetector(check_count=500, rms=0.013)   # frozen check
        _wd_reset(clock)
        self._seed_prev(clock, hub, det, check_delta=0)    # prev == now -> 0.0/s
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server"), \
             mock.patch.object(module_mic, "queue_message") as qm:
            module_mic._active_listener = det
            try:
                clock.advance(2)
                self._stamp(hub, clock, 7.2, 7.2)
                module_mic._hub_watchdog_tick()
            finally:
                module_mic._active_listener = None
        line = qm.call_args.args[0]
        for field in ("mode=callbacks-stopped", "chunk_age=7.2s",
                      "check_rate=0.0/s", "rms=0.013", "exc=3",
                      "cb=[atomik,spectrum]", "-> L1-reopen"):
            assert field in line, f"STALE line missing {field!r}: {line}"

    def test_stale_mode_digital_silence(self):
        """Mode (b): chunks fresh, non-zero audio age grows, rms ~ 0."""
        clock = _FakeClock()
        hub = self._hub_full(clock, chunk_age=0.01, nz_age=61.0, exc=0)
        det = _FakeDetector(check_count=500, rms=0.0)
        _wd_reset(clock)
        self._seed_prev(clock, hub, det, check_delta=0)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server"), \
             mock.patch.object(module_mic, "queue_message") as qm:
            module_mic._active_listener = det
            try:
                clock.advance(2)
                self._stamp(hub, clock, 0.01, 61.0)
                module_mic._hub_watchdog_tick()
            finally:
                module_mic._active_listener = None
        line = qm.call_args.args[0]
        assert "mode=digital-silence" in line
        assert "chunk_age=0.01s" in line          # chunks ARE fresh
        assert "check_rate=0.0/s" in line

    def test_stale_mode_detect_stalled(self):
        """Mode (c): chunks advance but the published listener's check
        counter is frozen ≥ 5 s — the detection path is stalled behind a
        healthy hub; the watchdog must emit mode=detect-stalled and
        escalate."""
        clock = _FakeClock()
        hub = self._hub_full(clock, chunk_age=0.01, nz_age=0.4, exc=2)
        det = _FakeDetector(check_count=500, rms=0.013)
        _wd_reset(clock)
        self._seed_prev(clock, hub, det, check_delta=0)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server"), \
             mock.patch.object(module_mic, "queue_message") as qm:
            module_mic._active_listener = det
            try:
                for _ in range(4):            # frozen 0 -> 2 -> 4 -> 6 s (≥ 5)
                    clock.advance(2)
                    hub._last_chunk_mono = clock.now - 0.01   # chunks keep arriving
                    hub._last_nonzero_mono = clock.now - 0.4
                    module_mic._hub_watchdog_tick()
            finally:
                module_mic._active_listener = None
        stale_lines = [c.args[0] for c in qm.call_args_list
                       if c.args and "STALE" in str(c.args[0])]
        assert stale_lines, "frozen check behind fresh chunks must go stale"
        line = stale_lines[-1]
        assert "mode=detect-stalled" in line
        assert "chunk_age=0.01s" in line          # chunks ARE advancing
        assert "check_rate=0.0/s" in line
        assert "rms=0.013" in line
        assert "exc=2" in line
        l1_req.assert_called()                    # mode (c) enters the ladder

    def test_healthy_when_check_advances(self):
        """Guard rail: an advancing check counter never goes stale."""
        clock = _FakeClock()
        hub = self._hub_full(clock)
        det = _FakeDetector(check_count=500)
        _wd_reset(clock)
        self._seed_prev(clock, hub, det, check_delta=0)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery") as l1_req, \
             mock.patch("modules.module_audio_recovery.restart_sound_server"), \
             mock.patch.object(module_mic, "queue_message"):
            module_mic._active_listener = det
            try:
                for _ in range(4):
                    clock.advance(2)
                    hub._last_chunk_mono = clock.now - 0.01
                    hub._last_nonzero_mono = clock.now - 0.4
                    det._check_count += 125               # detection alive
                    module_mic._hub_watchdog_tick()
            finally:
                module_mic._active_listener = None
        l1_req.assert_not_called()


class TestDocsQuoteEmittedShapes:
    """F2: the AUDIO-HUB example lines in docs/voice-pipeline.md must be
    structurally identical (numbers normalized) to what the watchdog
    actually emits — a cheap guard against doc/code drift."""

    @staticmethod
    def _normalize(line):
        return re.sub(r"\d+(?:\.\d+)?", "N", line)

    def test_docs_audio_hub_block_matches_emitted_lines(self):
        clock = _FakeClock()
        hub = TestWatchdogDiscriminatorLogging()._hub_full(clock)
        det = _FakeDetector(check_count=1000, rms=0.013)
        _wd_reset(clock)
        TestWatchdogDiscriminatorLogging()._seed_prev(clock, hub, det, check_delta=125)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(hub, "request_l1_recovery"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server"), \
             mock.patch.object(module_mic, "queue_message") as qm:
            module_mic._active_listener = det
            try:
                clock.advance(2)
                TestWatchdogDiscriminatorLogging()._stamp(hub, clock, 0.01, 0.4)
                module_mic._hub_watchdog_tick()          # healthy heartbeat
            finally:
                module_mic._active_listener = None
            healthy = qm.call_args.args[0]

        # stale scenario: chunks stale, frozen check, swallowed excs
        clock2 = _FakeClock()
        hub2 = TestWatchdogDiscriminatorLogging()._hub_full(
            clock2, chunk_age=7.2, nz_age=7.2, exc=3)
        det2 = _FakeDetector(check_count=500, rms=0.013)
        _wd_reset(clock2)
        TestWatchdogDiscriminatorLogging()._seed_prev(clock2, hub2, det2, check_delta=0)
        with mock.patch.object(module_mic.time, "monotonic", clock2.monotonic), \
             mock.patch.object(module_mic, "_hub", hub2), \
             mock.patch.object(hub2, "request_l1_recovery"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server"), \
             mock.patch.object(module_mic, "queue_message") as qm2:
            module_mic._active_listener = det2
            try:
                clock2.advance(2)
                TestWatchdogDiscriminatorLogging()._stamp(hub2, clock2, 7.2, 7.2)
                module_mic._hub_watchdog_tick()          # stale discriminator
            finally:
                module_mic._active_listener = None
            stale = qm2.call_args.args[0]

        docs = (Path(__file__).resolve().parents[2] / "docs" / "voice-pipeline.md").read_text()
        block = re.search(r"```(?:text)?\n(AUDIO-HUB:.*?)```", docs, re.S).group(1)
        doc_lines = block.strip().splitlines()
        # The block documents the two captured lines, plus the two escalation
        # rungs (L1 -> L2, and the repeating L2) so an operator can grep them.
        assert len(doc_lines) == 4, doc_lines
        assert [self._normalize(l) for l in doc_lines[:2]] == \
               [self._normalize(healthy), self._normalize(stale)]
        # The escalation lines must match the real format strings verbatim.
        assert doc_lines[2] == ("WARNING: AUDIO-HUB still stale "
                               "mode=callbacks-stopped 10s after L1 "
                               "-> L2 restart_sound_server"), doc_lines[2]
        assert doc_lines[3] == ("CRITICAL: AUDIO-HUB still stale "
                               "mode=callbacks-stopped 60s after L2 "
                               "\u2014 repeating L2 restart_sound_server"), doc_lines[3]
        # Guard the literals above against drift: both escalation lines must
        # still exist as format strings in the emitter, so this test fails if
        # the code's wording changes without the docs following.
        src = (Path(__file__).resolve().parents[1] / "modules" / "module_mic.py").read_text()
        assert "still stale mode={mode} 10s after L1 " in src
        assert "-> L2 restart_sound_server" in src
        assert "still stale mode={mode} 60s after L2 " in src
        assert "\u2014 repeating L2 restart_sound_server" in src


class TestAuditFixes:
    """Defects found by the dual-blind audit gate (A/F1, B/F1, A/F2)."""

    def test_closed_stream_reopen_failure_escalates_to_l2(self):
        """A/F1: rule 3 returned before the ladder, so a stream that could not
        be reopened looped _ensure_stream() forever and never reached L2 —
        the only rung that releases a wedged USB card. L2 must be reachable."""
        clock = _FakeClock()
        hub = _wd_hub(clock, stale=True)
        hub._stream = None                      # stream_open False
        hub._ensure_stream = mock.Mock(side_effect=RuntimeError("PaErrorCode -9985"))
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            for _ in range(12):                 # 12 x 2 s = 24 s > the 10 s window
                module_mic._hub_watchdog_tick()
                clock.advance(2)
        assert l2.called, "reopen failure must escalate to L2, not loop forever"

    def test_never_nonzero_silence_does_not_storm_l2(self):
        """B/F1: chunks arriving but never non-zero kept digital_silence True
        forever, so the ladder never reset and L2 repeated every 60 s — a
        ~22 s outage every 82 s on a robot that is actually working."""
        clock = _FakeClock()
        hub = _wd_hub(clock, stale=False)
        hub._chunk_count = 500
        hub._first_chunk_mono = clock.now - 120.0   # past the 60 s threshold
        hub._last_nonzero_mono = 0.0                # never non-zero -> nz None
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server") as l2:
            for _ in range(150):                # 150 x 2 s = 300 s
                module_mic._hub_watchdog_tick()
                clock.advance(2)
        assert l2.call_count <= 1, (
            "never-nonzero silence must not restart the sound server repeatedly "
            f"(got {l2.call_count} L2 restarts in 300 s)")

    def test_silence_storm_not_rearmed_by_post_l2_reopen(self):
        """B/F1 regression: restart_sound_server() ends with
        invalidate_audio_hub(), so after every L2 the stream is closed and the
        rule-3 branch reopens it. Clearing the ladder on that reopen re-armed
        the escalation and produced an L1->L2 restart every ~10 s. The ladder
        must only be cleared by genuine delivery."""
        clock = _FakeClock()
        hub = _wd_hub(clock, stale=False)
        hub._chunk_count = 500
        hub._first_chunk_mono = clock.now - 120.0
        hub._last_nonzero_mono = 0.0                # never non-zero
        _wd_reset(clock)
        # A real reopen must actually restore the stream, otherwise the tick
        # stays in rule 3 forever and never exercises the ladder at all.
        hub._ensure_stream = lambda: setattr(hub, "_stream", mock.MagicMock(active=True))

        def _restart_closes_stream():
            hub._stream = None                      # invalidate_audio_hub()

        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server",
                        side_effect=_restart_closes_stream) as l2:
            for _ in range(150):                    # 300 s
                module_mic._hub_watchdog_tick()
                clock.advance(2)

        assert l2.call_count <= 1, (
            "a post-L2 reopen must not re-arm the silence storm "
            f"(got {l2.call_count} restarts in 300 s)")

    def test_healthy_tick_resets_hub_l1_rate_limit(self):
        """A/F2: the watchdog cleared its own l1 anchor on a healthy tick but
        the hub's 10 s limiter kept running, so a re-stall within 10 s issued a
        phantom L1 (logged, never executed) and escalated to L2 instead."""
        clock = _FakeClock()
        hub = _wd_hub(clock, stale=False)
        hub._last_l1_mono = clock.now - 3.0         # hub still rate-limited
        _wd_reset(clock)
        with mock.patch.object(module_mic.time, "monotonic", clock.monotonic), \
             mock.patch.object(module_mic, "_hub", hub), \
             mock.patch.object(module_mic, "queue_message"), \
             mock.patch.object(hub, "request_l1_recovery"), \
             mock.patch("modules.module_audio_recovery.restart_sound_server"):
            clock.advance(2)
            module_mic._hub_watchdog_tick()
        assert hub._last_l1_mono is None, (
            "a healthy tick must re-arm the hub's L1 limiter so the next stall "
            "gets a real L1 instead of a phantom one")


class TestHubWatchdogStartStop:
    """S-M1-07: start/stop lifecycle (idempotent start, clean stop)."""

    def test_start_idempotent_and_stop_joins(self):
        module_mic._hub_watchdog_stop.set()         # any prior state cleared
        module_mic._hub_watchdog_thread = None

        def fake_sleep(s):
            module_mic._hub_watchdog_stop.wait(0.5)

        with mock.patch.object(module_mic.time, "sleep", fake_sleep), \
             mock.patch.object(module_mic, "_hub_watchdog_tick") as tick:
            module_mic.start_hub_watchdog(interval=2.0)
            t1 = module_mic._hub_watchdog_thread
            assert t1 is not None and t1.is_alive()
            module_mic.start_hub_watchdog(interval=2.0)   # second start: no-op
            assert module_mic._hub_watchdog_thread is t1
            module_mic.stop_hub_watchdog()
            t1.join(5.0)
            assert not t1.is_alive()

    def test_start_hub_watchdog_idempotent(self):
        """S-M1-13 regression guard: two starts, exactly one live daemon
        thread named `audio-hub-watchdog`; stop joins it."""
        module_mic._hub_watchdog_stop.set()         # any prior state cleared
        if module_mic._hub_watchdog_thread is not None:
            module_mic._hub_watchdog_thread.join(2.0)
        module_mic._hub_watchdog_thread = None
        try:
            with mock.patch.object(module_mic, "_hub_watchdog_tick"):
                module_mic.start_hub_watchdog(interval=0.05)
                t1 = module_mic._hub_watchdog_thread
                module_mic.start_hub_watchdog(interval=0.05)   # idempotent
                assert module_mic._hub_watchdog_thread is t1
                alive = [th for th in threading.enumerate()
                         if th.name == "audio-hub-watchdog"]
                assert len(alive) == 1
                module_mic.stop_hub_watchdog()
                t1.join(5.0)
                assert not t1.is_alive()
        finally:
            module_mic._hub_watchdog_thread = None
