"""
module_piper.py

Piper TTS provider for TARS-AI (local, offline).

Streaming (D5): text_to_speech_with_pipelining_piper yields one WAV chunk per
sentence — Piper's native per-sentence iterator (piper-tts >= 1.3, incl. the
robot's 1.8.0, uses voice.synthesize(text, syn_config=SynthesisConfig(...));
piper-tts 1.2.x uses synthesize_stream_raw(); the branch is keyed on
synthesize_stream_raw's presence, which only 1.2.x exposes). Each chunk is
WAV-wrapped at
the voice's native rate (22050 Hz mono 16-bit for TARS.onnx) and optionally
piped through the sox robot-FX chain ([TTS] piper_robot_fx / piper_fx).
ONNX inference AND the sox subprocess run in a worker thread
(asyncio.to_thread) — never on the asyncio event loop — so STT, barge-in and
the web UI stay responsive during synthesis.

BEHAVIOUR CHANGE (D6, speaker IDs): the speaker is now passed as
SynthesisConfig(speaker_id=...) instead of a dead voice.config.speaker_id
assignment that no piper-tts version read (multispeaker voices silently used
speaker 0 = "anger"). piper_speaker_<emotion> actually takes effect.
Escape hatch: set piper_multispeaker = False, or set the piper_speaker_*
ids back to 0 to reproduce the old (accidental) sound.
"""
import sounddevice as sd
import soundfile as sf
from io import BytesIO
from piper.voice import PiperVoice
import asyncio
import wave
import os
import ctypes
import shlex
import shutil
import subprocess
from urllib.request import urlretrieve

# === Custom Modules ===
from modules.module_config import load_config
from modules.module_messageQue import queue_message
from modules.module_fishaudio import _pcm_to_wav_buffer

CONFIG = load_config()

character_path = CONFIG['CHAR']['character_card_path']
character_name = os.path.splitext(os.path.basename(character_path))[0]  # Extract filename without extension

# Define the error handler function type
ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(
    None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p
)

# Define the custom error handler function
def py_error_handler(filename, line, function, err, fmt):
    pass  # Suppress the error message

# Create a C-compatible function pointer
c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)

# Load the ALSA library
asound = ctypes.cdll.LoadLibrary('libasound.so')

# Load the Piper model globally
script_dir = os.path.dirname(__file__)
model_path = os.path.join(script_dir, '..', f'character/{character_name}/voice/{character_name}.onnx')

HF_BASE_URL = "https://huggingface.co/olivierdion007/TARS-AI/resolve/main"

def _download_voice_model(character, dest_path):
    """Download Piper voice model (.onnx and .onnx.json) from Hugging Face.

    Returns True if model is available after download, False on failure.
    """
    voice_dir = os.path.dirname(dest_path)
    os.makedirs(voice_dir, exist_ok=True)

    for ext in [".onnx", ".onnx.json"]:
        url = f"{HF_BASE_URL}/{character}{ext}"
        local = dest_path if ext == ".onnx" else dest_path + ".json"
        if os.path.isfile(local) and not _is_lfs_pointer(local):
            continue
        queue_message(f"[Piper] Downloading {character}{ext} from Hugging Face...")
        try:
            urlretrieve(url, local)
            size_mb = os.path.getsize(local) / (1024 * 1024)
            queue_message(f"[Piper] Downloaded {character}{ext} ({size_mb:.1f} MB)")
        except Exception as e:
            queue_message(f"[Piper] Failed to download {character}{ext}: {e}")
            if os.path.exists(local):
                os.remove(local)
            return False
    return True

def _is_lfs_pointer(filepath):
    """Check if a file is a Git LFS pointer instead of actual content."""
    try:
        with open(filepath, 'rb') as f:
            header = f.read(20)
            return header.startswith(b'version https://git-lfs')
    except Exception:
        return False

voice = None

def _ensure_voice_loaded():
    """Load the Piper voice model on first use, regardless of ttsoption.

    The TTS fallback chain may reach Piper even when ttsoption is another
    provider, so the model cannot be loaded only when ttsoption == 'piper'.
    Returns True when the voice is loaded and usable.
    """
    global voice
    if voice is not None:
        return True
    # Auto-download from Hugging Face if missing or is a Git LFS pointer
    if not os.path.isfile(model_path) or _is_lfs_pointer(model_path):
        queue_message(f"[Piper] Voice model missing or incomplete, attempting download from Hugging Face...")
        if not _download_voice_model(character_name, model_path):
            queue_message("[Piper] Auto-download failed. Please manually place a valid .onnx voice model in the character voice folder.")

    # Load the model if it now exists
    if os.path.isfile(model_path) and not _is_lfs_pointer(model_path):
        try:
            voice = PiperVoice.load(model_path)
            # Warmup: run a dummy synthesis to trigger ONNX runtime JIT
            # so the first real call doesn't pay the compilation cost.
            _warmup_buf = BytesIO()
            with wave.open(_warmup_buf, 'wb') as _wf:
                _wf.setnchannels(1)
                _wf.setsampwidth(2)
                _wf.setframerate(voice.config.sample_rate)
                if hasattr(voice, "synthesize_wav"):
                    voice.synthesize_wav("warm", _wf)
                elif hasattr(voice, "synthesize"):
                    voice.synthesize("warm", _wf)
            del _warmup_buf
            return True
        except Exception as e:
            queue_message(f"[Piper] Failed to load voice model: {e}")
            queue_message(f"[Piper] The file may be corrupt: {model_path}")
            queue_message("[Piper] Try re-downloading the .onnx voice model.")
            voice = None
    return False

if CONFIG['TTS']['ttsoption'] == 'piper':
    _ensure_voice_loaded()

def _get_piper_speaker_id(emotion):
    """Return speaker ID for the given emotion axis, or None if multispeaker is disabled."""
    try:
        if not CONFIG['TTS']['piper_multispeaker']:
            return None
    except (KeyError, AttributeError):
        return None
    # Default to neutral when no emotion detected
    if not emotion:
        emotion = 'neutral'
    try:
        return int(CONFIG['TTS'][f'piper_speaker_{emotion}'])
    except (KeyError, AttributeError, ValueError, TypeError):
        # Fall back to neutral if the specific emotion key doesn't exist
        try:
            return int(CONFIG['TTS']['piper_speaker_neutral'])
        except (KeyError, AttributeError, ValueError, TypeError):
            return None


def _iter_pcm_chunks(voice_obj, text, speaker_id=None):
    """Yield raw int16 mono PCM bytes, one chunk per sentence, at
    voice.config.sample_rate (22050 for TARS.onnx). Version-adaptive, keyed
    on synthesize_stream_raw's presence (ONLY piper-tts 1.2.x has it —
    verified absent on 1.8.0; both versions expose synthesize()): 1.2.x uses
    synthesize_stream_raw(); >= 1.3 (incl. the robot's 1.8.0) uses
    synthesize() with SynthesisConfig."""
    if hasattr(voice_obj, "synthesize_stream_raw"):    # piper-tts 1.2.x ONLY
        for raw in voice_obj.synthesize_stream_raw(text, speaker_id=speaker_id):
            yield raw
    elif hasattr(voice_obj, "synthesize"):              # piper-tts >= 1.3
        try:
            from piper import SynthesisConfig          # piper-tts 1.8.0 package root
        except ImportError:
            from piper.config import SynthesisConfig   # alternate layout
        cfg = SynthesisConfig(speaker_id=speaker_id)   # D6: fixes dead speaker_id
        for chunk in voice_obj.synthesize(text, syn_config=cfg):
            yield chunk.audio_int16_bytes
    else:
        raise AttributeError(
            "Loaded PiperVoice exposes neither synthesize() nor synthesize_stream_raw()")


# Robot-voice sox recipe (user-auditioned chain C2 on the robot's speaker):
# flanger + pitch down. Configurable via [TTS] piper_fx.
DEFAULT_PIPER_FX = "pitch -150 flanger 5 3 0 70 0.4 triangle 25 quad gain -n -3"
SOX_TIMEOUT = 30


def _tts_config_value(key, default=None):
    """Read a [TTS] config value at call time (works for dict-style and TTSConfig-style sections)."""
    try:
        section = CONFIG['TTS']
    except Exception:
        return default
    try:
        getter = getattr(section, 'get', None)
        if callable(getter):
            value = getter(key, default)
        else:
            value = section[key]
    except Exception:
        return default
    return default if value is None else value


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def piper_robot_fx_enabled():
    """True when the sox robot-FX chain should be applied to local Piper output ([TTS] piper_robot_fx)."""
    return _as_bool(_tts_config_value('piper_robot_fx', False), False)


def _run_sox_chain(wav_bytes, fx_chain):
    """Pipe wav bytes through `sox - - <fx_chain>` and return the processed bytes.

    Returns None (and logs a warning) when sox is missing or fails — callers
    then fall back to the unprocessed audio.
    """
    sox = shutil.which('sox')
    if not sox:
        queue_message("WARNING: [Piper] piper_robot_fx is enabled but sox is not installed — using unprocessed audio")
        return None
    # sox cannot sniff the format of stdin ('-'), so declare wav explicitly on both ends
    cmd = [sox, '-t', 'wav', '-', '-t', 'wav', '-'] + shlex.split(fx_chain)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(input=wav_bytes, timeout=SOX_TIMEOUT)
        if proc.returncode == 0 and out:
            return out
        detail = (err or b'').decode('utf-8', 'replace')[:200]
        queue_message(f"WARNING: [Piper] sox FX chain failed (rc={proc.returncode}): {detail}")
        return None
    except Exception as e:
        queue_message(f"WARNING: [Piper] sox FX pipe failed: {e}")
        return None


def _resolve_fx_chain():
    """The sox FX chain to apply, or None when FX is disabled.
    An EMPTY piper_fx falls back to DEFAULT_PIPER_FX — unchanged semantics."""
    if not piper_robot_fx_enabled():
        return None
    chain = (_tts_config_value('piper_fx', '') or DEFAULT_PIPER_FX).strip()
    return chain or None


def _wrap_and_apply_fx(pcm_bytes, sample_rate, fx_chain):
    """BLOCKING: wrap PCM in a WAV header, then pipe through sox. Runs in a
    worker thread. Any sox failure returns the unprocessed WAV — speech is
    never lost."""
    wav = _pcm_to_wav_buffer(pcm_bytes, sample_rate=sample_rate)
    if not fx_chain:
        return wav
    processed = _run_sox_chain(wav.getvalue(), fx_chain)
    if processed is None:
        return wav
    out = BytesIO(processed)
    out.seek(0)
    return out


def apply_piper_fx(wav_buffer):
    """Apply the configured sox robot-FX chain to a synthesized wav buffer.

    Enabled via [TTS] piper_robot_fx; chain from [TTS] piper_fx (defaults to
    DEFAULT_PIPER_FX when empty). Config is read at call time. Any failure
    returns the original buffer unchanged so speech is never lost.
    """
    try:
        fx_chain = _resolve_fx_chain()
        if not fx_chain:
            return wav_buffer
        processed = _run_sox_chain(wav_buffer.getvalue(), fx_chain)
        if processed is None:
            return wav_buffer
        out = BytesIO(processed)
        out.seek(0)
        return out
    except Exception as e:
        queue_message(f"WARNING: [Piper] robot FX error, using unprocessed audio: {e}")
        return wav_buffer


async def text_to_speech_with_pipelining_piper(text, emotion=None):
    """Piper TTS: one WAV chunk per sentence, synthesized OFF the event loop."""
    if not text or not str(text).strip():
        return                                   # NEW guard: matches the other providers
    if not _ensure_voice_loaded():
        queue_message("[Piper] Cannot synthesize - voice model not loaded. Check logs for details.")
        return

    speaker_id = _get_piper_speaker_id(emotion)
    queue_message(f"DEBUG: [Piper] emotion={emotion or 'none'}, speaker_id={speaker_id}, "
                  f"multispeaker={_tts_config_value('piper_multispeaker')}")

    fx_chain = _resolve_fx_chain()               # config read on the LOOP (cheap)
    sample_rate = voice.config.sample_rate        # 22050 for TARS.onnx
    it = _iter_pcm_chunks(voice, str(text), speaker_id=speaker_id)
    sentinel = object()
    try:
        while True:
            # ONNX inference for one sentence runs in a worker thread (D5)
            pcm = await asyncio.to_thread(next, it, sentinel)
            if pcm is sentinel:
                break
            if not pcm:
                continue
            # WAV-wrap + sox FX in the SAME worker thread (D5): neither the
            # wave header write nor the sox subprocess may touch the loop.
            yield await asyncio.to_thread(_wrap_and_apply_fx, pcm, sample_rate, fx_chain)
    finally:
        # Barge-in / the per-utterance deadline can aclose() this generator
        # while a worker thread is still INSIDE `it` (ONNX inference):
        # close() on a RUNNING generator raises ValueError, which the chain
        # would swallow — leaving the generator never actually closed. Skip
        # the close then; `it` holds no open resources and is collected once
        # the abandoned next() returns.
        if not getattr(it, "gi_running", False):
            it.close()