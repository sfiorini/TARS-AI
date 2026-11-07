"""
Module Openai
Author: Charles-Olivier Dion (AtomikSpace)
Contact: atomikspace.labs@gmail.com
Copyright (c) 2026 Charles-Olivier Dion

This file is authored by Charles-Olivier Dion and is dual-licensed.

Non-Commercial License:
This file is licensed under Creative Commons Attribution-NonCommercial 4.0 International (CC-BY-NC 4.0).
You may use, modify, and redistribute this file for NON-COMMERCIAL purposes only, with attribution.

Commercial License:
Commercial use (including selling products, paid services, SaaS, subscriptions, Patreon rewards, or derivatives)
requires a separate written license from Charles-Olivier Dion (AtomikSpace).

This license applies only to this file and does not override licenses of other files in the repository.
"""

import io
import re
import os
import hashlib
import openai
from modules.module_messageQue import queue_message
from modules.module_config import load_config, DEFAULT_TTS_TIMEOUT
# Shared typed provider errors (single exception hierarchy for the fallback
# chain in module_tts.py — same classes module_fishaudio raises).
from modules.module_fishaudio import (
    TTSProviderError,
    QuotaExhausted,
    RateLimited,
    AuthFailed,
    TransientError,
)

CONFIG = load_config()
openai.api_key = CONFIG["TTS"]["openai_api_key"]
VOICE = CONFIG["TTS"]["openai_voice"]

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tts", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)


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


def _classify_openai_error(exc):
    """Map an openai SDK exception to a typed TTSProviderError.

    Classification branches on the HTTP status and the body's error.code:
      429 with a quota body (insufficient_quota / "quota") -> QuotaExhausted
      429 otherwise                                        -> RateLimited
      401/403 and other 4xx                                -> AuthFailed
      timeout / connection error / 5xx / anything else     -> TransientError
    Only the response body is inspected — never request headers — so the API
    key cannot leak into the raised message.
    """
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    code = ""
    message = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            message = str(err.get("message") or "")
    detail = message or type(exc).__name__
    label = " [%s]" % code if code else ""
    if status == 429 and "quota" in ("%s %s" % (code, message)).lower():
        return QuotaExhausted(
            "OpenAI TTS returned 429%s: %s" % (label, detail), status_code=429)
    if status == 429:
        return RateLimited(
            "OpenAI TTS returned 429%s: %s" % (label, detail), status_code=429)
    if status is not None and 400 <= status < 500:
        return AuthFailed(
            "OpenAI TTS returned %s%s: %s" % (status, label, detail), status_code=status)
    return TransientError("OpenAI TTS failed (%s): %s" % (type(exc).__name__, detail))


def get_cache_filename(text):
    """Generate a cache filename based on text hash"""
    text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
    return os.path.join(CACHE_DIR, f"openai_{text_hash}.mp3")


async def text_to_speech_with_pipelining_openai(text, is_wakeword):
    """Synthesize with the OpenAI TTS SDK; yield one BytesIO chunk.

    The SDK request gets a per-request timeout ([TTS] tts_timeout, default
    30 s) and failures raise typed TTSProviderError subclasses instead of
    yielding None, so the ordered fallback chain in module_tts.py fails over
    to the next provider (a None chunk would count as "produced audio" and
    block failover). A missing API key skips the provider before any HTTP
    call. Signature is unchanged.
    """
    api_key = _cfg("openai_api_key")
    if not api_key:
        queue_message("WARNING: OPENAI_API_KEY not set — skipping OpenAI TTS (no request sent)")
        return
    openai.api_key = api_key
    voice = _cfg("openai_voice", VOICE)
    timeout = _tts_timeout()

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
                queue_message(f"ERROR: Failed to load cache file: {e}")

        try:
            response = openai.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text,
                timeout=timeout
            )

            audio_bytes = response.read()
        except Exception as e:
            # Typed classification so the chain fails over with a reason.
            raise _classify_openai_error(e) from e

        try:
            with open(cache_file, 'wb') as f:
                f.write(audio_bytes)
        except Exception as e:
            queue_message(f"ERROR: Failed to cache audio: {e}")

        audio_buffer = io.BytesIO(audio_bytes)
        audio_buffer.seek(0)
        yield audio_buffer
    else:
        try:
            response = openai.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text,
                timeout=timeout
            )

            audio_bytes = response.read()
        except Exception as e:
            # Typed classification so the chain fails over with a reason.
            raise _classify_openai_error(e) from e

        audio_buffer = io.BytesIO(audio_bytes)
        audio_buffer.seek(0)

        yield audio_buffer