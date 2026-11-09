"""
module_speechify.py

Speechify TTS provider for TARS-AI (cloud, low-cost alternative to ElevenLabs).

Endpoint: POST https://api.speechify.ai/v1/audio/stream  (streaming, default)
          ONE request per utterance; body {input, voice_id, model, output_format}
          -> raw 16-bit mono PCM (audio/L16) over chunked transfer, coalesced
          into ~1 s WAV slices at the configured rate (pcm_24000 default).
          speechify_stream = False rolls back to the legacy path:
          POST /v1/audio/speech per sentence, body {input, voice_id,
          audio_format: "wav", model} -> JSON {"audio_data": base64}.
Auth:     Authorization: Bearer $SPEECHIFY_API_KEY  (key from .env, never logged)
Session:  one module-level requests.Session is reused for every request
          (measured warm TTFB ~0.2 s vs ~0.7 s cold).

Error classification (typed exceptions consumed by the TTS fallback chain).
Speechify reports failures in the body as {"error": {"code", "message"}}, so
classification branches on error.code FIRST, then falls back to the status:
  payment_required / spend_cap_exceeded / spend_budget_exceeded /
  spend_limit_exceeded  -> QuotaExhausted  (no credit; no retry)
  rate_limited / concurrency_limit_reached -> RateLimited; HTTP 429 carries a
      Retry-After header which is honored for the single backoff retry
  401/403 and other 4xx -> AuthFailed      (no retry)
  408 / 5xx / timeout   -> one backoff retry, then TransientError
                          (408 request-timeout is transient, not an auth failure)

The typed error classes are IMPORTED from module_fishaudio (not redefined) so
the fallback chain in module_tts.py catches a single exception hierarchy.

Mid-stream failures: Speechify closes errored streams silently after the first
byte, so a truncation ends the utterance with a logged WARNING (no mid-utterance
voice switch); the byte-count DEBUG line is the only diagnostic. Typed errors
apply ONLY to pre-stream (pre-first-byte) failures.

Public interface: text_to_speech_with_speechify(text, ...) — async generator
yielding io.BytesIO wav chunks (one per sentence), same shape as the other
provider modules in module_tts.py.
"""

import asyncio
import base64
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO

import requests

from modules.module_config import (
    load_config,
    DEFAULT_SPEECHIFY_VOICE_ID,
    DEFAULT_SPEECHIFY_MODEL,
    DEFAULT_TTS_TIMEOUT,
)
from modules.module_messageQue import queue_message
# Shared typed errors + sentence-splitting policy (single hierarchy for the
# fallback chain; identical chunking policy as the other cloud providers).
from modules.module_fishaudio import (
    TTSProviderError,
    QuotaExhausted,
    RateLimited,
    AuthFailed,
    TransientError,
    _split_into_sentences,
)

CONFIG = load_config()

SPEECHIFY_TTS_URL = "https://api.speechify.ai/v1/audio/speech"

# One backoff retry for transient failures (429 / 5xx / timeout), then fail over.
_RETRY_BACKOFF_SECONDS = 1.0
_MAX_ATTEMPTS = 2  # initial try + exactly one retry
# Retry-After is honored but capped so a huge value cannot hang the chain.
_MAX_RETRY_AFTER_SECONDS = 60.0

# error.code values reported in the response body (branch on code, not status).
_QUOTA_CODES = frozenset({
    "payment_required",
    "spend_cap_exceeded",
    "spend_budget_exceeded",
    "spend_limit_exceeded",
})
_RATE_CODES = frozenset({
    "rate_limited",
    "concurrency_limit_reached",
})

__all__ = [
    "text_to_speech_with_speechify",
    "TTSProviderError",
    "QuotaExhausted",
    "RateLimited",
    "AuthFailed",
    "TransientError",
    "SPEECHIFY_TTS_URL",
    "SPEECHIFY_STREAM_URL",
]


async def _backoff(seconds=_RETRY_BACKOFF_SECONDS):
    """Async sleep between the initial attempt and the single retry."""
    await asyncio.sleep(seconds)


def _cfg(key, default=None):
    """Read a [TTS] key from the loaded config at call time (never at import).

    None-safe: an explicitly-set falsy value (False/0/"") is returned
    verbatim — the `or default` form used to silently override it. The
    default applies only when the key is missing or None.
    """
    try:
        value = getattr(CONFIG["TTS"], key, None)
    except Exception:
        return default
    return default if value is None else value


def _parse_error_body(response):
    """Return (code, message) from a Speechify error body.

    Error bodies are JSON {"error": {"code": ..., "message": ...}}; fall back
    to {"message": ...} or the raw text. Never includes request/response
    headers, so the API key cannot leak.
    """
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or ""
            return code, str(message)
        if isinstance(err, str) and err:
            return None, err
        msg = payload.get("message")
        if msg:
            return None, str(msg)
    text = getattr(response, "text", "") or ""
    return None, text[:200] if text else "no error details"


def _retry_after_seconds(headers, default=_RETRY_BACKOFF_SECONDS,
                         cap=_MAX_RETRY_AFTER_SECONDS):
    """Parse the Retry-After response header (delta-seconds or HTTP-date).

    Returns the wait in seconds, capped at ``cap`` so a huge value cannot hang
    the chain; falls back to ``default`` when absent or unparsable.
    """
    if not headers:
        return default
    raw = None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return default
    if raw is None:
        return default
    try:
        return min(max(float(raw), 0.0), cap)
    except (TypeError, ValueError):
        pass
    try:  # HTTP-date form
        when = parsedate_to_datetime(str(raw))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return min(max(delta, 0.0), cap)
    except Exception:
        return default


def _decode_audio_data(payload):
    """Decode {"audio_data": base64} into raw bytes, or None (with warning)."""
    audio_b64 = payload.get("audio_data") if isinstance(payload, dict) else None
    if not audio_b64 or not isinstance(audio_b64, str):
        queue_message("WARNING: Speechify returned 200 with no audio_data field")
        return None
    compact = re.sub(r"\s+", "", audio_b64)
    try:
        raw = base64.b64decode(compact, validate=True)
    except Exception:
        queue_message("WARNING: Speechify returned invalid base64 audio_data")
        return None
    if not raw:
        queue_message("WARNING: Speechify returned empty audio")
        return None
    return raw


async def _synthesize_sentence(sentence, headers, body, timeout):
    """POST one sentence to Speechify; return BytesIO wav or None (empty audio).

    Raises typed TTSProviderError subclasses on failure so the chain runner can
    fail over to the next provider with a logged reason.
    """
    payload = dict(body)
    payload["input"] = sentence

    attempt = 0
    while True:
        attempt += 1
        try:
            # to_thread: never block the event loop on HTTP (app would freeze)
            response = await asyncio.to_thread(
                requests.post, SPEECHIFY_TTS_URL, headers=headers, json=payload, timeout=timeout
            )
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_ATTEMPTS:
                await _backoff()
                continue
            raise TransientError(f"Speechify request failed: {exc}") from exc

        status = response.status_code
        if status == 200:
            try:
                response_payload = response.json()
            except Exception:
                response_payload = None
            raw = _decode_audio_data(response_payload)
            if raw is not None:
                buffer = BytesIO(raw)
                buffer.seek(0)
                return buffer
            return None

        code, message = _parse_error_body(response)

        # Branch on error.code first (per Speechify API), then on the status.
        if code in _QUOTA_CODES or (status == 402 and code not in _RATE_CODES):
            # No credit: retrying cannot help — fail over immediately.
            label = f" [{code}]" if code else ""
            raise QuotaExhausted(
                f"Speechify returned {status}{label}: {message}",
                status_code=status,
            )
        if code in _RATE_CODES or status == 429:
            if attempt < _MAX_ATTEMPTS:
                # Honor the Retry-After header on the single backoff retry.
                await _backoff(_retry_after_seconds(getattr(response, "headers", None)))
                continue
            label = f" [{code}]" if code else ""
            raise RateLimited(
                f"Speechify returned {status}{label} after retry: {message}",
                status_code=status,
            )
        if 400 <= status < 500 and status != 408:
            # 401/403 auth and other client errors: no retry, fail over now.
            # 408 (request timeout) is transient: it falls through to the
            # backoff-retry path below instead of the permanent AuthFailed bucket.
            label = f" [{code}]" if code else ""
            raise AuthFailed(
                f"Speechify returned {status}{label}: {message}",
                status_code=status,
            )
        # 5xx and anything else: exactly one backoff retry, then transient.
        if attempt < _MAX_ATTEMPTS:
            await _backoff()
            continue
        label = f" [{code}]" if code else ""
        raise TransientError(
            f"Speechify returned {status}{label} after retry: {message}",
            status_code=status,
        )


async def text_to_speech_with_speechify(text, voice_id=None, model=None,
                                        timeout=None, **_ignored):
    """Synthesize text with Speechify; yield one BytesIO wav chunk per sentence.

    Config values are read at call time (never baked in at import):
      - API key:  SPEECHIFY_API_KEY env var (.env) — missing key => no request.
      - voice_id: [TTS] speechify_voice_id (default "george").
      - model:    [TTS] speechify_model (default "simba-3.5"), sent verbatim
                  in the request body.
      - timeout:  [TTS] tts_timeout seconds.

    Raises QuotaExhausted / RateLimited / AuthFailed / TransientError so the
    ordered fallback chain in module_tts.py can switch providers.
    """
    if not text or not str(text).strip():
        queue_message("WARNING: Speechify TTS called with empty text — skipping")
        return

    # Key read at call time (like ELEVENLABS_API_KEY in module_stt); never logged.
    api_key = os.environ.get("SPEECHIFY_API_KEY", "")
    if not api_key:
        queue_message("WARNING: SPEECHIFY_API_KEY not set — skipping Speechify TTS (no request sent)")
        return

    if not voice_id:
        voice_id = _cfg("speechify_voice_id", DEFAULT_SPEECHIFY_VOICE_ID)
    if not model:
        model = _cfg("speechify_model", DEFAULT_SPEECHIFY_MODEL)
    if not timeout:
        timeout = _cfg("tts_timeout", DEFAULT_TTS_TIMEOUT)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = float(DEFAULT_TTS_TIMEOUT)

    headers = {"Authorization": f"Bearer {api_key}"}
    body = {
        "voice_id": voice_id,
        "audio_format": "wav",
        "model": model,
    }

    for sentence in _split_into_sentences(str(text)):
        buffer = await _synthesize_sentence(sentence, headers, body, timeout)
        if buffer is not None:
            yield buffer
