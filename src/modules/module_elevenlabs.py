import io
import re
import asyncio
import json
import os
import hashlib
from modules.module_config import load_config, DEFAULT_TTS_TIMEOUT

from modules.module_messageQue import queue_message
# Shared typed provider errors + backoff (single exception hierarchy for the
# fallback chain in module_tts.py — same classes module_fishaudio raises).
from modules.module_fishaudio import (
    TTSProviderError,
    QuotaExhausted,
    RateLimited,
    AuthFailed,
    TransientError,
    _backoff,
)

CONFIG = load_config()

# NOTE: no ElevenLabs SDK client is constructed here. All requests go through
# the aiohttp helper (_post_elevenlabs); an import-time client with a missing
# key raised ValueError (not ImportError) and killed module_tts's import.

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tts", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# One backoff retry for transient failures (429 non-quota / 5xx / timeout),
# then fail over — same policy as the Fish.audio / Speechify providers.
_MAX_ATTEMPTS = 2  # initial try + exactly one retry


def _cfg(key, default=None):
    """Read a [TTS] key from the loaded config at call time (never at import)."""
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
    """Per-request HTTP timeout from [TTS] tts_timeout (seconds, float)."""
    try:
        return float(_cfg("tts_timeout", DEFAULT_TTS_TIMEOUT))
    except (TypeError, ValueError):
        return float(DEFAULT_TTS_TIMEOUT)


def _parse_el_error(error_text):
    """Return (status_token, message) from an ElevenLabs error body.

    EL error bodies are {"detail": {"status": ..., "message": ...}} or
    {"detail": "..."} / {"message": ...}; fall back to the raw text. Only the
    body is parsed — never headers — so the API key cannot leak.
    """
    try:
        payload = json.loads(error_text)
    except Exception:
        return None, (error_text or "")[:200]
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return detail.get("status"), str(detail.get("message") or "")
        if isinstance(detail, str) and detail:
            return None, detail
        msg = payload.get("message")
        if msg:
            return None, str(msg)
    return None, (error_text or "")[:200]


def _classify_el(status, status_token, message):
    """Map an EL HTTP status + body to a typed error class (None = transient)."""
    text = "%s %s" % (status_token or "", message or "")
    if status in (429, 401) and "quota" in text.lower():
        return QuotaExhausted  # 429/401 with a quota body: no credit left
    if status == 429:
        return RateLimited
    if status is not None and 400 <= status < 500:
        return AuthFailed
    return None  # 5xx and anything else -> transient


async def _post_elevenlabs(url, headers, payload, timeout):
    """POST to the ElevenLabs REST API with a per-request timeout.

    Returns the raw audio bytes on HTTP 200 (may be empty). Raises typed
    TTSProviderError subclasses on failure so the fallback chain in
    module_tts.py can fail over with a logged reason:
      429/401 with quota body -> QuotaExhausted (no retry)
      429 (non-quota)         -> one backoff retry, then RateLimited
      other 4xx (401 auth…)   -> AuthFailed (no retry)
      5xx / timeout / conn err-> one backoff retry, then TransientError
    """
    import aiohttp

    attempt = 0
    while True:
        attempt += 1
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    status = response.status
                    if status == 200:
                        return await response.read()
                    error_text = await response.text()
        except TTSProviderError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            if attempt < _MAX_ATTEMPTS:
                await _backoff()
                continue
            raise TransientError(
                "ElevenLabs request failed: %s" % type(exc).__name__
            ) from exc
        # Non-200 HTTP response: classify from the body (never the headers).
        status_token, message = _parse_el_error(error_text)
        exc_class = _classify_el(status, status_token, message)
        label = " [%s]" % status_token if status_token else ""
        if exc_class is QuotaExhausted or exc_class is AuthFailed:
            # No credit / bad credentials: retrying cannot help — fail over now.
            raise exc_class(
                "ElevenLabs returned %s%s: %s" % (status, label, message),
                status_code=status,
            )
        if attempt < _MAX_ATTEMPTS:
            # 429 (non-quota), 5xx: exactly one backoff retry.
            await _backoff()
            continue
        if exc_class is RateLimited:
            raise RateLimited(
                "ElevenLabs returned %s%s after retry: %s" % (status, label, message),
                status_code=status,
            )
        raise TransientError(
            "ElevenLabs returned %s%s after retry: %s" % (status, label, message),
            status_code=status,
        )

def split_into_sentences(text, max_length=80):
    sentences = re.split(r'(?<=[.!?])\s+', text)

    chunks = []
    current_chunk = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        if current_chunk and len(current_chunk + " " + sentence) > max_length:
            chunks.append(current_chunk)
            current_chunk = sentence
        else:
            if current_chunk:
                current_chunk += " " + sentence
            else:
                current_chunk = sentence

    if current_chunk:
        chunks.append(current_chunk)

    return chunks if chunks else [text]

def get_cache_filename(text):
    text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
    return os.path.join(CACHE_DIR, f"elevenlabs_{text_hash}.mp3")

async def synthesize_elevenlabs_streaming(chunk):
    """
    Synthesize using direct REST API to ensure SSML tags are processed.
    The Python SDK sometimes doesn't handle SSML properly, so we use direct HTTP.

    The HTTP client now has a per-request timeout ([TTS] tts_timeout) and
    failures raise typed TTSProviderError subclasses (QuotaExhausted /
    RateLimited / AuthFailed / TransientError) so the fallback chain in
    module_tts.py fails over with a logged reason; an empty 200 body still
    returns None (the chain treats "no chunks" as failover).
    """
    api_key = _cfg("elevenlabs_api_key")
    if not api_key:
        queue_message("ERROR: Elevenlabs API key not found in env file")
        return

    url = "https://api.elevenlabs.io/v1/text-to-speech/%s/stream" % _cfg("elevenlabs_voice_id", "")
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json"
    }

    # Check if model supports SSML
    model_id = _cfg("elevenlabs_model", "")
    is_eleven_v3 = 'v3' in model_id.lower()

    if is_eleven_v3 and '<break' in chunk:
        # Eleven V3 doesn't support SSML, log warning
        queue_message(f"WARNING: Model {model_id} doesn't support SSML tags. Use [pause], [short pause], [long pause] instead.")

    payload = {
        "text": chunk,
        "model_id": model_id,
        "output_format": "mp3_44100_128",
        "optimize_streaming_latency": 3,
        "enable_ssml": True  # CRITICAL: Enable SSML parsing
    }

    # Log the actual text being sent (for debugging)
    if '<break' in chunk:
        queue_message(f"DEBUG: Sending text with SSML: {chunk[:100]}...")

    try:
        audio_bytes = await _post_elevenlabs(url, headers, payload, _tts_timeout())
    except TTSProviderError:
        raise  # typed failure: let the chain fail over with the reason
    except Exception as e:
        queue_message(f"ERROR: ElevenLabs streaming failed: {e}")
        import traceback
        traceback.print_exc()
        return None

    if not audio_bytes:
        queue_message(f"ERROR: ElevenLabs returned empty response")
        return None

    audio_buffer = io.BytesIO(audio_bytes)
    audio_buffer.seek(0)
    return audio_buffer

async def synthesize_elevenlabs_complete(text):
    """
    Synthesize complete speech using direct REST API.

    Same timeout + typed-error policy as synthesize_elevenlabs_streaming.
    """
    api_key = _cfg("elevenlabs_api_key")
    if not api_key:
        queue_message("ERROR: Elevenlabs API key not found in env file")
        return

    url = "https://api.elevenlabs.io/v1/text-to-speech/%s" % _cfg("elevenlabs_voice_id", "")
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json"
    }

    # Check if model supports SSML
    model_id = _cfg("elevenlabs_model", "")
    is_eleven_v3 = 'v3' in model_id.lower()

    if is_eleven_v3 and '<break' in text:
        queue_message(f"WARNING: Model {model_id} doesn't support SSML tags. Use [pause], [short pause], [long pause] instead.")

    payload = {
        "text": text,
        "model_id": model_id,
        "output_format": "mp3_44100_128",
        "enable_ssml": True  # CRITICAL: Enable SSML parsing
    }

    # Log if SSML is present
    if '<break' in text:
        queue_message(f"DEBUG: Sending wakeword with SSML: {text}")

    try:
        audio_bytes = await _post_elevenlabs(url, headers, payload, _tts_timeout())
    except TTSProviderError:
        raise  # typed failure: let the chain fail over with the reason
    except Exception as e:
        queue_message(f"ERROR: ElevenLabs synthesis failed: {e}")
        import traceback
        traceback.print_exc()
        return None

    if not audio_bytes:
        queue_message(f"ERROR: ElevenLabs returned empty response")
        return None

    audio_buffer = io.BytesIO(audio_bytes)
    audio_buffer.seek(0)
    return audio_buffer

async def text_to_speech_with_pipelining_elevenlabs(text, is_wakeword):    

    if is_wakeword:
        cache_file = get_cache_filename(text)

        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'rb') as f:
                    audio_bytes = f.read()
                audio_buffer = io.BytesIO(audio_bytes)
                audio_buffer.seek(0)
                yield audio_buffer
                return
            except Exception as e:
                queue_message(f"ERROR: Failed to load cache: {e}")

        queue_message(f"Caching wakeword: {text}")
        audio_buffer = await synthesize_elevenlabs_complete(text)
        if audio_buffer:
            try:
                audio_bytes = audio_buffer.read()
                with open(cache_file, 'wb') as f:
                    f.write(audio_bytes)

                audio_buffer = io.BytesIO(audio_bytes)
                audio_buffer.seek(0)
                yield audio_buffer
            except Exception as e:
                queue_message(f"ERROR: Failed to cache: {e}")
                audio_buffer.seek(0)
                yield audio_buffer

    else:

        chunks = split_into_sentences(text, max_length=80)
        for i, chunk in enumerate(chunks):
            audio_buffer = await synthesize_elevenlabs_streaming(chunk)
            if audio_buffer:
                yield audio_buffer
            else:
                queue_message(f"WARNING: Chunk {i+1} failed, skipping")