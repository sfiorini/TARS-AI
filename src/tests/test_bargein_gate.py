"""EchoGate unit tests (S-M3-02): pure logic, no SDK, no audio device.

The gate is the SOLE owner of the candidate streak: `on_frame` returns
"candidate" on the triggering frame only and "bleed" on the non-triggering
above-gate frames of a streak.  Callers must not keep their own counter.
"""
import time
import unittest

from modules.module_bargein import EchoGate


def make_gate(**kwargs):
    return EchoGate(silence_threshold=0.01, rms_scale=1.0, **kwargs)


class TestEchoGate(unittest.TestCase):
    def test_ema_convergence(self):
        """Playing frames at rms=0.05 converge the bleed floor to ~0.05."""
        gate = make_gate()
        for _ in range(50):
            self.assertEqual(gate.on_frame(0.05, tts_playing=True), "bleed")
        self.assertAlmostEqual(gate.bleed_floor, 0.05, delta=0.05 * 0.05)

    def test_ema_never_drags_floor_down_mid_playback(self):
        """A silent frame during playback must not lower the floor."""
        gate = make_gate()
        for _ in range(50):
            gate.on_frame(0.05, tts_playing=True)
        floor = gate.bleed_floor
        gate.on_frame(0.0, tts_playing=True)
        self.assertGreaterEqual(gate.bleed_floor, floor)

    def test_threshold_max_of_both(self):
        """threshold == max(silence_threshold*rms_scale, bleed_floor*bleed_factor)."""
        gate = make_gate(bleed_factor=2.5)
        for _ in range(50):
            gate.on_frame(0.05, tts_playing=True)
        self.assertAlmostEqual(gate.bleed_floor, 0.05, delta=0.05 * 0.05)
        # bleed term dominates: 0.05*2.5 = 0.125 > 0.01*1.0
        self.assertAlmostEqual(gate.threshold, 0.125, delta=0.0125)
        # and equals the max of both terms exactly
        self.assertEqual(
            gate.threshold,
            max(gate.silence_threshold * gate.rms_scale,
                gate.bleed_floor * gate.bleed_factor))

    def test_candidate_streak(self):
        """First above-gate frame -> 'bleed' (streak 1/2); second -> 'candidate'."""
        gate = make_gate(min_candidates=2)
        self.assertEqual(gate.on_frame(0.5, tts_playing=False), "bleed")
        self.assertEqual(gate.on_frame(0.5, tts_playing=False), "candidate")
        # streak resets after a trigger: two more frames needed
        self.assertEqual(gate.on_frame(0.5, tts_playing=False), "bleed")
        self.assertEqual(gate.on_frame(0.5, tts_playing=False), "candidate")

    def test_quiet_below_threshold(self):
        gate = make_gate()
        self.assertEqual(gate.on_frame(0.0, tts_playing=False), "quiet")

    def test_playing_never_emits_candidate(self):
        """While tts_playing the gate NEVER returns 'candidate'."""
        gate = make_gate(min_candidates=1)
        for _ in range(20):
            self.assertEqual(gate.on_frame(10.0, tts_playing=True), "bleed")

    def test_grace_after_playback(self):
        """Two grace frames after playback stops — no candidates (residual echo)."""
        gate = make_gate(min_candidates=1)
        gate.on_frame(0.05, tts_playing=True)
        self.assertEqual(gate.on_frame(10.0, tts_playing=False), "grace")
        self.assertEqual(gate.on_frame(10.0, tts_playing=False), "grace")
        # grace exhausted: loud frame is now evaluated
        self.assertEqual(gate.on_frame(10.0, tts_playing=False), "candidate")

    def test_backoff_raises_bar_then_expires(self):
        """After a veto: min_candidates >= 3, bleed_factor x1.5, in_backoff True."""
        gate = make_gate(min_candidates=2, bleed_factor=2.5)
        self.assertFalse(gate.in_backoff())
        gate.backoff(seconds=0.05)
        self.assertTrue(gate.in_backoff())
        self.assertGreaterEqual(gate.min_candidates, 3)
        self.assertAlmostEqual(gate.bleed_factor, 2.5 * 1.5)
        # expires after the window
        time.sleep(0.1)
        self.assertFalse(gate.in_backoff())

    def test_backoff_does_not_compound_across_vetoes(self):
        """Repeated vetoes inside the SAME window must NOT compound
        bleed_factor (review F5): the raised bar is a FIXED base x1.5,
        not repeatedly multiplied — otherwise a persistently noisy
        environment drives it 2.5 -> 3.75 -> 5.6 -> ... and silently
        kills barge-in. min_candidates stays stable at 3; the raised
        bar still expires after the window."""
        gate = make_gate(min_candidates=2, bleed_factor=2.5)
        gate.backoff(seconds=0.05)
        raised = gate.bleed_factor
        self.assertAlmostEqual(raised, 2.5 * 1.5)
        self.assertEqual(gate.min_candidates, 3)
        # second veto while the window is still open: must not raise further
        gate.backoff(seconds=0.05)
        self.assertEqual(gate.bleed_factor, raised,
                         "bleed_factor compounded across vetoes — the "
                         "raised bar must be fixed, not repeatedly x1.5")
        self.assertEqual(gate.min_candidates, 3)
        # the raised bar still expires after the window
        time.sleep(0.1)
        self.assertFalse(gate.in_backoff())

    def test_backoff_does_not_compound_across_runs(self):
        """A persisted backoff restored onto a fresh gate (review F3)
        plus a NEW veto must not compound either (review F5): the fresh
        gate computes its raised bar against its configured base, not
        against the restored value."""
        gate1 = make_gate(min_candidates=2, bleed_factor=2.5)
        gate1.backoff()
        state = gate1.backoff_state()
        gate2 = make_gate(min_candidates=2, bleed_factor=2.5)
        gate2.restore_backoff(state)
        gate2.backoff()
        self.assertAlmostEqual(gate2.bleed_factor, gate1.bleed_factor)
        self.assertEqual(gate2.min_candidates, 3)

    def test_backoff_default_60s(self):
        gate = make_gate()
        gate.backoff()
        self.assertTrue(gate.in_backoff())


if __name__ == "__main__":
    unittest.main()
