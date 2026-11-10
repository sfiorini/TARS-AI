"""Regression tests: TTS playback correctness in module_tts.py.

Covers the audit findings (ai_plan/2026-11-07-code-docs-audit/plan.md):
  A1 — is_tts_playing() must be True while the persistent OutputStream is
       actually writing audio (set BEFORE out_stream.write, held across
       streamed slices, cleared once the audio drains and in the outer
       finally) so STT's barge-in / self-hearing checks do not treat TTS
       as over mid-utterance.
  A3 — a persistent-stream write failure must abort the stream and fall
       through to the existing sd.play recovery ladder (-9985 recovery,
       sound-server restart, pw-play fallback); _recover_sound_server must
       invalidate the mic hub's dangling InputStream after PortAudio re-init.
  A4 — _recover_sound_server must run via asyncio.to_thread so the event
       loop keeps servicing other tasks (barge-in stays responsive) while
       recovery blocks.
  B1 — the 300ms barge-in window (chunks >= 3s) applies on the persistent
       path as well as the sd.play path; streaming slices (<3s) get none.
  B2 — a barge-in landing between utterances is honored (not erased by the
       utterance start) and an honored cancel is consumed so it does not
       leak into the next utterance.
  B3 — play_audio_stream (dead code) stays deleted.

Conventions follow test_tts_output_device.py / test_module_tts_chain.py:
unittest + mock, sys.path bootstrap, config.ini bootstrapped from the
template, and every device call replaced with fakes so no real audio device
is opened.
"""
import asyncio
import contextlib
import io
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock


# Bootstrap gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_INI = os.path.join(_SRC, "config.ini")
if not os.path.exists(_CONFIG_INI):
    import shutil
    shutil.copyfile(os.path.join(_SRC, "config.ini.template"), _CONFIG_INI)

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from modules import module_tts as tts  # noqa: E402


def _wav_chunk(seconds, rate=44100):
    """A WAV BytesIO of silence — what the TTS providers yield per chunk."""
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(rate * seconds), dtype="float32"), rate, format="WAV")
    buf.seek(0)
    return buf


def _stub_generator(chunks):
    """Stub generate_tts_audio: yields the given chunks, ignores args."""
    async def gen(*args, **kwargs):
        for chunk in chunks:
            yield chunk
    return gen


class _FakeOutputStream:
    """Minimal stand-in for sd.OutputStream (persistent playback path)."""

    def __init__(self):
        self.started = False
        self.aborted = False
        self.stopped = False
        self.closed = False
        self.writes = []
        self.write_error = None   # raise this from write() if set
        self.on_write = None      # callback(stream) hook for tests

    def start(self):
        self.started = True

    def write(self, data):
        self.writes.append(data)
        if self.write_error is not None:
            raise self.write_error
        if self.on_write is not None:
            self.on_write(self)

    def abort(self):
        self.aborted = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _reset_cancel_state():
    """Reset module-level playback/cancel flags between tests."""
    tts._tts_cancel_event.clear()
    tts._tts_playing.clear()
    tts._tts_needs_flush.clear()
    tts._output_device = None
    tts._output_device_resolved = True
    # Generation state exists only after the B2 fix — guard so the same
    # helper works before and after.
    lock = getattr(tts, "_tts_cancel_lock", None)
    if lock is not None:
        with lock:
            tts._tts_cancel_generation = 0
            tts._tts_cancel_consumed_gen = 0


class PlayTestCase(unittest.TestCase):
    """Base class: resets module state and provides the play harness."""

    def setUp(self):
        _reset_cancel_state()

    @contextlib.contextmanager
    def _play_ctx(self, chunks, *, stream_factory=None, sd_play=None,
                  recover=None):
        """Patch everything play_audio_chunks touches except the code under test.

        stream_factory: replacement for sd.OutputStream. Default raises so the
        per-chunk sd.play path is used; pass mock.Mock(return_value=fake) for
        the persistent-stream path.
        sd_play: replacement for sd.play (default: instant no-op Mock).
        recover: replacement for _recover_sound_server (default: no-op Mock).
        """
        if stream_factory is None:
            stream_factory = mock.Mock(side_effect=Exception("no persistent stream"))
        if sd_play is None:
            sd_play = mock.Mock()
        if recover is None:
            recover = mock.Mock()
        with mock.patch.object(tts, "generate_tts_audio", _stub_generator(chunks)), \
             mock.patch.object(tts, "_resolve_output_device"), \
             mock.patch.object(tts.requests, "get"), \
             mock.patch("modules.module_speed.mark_first_audio") as mark_first, \
             mock.patch.object(tts.sd, "OutputStream", stream_factory), \
             mock.patch.object(tts.sd, "play", sd_play), \
             mock.patch.object(tts.sd, "stop"), \
             mock.patch.object(tts.sd, "get_stream", return_value=mock.Mock(active=False)), \
             mock.patch.object(tts, "_recover_sound_server", recover):
            yield types.SimpleNamespace(
                mark_first_audio=mark_first, sd_play=sd_play, recover=recover)


class TestPersistentWriteFailureRecovery(PlayTestCase):
    """A3: a dead persistent stream must fall through to the sd.play ladder."""

    def test_write_failure_aborts_stream_and_falls_back_to_sd_play(self):
        fake = _FakeOutputStream()
        fake.write_error = tts.sd.PortAudioError("Device unavailable [PaErrorCode -9985]")
        sd_play = mock.Mock()
        with self._play_ctx([_wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake),
                            sd_play=sd_play):
            result = asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertTrue(fake.aborted)          # dead stream aborted, not retried
        self.assertTrue(sd_play.called)        # audio still played via sd.play
        self.assertFalse(result)              # not interrupted

    def test_write_failure_closes_aborted_stream(self):
        """F8: on the non-cancel write-error fallback the stream is aborted
        and immediately None'd, so the trailing stop()/close() block never
        runs — the aborted stream must be closed right there or a
        PortAudio stream object leaks per failure (cumulative on a
        wedging card)."""
        fake = _FakeOutputStream()
        fake.write_error = tts.sd.PortAudioError("Device unavailable [PaErrorCode -9985]")
        with self._play_ctx([_wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake)):
            asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertTrue(fake.aborted)
        self.assertTrue(fake.closed)         # closed, not just aborted

    def test_write_failure_runs_full_recovery_ladder(self):
        fake = _FakeOutputStream()
        fake.write_error = tts.sd.PortAudioError("Device unavailable [PaErrorCode -9985]")
        sd_play = mock.Mock(side_effect=[
            tts.sd.PortAudioError("Device unavailable [PaErrorCode -9985]"), None])
        with self._play_ctx([_wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake),
                            sd_play=sd_play) as ctx:
            result = asyncio.run(tts.play_audio_chunks("hello", {}))
        ctx.recover.assert_called_once()      # -9985 recovery ran
        self.assertEqual(sd_play.call_count, 2)  # retried after recovery
        self.assertFalse(result)


class TestResampleDtype(PlayTestCase):
    """A non-44100 provider chunk must still reach the persistent float32 stream.

    Regression: np.interp (the 24000/22050 -> 44100 resample) ALWAYS returns
    float64, while the persistent OutputStream is float32. Writing float64
    raised "dtype mismatch: 'float64' vs 'float32'", dropped the chunk to the
    sd.play recovery path, and was logged as a warning on the robot for every
    Speechify (pcm_24000) and Piper (22050) utterance.
    """

    def test_resampled_chunk_is_written_as_float32(self):
        for rate in (24000, 22050):
            with self.subTest(rate=rate):
                _reset_cancel_state()
                fake = _FakeOutputStream()
                sd_play = mock.Mock()
                with self._play_ctx([_wav_chunk(0.05, rate=rate)],
                                    stream_factory=mock.Mock(return_value=fake),
                                    sd_play=sd_play):
                    asyncio.run(tts.play_audio_chunks("hello", {}))
                self.assertTrue(fake.writes, f"no write at {rate} Hz")
                self.assertEqual(fake.writes[0].dtype, np.float32,
                                 f"resampled {rate} Hz chunk written as "
                                 f"{fake.writes[0].dtype}, not float32")
                self.assertFalse(sd_play.called,
                                 "chunk fell back to sd.play instead of the "
                                 "persistent stream")


class TestRecoverSoundServerInvalidatesMicHub(PlayTestCase):
    """A3: PortAudio re-init must invalidate the mic hub's dangling stream."""

    def test_recover_sound_server_signals_mic_hub(self):
        fake_mic = mock.MagicMock()
        with mock.patch("subprocess.run"), \
             mock.patch.object(tts.time, "sleep"), \
             mock.patch.object(tts.sd, "_terminate"), \
             mock.patch.object(tts.sd, "_initialize"), \
             mock.patch.dict(sys.modules, {"modules.module_mic": fake_mic}):
            tts._recover_sound_server()
        fake_mic.invalidate_audio_hub.assert_called_once()


class TestTtsPlayingDuringPersistentWrite(PlayTestCase):
    """A1: _tts_playing must be set while the persistent stream writes."""

    def test_is_tts_playing_true_during_write(self):
        observed = []
        fake = _FakeOutputStream()
        fake.on_write = lambda stream: observed.append(tts.is_tts_playing())
        with self._play_ctx([_wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake)):
            asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertEqual(observed, [True])
        self.assertTrue(len(fake.writes) == 1)

    def test_flag_held_while_pending_cleared_when_drained_documented_residual(self):
        """F8 (revised residual), pinned. The flag is set during write()
        (A1) and now HELD across consecutive streamed slices: it is
        cleared only once the utterance's audio has actually drained
        (queue empty AND synthesis done), with the outer A1 finally as
        backstop. Residual semantics: while audio is still queued the
        PortAudio ring buffer may drain the previous slice's tail with
        the flag already cleared — accepted, latency='low' keeps the
        tail tiny. Trade-off, stated honestly: mic-based barge-in is no
        longer available mid-utterance while a stream is continuously
        fed — it becomes available once audio drains. Wake-word
        interruption (stop_tts_playback) is a separate path and is
        unaffected. If this test ever fails, the drain contract changed
        — re-evaluate the F8 comment in module_tts.py, do not just flip
        the assertion."""
        samples = []
        fake = _FakeOutputStream()

        def on_write(stream):
            # Make the producer outrun the consumer: while the first
            # slice plays, the generator finishes, so the second write
            # is the one that drains the queue (deterministic ordering —
            # the event loop is free during the to_thread write).
            if len(fake.writes) == 1:
                time.sleep(0.05)

        fake.on_write = on_write
        with self._play_ctx([_wav_chunk(0.05), _wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake)):
            # _tts_needs_flush.set() runs right after each successful
            # write's finally clause — sample the flag there.
            with mock.patch.object(tts, "_tts_needs_flush") as flush:
                flush.set.side_effect = \
                    lambda: samples.append(tts.is_tts_playing())
                asyncio.run(tts.play_audio_chunks("hello", {}))
        # slice 1: chunk 2 still pending -> flag held (no mid-sentence
        # barge-in window)
        # slice 2: queue drained + synthesis done -> flag cleared in the
        # write's finally
        self.assertEqual(samples, [True, False])
        self.assertEqual(len(fake.writes), 2)
        self.assertFalse(tts.is_tts_playing())  # never left set (A1)

    def test_flag_does_not_flicker_between_streamed_slices(self):
        """Regression (live bug): streaming providers yield ~1 s slices,
        and the old unconditional _tts_playing.clear() after EVERY write
        made the flag flicker False roughly once per second
        MID-SENTENCE — module_stt's barge-in loop then collected the
        robot's own voice during each gap and cancelled playback
        mid-sentence. The flag must stay set between consecutive slices
        while the utterance continues."""
        after_first_write = []
        fake = _FakeOutputStream()
        with self._play_ctx([_wav_chunk(0.05), _wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake)) as ctx:
            # mark_first_audio runs right after the FIRST write's finally
            # clause — the exact moment the old code flickered the flag.
            ctx.mark_first_audio.side_effect = \
                lambda: after_first_write.append(tts.is_tts_playing())
            asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertEqual(after_first_write, [True])  # no flicker between slices
        self.assertEqual(len(fake.writes), 2)         # both slices played
        self.assertFalse(tts.is_tts_playing())       # cleared once drained (A1)

    def test_flag_does_not_flicker_between_slices_on_sd_play_path(self):
        """Regression (live bug, second branch): when the USB card is wedged
        (-9985) the persistent stream is unavailable and playback falls back
        to per-chunk sd.play. That branch ALSO cleared _tts_playing after
        every chunk, so the same mid-sentence barge-in cutoff reappeared on
        the fallback path even after the persistent path was fixed.

        Sampled at each sd.play() call: the flag must still be set when the
        SECOND slice starts, i.e. it was not cleared between slices."""
        seen = []
        # stream_factory default raises -> the per-chunk sd.play branch runs
        with self._play_ctx([_wav_chunk(0.05), _wav_chunk(0.05)]) as ctx:
            ctx.sd_play.side_effect = lambda *a, **k: seen.append(tts.is_tts_playing())
            asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertEqual(len(seen), 2, "expected two sd.play calls")
        self.assertFalse(seen[0], "flag should not be set before the first slice")
        self.assertTrue(seen[1],
                        "flag was cleared between slices on the sd.play path — "
                        "barge-in would collect TARS's own voice mid-sentence")
        self.assertFalse(tts.is_tts_playing())   # A1 backstop still clears

    def test_is_tts_playing_cleared_after_playback(self):
        fake = _FakeOutputStream()
        with self._play_ctx([_wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake)):
            asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertFalse(tts.is_tts_playing())


class TestRecoverRunsOffEventLoop(PlayTestCase):
    """A4: _recover_sound_server must run via asyncio.to_thread — the event
    loop must keep servicing other tasks while recovery blocks."""

    def test_event_loop_progresses_while_recovery_blocks(self):
        recover_started = threading.Event()
        recover_finished = threading.Event()
        release = threading.Event()
        ticks = []

        def blocking_recover():
            recover_started.set()
            release.wait(timeout=5)
            recover_finished.set()

        # First sd.play fails with -9985 (triggers recovery), retry succeeds.
        sd_play = mock.Mock(side_effect=[
            tts.sd.PortAudioError("Device unavailable [PaErrorCode -9985]"),
            None,
        ])

        async def watcher():
            while not release.is_set():
                ticks.append(1)
                await asyncio.sleep(0.01)

        async def scenario():
            watcher_task = asyncio.create_task(watcher())
            play_task = asyncio.create_task(tts.play_audio_chunks("hi", {}))
            # Wait until the recovery call is actually inside blocking_recover.
            await asyncio.to_thread(recover_started.wait, 5)
            # This sleep only yields if the loop is NOT frozen inside recovery.
            await asyncio.sleep(0.15)
            ticks_during_recovery = len(ticks)
            recovery_done_at_sample = recover_finished.is_set()
            release.set()
            was_int = await play_task
            watcher_task.cancel()
            return ticks_during_recovery, recovery_done_at_sample, was_int

        with self._play_ctx([_wav_chunk(0.05)], sd_play=sd_play,
                            recover=blocking_recover):
            ticks_during, recovery_done, was_int = asyncio.run(scenario())

        # The watcher kept progressing while recovery was still blocked —
        # i.e. the loop was free, not frozen inside the recovery call.
        self.assertGreaterEqual(ticks_during, 3)
        self.assertFalse(recovery_done)
        self.assertFalse(was_int)


class TestPersistentPathBargeInWindow(PlayTestCase):
    """B1: the 300ms barge-in window (chunks >= 3s) must also run on the
    persistent-stream path, not only in the sd.play branch."""

    def _run_with_fake_sleep(self, seconds, cancel_after=None):
        """Run one chunk through the persistent path, counting asyncio.sleep
        calls from the barge-in window (everything else here is instant)."""
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)
            if cancel_after is not None and len(sleeps) >= cancel_after:
                tts._tts_cancel_event.set()

        fake = _FakeOutputStream()
        with self._play_ctx([_wav_chunk(seconds)],
                            stream_factory=mock.Mock(return_value=fake)):
            with mock.patch("asyncio.sleep", side_effect=fake_sleep):
                result = asyncio.run(tts.play_audio_chunks("hello", {}))
        return result, fake, sleeps

    def test_long_chunk_gets_300ms_window(self):
        result, fake, sleeps = self._run_with_fake_sleep(4.0)
        self.assertFalse(result)
        self.assertEqual(sleeps, [0.05] * 6)  # 6 x 50ms window ran

    def test_streaming_slice_skips_window(self):
        result, fake, sleeps = self._run_with_fake_sleep(0.4)
        self.assertFalse(result)
        self.assertEqual(sleeps, [])  # <3s slices: no window

    def test_cancel_during_window_interrupts(self):
        result, fake, sleeps = self._run_with_fake_sleep(4.0, cancel_after=3)
        self.assertTrue(result)
        self.assertTrue(fake.aborted)
        self.assertEqual(len(sleeps), 3)  # broke out mid-window


class TestPerUtteranceCancelToken(PlayTestCase):
    """B2: a bare _tts_cancel_event.clear() at utterance start must not
    discard a barge-in that landed between utterances; an honored cancel
    must be consumed so it does not leak into the next utterance."""

    def test_bargein_between_utterances_is_honored(self):
        fake = _FakeOutputStream()
        with self._play_ctx([_wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake)):
            # Barge-in lands before the utterance starts (between utterances).
            tts.stop_tts_playback()
            result = asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertTrue(result)                             # honored, not discarded
        self.assertEqual(fake.writes, [])                   # nothing was spoken
        self.assertFalse(tts._tts_cancel_event.is_set())    # consumed once honored

    def test_honored_cancel_does_not_leak_into_next_utterance(self):
        fake = _FakeOutputStream()
        fake.on_write = lambda stream: tts.stop_tts_playback()  # barge-in mid-write
        with self._play_ctx([_wav_chunk(0.05), _wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake)):
            result = asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertTrue(result)

        # The reply to the barge-in must start with a clean slate.
        fake2 = _FakeOutputStream()
        with self._play_ctx([_wav_chunk(0.05)],
                            stream_factory=mock.Mock(return_value=fake2)):
            result2 = asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertFalse(result2)
        self.assertEqual(len(fake2.writes), 1)


class TestSynthesizeChunksBoundedPut(PlayTestCase):
    """C14: synthesize_chunks must use a bounded asyncio.wait_for(queue.put,
    0.05) instead of the put_nowait + sleep(0.05) busy-wait spin, keeping the
    cancel check on every iteration."""

    def test_full_queue_still_yields_chunks_when_consumer_drains(self):
        # 10 chunks through the maxsize=3 queue with a fast consumer: the
        # producer must keep making progress while the consumer drains —
        # every chunk is delivered, none lost to a full queue.
        chunks = [_wav_chunk(0.02) for _ in range(10)]
        fake = _FakeOutputStream()
        with self._play_ctx(chunks, stream_factory=mock.Mock(return_value=fake)):
            result = asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertFalse(result)
        self.assertEqual(len(fake.writes), 10)

    def test_put_nowait_spin_replaced_by_bounded_wait(self):
        # The mechanism itself: no put_nowait/QueueFull busy-wait spin left
        # in play_audio_chunks; the bounded wait_for put is used instead.
        import inspect
        source = inspect.getsource(tts.play_audio_chunks)
        self.assertNotIn("put_nowait", source)
        self.assertNotIn("QueueFull", source)
        self.assertIn("wait_for(audio_queue.put", source)


class TestNoChunkDroppedUnderBackpressure(PlayTestCase):
    """Regression (live bug): play_audio_chunks dropped audio chunks.

    The producer (synthesize_chunks) used a 50 ms deadline on queue.put and
    SILENTLY DISCARDED the chunk on timeout. That contract was written for
    whole-sentence chunks (a drop lost at most one sentence), but streaming
    providers (Speechify, Piper) yield ~1 s slices ~17x faster than realtime,
    so the maxsize=3 queue was permanently full and MOST of the utterance was
    dropped: on the robot a story "jumped a sentence" and playback ended
    after ~2 sentences.

    New contract: the producer retries the bounded put until the consumer
    drains — no audio is ever dropped; cancellation is still honoured every
    retry (~250 ms). Pinned here with a slow consumer: every yielded chunk
    must reach playback.
    """

    def test_slow_consumer_receives_every_chunk(self):
        # 10 chunks through the maxsize=3 queue with a consumer slower than
        # the producer (each write sleeps 120 ms). The old drop-on-timeout put
        # discarded most chunks here; the backpressure retry must deliver all.
        chunks = [_wav_chunk(0.02) for _ in range(10)]
        fake = _FakeOutputStream()

        def slow_write(stream):
            time.sleep(0.12)

        fake.on_write = slow_write
        with self._play_ctx(chunks, stream_factory=mock.Mock(return_value=fake)):
            result = asyncio.run(tts.play_audio_chunks("hello", {}))
        self.assertFalse(result)               # not interrupted, ran to the end
        self.assertEqual(
            len(fake.writes), len(chunks),
            f"{len(fake.writes)}/{len(chunks)} chunks reached playback — "
            f"audio was dropped by a full queue (the live-reported bug)")


class TestDeadCodeRemoval(unittest.TestCase):
    """B3: play_audio_stream (module_tts.py) had zero callers and duplicated
    resample/normalize/gain with different constants — it must stay deleted so
    the dead duplicate cannot drift from play_audio_chunks' real pipeline."""

    def test_play_audio_stream_is_deleted(self):
        self.assertFalse(
            hasattr(tts, "play_audio_stream"),
            "play_audio_stream is dead code (zero callers) and must not exist",
        )


if __name__ == "__main__":
    unittest.main()
