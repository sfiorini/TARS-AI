"""M3 barge-in: TTS cancel latency (S-M3-03, S-M3-04).

S-M3-03 — the persistent OutputStream must be fed in <=100 ms sub-slices
with the cancel event checked between them (a barge-in stops the feed
within one sub-slice), registered as the module's active stream so
stop_tts_playback() can abort it cross-thread (sd.stop() alone never
touched the persistent stream), and cleared in play_chunks' finally so a
stale stream is never aborted by a later utterance's cancel.

S-M3-04 — a cancel-induced write error is a CLEAN cancel: it must never
enter the sd.play recovery ladder, which would keep playing after the
cancel. A genuine device death without a cancel still takes the ladder
(pinned: test_tts_playback_recovery.py
TestPersistentWriteFailureRecovery).

Conventions follow test_tts_playback_recovery.py: unittest + mock, config
bootstrapped from the template, every device call faked.
"""
import asyncio
import os
import shutil
import threading
import time
import unittest
from unittest import mock


# Bootstrap gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_INI = os.path.join(_SRC, "config.ini")
if not os.path.exists(_CONFIG_INI):
    shutil.copyfile(os.path.join(_SRC, "config.ini.template"), _CONFIG_INI)

from modules import module_tts as tts  # noqa: E402
from test_tts_playback_recovery import (  # noqa: E402
    PlayTestCase, _FakeOutputStream, _wav_chunk, _reset_cancel_state,
)


class CancelLatencyTestCase(PlayTestCase):
    """Base: pinned-harness reset plus the M3 active-stream globals."""

    def setUp(self):
        super().setUp()
        # The active-stream globals exist only after S-M3-03 — create/clear
        # them defensively so a RED run fails on assertions, not AttributeErrors.
        tts._active_out_stream = None
        self.addCleanup(setattr, tts, "_active_out_stream", None)


class TestSubsliceCancel(CancelLatencyTestCase):
    """S-M3-03: a mid-write cancel stops the feed within one sub-slice."""

    def test_cancel_mid_write_breaks_within_one_subslice(self):
        completed = []   # monotonic completion time of each sub-slice write
        cancel_at = [None]

        fake = _FakeOutputStream()

        def on_write(stream):
            time.sleep(0.05)          # each sub-slice takes 50 ms
            completed.append(time.monotonic())

        fake.on_write = on_write

        def barge_in():
            time.sleep(0.15)          # cancel mid-playback (~3 slices in)
            cancel_at[0] = time.monotonic()
            tts._tts_cancel_event.set()

        with self._play_ctx([_wav_chunk(1.0)],
                            stream_factory=mock.Mock(return_value=fake)):
            thread = threading.Thread(target=barge_in, daemon=True)
            thread.start()
            result = asyncio.run(tts.play_audio_chunks("hello", {}))
            thread.join(timeout=5)

        self.assertTrue(result, "cancel mid-write did not interrupt playback")
        self.assertTrue(fake.aborted, "active stream was not aborted on cancel")
        # The feed must stop within one sub-slice of the cancel: at most a
        # handful of the ten 100 ms sub-slices complete after cancel onset.
        after_onset = [t for t in completed
                       if cancel_at[0] is not None and t > cancel_at[0]]
        self.assertLessEqual(
            len(after_onset), 3,
            f"{len(after_onset)} sub-slices completed after cancel onset "
            f"(cancel_at={cancel_at[0]}, completed={completed})")
        self.assertLess(len(fake.writes), 10,
                        "the whole 1 s chunk was fed — no sub-slice cancel check")


class TestActiveStreamRegistration(CancelLatencyTestCase):
    """S-M3-03: the persistent stream registers while playing, clears after."""

    def test_active_stream_registered_and_cleared(self):
        fake = _FakeOutputStream()
        seen = []
        fake.on_write = lambda stream: seen.append(
            getattr(tts, "_active_out_stream", None))
        with self._play_ctx([_wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake)):
            asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertEqual(seen, [fake],
                         "stream not registered as active during playback")
        self.assertIsNone(tts._active_out_stream,
                          "active stream not cleared in play_chunks' finally")

    def test_stale_stream_not_aborted_after_playback_ends(self):
        """A finished utterance's stream must never be aborted by a later
        cancel — the finally clears the registration."""
        fake = _FakeOutputStream()
        fake.abort = mock.Mock()
        with self._play_ctx([_wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake)):
            asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertIsNone(tts._active_out_stream)
        with mock.patch.object(tts.sd, "stop"):
            tts.stop_tts_playback()
        fake.abort.assert_not_called()


class TestStopTtsAbortsActiveStream(CancelLatencyTestCase):
    """S-M3-03: stop_tts_playback aborts the registered stream cross-thread."""

    def test_stop_tts_aborts_active_stream(self):
        fake = _FakeOutputStream()
        fake.abort = mock.Mock()
        tts._active_out_stream = fake
        with mock.patch.object(tts.sd, "stop"):
            tts.stop_tts_playback()
        cancel_bumped = tts._tts_cancel_event.is_set()
        _reset_cancel_state()
        tts._active_out_stream = None
        fake.abort.assert_called_once()
        self.assertTrue(cancel_bumped,
                        "the cancel generation bump must still happen")


class TestCancelVsErrorDiscrimination(CancelLatencyTestCase):
    """S-M3-04: a cancel-induced write error is a clean cancel — never the
    sd.play recovery ladder (which would keep playing after the cancel).
    The contrast case (a genuine write failure WITHOUT a cancel still takes
    the ladder) is pinned by test_tts_playback_recovery.py
    TestPersistentWriteFailureRecovery."""

    def test_cancel_induced_error_no_recovery_ladder(self):
        fake = _FakeOutputStream()
        err = tts.sd.PortAudioError("Stream aborted [PaErrorCode -9985]")

        def on_write(stream):
            # The abort racing the in-flight write: the cancel lands and the
            # device write then fails because the stream is being torn down.
            tts._tts_cancel_event.set()
            raise err

        fake.on_write = on_write
        sd_play = mock.Mock()
        with mock.patch.object(tts, "queue_message") as qm:
            with self._play_ctx([_wav_chunk(0.05)],
                                stream_factory=mock.Mock(return_value=fake),
                                sd_play=sd_play):
                result = asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertTrue(result, "a cancel-induced error must read as interrupted")
        sd_play.assert_not_called()
        logged = " ".join(str(call) for call in qm.call_args_list)
        self.assertNotIn("falling back to sd.play recovery", logged,
                         "a cancel entered the sd.play recovery ladder")


if __name__ == "__main__":
    unittest.main()
