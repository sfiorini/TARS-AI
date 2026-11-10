"""
module_tts.py

Text-to-Speech (TTS) module for TARS-AI application.

Handles TTS functionality to convert text into audio using a multi-provider
fallback chain: [ttsoption] + [TTS] tts_fallback_list + espeak (always the
guaranteed last resort), deduplicated with first occurrence winning.
Providers: fishaudio, elevenlabs, speechify, openai, piper and espeak-ng
(local). The first provider that yields audio wins; a per-utterance deadline
(tts_timeout x len(chain)) bounds the whole chain so TARS cannot hang on a
dead provider.

"""


import requests
import os
import shutil
import time
import threading
from datetime import datetime
import numpy as np
import sounddevice as sd
import soundfile as sf
from io import BytesIO
import asyncio

from modules.module_messageQue import queue_message
from modules.module_config import load_config, DEFAULT_TTS_TIMEOUT
from modules import module_audio_devices

CONFIG = load_config()

# Barge-in: TTS cancellation event (thread-safe). B2: every stop request bumps
# _tts_cancel_generation; a request stays pending until an utterance honors it
# (_tts_cancel_consumed_gen catches up), so a barge-in landing between
# utterances is not silently discarded by the next utterance's start.
_tts_cancel_event = threading.Event()
_tts_cancel_lock = threading.Lock()
_tts_cancel_generation = 0
_tts_cancel_consumed_gen = 0
_tts_playing = threading.Event()  # Set while sd.play() is actively outputting audio
_tts_needs_flush = threading.Event()  # Set after TTS finishes; cleared by STT after flushing

# M3 barge-in: the persistent OutputStream currently playing, registered
# after start() and cleared in play_chunks' finally. stop_tts_playback()
# aborts it cross-thread — Pa_AbortStream is thread-safe and flushes the
# device buffer; sd.stop() alone never touched the persistent stream, so a
# barge-in mid-utterance kept feeding the device until the chunk drained.
_active_out_stream = None
_active_out_lock = threading.Lock()

# Output device discovery — resolved once on first playback. The lock makes
# resolution thread-safe: a concurrent caller used to see the resolved flag
# set BEFORE the scan finished and silently got the system default.
_output_device = None
_output_device_resolved = False
_output_device_lock = threading.Lock()


def _resolve_output_device():
    """Find and cache the audio output device. Runs once, no-op after.

    Prefers real hardware (USB audio, headphones, I2S DACs) over virtual
    ALSA devices like 'default' or 'dmix' which may route to HDMI/null.
    Thread-safe: the scan runs under _output_device_lock and the resolved
    flag is set only AFTER the scan completes, so concurrent callers either
    wait for the result or see the cached device — never a half-resolved None.
    """
    global _output_device, _output_device_resolved
    if _output_device_resolved:
        return

    with _output_device_lock:
        if _output_device_resolved:
            return
        _scan_output_device()
        # Flag goes up only AFTER the scan completes (all exits above set
        # _output_device): a concurrent caller must never observe
        # resolved=True with a still-None device.
        _output_device_resolved = True


def _scan_output_device():
    """Scan and cache the best output device into _output_device.

    Callers hold _output_device_lock. Delegates the candidate scan to
    module_audio_devices.find_device (shared with module_mic), passing our
    own _device_opens as the validator and queue_message as the logger so
    the user-facing messages are unchanged. Prefers the sound server (pulse)
    > USB > hardware > virtual ALSA devices; HDMI outputs are skipped.
    """
    global _output_device

    try:
        idx, _dev, _label = module_audio_devices.find_device(
            kind="output", log=queue_message, validator=_device_opens)
    except Exception:
        queue_message("WARNING: Could not query audio devices — using system default")
        _output_device = None
        return
    _output_device = idx


def _device_opens(idx):
    """True if a short OutputStream can be opened on device idx right now."""
    try:
        with sd.OutputStream(device=idx, samplerate=16000, channels=1, dtype='float32', blocksize=1024):
            return True
    except Exception:
        return False


def _fallback_to_default_output():
    """Switch cached output device to the system default (sound server route)."""
    global _output_device, _output_device_resolved
    with _output_device_lock:
        _output_device = None
        _output_device_resolved = True


def _recover_sound_server():
    """Delegate to module_audio_recovery.restart_sound_server (shared ladder).

    Servo power-on spikes can wedge the USB audio card: PipeWire keeps a dead
    stream on the PCM (subdevices 0/1), after which BOTH direct hw opens and
    the pulse default fail with PaErrorCode -9985. Restarting the user sound
    services releases the card. No sudo needed — these are --user services.
    The shared implementation also invalidates the mic hub's dangling
    InputStream (PortAudio re-init kills it) so a TTS self-heal cannot deafen
    the mic until reboot.
    """
    from modules.module_audio_recovery import restart_sound_server
    restart_sound_server()


def _reset_output_device_resolution():
    """Forget the cached output device so the next playback re-resolves it."""
    global _output_device, _output_device_resolved
    with _output_device_lock:
        _output_device = None
        _output_device_resolved = False


def _playback_gain():
    """Playback volume from [TTS] tts_volume (0.0-2.0, default 1.0); 1.5 was the old hardcoded gain."""
    try:
        v = float(CONFIG["TTS"].get("tts_volume", 1.0))
    except (TypeError, ValueError):
        return 1.0
    if v < 0.0:
        return 0.0
    if v > 2.0:
        return 2.0
    return v


def _play_via_pwplay(data, samplerate):
    """Last-resort playback through PipeWire's native API (pw-play subprocess).

    Used when PortAudio cannot open any device (USB card wedged by a dead sound
    server stream after servo power-on spikes). Writes a temp wav and plays it
    via pw-play, which talks to PipeWire directly and never touches the ALSA hw
    PCM. Returns the Popen (poll it for completion / terminate for barge-in);
    the temp wav path is tracked on the proc as `tars_tmp_wav` so the caller
    can unlink it in a finally. If anything fails after the temp file is
    created, it is removed here — no leaks.
    """
    import subprocess, tempfile
    pwplay = shutil.which("pw-play")
    if not pwplay:
        queue_message("WARNING: pw-play not found — cannot use PipeWire-native fallback")
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        sf.write(tmp_path, data, samplerate)
        proc = subprocess.Popen([pwplay, tmp_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        _unlink_quiet(tmp_path)
        queue_message(f"WARNING: pw-play fallback failed: {e}")
        return None
    # Playback is asynchronous — the caller unlinks the temp wav in a finally
    # via _cleanup_pwplay_proc once the proc finishes (or dies).
    proc.tars_tmp_wav = tmp_path
    queue_message("INFO: playing chunk via pw-play (PipeWire native, bypasses wedged ALSA device)")
    return proc


def _unlink_quiet(path):
    """Best-effort unlink; a missing file is fine."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _cleanup_pwplay_proc(proc):
    """Remove the temp wav tracked on a pw-play proc (no-op without one)."""
    if proc is None:
        return
    tmp_path = getattr(proc, "tars_tmp_wav", None)
    if tmp_path:
        _unlink_quiet(tmp_path)


def init_audio_output():
    """Resolve and log the audio output device at startup."""
    _resolve_output_device()


def _consume_pending_cancel():
    """Mark the pending cancel request as honored (B2).

    Called when an utterance returns interrupted: the barge-in has been
    honored, so later utterances (e.g. the reply to the barge-in) start with
    a clean slate instead of inheriting a stale cancellation.
    """
    global _tts_cancel_consumed_gen
    with _tts_cancel_lock:
        _tts_cancel_consumed_gen = _tts_cancel_generation
        _tts_cancel_event.clear()


def stop_tts_playback():
    """Signal TTS to stop immediately. Safe to call from any thread.

    B2: each request bumps a cancel generation instead of relying on the
    next utterance noticing a bare global event — the request stays pending
    until an utterance honors it.
    """
    global _tts_cancel_generation
    with _tts_cancel_lock:
        _tts_cancel_generation += 1
        _tts_cancel_event.set()
    # M3: abort the registered persistent stream (thread-safe read under the
    # lock; the stream may already be gone — a finished utterance's stream is
    # deregistered in play_chunks' finally, so a later cancel never aborts a
    # stale stream).
    global _active_out_stream
    with _active_out_lock:
        stream = _active_out_stream
    if stream is not None:
        try:
            stream.abort()  # Pa_AbortStream: thread-safe, flushes the buffer
        except Exception:
            pass
    try:
        sd.stop()
    except Exception:
        pass

def is_tts_playing():
    """Check if TTS audio is currently being output. Used by barge-in monitor."""
    return _tts_playing.is_set()

def needs_mic_flush():
    """Check and clear the flush flag. Returns True once after each TTS playback."""
    return _tts_needs_flush.is_set()

def clear_mic_flush():
    """Clear the flush flag after STT has flushed the mic buffer."""
    _tts_needs_flush.clear()

# Conditional TTS module imports - not all are available on all devices
text_to_speech_with_pipelining_piper = None
text_to_speech_with_pipelining_silero = None
text_to_speech_with_pipelining_espeak = None
text_to_speech_with_pipelining_elevenlabs = None
text_to_speech_with_pipelining_openai = None
text_to_speech_with_pipelining_fishaudio = None
text_to_speech_with_pipelining_speechify = None
try:
    from modules.module_piper import text_to_speech_with_pipelining_piper as _piper
    text_to_speech_with_pipelining_piper = _piper
except Exception as e:
    queue_message(f"[TTS] Piper module failed to load: {e}")
    pass

try:
    from modules.module_silero import text_to_speech_with_pipelining_silero as _silero
    text_to_speech_with_pipelining_silero = _silero
except ImportError:
    pass

try:
    from modules.module_espeak import text_to_speech_with_pipelining_espeak as _espeak
    text_to_speech_with_pipelining_espeak = _espeak
except ImportError:
    pass

try:
    from modules.module_elevenlabs import text_to_speech_with_pipelining_elevenlabs as _elevenlabs
    text_to_speech_with_pipelining_elevenlabs = _elevenlabs
except ImportError:
    pass

try:
    from modules.module_openai import text_to_speech_with_pipelining_openai as _openai
    text_to_speech_with_pipelining_openai = _openai
except ImportError:
    pass

try:
    from modules.module_fishaudio import text_to_speech_with_fishaudio as _fishaudio
    text_to_speech_with_pipelining_fishaudio = _fishaudio
except Exception as e:
    queue_message(f"[TTS] Fish.audio module failed to load: {e}")
    pass

try:
    from modules.module_speechify import text_to_speech_with_speechify as _speechify
    text_to_speech_with_pipelining_speechify = _speechify
except Exception as e:
    queue_message(f"[TTS] Speechify module failed to load: {e}")
    pass

# ── Typed provider errors + ordered fallback chain (single exception hierarchy) ──
try:
    from modules.module_fishaudio import (
        TTSProviderError,
        QuotaExhausted,
        RateLimited,
        AuthFailed,
        TransientError,
    )
except Exception:  # pragma: no cover — module_fishaudio has no optional deps
    class TTSProviderError(Exception):
        def __init__(self, message, status_code=None):
            super().__init__(message)
            self.status_code = status_code

    class QuotaExhausted(TTSProviderError):
        pass

    class RateLimited(TTSProviderError):
        pass

    class AuthFailed(TTSProviderError):
        pass

    class TransientError(TTSProviderError):
        pass


def _tts_cfg(key, default=None):
    """Read a [TTS] config value at call time (tolerates dict or attr sections)."""
    try:
        section = CONFIG["TTS"]
    except Exception:
        return default
    getter = getattr(section, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except Exception:
            value = default
    else:
        value = getattr(section, key, default)
    return default if value is None else value


def _tts_timeout():
    """Per-provider HTTP timeout from [TTS] tts_timeout (seconds, float)."""
    try:
        return float(_tts_cfg("tts_timeout", DEFAULT_TTS_TIMEOUT))
    except (TypeError, ValueError):
        return float(DEFAULT_TTS_TIMEOUT)


def _warmup_piper_if_in_chain():
    """Pre-load the Piper voice model in a daemon thread when piper is in the
    fallback chain, so the first failover doesn't pay the multi-second load."""
    try:
        chain = _effective_chain(str(CONFIG["TTS"].get("ttsoption", "") or ""))
        if "piper" in chain:
            def _load():
                try:
                    import modules.module_piper as _mp
                    _mp._ensure_voice_loaded()
                except Exception:
                    pass
            threading.Thread(target=_load, daemon=True, name="piper-warmup").start()
    except Exception:
        pass


def _effective_chain(ttsoption):
    """Ordered, deduped provider chain: [ttsoption] + tts_fallback_list + 'espeak'.

    'espeak' is always the guaranteed last resort; duplicates (e.g. ttsoption
    also present in the fallback list) are tried only once, first occurrence wins.
    """
    names = []
    primary = str(ttsoption or "").strip().lower()
    if primary:
        names.append(primary)
    raw = _tts_cfg("tts_fallback_list", [])
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",") if item.strip()]
    for item in (raw or []):
        name = str(item).strip().lower()
        if name:
            names.append(name)
    names.append("espeak")
    seen = set()
    chain = []
    for name in names:
        if name not in seen:
            seen.add(name)
            chain.append(name)
    return chain

# Pre-load the local Piper voice in the background when it is part of the
# fallback chain (avoids a multi-second model load on first failover).
_warmup_piper_if_in_chain()


# Provider adapters: normalize the differing per-provider signatures into
# (text, is_wakeword, emotion) -> async generator (or None when the module is
# not available on this device). Module globals are read at call time so tests
# can swap providers. Cloud providers get the configured per-provider timeout.
def _prov_espeak(text, is_wakeword, emotion):
    fn = text_to_speech_with_pipelining_espeak
    return fn(text) if fn is not None else None


def _prov_piper(text, is_wakeword, emotion):
    fn = text_to_speech_with_pipelining_piper
    return fn(text, emotion=emotion) if fn is not None else None


def _prov_silero(text, is_wakeword, emotion):
    fn = text_to_speech_with_pipelining_silero
    return fn(text) if fn is not None else None


def _prov_elevenlabs(text, is_wakeword, emotion):
    fn = text_to_speech_with_pipelining_elevenlabs
    return fn(text, is_wakeword) if fn is not None else None


def _prov_openai(text, is_wakeword, emotion):
    fn = text_to_speech_with_pipelining_openai
    return fn(text, is_wakeword) if fn is not None else None


def _prov_fishaudio(text, is_wakeword, emotion):
    fn = text_to_speech_with_pipelining_fishaudio
    return fn(text, timeout=_tts_timeout()) if fn is not None else None


def _prov_speechify(text, is_wakeword, emotion):
    fn = text_to_speech_with_pipelining_speechify
    return fn(text, timeout=_tts_timeout()) if fn is not None else None


def _prov_external(text, is_wakeword, emotion):
    return _external_tts_chunks(text, timeout=_tts_timeout())


# Registry of chain providers: name -> adapter. Unknown names are skipped with
# a warning by the chain runner; "other" simply isn't here and falls through
# to whatever tts_fallback_list contains.
_PROVIDERS = {
    "espeak": _prov_espeak,
    "piper": _prov_piper,
    "silero": _prov_silero,
    "elevenlabs": _prov_elevenlabs,
    "openai": _prov_openai,
    "fishaudio": _prov_fishaudio,
    "speechify": _prov_speechify,
    "external": _prov_external,
}


def update_tts_settings(ttsurl):
    url = f"{ttsurl}/set_tts_settings"
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }
    payload = {
        "stream_chunk_size": 100,
        "temperature": 0.75,
        "speed": 1,
        "length_penalty": 1.0,
        "repetition_penalty": 5,
        "top_p": 0.85,
        "top_k": 50,
        "enable_text_splitting": True
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            queue_message(f"LOAD: TTS Settings updated successfully.")
        else:
            queue_message(f"ERROR: Failed to update TTS settings. Status code: {response.status_code}")
            queue_message(f"INFO: Response: {response.text}")
    except Exception as e:
        queue_message(f"ERROR: TTS update failed: {e}")

async def _external_tts_chunks(text, timeout=None):
    """Fetch audio from the external TTS server (POST {ttsurl}/tts/generate).

    Yields one BytesIO on HTTP 200; yields nothing (logs the error) on failure.
    timeout defaults to [TTS] tts_timeout when not given.
    """
    if not timeout:
        try:
            timeout = float(_tts_cfg("tts_timeout", DEFAULT_TTS_TIMEOUT))
        except (TypeError, ValueError):
            timeout = float(DEFAULT_TTS_TIMEOUT)
    external_url = (CONFIG["TTS"].ttsurl or "").rstrip("/")
    if not external_url:
        queue_message("ERROR: External TTS URL (ttsurl) is not configured")
        return
    payload = {"text": text}
    ext_voice = CONFIG["TTS"].tts_voice or ""
    if ext_voice:
        payload["voice"] = ext_voice
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get('EXTERNAL_API_KEY', '')
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        # to_thread: never block the event loop on HTTP (app would freeze)
        response = await asyncio.to_thread(
            requests.post, f"{external_url}/tts/generate", json=payload, headers=headers, timeout=timeout
        )
        if response.status_code == 200:
            audio_buffer = BytesIO(response.content)
            audio_buffer.seek(0)
            yield audio_buffer
        else:
            queue_message(f"ERROR: External TTS server returned {response.status_code}")
    except requests.exceptions.ConnectionError:
        queue_message(f"ERROR: Cannot connect to external TTS server at {external_url}")
    except Exception as e:
        queue_message(f"ERROR: External TTS failed: {e}")


async def generate_tts_audio(text, ttsoption, is_wakeword=False, ttsurl=None, toggle_charvoice=True, tts_voice=None, emotion=None):
    """Synthesize `text` by walking the ordered provider chain.

    Chain = dedup([ttsoption] + [TTS] tts_fallback_list) with 'espeak' appended
    last. The first provider that yields >= 1 audio chunk wins; typed provider
    failures (QuotaExhausted / RateLimited / AuthFailed / TransientError) and
    empty results log the reason and fail over to the next provider. Once a
    provider has yielded audio, a later error ends the utterance (the chunks
    already produced are kept — no mid-utterance voice switch). A per-utterance
    deadline (tts_timeout * len(chain)) bounds the whole chain so TARS cannot
    hang on a stuck provider. Signature is unchanged for existing callers.
    """
    try:
        if not text or not str(text).strip():
            queue_message("WARNING: TTS called with empty text — nothing to synthesize")
            return

        chain = _effective_chain(ttsoption)
        timeout = _tts_timeout()
        deadline = time.monotonic() + timeout * max(1, len(chain))

        for index, name in enumerate(chain):
            if time.monotonic() > deadline:
                queue_message(f"ERROR: TTS per-utterance deadline exceeded before provider '{name}' — giving up")
                return
            adapter = _PROVIDERS.get(name)
            if adapter is None:
                queue_message(f"WARNING: TTS provider '{name}' in chain is unknown — skipping")
                continue
            gen = adapter(text, is_wakeword, emotion)
            if gen is None:
                queue_message(f"WARNING: TTS provider '{name}' is not available on this device — skipping")
                continue
            yielded = 0
            try:
                async for chunk in gen:
                    yielded += 1
                    yield chunk
                    if time.monotonic() > deadline:
                        queue_message(f"ERROR: TTS per-utterance deadline exceeded during '{name}' — stopping")
                        return
            except Exception as e:
                if yielded > 0:
                    # Mid-stream failure after audio was already produced: keep
                    # what was spoken, do not switch voice mid-utterance.
                    queue_message(f"ERROR: TTS provider '{name}' failed mid-stream after {yielded} chunk(s): {type(e).__name__}: {e}")
                    return
                reason = f"{type(e).__name__}: {e}"
                next_name = next((n for n in chain[index + 1:] if n in _PROVIDERS), None)
                if next_name:
                    queue_message(f"WARNING: TTS provider '{name}' failed ({reason}) — failing over to '{next_name}'")
                else:
                    queue_message(f"WARNING: TTS provider '{name}' failed ({reason}) — no further providers available")
                continue
            finally:
                try:
                    await gen.aclose()
                except Exception:
                    pass
            if yielded > 0:
                return
            queue_message(f"WARNING: TTS provider '{name}' produced no audio — trying next provider in chain")

        queue_message(f"ERROR: No TTS provider in chain {chain} produced audio (ttsoption='{ttsoption}')")

    except Exception as e:
        queue_message(f"ERROR: Text-to-speech generation failed: {e}")

import re as _re
import queue as _queue


class SentenceTTSPipeline:
    """Sentence-by-sentence TTS pipeline.

    Accepts streamed text via feed(), splits at sentence boundaries,
    and plays each sentence through TTS as soon as it's complete.
    TTS plays sentence 1 while the LLM is still generating sentence 2.

    Usage:
        pipeline = SentenceTTSPipeline('piper', sanitize=my_func, on_first_play=cb)
        pipeline.start()
        pipeline.feed("Hello there! How are ")
        pipeline.feed("you doing today?")
        pipeline.finish()
        pipeline.join()
        print(pipeline.interrupted, pipeline.duration)
    """

    _SENT_RE = _re.compile(r'(?<=[.!?])\s+')
    _CLAUSE_RE = _re.compile(r'(?<=[.!?;:,])\s+')
    _MIN_SENT = 20
    _MIN_CLAUSE = 12      # ~3 words; first chunk only for lower latency

    def __init__(self, tts_option, sanitize=None, on_first_play=None, play_func=None):
        self._tts_option = tts_option
        self._sanitize = sanitize or str.strip
        self._on_first_play = on_first_play
        # Custom async play function: async def(sentence, tts_option) -> bool (interrupted).
        # When None, defaults to play_audio_chunks (device speaker).
        self._play_func = play_func
        self._queue = _queue.Queue()
        self._remainder = ''
        self._first_queued = False
        self._interrupted = False
        self._duration = 0.0
        self._play_time = 0.0   # Actual TTS synthesis+playback time (excludes queue waits)
        self._thread = None
        self._emotion = None    # Set via set_emotion() once detected

    def start(self):
        """Start the TTS worker thread."""
        self._thread = threading.Thread(target=self._worker, daemon=True, name="tts-pipeline")
        self._thread.start()

    def set_emotion(self, emotion):
        """Set the emotion for multispeaker TTS. Can be called while pipeline is running."""
        self._emotion = emotion

    def feed(self, text):
        """Feed visible text. Complete sentences are queued for TTS immediately."""
        self._remainder = self._extract_sentences(self._remainder + text)

    def finish(self, remaining=None):
        """Flush remaining text and signal end of stream.

        If remaining is provided, it replaces the internal remainder
        (useful for fallback flush from raw LLM output).
        """
        text = remaining if remaining is not None else self._remainder
        text = self._sanitize(text) if text else ''
        if text:
            self._queue.put(text)
        self._remainder = ''
        self._queue.put(None)

    def join(self, timeout=120):
        """Wait for TTS worker to finish playing all sentences."""
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def remainder(self):
        """Current un-flushed text (no complete sentence boundary yet)."""
        return self._remainder

    @property
    def interrupted(self):
        return self._interrupted

    @property
    def duration(self):
        return self._duration

    @property
    def play_time(self):
        """Actual TTS synthesis+playback time (excludes queue wait time)."""
        return self._play_time

    def _extract_sentences(self, text):
        """Split text at boundaries, queue complete chunks, return remainder.

        First chunk splits at clause boundaries (,;:) with a shorter
        threshold for lower initial latency.  Subsequent chunks use full
        sentence boundaries (.!?) for better prosody.
        """
        while True:
            pos = 0
            m = None
            if self._first_queued:
                # Sentence boundaries for subsequent chunks
                while True:
                    m = self._SENT_RE.search(text, pos)
                    if not m or m.start() >= self._MIN_SENT:
                        break
                    pos = m.end()
            else:
                # Clause boundaries for first chunk (lower latency)
                while True:
                    m = self._CLAUSE_RE.search(text, pos)
                    if not m or m.start() >= self._MIN_CLAUSE:
                        break
                    pos = m.end()
            if not m:
                break
            sentence = self._sanitize(text[:m.start() + 1])
            text = text[m.end():]
            if sentence:
                self._queue.put(sentence)
                if not self._first_queued:
                    self._first_queued = True
        return text

    def _worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        t_start = time.perf_counter()
        play_total = 0.0
        first = True
        try:
            while not self._interrupted:
                try:
                    sentence = self._queue.get(timeout=30)
                except _queue.Empty:
                    break
                if sentence is None:
                    break
                if first and self._on_first_play:
                    first = False
                    try:
                        self._on_first_play()
                    except Exception:
                        pass
                queue_message(f"DEBUG: TTS speaking: {sentence}")
                t_play = time.perf_counter()
                try:
                    if self._play_func:
                        was_int = loop.run_until_complete(
                            self._play_func(sentence, self._tts_option)
                        )
                    else:
                        was_int = loop.run_until_complete(
                            play_audio_chunks(sentence, self._tts_option, emotion=self._emotion)
                        )
                except Exception as e:
                    queue_message(f"ERROR: TTS pipeline failed: {e}")
                    was_int = False
                play_total += time.perf_counter() - t_play
                if was_int:
                    self._interrupted = True
                    while not self._queue.empty():
                        try:
                            self._queue.get_nowait()
                        except _queue.Empty:
                            break
        finally:
            self._duration = time.perf_counter() - t_start
            self._play_time = play_total
            loop.close()


async def play_audio_chunks(text, config, is_wakeword=False, emotion=None):
    _resolve_output_device()
    if not is_wakeword:
        queue_message(f"DEBUG: TTS speaking (direct): {text}")
    # B2: do NOT erase a pending barge-in with a bare clear() — a cancel that
    # landed between utterances must be honored by this utterance (the first
    # cancel check in play_chunks interrupts it, and play_audio_chunks then
    # consumes the request). Only a cancel that was already honored (consumed)
    # is stale and cleared here.
    with _tts_cancel_lock:
        if _tts_cancel_generation <= _tts_cancel_consumed_gen:
            _tts_cancel_event.clear()
    audio_queue = asyncio.Queue(maxsize=3)
    synthesis_done = asyncio.Event()
    was_interrupted = False

    async def synthesize_chunks():
        gen = generate_tts_audio(text, config, is_wakeword, emotion=emotion).__aiter__()
        try:
            async for audio_chunk in gen:
                if _tts_cancel_event.is_set():
                    break
                # Bounded put (C14) with cancel-aware backpressure: waiting
                # on queue.put replaces the old busy-wait spin (non-blocking
                # enqueue + sleep) — one code path, and the cancel check above
                # still runs every iteration. The put is retried while the
                # queue is full, so the producer BLOCKS until the consumer
                # drains — no audio chunk is ever dropped. (The old contract
                # dropped the chunk on a 50 ms timeout; that was written for
                # whole-sentence chunks, but streaming providers yield ~1 s
                # slices ~17x faster than realtime, so the maxsize=3 queue was
                # permanently full and most of the utterance was silently
                # discarded — the robot "skipped sentences" and ended early.)
                # The bounded retry keeps cancellation honoured within ~250 ms.
                while not _tts_cancel_event.is_set():
                    try:
                        await asyncio.wait_for(audio_queue.put(audio_chunk), 0.25)
                        break
                    except asyncio.TimeoutError:
                        continue  # queue still full: the consumer is slower
                                  # than the producer. Retry instead of
                                  # dropping the chunk.
        except Exception as e:
            queue_message(f"ERROR: Synthesis failed: {e}")
        finally:
            # Explicitly close the async generator so pending HTTP streams
            # (ElevenLabs, OpenAI, etc.) are cleaned up before the event loop closes.
            try:
                await gen.aclose()
            except Exception:
                pass
            synthesis_done.set()

    def _notify_talking_state(endpoint):
        """Fire-and-forget HTTP notification to webui (non-blocking)."""
        try:
            requests.get(f"http://127.0.0.1:{CONFIG['ACCESS'].get('webui_port', 80)}/{endpoint}", timeout=1)
        except Exception:
            pass

    async def play_chunks():
        nonlocal was_interrupted
        global _active_out_stream
        threading.Thread(target=_notify_talking_state, args=("start_talking",), daemon=True).start()

        # Persistent output stream: one stream per utterance avoids the
        # start/stop gap of sd.play() per chunk (audible stutter between
        # streaming slices on long sentences). Falls back to per-chunk sd.play.
        out_stream = None
        try:
            out_stream = sd.OutputStream(samplerate=44100, channels=1, dtype='float32',
                                         device=_output_device, latency='low')
            out_stream.start()
        except Exception as e:
            queue_message(f"WARNING: persistent output stream failed ({e}) — using per-chunk sd.play")
            out_stream = None
        if out_stream is not None:
            with _active_out_lock:
                _active_out_stream = out_stream
        first_audio_logged = False

        try:
            while True:
                if _tts_cancel_event.is_set():
                    was_interrupted = True
                    break

                try:
                    try:
                        audio_chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        if synthesis_done.is_set() and audio_queue.empty():
                            break
                        continue

                    data, samplerate = sf.read(audio_chunk, dtype='float32')

                    # Resample to 16kHz if needed
                    if samplerate != 44100:
                        ratio = 44100 / samplerate
                        new_len = int(len(data) * ratio)
                        if data.ndim == 1:
                            indices = np.linspace(0, len(data) - 1, new_len)
                            data = np.interp(indices, np.arange(len(data)), data)
                        else:
                            indices = np.linspace(0, len(data) - 1, new_len)
                            data = np.column_stack([
                                np.interp(indices, np.arange(len(data)), data[:, ch])
                                for ch in range(data.shape[1])
                            ])
                        samplerate = 44100
                        # np.interp ALWAYS returns float64, and the persistent
                        # OutputStream is float32 — writing float64 raises
                        # "dtype mismatch: 'float64' vs 'float32'" and drops the
                        # whole chunk to the sd.play recovery path. Cast back so
                        # every non-44100 provider (Piper 22050, Speechify 24000)
                        # plays through the persistent stream.
                        data = data.astype(np.float32, copy=False)

                    # Per-chunk peak normalization causes volume pumping between
                    # streaming slices; only normalize long (non-streaming) chunks.
                    if len(data) / max(samplerate, 1) > 2.0:
                        max_val = np.max(np.abs(data))
                        if max_val > 0:
                            data = data / max_val

                    gain = _playback_gain()
                    data = np.clip(data * gain, -1.0, 1.0)

                    if out_stream is not None:
                        if data.ndim > 1:
                            data = np.ascontiguousarray(data[:, 0])
                        # Set BEFORE the write: is_tts_playing() must be True
                        # while audio is actually being output, else STT's
                        # barge-in / self-hearing checks treat TTS as over
                        # mid-utterance (false barge-in on the default path).
                        _tts_playing.set()
                        write_error = None
                        try:
                            # M3 barge-in: feed in <=100 ms sub-slices, checking
                            # the cancel event between them — a barge-in stops
                            # the feed within one sub-slice instead of after the
                            # whole chunk. Slice size derives from the stream's
                            # sample rate (fake streams in tests expose none —
                            # fall back to the 44100 the stream is opened at).
                            rate = getattr(out_stream, "samplerate", None) or 44100
                            sub = max(1, int(float(rate) * 0.1))
                            n = len(data)
                            for start in range(0, n, sub):
                                if _tts_cancel_event.is_set():
                                    out_stream.abort()
                                    was_interrupted = True
                                    break
                                await asyncio.to_thread(
                                    out_stream.write, data[start:start + sub])
                        except Exception as write_err:
                            write_error = write_err
                        finally:
                            # Keep the flag SET while more audio is queued. Streaming
                            # providers yield ~1 s slices, so clearing per write
                            # opened a barge-in window between slices; the barge-in
                            # loop (module_stt) then collected the robot's own voice
                            # and cancelled playback mid-sentence. Clear only once the
                            # utterance's audio has actually drained.
                            # Trade-off: mic-based barge-in is no longer available
                            # mid-utterance while a stream is continuously fed — it
                            # becomes available once audio drains. Wake-word
                            # interruption uses a separate path (stop_tts_playback)
                            # and is unaffected.
                            # Pinned by test_flag_held_while_pending_cleared_when_drained_documented_residual
                            # and test_flag_does_not_flicker_between_streamed_slices
                            # (src/tests/test_tts_playback_recovery.py).
                            if audio_queue.empty() and synthesis_done.is_set():
                                _tts_playing.clear()
                        if write_error is not None:
                            # The persistent stream is dead (e.g. wedged USB
                            # card) — abort it and fall through to the sd.play
                            # recovery ladder below (-9985 recovery, sound-server
                            # restart, pw-play fallback) instead of retrying
                            # the dead stream forever.
                            queue_message(f"WARNING: persistent output stream write failed ({write_error}) — falling back to sd.play recovery")
                            try:
                                out_stream.abort()
                            except Exception:
                                pass
                            out_stream = None
                        else:
                            if not first_audio_logged:
                                import modules.module_speed as speed
                                speed.mark_first_audio()
                                first_audio_logged = True
                            if _tts_cancel_event.is_set():
                                out_stream.abort()
                                was_interrupted = True
                                break
                            if not is_wakeword:
                                _tts_needs_flush.set()
                            # B1: same sentence-level barge-in window as the
                            # sd.play path below — whole-clip chunks (>=3s,
                            # fishaudio_stream=False) get a 300ms mic-check
                            # window between sentences; streaming slices
                            # (<3s) get none (window only between sentences).
                            chunk_seconds = len(data) / max(samplerate, 1)
                            window = 6 if chunk_seconds >= 3.0 else 0
                            for _ in range(window):  # 300ms window (6 x 50ms)
                                if _tts_cancel_event.is_set():
                                    out_stream.abort()
                                    was_interrupted = True
                                    break
                                await asyncio.sleep(0.05)
                            if was_interrupted:
                                break
                            continue

                    pw_proc = None
                    try:
                        sd.play(data, samplerate, device=_output_device)
                    except sd.PortAudioError as e:
                        # Device unavailable (PaErrorCode -9985): the USB card can be
                        # wedged by a dead PipeWire stream after servo power-on spikes.
                        # Restart the user sound server, re-resolve the device, retry once.
                        queue_message(f"WARNING: Audio device {_output_device} failed ({e}) — recovering sound server and retrying")
                        # A4: recovery blocks up to ~22s (subprocess + sleep) — run it
                        # in a worker thread so the event loop (barge-in, STT) keeps
                        # servicing instead of freezing.
                        await asyncio.to_thread(_recover_sound_server)
                        _reset_output_device_resolution()
                        _resolve_output_device()
                        try:
                            sd.play(data, samplerate, device=_output_device)
                        except sd.PortAudioError as e2:
                            queue_message(f"WARNING: retry on resolved device failed ({e2}) — final retry on system default")
                            _fallback_to_default_output()
                            try:
                                sd.play(data, samplerate, device=None)
                            except sd.PortAudioError as e3:
                                queue_message(f"WARNING: all PortAudio devices failed ({e3}) — falling back to pw-play")
                                pw_proc = _play_via_pwplay(data, samplerate)
                    _tts_playing.set()

                    # Log time-to-first-audio on the first chunk
                    import modules.module_speed as speed
                    speed.mark_first_audio()

                    # Poll instead of sd.wait() so we can check for barge-in.
                    # finally: the pw-play temp wav must go even if this loop
                    # raises — it is unlinked exactly once, on every exit path.
                    try:
                        while True:
                            if _tts_cancel_event.is_set():
                                if pw_proc is not None:
                                    pw_proc.terminate()
                                else:
                                    sd.stop()
                                was_interrupted = True
                                break
                            if pw_proc is not None:
                                if pw_proc.poll() is not None:
                                    break
                            else:
                                try:
                                    stream = sd.get_stream()
                                    if stream is None or not stream.active:
                                        break
                                except Exception:
                                    break
                            await asyncio.sleep(0.05)
                    finally:
                        _cleanup_pwplay_proc(pw_proc)

                    # Same rule as the persistent-stream path: keep the flag
                    # SET while more audio is queued. This sd.play branch is the
                    # one that actually runs when the USB card is wedged (-9985),
                    # and clearing per chunk there opened a barge-in window
                    # between ~1 s streaming slices — the barge-in loop then
                    # collected TARS's own voice and cut the sentence short.
                    if audio_queue.empty() and synthesis_done.is_set():
                        _tts_playing.clear()
                    if not is_wakeword:
                        _tts_needs_flush.set()

                    if was_interrupted:
                        break

                    # Brief pause between chunks for barge-in detection.
                    # Monitor checks mic RMS only when _tts_playing is clear.
                    # Skip it for streaming slices (<3s) or playback stutters —
                    # the window only makes sense between full sentences.
                    chunk_seconds = len(data) / max(samplerate, 1)
                    window = 6 if chunk_seconds >= 3.0 else 0
                    for _ in range(window):  # 300ms window (6 x 50ms)
                        if _tts_cancel_event.is_set():
                            was_interrupted = True
                            break
                        await asyncio.sleep(0.05)

                    if was_interrupted:
                        break

                except Exception as e:
                    queue_message(f"ERROR: Failed to play chunk: {e}")
                    if synthesis_done.is_set() and audio_queue.empty():
                        break
        finally:
            # A1: never leave _tts_playing set after play_chunks ends.
            _tts_playing.clear()
            # M3: deregister the active stream BEFORE the close block below —
            # a cancel landing after this utterance must never abort a stale
            # stream (the next utterance registers its own).
            with _active_out_lock:
                _active_out_stream = None

        if out_stream is not None:
            try:
                if was_interrupted:
                    out_stream.abort()
                else:
                    out_stream.stop()  # drain remaining buffered audio
                out_stream.close()
            except Exception:
                pass

        threading.Thread(target=_notify_talking_state, args=("stop_talking",), daemon=True).start()

    await asyncio.gather(
        synthesize_chunks(),
        play_chunks()
    )
    if was_interrupted:
        # B2: the barge-in was honored by this utterance — consume it so the
        # next utterance (e.g. the reply to the barge-in) is not cancelled by
        # a stale request.
        _consume_pending_cancel()
    return was_interrupted