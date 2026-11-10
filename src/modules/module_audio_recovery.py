"""Shared sound-server recovery for the audio input path (decoupled from TTS).

L2 rung of the shared escalation ladder: restarts the user sound services
(Linux only), re-initializes PortAudio, and invalidates the mic hub so its
dangling InputStream is lazily re-created. Reachable from the hub watchdog,
the STT loop, and TTS playback failures alike.
"""
import subprocess
import time

import sounddevice as sd

from modules.module_messageQue import queue_message


def restart_sound_server() -> bool:
    """systemctl --user restart pipewire pipewire-pulse wireplumber (timeout=20),
    sleep 2, sd._terminate(); sd._initialize(); invalidate_audio_hub().
    Sets hub _recovery_in_progress around the whole thing.
    PLATFORM-GATE SCOPE (Option A): the sys.platform/shutil.which gate covers
    ONLY the systemctl subprocess call. sd._terminate()/sd._initialize()/
    invalidate_audio_hub() run on EVERY platform — they are the actions that
    fix dangling PortAudio streams, and the pre-existing module_tts recovery
    already performed them unguarded.
    Returns True only when the sound services were actually restarted;
    False = "no service restart happened" (off-Linux / no systemctl / any
    failure) — NOT "nothing was done".
    """
    import sys
    import shutil
    from modules.module_mic import _hub, invalidate_audio_hub
    restarted = False
    _hub._recovery_in_progress = True
    try:
        if sys.platform == "linux" and shutil.which("systemctl") is not None:
            subprocess.run(
                ["systemctl", "--user", "restart",
                 "pipewire", "pipewire-pulse", "wireplumber"],
                timeout=20, capture_output=True,
            )
            time.sleep(2)
            restarted = True
        else:
            queue_message("WARNING: systemctl restart skipped (not Linux / systemctl missing) "
                          "— PortAudio re-init + mic hub invalidation only")
        sd._terminate()
        sd._initialize()
        invalidate_audio_hub()
        queue_message("INFO: sound server recovery ran (services_restarted=%s) "
                      "— mic hub invalidated for lazy re-create" % restarted)
        return restarted
    except Exception as e:
        queue_message(f"WARNING: sound server recovery failed: {e}")
        return False
    finally:
        _hub._recovery_in_progress = False


def reprobe_audio() -> None:
    """Reset TTS output-device resolution + invalidate the mic hub. Used
    post-boot-movement (M2) so streams open against a re-enumerated card."""
    try:
        from modules.module_tts import _reset_output_device_resolution
        _reset_output_device_resolution()
        from modules.module_mic import invalidate_audio_hub
        invalidate_audio_hub()
    except Exception as e:
        queue_message(f"WARNING: audio reprobe failed: {e}")
