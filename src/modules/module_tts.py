"""
module_tts.py

Text-to-Speech (TTS) module for TARS-AI application.

Handles TTS functionality to convert text into audio using:
- Local tools (e.g., espeak-ng)
- Server-based TTS systems

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

CONFIG = load_config()

# Barge-in: TTS cancellation event (thread-safe)
_tts_cancel_event = threading.Event()
_tts_playing = threading.Event()  # Set while sd.play() is actively outputting audio
_tts_needs_flush = threading.Event()  # Set after TTS finishes; cleared by STT after flushing

# Output device discovery — resolved once on first playback
_output_device = None
_output_device_resolved = False


def _resolve_output_device():
    """Find and cache the audio output device. Runs once, no-op after.

    Prefers real hardware (USB audio, headphones, I2S DACs) over virtual
    ALSA devices like 'default' or 'dmix' which may route to HDMI/null.
    """
    global _output_device, _output_device_resolved
    if _output_device_resolved:
        return

    _output_device_resolved = True

    try:
        devices = sd.query_devices()
    except Exception:
        queue_message("WARNING: Could not query audio devices — using system default")
        _output_device = None
        return

    # Categorize output devices by priority
    pulse_devices = []    # Sound-server devices (PipeWire/PulseAudio "pulse"/"default")
    usb_devices = []      # USB audio — most likely the external speaker
    hw_devices = []       # Hardware devices (headphones, I2S DACs, bcm2835)
    virtual_devices = []  # Other virtual/default ALSA devices

    for i, dev in enumerate(devices):
        if dev.get("max_output_channels", 0) < 1:
            continue
        name = dev.get("name", "").lower()
        # Skip HDMI outputs — they typically don't support standard PCM formats
        if "hdmi" in name:
            continue
        if "pulse" in name:
            pulse_devices.append((i, dev))
        elif "usb" in name:
            usb_devices.append((i, dev))
        elif "default" in name or "dmix" in name or "sysdefault" in name:
            virtual_devices.append((i, dev))
        else:
            hw_devices.append((i, dev))

    # Pick best device: sound-server (pulse) > USB > hardware > virtual.
    # In a desktop session PipeWire owns the raw USB card; opening the hw PCM
    # directly then fails with PaErrorCode -9985 (device unavailable), so the
    # pulse route must win. Every candidate is validated by actually opening
    # a stream; unopenable devices are skipped, and the final fallback is the
    # system default (None), which also routes through the sound server.
    for label, candidates in [("pulse", pulse_devices), ("USB", usb_devices), ("hardware", hw_devices), ("virtual", virtual_devices)]:
        for idx, dev in candidates:
            if _device_opens(idx):
                _output_device = idx
                queue_message(f"INFO: Audio output: {dev['name']} (device {idx}, {label})")
                return
            queue_message(f"WARNING: Audio device {dev['name']} (device {idx}) unavailable — trying next")

    queue_message("WARNING: No directly-openable audio output device found — using system default (sound server)")
    _output_device = None


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
    _output_device = None
    _output_device_resolved = True


def _recover_sound_server():
    """Best-effort restart of the user sound server and PortAudio re-init.

    Servo power-on spikes can wedge the USB audio card: PipeWire keeps a dead
    stream on the PCM (subdevices 0/1), after which BOTH direct hw opens and
    the pulse default fail with PaErrorCode -9985. Restarting the user sound
    services releases the card. No sudo needed — these are --user services.
    """
    import subprocess, time
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"],
            timeout=20, capture_output=True,
        )
        time.sleep(2)
        sd._terminate()
        sd._initialize()
        # PortAudio re-init invalidates every live stream — including the mic
        # hub's shared InputStream (module_mic). Signal the hub so it closes its
        # now-dangling stream and lazily re-creates it on the next read;
        # without this a TTS self-heal deafens the mic until reboot.
        try:
            from modules.module_mic import invalidate_audio_hub
            invalidate_audio_hub()
        except Exception as e:
            queue_message(f"WARNING: could not invalidate mic stream after sound server recovery: {e}")
        queue_message("INFO: sound server restarted after audio device failure — re-resolving output device")
    except Exception as e:
        queue_message(f"WARNING: sound server recovery failed: {e}")


def _reset_output_device_resolution():
    global _output_device, _output_device_resolved
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
    PCM. Returns the Popen (poll it for completion / terminate for barge-in).
    """
    import subprocess, tempfile
    pwplay = shutil.which("pw-play")
    if not pwplay:
        queue_message("WARNING: pw-play not found — cannot use PipeWire-native fallback")
        return None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        sf.write(tmp.name, data, samplerate)
        tmp.close()
        proc = subprocess.Popen([pwplay, tmp.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        queue_message("INFO: playing chunk via pw-play (PipeWire native, bypasses wedged ALSA device)")
        return proc
    except Exception as e:
        queue_message(f"WARNING: pw-play fallback failed: {e}")
        return None


def init_audio_output():
    """Resolve and log the audio output device at startup."""
    _resolve_output_device()


def stop_tts_playback():
    """Signal TTS to stop immediately. Safe to call from any thread."""
    _tts_cancel_event.set()
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

def play_audio_stream(tts_stream, samplerate=22050, channels=1, gain=1.0, normalize=False):
    _resolve_output_device()
    try:
        target_rate = 16000
        with sd.OutputStream(samplerate=target_rate, channels=channels, dtype='int16', blocksize=4096, device=_output_device) as stream:
            for chunk in tts_stream:
                if chunk:
                    audio_data = np.frombuffer(chunk, dtype='int16')

                    # Resample to 16kHz if needed
                    if samplerate != target_rate and samplerate > 0:
                        ratio = target_rate / samplerate
                        new_len = int(len(audio_data) * ratio)
                        indices = np.linspace(0, len(audio_data) - 1, new_len)
                        audio_data = np.interp(indices, np.arange(len(audio_data)), audio_data.astype(np.float32)).astype('int16')

                    if normalize:
                        max_value = np.max(np.abs(audio_data))
                        if max_value > 0:
                            audio_data = audio_data / max_value * 32767

                    audio_data = np.clip(audio_data * gain, -32768, 32767).astype('int16')
                    stream.write(audio_data)
                else:
                    queue_message(f"ERROR: Received empty chunk.")
    except Exception as e:
        queue_message(f"ERROR: Error during audio playback: {e}")


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
                # Use put_nowait with a wait loop so we can check for cancellation
                while not _tts_cancel_event.is_set():
                    try:
                        audio_queue.put_nowait(audio_chunk)
                        break
                    except asyncio.QueueFull:
                        await asyncio.sleep(0.05)
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
                            await asyncio.to_thread(out_stream.write, data)
                        except Exception as write_err:
                            write_error = write_err
                        finally:
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
                            continue

                    pw_proc = None
                    try:
                        sd.play(data, samplerate, device=_output_device)
                    except sd.PortAudioError as e:
                        # Device unavailable (PaErrorCode -9985): the USB card can be
                        # wedged by a dead PipeWire stream after servo power-on spikes.
                        # Restart the user sound server, re-resolve the device, retry once.
                        queue_message(f"WARNING: Audio device {_output_device} failed ({e}) — recovering sound server and retrying")
                        _recover_sound_server()
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

                    # Poll instead of sd.wait() so we can check for barge-in
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
                    if pw_proc is not None:
                        try:
                            import os as _os
                            _os.unlink(pw_proc.args[-1])
                        except Exception:
                            pass

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
    return was_interrupted