"""S-M1-06: module_audio_recovery.restart_sound_server + TTS delegate.

Off-robot: systemctl/subprocess are always mocked; the platform pin is
REQUIRED so the intended branch runs regardless of the host OS. sd re-init
and mic-hub invalidation run on EVERY platform (Option A) — that is the
recovery action; only the systemctl subprocess is platform-gated.
"""
import sys
import threading
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


class TestRestartReentrancy:
    """F1: restart_sound_server is reentrancy-guarded — the hub watchdog's
    L2 rung and the STT AudioStallError catch can both fire during a hard
    wedge. A concurrent second caller must return False immediately
    (NOT block ~22 s) and must not touch PortAudio concurrently."""

    def test_concurrent_second_caller_returns_false(self):
        fake_mic = mock.MagicMock()
        entered = threading.Event()
        release = threading.Event()
        results = {}

        def slow_terminate():
            entered.set()                      # first caller is inside the work
            release.wait(5.0)                   # park it there (~22 s L2 stand-in)

        with mock.patch("sys.platform", "darwin"), \
             mock.patch("subprocess.run"), \
             mock.patch.dict(sys.modules, {"modules.module_mic": fake_mic}), \
             mock.patch.object(module_audio_recovery.sd, "_terminate",
                               side_effect=slow_terminate), \
             mock.patch.object(module_audio_recovery.sd, "_initialize") as init, \
             mock.patch.object(module_audio_recovery, "queue_message"):
            t1 = threading.Thread(
                target=lambda: results.__setitem__(
                    "first", module_audio_recovery.restart_sound_server()))
            t1.start()
            assert entered.wait(5.0), "first caller never reached sd._terminate"
            results["second"] = module_audio_recovery.restart_sound_server()
            release.set()
            t1.join(5.0)
            init_calls = init.call_count
        assert not t1.is_alive()
        assert results["second"] is False      # refused immediately, not blocked
        assert init_calls == 1                 # exactly ONE sd._initialize — no race

    def test_sequential_calls_still_run(self):
        """The guard must not latch: after the first caller finishes, the
        next call performs the recovery again."""
        fake_mic = mock.MagicMock()
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("subprocess.run"), \
             mock.patch.dict(sys.modules, {"modules.module_mic": fake_mic}), \
             mock.patch.object(module_audio_recovery.sd, "_terminate"), \
             mock.patch.object(module_audio_recovery.sd, "_initialize") as init, \
             mock.patch.object(module_audio_recovery, "queue_message"):
            module_audio_recovery.restart_sound_server()
            module_audio_recovery.restart_sound_server()
            init_calls = init.call_count
        assert init_calls == 2                 # guard released after the first run


class _RecordingLock:
    """Real lock that records whether writes happen while it is held."""

    def __init__(self):
        self._lock = threading.Lock()
        self.held = False
        self.write_under_lock = []

    def __enter__(self):
        self._lock.acquire()
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        self._lock.release()


class _FakeHub:
    """Hub stand-in whose _recovery_in_progress setter records lock state."""

    def __init__(self):
        self._lock = _RecordingLock()
        self._rip = False

    @property
    def _recovery_in_progress(self):
        return self._rip

    @_recovery_in_progress.setter
    def _recovery_in_progress(self, value):
        self._lock.write_under_lock.append(self._lock.held)
        self._rip = value


class TestRecoveryFlagLockContract:
    """F6: _recovery_in_progress is written under the hub _lock (contract at
    module_mic._AudioHub.__init__: 'written under _lock, read atomically')."""

    def test_flag_written_under_hub_lock(self):
        fake_mic = mock.MagicMock()
        fake_mic._hub = _FakeHub()
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("subprocess.run"), \
             mock.patch.dict(sys.modules, {"modules.module_mic": fake_mic}), \
             mock.patch.object(module_audio_recovery.sd, "_terminate"), \
             mock.patch.object(module_audio_recovery.sd, "_initialize"), \
             mock.patch.object(module_audio_recovery, "queue_message"):
            module_audio_recovery.restart_sound_server()
        writes = fake_mic._hub._lock.write_under_lock
        assert len(writes) == 2               # set + clear
        assert all(writes), "_recovery_in_progress written outside _hub._lock"


class TestTtsDelegate:
    """module_tts._recover_sound_server delegates to the shared module."""

    def test_tts_recovers_delegates(self):
        recorded = []
        with mock.patch.object(module_audio_recovery, "restart_sound_server",
                               side_effect=lambda: recorded.append(1)):
            tts._recover_sound_server()
        assert recorded == [1]
