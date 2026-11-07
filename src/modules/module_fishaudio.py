"""
module_fishaudio.py

Fish.audio TTS provider for TARS-AI (cloud, low-cost alternative to ElevenLabs).

Endpoint: POST https://api.fish.audio/v1/tts
Auth:     Authorization: Bearer $FISH_AUDIO_API_KEY  (key from .env, never logged)
Model:    sent as the "model" HTTP header on EVERY request — omitting it makes
          Fish silently bill the paid model, so the header is always sent
          (default "s2.1-pro-free", configurable via [TTS] fishaudio_model).
Body:     {text, reference_id (optional), format: "wav", latency: "normal"}
Response: raw audio bytes (wav) on HTTP 200.

Error classification (typed exceptions consumed by the TTS fallback chain):
  402            -> QuotaExhausted  (no credit; no retry)
  429            -> one backoff retry, then RateLimited (concurrency, no Retry-After)
  400/401/403/.. -> AuthFailed      (no retry)
  5xx / timeout  -> one backoff retry, then TransientError

Public interface: text_to_speech_with_fishaudio(text, ...) — async generator
yielding io.BytesIO wav chunks (one per sentence), same shape as the other
provider modules in module_tts.py.
"""

import asyncio
import os
import re
from io import BytesIO

import requests

from modules.module_config import load_config, DEFAULT_FISHAUDIO_MODEL, DEFAULT_TTS_TIMEOUT
from modules.module_messageQue import queue_message

CONFIG = load_config()

FISH_AUDIO_TTS_URL = "https://api.fish.audio/v1/tts"

# One backoff retry for transient failures (429 / 5xx / timeout), then fail over.
_RETRY_BACKOFF_SECONDS = 1.0
_MAX_ATTEMPTS = 2  # initial try + exactly one retry


class TTSProviderError(Exception):
    """Base class for typed TTS provider failures used by the fallback chain."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class QuotaExhausted(TTSProviderError):
    """Provider has no credit left (Fish.audio HTTP 402)."""


class RateLimited(TTSProviderError):
    """Provider is rate-limited / at concurrency limit (Fish.audio HTTP 429)."""


class AuthFailed(TTSProviderError):
    """Bad or missing credentials (Fish.audio HTTP 400/401/403)."""


class TransientError(TTSProviderError):
    """Server-side or network failure (5xx, timeout) after the one retry."""


async def _backoff(seconds=_RETRY_BACKOFF_SECONDS):
    """Async sleep between the initial attempt and the single retry."""
    await asyncio.sleep(seconds)


def _cfg(key, default=None):
    """Read a [TTS] key from the loaded config at call time (never at import)."""
    try:
        return getattr(CONFIG["TTS"], key, None) or default
    except Exception:
        return default


def _split_into_sentences(text, max_length=80):
    """Split text into sentence chunks (same policy as the ElevenLabs provider)."""
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
            current_chunk = (current_chunk + " " + sentence) if current_chunk else sentence
    if current_chunk:
        chunks.append(current_chunk)
    return chunks if chunks else [text]


def _error_message(response):
    """Extract a human-readable message from a Fish.audio error body.

    Error bodies are JSON {"message": ..., "status": ...}; fall back to the
    raw text. Never includes request headers, so the API key cannot leak.
    """
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        msg = payload.get("message") or payload.get("error") or ""
        if isinstance(msg, dict):
            msg = msg.get("message", "")
        if msg:
            return str(msg)
    text = getattr(response, "text", "") or ""
    return text[:200] if text else "no error details"


def _classify(status_code):
    if status_code == 402:
        return QuotaExhausted
    if status_code == 429:
        return RateLimited
    if 400 <= status_code < 500:
        return AuthFailed
    return None  # 5xx and anything else -> transient


async def _synthesize_sentence(sentence, headers, timeout, reference_id, latency="balanced"):
    """POST one sentence to Fish.audio; return BytesIO wav or None (empty audio).

    Raises typed TTSProviderError subclasses on failure so the chain runner can
    fail over to the next provider with a logged reason.
    """
    body = {
        "text": sentence,
        "format": "wav",
        "latency": latency if latency in ("low", "normal", "balanced") else "balanced",
    }
    if reference_id:
        body["reference_id"] = reference_id

    attempt = 0
    while True:
        attempt += 1
        try:
            response = requests.post(
                FISH_AUDIO_TTS_URL, headers=headers, json=body, timeout=timeout
            )
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS:
                await _backoff()
                continue
            raise TransientError(f"Fish.audio request failed: {exc}") from exc

        status = response.status_code
        if status == 200:
            content = response.content
            if content:
                buffer = BytesIO(content)
                buffer.seek(0)
                return buffer
            queue_message("WARNING: Fish.audio returned 200 with empty audio body")
            return None

        exc_class = _classify(status)
        if exc_class is not None and exc_class is not RateLimited:
            # Quota (402) and auth/client errors (400/401/403/...) fail over
            # immediately — retrying cannot help.
            raise exc_class(
                f"Fish.audio returned {status}: {_error_message(response)}",
                status_code=status,
            )
        if attempt < _MAX_ATTEMPTS:
            # 429 (concurrency, no Retry-After) and 5xx: exactly one backoff retry.
            await _backoff()
            continue
        if exc_class is RateLimited:
            raise RateLimited(
                f"Fish.audio returned {status} after retry: {_error_message(response)}",
                status_code=status,
            )
        raise TransientError(
            f"Fish.audio returned {status} after retry: {_error_message(response)}",
            status_code=status,
        )


async def text_to_speech_with_fishaudio(text, reference_id=None, model=None,
                                        timeout=None, **_ignored):
    """Synthesize text with Fish.audio; yield one BytesIO wav chunk per sentence.

    Config values are read at call time (never baked in at import):
      - API key:   FISH_AUDIO_API_KEY env var (.env) — missing key => no request.
      - model:     [TTS] fishaudio_model (default s2.1-pro-free), sent as the
                   "model" HTTP header on every request.
      - reference_id: [TTS] fishaudio_reference_id (voice model id, e.g. the
                   public "Tars" voice); empty => Fish default voice.
      - timeout:   [TTS] tts_timeout seconds.

    Raises QuotaExhausted / RateLimited / AuthFailed / TransientError so the
    ordered fallback chain in module_tts.py can switch providers.
    """
    if not text or not str(text).strip():
        queue_message("WARNING: Fish.audio TTS called with empty text — skipping")
        return

    # Key read at call time (like ELEVENLABS_API_KEY in module_stt); never logged.
    api_key = os.environ.get("FISH_AUDIO_API_KEY", "")
    if not api_key:
        queue_message("WARNING: FISH_AUDIO_API_KEY not set — skipping Fish.audio TTS (no request sent)")
        return

    if not model:
        model = _cfg("fishaudio_model", DEFAULT_FISHAUDIO_MODEL)
    if not reference_id:
        reference_id = _cfg("fishaudio_reference_id", "") or ""
    if not timeout:
        timeout = _cfg("tts_timeout", DEFAULT_TTS_TIMEOUT)
    latency = _cfg("fishaudio_latency", "balanced") or "balanced"
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = float(DEFAULT_TTS_TIMEOUT)

    # The "model" header is ALWAYS sent: omitting it makes Fish silently bill
    # the paid s2.1-pro model instead of the free tier.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "model": model,
    }

    for sentence in _split_into_sentences(str(text)):
        buffer = await _synthesize_sentence(sentence, headers, timeout, reference_id, latency)
        if buffer is not None:
            yield buffer
