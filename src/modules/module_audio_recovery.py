"""Shared sound-server recovery for the audio input path (decoupled from TTS).

L2 rung of the shared escalation ladder: restarts the user sound services
(Linux only), re-initializes PortAudio, and invalidates the mic hub so its
dangling InputStream is lazily re-created. Reachable from the hub watchdog,
the STT loop, and TTS playback failures alike.
"""
import subprocess
import threading
import time

import sounddevice as sd

from modules.module_messageQue import queue_message

# Reentrancy guard (F1): PortAudio (sd._terminate/_initialize) and the
# systemctl restart are NOT thread-safe — the hub watchdog's L2 rung and
# the STT AudioStallError catch can both fire during a hard wedge. A
# second concurrent caller returns False immediately ("recovery already
# in progress") instead of blocking ~22 s or racing the first caller.
_recovery_lock = threading.Lock()


def restart_sound_server() -> bool:
    """systemctl --user restart pipewire pipewire-pulse wireplumber (timeout=20),
    sleep 2, sd._terminate(); sd._initialize(); invalidate_audio_hub().
    Sets hub _recovery_in_progress (under the hub _lock) around the whole
    thing. Reentrancy-guarded: a concurrent second caller returns False
    immediately without doing any work.
    PLATFORM-GATE SCOPE (Option A): the sys.platform/shutil.which gate covers
    ONLY the systemctl subprocess call. sd._terminate()/sd._initialize()/
    invalidate_audio_hub() run on EVERY platform — they are the actions that
    fix dangling PortAudio streams, and the pre-existing module_tts recovery
    already performed them unguarded.
    Returns True only when the sound services were actually restarted;
    False = "no service restart happened" (off-Linux / no systemctl / any
    failure / a recovery already in progress) — NOT "nothing was done".
    """
    import sys
    import shutil
    from modules.module_mic import _hub, invalidate_audio_hub
    if not _recovery_lock.acquire(blocking=False):
        queue_message("WARNING: sound server recovery already in progress "
                      "— skipping concurrent restart")
        return False
    restarted = False
    try:
        with _hub._lock:                  # F6: contract — written under _lock
            _hub._recovery_in_progress = True
        if sys.platform == "linux" and shutil.which("systemctl") is not None:
            # Scoped to the systemctl call ONLY: a raise or non-zero exit
            # must be reported as failure, not crash the recovery — and must
            # NOT skip the PortAudio/hub housekeeping below (docstring: it
            # runs on EVERY platform, success or failure).
            try:
                result = subprocess.run(
                    ["systemctl", "--user", "restart",
                     "pipewire", "pipewire-pulse", "wireplumber"],
                    timeout=20, capture_output=True,
                )
            except Exception as e:
                queue_message(f"WARNING: systemctl --user restart failed: {e}")
            else:
                if result.returncode == 0:
                    time.sleep(2)
                    restarted = True
                else:
                    queue_message(
                        f"WARNING: systemctl --user restart failed "
                        f"(returncode={result.returncode}) — services NOT restarted")
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
        try:
            with _hub._lock:              # F6: written under _lock
                _hub._recovery_in_progress = False
        finally:
            _recovery_lock.release()
