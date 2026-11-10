"""
module_mic.py — Shared microphone utilities.

Detects the default input device's native sample rate once, provides
resampling helpers, and a shared audio hub so every consumer can read
from the same physical mic without ALSA exclusion conflicts.

USB audio devices typically don't support ALSA dsnoop (shared capture),
so only one PortAudio stream can access the device at a time. The
_AudioHub singleton manages that single stream and dispatches audio to
all registered consumers (callback-based and read-based).
"""

import os
import time
import threading
import collections
import numpy as np
from math import gcd

# Prevent pygame/SDL from grabbing ALSA devices exclusively.
# TARS uses sounddevice (PortAudio) for all mic input — SDL audio is unused.
# This must be set before pygame.init() or sounddevice import triggers PA init.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import sounddevice as sd

from modules import module_audio_devices
from modules.module_messageQue import queue_message

try:
    import soxr as _soxr
    _HAS_SOXR = True
except ImportError:
    from scipy.signal import resample_poly as _resample_poly
    _HAS_SOXR = False

MODEL_RATE = 16000

# ── Singleton device info ──────────────────────────────────────────

_device_info = None  # cached (device_idx, native_rate) — only successful detections
_device_lock = threading.Lock()
_last_device_probe = 0.0  # time of the last FAILED detection attempt
_DEVICE_RETRY_INTERVAL = 5.0  # min seconds between failed-detection re-probes


def _find_input_device():
    """Try to find a working input device. Returns (idx, rate) or raises.

    Delegates the candidate scan to module_audio_devices.find_device
    (shared with module_tts): system default if real hardware, else pulse,
    else real hardware, else any input, else the PortAudio C default.
    Input candidates are never stream-validated — opening the mic would
    race the audio hub's exclusive InputStream.
    """
    idx, info, _label = module_audio_devices.find_device(kind="input")
    if idx is None:
        raise RuntimeError("No input audio device found")
    return idx, int(info.get("default_samplerate", MODEL_RATE))


def get_device_info(retries=4, backoff=1.0):
    """Return (device_idx, native_sample_rate), cached after first success.

    Retries with backoff to handle USB audio devices that aren't ready at boot.
    A failed detection is NOT cached forever: like servoctl's
    _try_lazy_pca_init, later calls re-probe (rate-limited to once per
    _DEVICE_RETRY_INTERVAL) so a mic that enumerates late is picked up
    without a reboot. Callers see (None, MODEL_RATE) while unresolved.
    """
    global _device_info, _last_device_probe
    if _device_info is not None:
        return _device_info

    with _device_lock:
        # Double-check under lock
        if _device_info is not None:
            return _device_info

        # A previous detection failed: retry at most once per interval instead
        # of spinning the whole retry ladder on every call (mirrors the 5s
        # lazy-init rate limit in module_servoctl).
        if _last_device_probe and time.time() - _last_device_probe < _DEVICE_RETRY_INTERVAL:
            return (None, MODEL_RATE)

        for attempt in range(retries):
            try:
                if attempt > 0:
                    sd._terminate()
                    sd._initialize()
                    time.sleep(backoff * attempt)

                idx, rate = _find_input_device()
                _device_info = (idx, rate)
                return _device_info
            except Exception as e:
                if attempt < retries - 1:
                    continue
                print(f"WARNING: Could not detect mic after {retries} attempts ({e}), using defaults — will retry later")
                _last_device_probe = time.time()
    return (None, MODEL_RATE)


def get_native_rate():
    """Return the default input device's native sample rate."""
    return get_device_info()[1]


def get_device_idx():
    """Return the default input device index (or None)."""
    return get_device_info()[0]


# ── Resampling helpers ─────────────────────────────────────────────

def resample(data, orig_sr, target_sr):
    """Resample audio from orig_sr to target_sr. No-op if rates match.

    Preserves dtype: int16 in -> int16 out, float in -> float32 out.
    """
    if orig_sr == target_sr:
        return data
    was_int = data.dtype == np.int16
    f = data.astype(np.float32) if was_int else np.asarray(data, dtype=np.float32)
    if _HAS_SOXR:
        out = _soxr.resample(f, orig_sr, target_sr)
    else:
        g = gcd(target_sr, orig_sr)
        out = _resample_poly(f, target_sr // g, orig_sr // g, axis=0)
    if was_int:
        return np.clip(out, -32768, 32767).astype(np.int16)
    return out.astype(np.float32)


def dev_frames(model_frames, native_rate=None):
    """Convert frame count from MODEL_RATE domain to native device domain."""
    if native_rate is None:
        native_rate = get_native_rate()
    if native_rate == MODEL_RATE:
        return model_frames
    return int(model_frames * native_rate / MODEL_RATE)


# ── Shared Audio Hub ───────────────────────────────────────────────
#
# One PortAudio stream, many consumers. Solves USB mic exclusion.
#

class AudioStallError(RuntimeError):
    """Raised by listenForWakeWord when the audio input path stops making
    progress after repeated L1 recoveries (dual-signal stall)."""


class _AudioHub:
    """Singleton that manages a single shared PortAudio input stream.

    Callback consumers register a function and receive raw audio at native rate.
    Read consumers get a thread-safe buffer they can pull chunks from.
    The stream starts when the first consumer arrives and stops 2s after
    the last consumer leaves (grace period avoids churn on USB devices).

    Thread safety: the PortAudio callback runs in a real-time thread and must
    not acquire locks or allocate memory. Consumer snapshots are atomically
    swapped (single pointer assignment under CPython's GIL) so the callback
    reads them lock-free.
    """

    _GRACE_PERIOD = 2.0  # seconds to keep stream alive after last consumer

    # No chunk for this long on an active stream => judged unhealthy (F2).
    # Generous: silent rooms still deliver (silent) chunks every callback
    # block (~10-30 ms), so this only trips when the callback stops firing.
    _STALE_CHUNK_PERIOD = 10.0

    def __init__(self):
        self._lock = threading.Lock()
        self._stream = None
        self._native_rate = None
        self._grace_timer = None

        # Mutable dicts — only mutated under self._lock
        self._callbacks = {}   # {id: callback_fn}
        self._readers = {}     # {id: (deque, Event, dtype)}

        # Atomic snapshots — read lock-free by the audio callback.
        # Replaced (single pointer swap) under self._lock after any mutation.
        self._cb_snapshot = ()           # tuple of callback_fn
        self._rd_snapshot = ()           # tuple of (buf, evt)

        self._next_id = 0

        # Single-writer counters — ONLY the RT callback thread writes these.
        # All other threads read them (GIL-atomic reads, no lock).
        self._stream_opened_mono = None  # when _ensure_stream last created a stream
        self._chunk_count = 0          # RT: += 1 per callback
        self._last_chunk_mono = 0.0    # RT: = time.monotonic()
        self._last_nonzero_mono = 0.0  # RT: = now if chunk.any()
        self._consumer_exc_count = 0   # RT: += 1 in the except branch
        self._consecutive_read_stalls = 0   # read() path (not RT)
        self._recovery_in_progress = False  # written under _lock by module_audio_recovery.restart_sound_server (reentrancy-guarded there); read atomically via stats()
        self._last_l1_mono = None  # L1 rate-limit anchor (None = never issued)
        self._callback_names = {}      # rid -> diagnostic name (S-M1-02)

    def _rebuild_snapshots(self):
        """Rebuild the lock-free snapshots. Must be called under self._lock."""
        self._cb_snapshot = tuple(self._callbacks.values())
        self._rd_snapshot = tuple(
            (buf, evt) for buf, evt, _dtype in self._readers.values()
        )

    def _on_audio(self, indata, frames, time_info, status):
        """Master callback — runs in PortAudio's real-time thread.

        No locks, no allocations beyond indata.copy(). Reads atomic snapshots.
        """
        # Single memcpy — required because PA reuses the indata buffer
        chunk = indata.copy()

        now = time.monotonic()
        self._chunk_count += 1
        self._last_chunk_mono = now
        if chunk.any():
            self._last_nonzero_mono = now

        # Dispatch to callback consumers
        for cb in self._cb_snapshot:
            try:
                cb(chunk, frames, time_info, status)
            except Exception:
                self._consumer_exc_count += 1

        # Buffer for read consumers (float32 only — int16 conversion in read())
        for buf, evt in self._rd_snapshot:
            buf.append(chunk)
            evt.set()

    def _stream_delivering(self):
        """Counter-aware liveness for the live stream (F2). Called UNDER _lock
        by _ensure_stream; reads the RT counters lock-free (GIL-atomic reads).

        True when the stream is delivering chunks per the S-M1-1 counters —
        an active-but-silent stream (active=True, stopped=False, no chunks)
        is the exact stall this feature targets and must be reopened.
        Conservative: a stream that has never delivered a chunk
        (_chunk_count == 0) or was just (re)created gets the benefit of the
        doubt — a fresh stream is legitimately silent until its first
        callback fires, and judging it stale would churn recreation.
        """
        if self._chunk_count == 0:
            return True
        anchor = self._last_chunk_mono
        if self._stream_opened_mono is not None:
            anchor = max(anchor, self._stream_opened_mono)
        return (time.monotonic() - anchor) < self._STALE_CHUNK_PERIOD

    def _ensure_stream(self):
        """Health check + bounded create. Callers must NOT hold _lock:
        this method acquires it per attempt and sleeps between attempts
        OUTSIDE the lock (failure-path backoff must not starve other threads)."""
        with self._lock:
            # Cancel any pending grace-period shutdown
            if self._grace_timer is not None:
                self._grace_timer.cancel()
                self._grace_timer = None
            if self._stream is not None:
                if (getattr(self._stream, "active", True)
                        and not getattr(self._stream, "stopped", False)
                        and self._stream_delivering()):
                    return                      # genuinely healthy — keep
                try:                            # dead/stopped/silent stream — recreate
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
        last_err = None
        for attempt in range(3):            # bounded retry, re-resolve device each attempt
            try:
                idx, rate = get_device_info()   # device may have re-enumerated
                kwargs = dict(samplerate=rate, channels=1, callback=self._on_audio)
                if idx is not None and idx >= 0:
                    kwargs["device"] = idx
                stream = sd.InputStream(**kwargs)
                stream.start()
                with self._lock:
                    if self._stream is None:
                        self._stream = stream
                        self._native_rate = rate
                        self._stream_opened_mono = time.monotonic()
                    else:                       # another thread won the race
                        try:
                            stream.stop(); stream.close()
                        except Exception:
                            pass
                return
            except Exception as e:
                last_err = e
            time.sleep(0.5 * (attempt + 1))     # OUTSIDE the lock
        raise last_err  # register_* rolls back (existing contract)

    def invalidate(self):
        """Stop and close the stream immediately; re-created lazily on next use.

        Called after PortAudio is re-initialized (sd._terminate/_initialize in
        TTS sound-server recovery): the live InputStream is dangling after that
        and must not be reused. Safe to call from any thread, with or without a
        live stream.
        """
        with self._lock:
            if self._grace_timer is not None:
                self._grace_timer.cancel()
                self._grace_timer = None
            if self._stream is not None:
                try:
                    self._stream.stop()
                except Exception:
                    pass
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

    def request_l1_recovery(self):
        """L1 rung of the shared escalation ladder: invalidate + reopen.

        Rate-limited to 1 per 10 s. Must NOT hold _lock: invalidate() takes
        the lock itself and _ensure_stream() acquires it per attempt (S-M1-04
        contract) — a plain Lock would deadlock.
        Returns True when the reopen succeeded, False when rate-limited or
        the reopen failed (caller escalates; never raises into the watchdog).
        """
        now = time.monotonic()
        # NOTE: None sentinel, not 0.0 — time.monotonic() is near 0.0 in a
        # fresh process on macOS, so a 0.0 anchor would wrongly rate-limit
        # the FIRST recovery for 10 s after boot.
        if self._last_l1_mono is not None and now - self._last_l1_mono < 10.0:
            return False
        self._last_l1_mono = now
        self.invalidate()                    # takes _lock internally
        try:
            self._ensure_stream()            # health check + bounded retry, lock-free call
            return True
        except Exception as e:
            queue_message(f"WARNING: audio hub L1 reopen failed: {e}")
            return False

    def _maybe_stop(self):
        """Schedule stream shutdown if no consumers remain. Called under lock."""
        if self._callbacks or self._readers:
            return
        # Delay shutdown to avoid USB device open/close churn
        if self._grace_timer is not None:
            self._grace_timer.cancel()
        self._grace_timer = threading.Timer(self._GRACE_PERIOD, self._shutdown)
        self._grace_timer.daemon = True
        self._grace_timer.start()

    def _shutdown(self):
        """Actually stop the stream (runs from grace timer thread)."""
        with self._lock:
            # Re-check: a new consumer may have arrived during grace period
            if self._callbacks or self._readers:
                return
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self._grace_timer = None

    @property
    def native_rate(self):
        if self._native_rate is None:
            self._native_rate = get_native_rate()
        return self._native_rate

    def stats(self) -> dict:
        """Lock-free-ish hub stats snapshot for supervision layers.

        Counter reads are GIL-atomic; `_lock` is taken only for the
        consumer-count/name reads. `last_*_age_s` is None before the first
        chunk ever arrives (callers must treat None as "no data yet").
        """
        now = time.monotonic()
        with self._lock:
            consumers = len(self._callbacks) + len(self._readers)
            names = [self._callback_names[r] for r in self._callbacks]
        stream = self._stream
        return {
            "chunk_count": self._chunk_count,
            "last_chunk_age_s": (now - self._last_chunk_mono) if self._last_chunk_mono else None,
            "last_nonzero_age_s": (now - self._last_nonzero_mono) if self._last_nonzero_mono else None,
            "stream_opened_age_s": (now - self._stream_opened_mono) if self._stream_opened_mono else None,
            "consumer_exc_count": self._consumer_exc_count,
            "consecutive_read_stalls": self._consecutive_read_stalls,
            "stream_open": stream is not None,
            "stream_active": bool(getattr(stream, "active", False)) if stream is not None else False,
            "consumers": consumers,
            "callback_names": names,
            "recovery_in_progress": self._recovery_in_progress,
        }

    # -- Callback consumer API --

    def register_callback(self, callback, name=None):
        """Register a callback consumer. Returns an ID for unregistering."""
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            self._callbacks[rid] = callback
            self._callback_names[rid] = name or f"cb-{rid}"
            self._rebuild_snapshots()
        try:
            self._ensure_stream()          # OUTSIDE the lock — may sleep up to ~3 s
        except Exception:
            with self._lock:
                self._callbacks.pop(rid, None)
                self._callback_names.pop(rid, None)
                self._rebuild_snapshots()
            raise
        return rid

    def unregister_callback(self, rid):
        """Remove a callback consumer by ID."""
        with self._lock:
            self._callbacks.pop(rid, None)
            self._callback_names.pop(rid, None)
            self._rebuild_snapshots()
            self._maybe_stop()

    # -- Read consumer API --

    def register_reader(self, dtype="float32"):
        """Register a read-based consumer. Returns an ID."""
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            self._readers[rid] = (collections.deque(maxlen=500), threading.Event(), dtype)
            self._rebuild_snapshots()
        try:
            self._ensure_stream()          # OUTSIDE the lock — may sleep up to ~3 s
        except Exception:
            with self._lock:
                self._readers.pop(rid, None)
                self._rebuild_snapshots()
            raise
        return rid

    def unregister_reader(self, rid):
        """Remove a read-based consumer by ID."""
        with self._lock:
            self._readers.pop(rid, None)
            self._rebuild_snapshots()
            self._maybe_stop()

    def read(self, rid, n_native_frames):
        """Read n_native_frames of audio for a reader. Blocks until available.

        Returns (data, overflow) where data has shape (-1, 1).
        Dtype conversion (float32 -> int16) happens here, not in the audio thread.
        """
        entry = self._readers.get(rid)
        if entry is None:
            raise RuntimeError("Reader not registered — call start() or use as context manager")
        buf, evt, dtype = entry

        collected = []
        remaining = n_native_frames

        while remaining > 0:
            # Drain available chunks
            while buf and remaining > 0:
                chunk = buf.popleft()
                flat = chunk.ravel()
                if len(flat) <= remaining:
                    collected.append(flat)
                    remaining -= len(flat)
                else:
                    # Take what we need, push rest back
                    collected.append(flat[:remaining])
                    leftover = flat[remaining:]
                    buf.appendleft(leftover.reshape(-1, 1))
                    remaining = 0

            if remaining > 0:
                # Clear BEFORE checking to avoid lost-wakeup race:
                # if callback fires between empty-check and clear(),
                # the set() would be lost.
                evt.clear()
                if buf:
                    continue  # data arrived between drain loop and clear
                if not evt.wait(timeout=5.0):
                    self._consecutive_read_stalls += 1
                    # Timeout — return what we have padded with silence
                    target_dtype = np.int16 if dtype == "int16" else np.float32
                    if collected:
                        audio = np.concatenate(collected)
                    else:
                        audio = np.zeros(n_native_frames, dtype=target_dtype)
                    if len(audio) < n_native_frames:
                        pad = np.zeros(n_native_frames - len(audio), dtype=audio.dtype)
                        audio = np.concatenate([audio, pad])
                    audio = audio[:n_native_frames]
                    if dtype == "int16":
                        audio = np.clip(audio * 32768, -32768, 32767).astype(np.int16)
                    return audio.reshape(-1, 1), True

        audio = np.concatenate(collected)[:n_native_frames]
        self._consecutive_read_stalls = 0   # data-producing return resets the streak

        # Convert to requested dtype (all buffered data is float32)
        if dtype == "int16":
            audio = np.clip(audio * 32768, -32768, 32767).astype(np.int16)

        return audio.reshape(-1, 1), False

    def flush_reader(self, rid, n=4, model_frames=2000):
        """Discard buffered audio for a reader (e.g. after TTS playback)."""
        entry = self._readers.get(rid)
        if entry is None:
            return
        buf, evt, _dtype = entry
        buf.clear()
        # Also consume ~0.5s of fresh audio to skip stale OS buffer
        total = dev_frames(model_frames, self.native_rate) * n
        consumed = 0
        deadline = time.monotonic() + 1.0
        while consumed < total and time.monotonic() < deadline:
            evt.clear()
            if buf:
                chunk = buf.popleft()
                consumed += chunk.size
            else:
                evt.wait(timeout=0.1)


# Global singleton
_hub = _AudioHub()


def invalidate_audio_hub():
    """Close the shared mic InputStream; re-created lazily on the next read.

    Called from module_tts._recover_sound_server after PortAudio re-init
    (sd._terminate/_initialize) — the hub's live InputStream is dangling after
    that and must not be reused, or the mic stays deaf until reboot.
    """
    _hub.invalidate()


def audio_hub_stats():
    """Lock-free-ish hub stats snapshot for STT/other consumers."""
    return _hub.stats()


# ── Hub watchdog daemon (S-M1-07) ─────────────────────────────────
#
# Always-on supervision for the shared input stream, covering the cases no
# listener sees: the invalidate() hole (stream closed with live consumers)
# and TTS-recovery deafness. Escalation ladder:
#   L1 (hub reopen, rate-limited 1/10 s) -> 10 s still stale ->
#   L2 (restart_sound_server, ~22 s) -> repeat L2 every 60 s.
#
# L1/L2 timestamps start as None sentinels so the FIRST stale tick can only
# take the L1 branch (a 0.0 init makes `now - 0.0 >= 60.0` true on any robot
# up >60 s, escalating straight to L2 while logging "-> L1-reopen").

_wd_state = {"heartbeat": 0.0, "l1": None, "l2": None, "invalidate": time.monotonic()}


def _hub_watchdog_tick():
    """One supervision pass. Directly unit-testable; driven by the daemon
    loop every 2 s. All deadlines monotonic."""
    from modules.module_audio_recovery import restart_sound_server
    s = _hub.stats()
    if s["consumers"] == 0 or s["recovery_in_progress"]:
        return                                            # rules 1-2
    now = time.monotonic()
    if not s["stream_open"]:
        # rule 3: invalidate() hole — consumers alive but stream gone.
        if now - _wd_state["invalidate"] > 2.0:           # 2 s throttle
            _hub._ensure_stream()                        # lock-free call (S-M1-04)
            _wd_state["invalidate"] = now                 # throttle anchor advances
        return
    age = s["last_chunk_age_s"]
    if s["chunk_count"] == 0:
        # F1 boot grace: "never delivered a chunk" is not "stopped
        # delivering" — a freshly opened stream is legitimately silent
        # until its first PortAudio callback fires (the same grace
        # _is_chunk_stream_healthy grants; a 2 s tick in that window must
        # not fire a spurious L1 at boot). Past the grace window a stream
        # that NEVER delivered is a real failure -> still stale, so there
        # is no permanent blindness at boot.
        opened_age = s["stream_opened_age_s"]
        stale = opened_age is None or opened_age > _AudioHub._STALE_CHUNK_PERIOD
    else:
        stale = age is None or age > 5.0
    nz = s["last_nonzero_age_s"]
    # Sustained digital silence: either ≥ 60 s since the last non-zero
    # chunk, or chunks arriving that have NEVER been non-zero (all-zeros
    # from the very first chunk — _last_nonzero_mono stays 0 -> nz None).
    # nz None with zero chunks is "no data yet", not silence.
    digital_silence = ((nz is not None and nz > 60.0)
                       or (nz is None and s["chunk_count"] > 0))
    if not stale and not digital_silence:
        # Healthy: reset the ladder so every stale episode starts at a fresh
        # cheap L1 — never an immediate ~22 s L2 after a long healthy gap.
        # BUT only when audio is genuinely flowing. A stream that reopened
        # successfully yet has still delivered nothing (chunk_count == 0) is
        # NOT recovered: `_ensure_stream` resets `_stream_opened_mono` on every
        # reopen, so clearing here would reset the escalation clock each time
        # and make the L2 rung unreachable for a wedged device that opens but
        # never delivers — the exact case L2 exists to fix.
        if s["chunk_count"] > 0:
            _wd_state["l1"] = None
            _wd_state["l2"] = None
        if now - _wd_state["heartbeat"] >= 30.0:
            _wd_state["heartbeat"] = now
            queue_message(f"AUDIO-HUB: cb={s['consumers']}({','.join(s['callback_names'])}) "
                          f"stream={'open' if s['stream_open'] else 'closed'}/"
                          f"{'active' if s['stream_active'] else 'inactive'} "
                          f"last_chunk={age if age is None else round(age, 2)}s "
                          f"exc={s['consumer_exc_count']} "
                          f"stalls={s['consecutive_read_stalls']} state=HEALTHY")
        return
    # STALE or digital silence -> discriminator + escalation ladder.
    # Ladder bookkeeping contract (rules 4-6):
    #   * `l1` is stamped ONLY when a fresh L1 is actually issued (`l1 is None
    #     or now - l1 >= 10.0`). It is NEVER stamped on ordinary stale ticks —
    #     that is what lets `now - l1` grow to 10 s so the L2 rung is reachable.
    #   * `l1`/`l2` are cleared to None by the FIRST healthy tick (above), so
    #     every stale episode starts at a fresh L1.
    #   * Once `l2` is stamped the ladder is in L2 mode: repeat L2 at >= 60 s
    #     cadence and NEVER re-issue L1 (no reopen-storm between L2s).
    mode = ("digital-silence" if digital_silence else
            "callbacks-stopped" if s["consumer_exc_count"] == 0 else "swallowed-exception")
    l1 = _wd_state["l1"]
    l2 = _wd_state["l2"]
    if l2 is not None:
        if now - l2 >= 60.0:                              # rule 6: repeat L2
            queue_message(f"CRITICAL: AUDIO-HUB still stale mode={mode} 60s after L2 "
                          "— repeating L2 restart_sound_server")
            restart_sound_server()
            _wd_state["l2"] = now
        return
    if l1 is not None and now - l1 >= 10.0:               # rule 5: 10 s after L1 -> L2
        queue_message(f"WARNING: AUDIO-HUB still stale mode={mode} 10s after L1 "
                      "-> L2 restart_sound_server")
        restart_sound_server()
        _wd_state["l2"] = now
        return
    if l1 is None or now - l1 >= 10.0:                   # rule 4: fresh L1
        queue_message(f"WARNING: AUDIO-HUB STALE mode={mode} chunk_age="
                      f"{age if age is None else round(age, 2)}s exc={s['consumer_exc_count']} "
                      f"cb={','.join(s['callback_names'])} -> L1-reopen")
        _wd_state["l1"] = now                            # stamp ONLY when issuing the L1
        _hub.request_l1_recovery()                       # return value ignored — ladder anchors on the stamp
        return
    # else: within the 10 s L1->L2 observation window — deliberately no action


def _hub_watchdog_loop(interval=2.0):
    while not _hub_watchdog_stop.is_set():
        time.sleep(interval)
        try:
            _hub_watchdog_tick()
        except Exception as e:
            queue_message(f"ERROR: audio hub watchdog tick failed: {e}")


_hub_watchdog_stop = threading.Event()
_hub_watchdog_thread = None


def start_hub_watchdog(interval=2.0):
    """Start the supervision daemon (idempotent). Consumed by boot wiring."""
    global _hub_watchdog_thread
    if _hub_watchdog_thread is not None and _hub_watchdog_thread.is_alive():
        return
    _hub_watchdog_stop.clear()
    _hub_watchdog_thread = threading.Thread(
        target=_hub_watchdog_loop, args=(interval,),
        daemon=True, name="audio-hub-watchdog")
    _hub_watchdog_thread.start()


def stop_hub_watchdog():
    """Signal the daemon loop to exit (thread is daemon)."""
    _hub_watchdog_stop.set()


# ── Public stream helpers ──────────────────────────────────────────

def open_native_stream(**kwargs):
    """Open an audio stream at the device's native rate.

    If a callback is provided, registers it with the shared audio hub.
    Returns a _SharedCallbackStream (context-manager compatible).

    If no callback, returns a _SharedReadStream for read-based access.

    Note: blocksize is not honored by the shared hub — the PortAudio stream
    uses device defaults. Callback consumers receive whatever chunk size the
    device delivers (typically 256-1024 frames). Consumers must handle
    variable-size chunks.
    """
    callback = kwargs.pop("callback", None)
    name = kwargs.pop("name", None)
    # blocksize and other kwargs are consumed but not used by the shared hub
    kwargs.pop("blocksize", None)
    if callback:
        return _SharedCallbackStream(callback, name=name)
    return _SharedReadStream()


class _SharedCallbackStream:
    """Wrapper returned by open_native_stream(callback=...).

    Compatible with sd.InputStream context-manager protocol.
    """

    def __init__(self, callback, name=None):
        self._callback = callback
        self._name = name
        self._rid = None

    def start(self):
        if self._rid is None:
            self._rid = _hub.register_callback(self._callback, name=self._name)

    def stop(self):
        pass  # stream stays alive for other consumers

    def close(self):
        if self._rid is not None:
            _hub.unregister_callback(self._rid)
            self._rid = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _SharedReadStream:
    """Wrapper returned by open_native_stream() without callback."""

    def __init__(self, dtype="float32"):
        self._dtype = dtype
        self._rid = None

    def start(self):
        if self._rid is None:
            self._rid = _hub.register_reader(self._dtype)

    def stop(self):
        pass

    def close(self):
        if self._rid is not None:
            _hub.unregister_reader(self._rid)
            self._rid = None

    def read(self, n_frames):
        if self._rid is None:
            raise RuntimeError("Stream not started — call start() or use as context manager")
        return _hub.read(self._rid, n_frames)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def flush_stream(stream, n=4, model_frames=2000):
    """Discard stale audio from stream buffer (e.g. after TTS playback)."""
    # For shared read streams, delegate to the hub
    if isinstance(stream, _SharedReadStream) and stream._rid is not None:
        _hub.flush_reader(stream._rid, n, model_frames)
        return
    # Legacy: raw sd.InputStream
    nr = get_native_rate()
    for _ in range(n):
        stream.read(dev_frames(model_frames, nr))


class ResamplingInputStream:
    """Read-based stream that transparently resamples to MODEL_RATE.

    Uses the shared audio hub — safe to use alongside callback consumers
    like the spectrum visualizer on the same USB mic.

    Usage::

        with ResamplingInputStream(dtype="int16") as mic:
            data, overflow = mic.read(4000)   # 4000 frames @ 16 kHz
            mic.flush()                        # discard stale audio
    """

    def __init__(self, **kwargs):
        self._dtype = kwargs.get("dtype", "float32")
        self._rid = None
        self._native_rate = get_native_rate()

    def __enter__(self):
        # Always buffer float32 in the hub; convert to requested dtype
        # AFTER resampling to avoid quantization noise before resample.
        self._rid = _hub.register_reader("float32")
        return self

    def __exit__(self, *exc):
        if self._rid is not None:
            _hub.unregister_reader(self._rid)
            self._rid = None
        return False

    def read(self, model_frames):
        """Read model_frames worth of audio, return resampled to MODEL_RATE."""
        n = dev_frames(model_frames, self._native_rate)
        raw, overflow = _hub.read(self._rid, n)
        data = resample(raw.ravel(), self._native_rate, MODEL_RATE)
        if self._dtype == "int16":
            data = np.clip(data * 32768, -32768, 32767).astype(np.int16)
        return data.reshape(-1, 1), overflow

    def read_exact(self, model_frames):
        """Like read() but guarantees exactly model_frames output samples."""
        data, overflow = self.read(model_frames)
        flat = data.ravel()
        if len(flat) > model_frames:
            flat = flat[:model_frames]
        elif len(flat) < model_frames:
            flat = np.pad(flat, (0, model_frames - len(flat)))
        return flat.reshape(-1, 1), overflow

    def flush(self, n=4, model_frames=2000):
        """Discard stale audio from the buffer."""
        if self._rid is not None:
            _hub.flush_reader(self._rid, n, model_frames)


def make_resampling_callback(user_callback):
    """Wrap an audio callback so it receives 16 kHz float32 mono audio.

    The resampling runs in the consumer's callback context (dispatched from
    the hub's audio thread). For heavy resampling on constrained hardware,
    consider buffering and resampling on a worker thread instead.

    ``user_callback(resampled_data, frames, time_info, status)`` where
    ``resampled_data`` is a float32 ndarray of shape ``(N,)`` at MODEL_RATE.
    """
    native_rate = get_native_rate()

    def _cb(indata, frames, time_info, status):
        audio = np.asarray(indata[:, 0], dtype=np.float32)
        if native_rate != MODEL_RATE:
            audio = resample(audio, native_rate, MODEL_RATE)
        user_callback(audio, len(audio), time_info, status)

    return _cb
