"""Pure barge-in echo gate. No SDK imports — fully unit-testable."""
import time


class EchoGate:
    """Energy-first barge-in gate with an EMA bleed floor.

    The gate SOLELY owns the candidate streak: ``on_frame`` returns
    "candidate" on the triggering frame only (the frame whose streak reaches
    ``min_candidates``) and "bleed" on the non-triggering above-gate frames of
    a streak.  Callers must NOT keep their own streak counter or reset
    anything on "bleed" — the return value IS the trigger signal.
    """

    def __init__(self, silence_threshold, rms_scale, bleed_factor=2.5,
                 min_candidates=2, ema_alpha=0.2):
        self.silence_threshold = float(silence_threshold)
        self.rms_scale = float(rms_scale)
        self.bleed_factor = float(bleed_factor)
        self.min_candidates = int(min_candidates)
        self.ema_alpha = float(ema_alpha)
        self.bleed_floor = 0.0
        self._grace_remaining = 0          # frames
        self._streak = 0
        self._backoff_until = 0.0

    @property
    def threshold(self) -> float:
        return max(self.silence_threshold * self.rms_scale,
                   self.bleed_floor * self.bleed_factor)

    def on_frame(self, rms, tts_playing) -> str:
        """Returns 'candidate' | 'bleed' | 'quiet' | 'grace'."""
        if tts_playing:
            self.bleed_floor = ((1 - self.ema_alpha) * self.bleed_floor
                                + self.ema_alpha * max(rms, self.bleed_floor))
            self._grace_remaining = 2       # 2 frames = 250 ms echo grace
            self._streak = 0
            return "bleed"
        if self._grace_remaining > 0:
            self._grace_remaining -= 1
            self._streak = 0
            return "grace"
        if rms > self.threshold:
            self._streak += 1
            if self._streak >= self.min_candidates:
                self._streak = 0
                return "candidate"
            return "bleed"                  # above gate but streak incomplete
        self._streak = 0
        return "quiet"

    def backoff(self, seconds=60.0):
        """After a voiceprint veto: raise the bar for `seconds`."""
        self._backoff_until = time.monotonic() + seconds
        self.min_candidates = max(self.min_candidates, 3)
        self.bleed_factor *= 1.5

    def in_backoff(self) -> bool:
        return time.monotonic() < self._backoff_until
