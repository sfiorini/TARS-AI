"""S-M1-06: module_audio_recovery.restart_sound_server + TTS delegate.

Off-robot: systemctl/subprocess are always mocked; the platform pin is
REQUIRED so the intended branch runs regardless of the host OS. sd re-init
and mic-hub invalidation run on EVERY platform (Option A) — that is the
recovery action; only the systemctl subprocess is platform-gated.
"""
import sys
from unittest import mock

from modules import module_audio_recovery
from modules import module_tts as tts


class TestRestartSoundServer:
    """Option A: gate ONLY the systemctl subprocess; sd re-init + hub
    invalidation run on every platform inside _recovery_in_progress."""

    def test_restart_off_robot_returns_false(self):
        fake_mic = mock.MagicMock()
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("subprocess.run") as sub_run, \
             mock.patch.dict(sys.modules, {"modules.module_mic": fake_mic}), \
             mock.patch.object(module_audio_recovery.sd, "_terminate") as term, \
             mock.patch.object(module_audio_recovery.sd, "_initialize") as init, \
             mock.patch.object(module_audio_recovery, "queue_message"):
            result = module_audio_recovery.restart_sound_server()
        assert result is False                       # no service restart happened
        sub_run.assert_not_called()                  # systemctl NEVER runs off-Linux
        term.assert_called_once()
        init.assert_called_once()
        fake_mic.invalidate_audio_hub.assert_called_once()

    def test_restart_linux_runs_systemctl(self):
        fake_mic = mock.MagicMock()
        with mock.patch("sys.platform", "linux"), \
             mock.patch("shutil.which", return_value="/usr/bin/systemctl"), \
             mock.patch("subprocess.run") as sub_run, \
             mock.patch.object(module_audio_recovery.time, "sleep"), \
             mock.patch.dict(sys.modules, {"modules.module_mic": fake_mic}), \
             mock.patch.object(module_audio_recovery.sd, "_terminate"), \
             mock.patch.object(module_audio_recovery.sd, "_initialize"), \
             mock.patch.object(module_audio_recovery, "queue_message"):
            result = module_audio_recovery.restart_sound_server()
        assert result is True
        sub_run.assert_called_once_with(
            ["systemctl", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"],
            timeout=20, capture_output=True,
        )
        fake_mic.invalidate_audio_hub.assert_called_once()

    def test_recovery_in_progress_flag_set_and_cleared(self):
        fake_mic = mock.MagicMock()
        seen = {}

        def record_flag():
            seen["during"] = fake_mic._hub._recovery_in_progress

        with mock.patch("sys.platform", "darwin"), \
             mock.patch("subprocess.run"), \
             mock.patch.dict(sys.modules, {"modules.module_mic": fake_mic}), \
             mock.patch.object(module_audio_recovery.sd, "_terminate",
                               side_effect=record_flag), \
             mock.patch.object(module_audio_recovery.sd, "_initialize"), \
             mock.patch.object(module_audio_recovery, "queue_message"):
            module_audio_recovery.restart_sound_server()
        assert seen["during"] is True    # set around the whole recovery
        assert fake_mic._hub._recovery_in_progress is False  # cleared in finally

    def test_recovery_flag_cleared_on_failure(self):
        fake_mic = mock.MagicMock()
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("subprocess.run"), \
             mock.patch.dict(sys.modules, {"modules.module_mic": fake_mic}), \
             mock.patch.object(module_audio_recovery.sd, "_terminate",
                               side_effect=RuntimeError("PA gone")), \
             mock.patch.object(module_audio_recovery.sd, "_initialize"), \
             mock.patch.object(module_audio_recovery, "queue_message") as qm:
            result = module_audio_recovery.restart_sound_server()
        assert result is False           # failure -> False, never raises
        assert fake_mic._hub._recovery_in_progress is False  # finally cleared
        assert qm.called                 # failure logged


class TestTtsDelegate:
    """module_tts._recover_sound_server delegates to the shared module."""

    def test_tts_recovers_delegates(self):
        recorded = []
        with mock.patch.object(module_audio_recovery, "restart_sound_server",
                               side_effect=lambda: recorded.append(1)):
            tts._recover_sound_server()
        assert recorded == [1]
