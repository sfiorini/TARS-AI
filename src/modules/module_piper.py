import sounddevice as sd
import soundfile as sf
from io import BytesIO
from piper.voice import PiperVoice
import wave
import re
import os
import ctypes
import shlex
import shutil
import subprocess
from urllib.request import urlretrieve

# === Custom Modules ===
from modules.module_config import load_config
from modules.module_messageQue import queue_message

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


# Robot-voice sox recipe (inspired by dsidlo/ComfyUI_SoX_Effects SoxVeRobotNode):
# chorus + overdrive + pitch down. Configurable via [TTS] piper_fx.
DEFAULT_PIPER_FX = "chorus 0.5 0.9 50 0.4 0.25 2 -t overdrive 10 pitch -100"
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
    cmd = [sox, '-', '-'] + shlex.split(fx_chain)
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


def apply_piper_fx(wav_buffer):
    """Apply the configured sox robot-FX chain to a synthesized wav buffer.

    Enabled via [TTS] piper_robot_fx; chain from [TTS] piper_fx (defaults to
    DEFAULT_PIPER_FX when empty). Config is read at call time. Any failure
    returns the original buffer unchanged so speech is never lost.
    """
    try:
        if not piper_robot_fx_enabled():
            return wav_buffer
        fx_chain = (_tts_config_value('piper_fx', '') or DEFAULT_PIPER_FX).strip()
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


async def synthesize(voice, chunk, speaker_id=None):
    """
    Synthesize a chunk of text into a BytesIO buffer.
    """
    wav_buffer = BytesIO()
    with wave.open(wav_buffer, 'wb') as wav_file:
        wav_file.setnchannels(1)  # Mono
        wav_file.setsampwidth(2)  # 16-bit samples
        wav_file.setframerate(voice.config.sample_rate)
        try:
            # Set speaker on the voice config for multispeaker models
            if speaker_id is not None:
                voice.config.speaker_id = speaker_id

            # need both methods for compatibility
            if hasattr(voice, "synthesize_wav"):
                voice.synthesize_wav(chunk, wav_file)
            elif hasattr(voice, "synthesize"):
                voice.synthesize(chunk, wav_file)
            else:
                raise AttributeError("Neither synthesize_wav nor synthesize found in voice object")

        except Exception as e:
            queue_message(f"ERROR during synthesis: {e}")
    wav_buffer.seek(0)
    return wav_buffer

async def text_to_speech_with_pipelining_piper(text, emotion=None):
    """
    Converts text to speech using the Piper model and streams audio as it's generated.
    When piper_multispeaker is enabled, selects speaker based on detected emotion.
    """
    if not _ensure_voice_loaded():
        queue_message("[Piper] Cannot synthesize - voice model not loaded. Check logs for details.")
        return

    speaker_id = _get_piper_speaker_id(emotion)
    queue_message(f"DEBUG: [Piper] emotion={emotion or 'none'}, speaker_id={speaker_id}, multispeaker={CONFIG['TTS']['piper_multispeaker']}")

    # Split text into smaller chunks
    # Split at sentence boundaries and commas for faster first-chunk playback
    chunks = re.split(r'(?<=[.!?;])\s+|,\s+', text)

    #chunks = [c.strip() for c in chunks if len(c.strip()) >= 3]
    #fix for missing "hi" or "hey"
    chunks = [c.strip() for c in chunks if c.strip()]
    # If splitting produced nothing (e.g. short text like "Hi"), use original text
    if not chunks and text.strip():
        chunks = [text.strip()]

    # Yield each audio chunk as soon as it's ready
    for chunk in chunks:
        if chunk.strip():  # Ignore empty chunks
            wav_buffer = await synthesize(voice, chunk.strip(), speaker_id=speaker_id)
            yield apply_piper_fx(wav_buffer)  # sox robot-FX post-processing when piper_robot_fx is enabled