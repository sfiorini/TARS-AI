"""
module_audio_devices.py — Shared PortAudio device discovery.

module_mic._find_input_device and module_tts._resolve_output_device used to
duplicate device-candidate logic with different priorities and validation
rules. This module keeps both behaviors exactly:

- Output: pulse (sound server) > USB > hardware > virtual ALSA devices.
  HDMI outputs are skipped; every candidate is validated by actually
  opening a short OutputStream (unopenable devices are skipped with a
  warning). Final fallback is (None, None, None) = system default, which
  also routes through the sound server.

- Input: the system default if it looks like real hardware, else a pulse
  device, else any real hardware device, else any device with input
  channels, else the PortAudio C-level default. Input devices are NEVER
  stream-validated: opening the mic would race the audio hub's exclusive
  InputStream.
"""
import sounddevice as sd


def _opens(idx, kind="output"):
    """True if a short stream can be opened on device idx right now.

    Only output devices are probed this way; input devices are never
    stream-validated (see module docstring).
    """
    try:
        with sd.OutputStream(device=idx, samplerate=16000, channels=1,
                             dtype='float32', blocksize=1024):
            return True
    except Exception:
        return False


def _is_real_hw_device(info):
    """True if the device looks like real hardware, not an ALSA plugin.

    ALSA plugin devices (default, dmix, etc.) report 128 channels which is
    a dead giveaway. Real USB mics report 1-2 channels.
    """
    return 1 <= info.get("max_input_channels", 0) <= 32


def _find_output_device(validate=True, validator=None, log=None):
    """Scan for the best output device. Returns (idx, dev, label).

    Priority: pulse (sound server) > USB > hardware > virtual. HDMI outputs
    are skipped. Every candidate is open-validated (or checked with the
    caller-supplied `validator`) unless validate=False; unopenable devices
    are skipped with a per-candidate warning via `log`. Returns
    (None, None, None) when nothing opens — callers fall back to the
    system default, which also routes through the sound server.
    """
    if log is None:
        log = print

    devices = sd.query_devices()

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
    # pulse route must win.
    for label, candidates in [("pulse", pulse_devices), ("USB", usb_devices),
                             ("hardware", hw_devices), ("virtual", virtual_devices)]:
        for idx, dev in candidates:
            if validate:
                if validator is not None:
                    ok = validator(idx)
                else:
                    ok = _opens(idx, kind="output")
                if not ok:
                    log(f"WARNING: Audio device {dev['name']} (device {idx}) unavailable — trying next")
                    continue
            log(f"INFO: Audio output: {dev['name']} (device {idx}, {label})")
            return idx, dev, label

    log("WARNING: No directly-openable audio output device found — using system default (sound server)")
    return None, None, None


def _find_input_device():
    """Scan for a working input device. Returns (idx, info, label).

    Never stream-validates candidates: opening the mic would race the
    audio hub's exclusive InputStream. Returns (None, None, None) when no
    input device exists at all.
    """
    # 1. Try the system default — but only if it looks like real hardware.
    #    ALSA plugin devices (e.g. "default") report 128 channels and may
    #    hide the actual hardware rate, causing wrong device selection.
    idx = sd.default.device[0]
    if idx is not None and idx >= 0:
        info = sd.query_devices(idx, kind="input")
        if _is_real_hw_device(info):
            return idx, info, "default"

    devices = sd.query_devices()

    # 2. Default failed or is a plugin — if a sound server (PipeWire/PulseAudio)
    #    is present, prefer its device: in a desktop session the server owns the
    #    raw USB card, and opening the hw PCM directly fails (device unavailable).
    for i, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) >= 1 and "pulse" in dev.get("name", "").lower():
            return i, dev, "pulse"

    # 3. Scan for real hardware devices
    for i, dev in enumerate(devices):
        if _is_real_hw_device(dev):
            return i, dev, "hardware"

    # 4. Fall back to any device with input channels (including plugins)
    for i, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) >= 1:
            return i, dev, "any"

    # 5. Last resort: ask PortAudio C-level for the default input
    try:
        pa_idx = sd._lib.Pa_GetDefaultInputDevice()
        if pa_idx >= 0:
            info = sd.query_devices(pa_idx, kind="input")
            return pa_idx, info, "portaudio-default"
    except Exception:
        pass

    return None, None, None


def find_device(kind, validate=True, validator=None, log=None):
    """Find an audio device. Returns (idx, device_info, label).

    kind="output": pulse > USB > hardware > virtual priority, HDMI skipped,
    every candidate open-validated (unless validate=False or a custom
    `validator` is given); (None, None, None) = use the system default.

    kind="input": system default if real hardware, else pulse, else real
    hardware, else any input, else the PortAudio C default. Input devices
    are never stream-validated regardless of `validate` — opening the mic
    would race the audio hub's exclusive InputStream.
    """
    if kind == "output":
        return _find_output_device(validate=validate, validator=validator, log=log)
    if kind == "input":
        return _find_input_device()
    raise ValueError(f"unknown device kind: {kind!r} (expected 'input' or 'output')")
