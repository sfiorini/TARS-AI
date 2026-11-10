"""Pure barge-in echo gate. No SDK imports — fully unit-testable."""
import time


class EchoGate:
    """Energy-first barge-in gate with an EMA bleed floor.

    The gate SOLELY owns the candidate streak: ``on_frame`` returns
    "candidate" on the triggering frame only (the frame whose streak reaches
    ``min_candidates``) and "bleed" on the non-triggering above-gate frames of
    a streak.  Callers must NOT keep their own streak counter or reset
    anything on "bleed" — the return value IS the trigger signal.

    Candidates ARE emitted while TTS is playing (that is the whole point of
    barge-in).  TARS's own voice is rejected by the bleed floor: every playing
    frame feeds an EMA that tracks the peak bleed envelope, and the threshold
    sits ``bleed_factor`` above it.  ``WARMUP_FRAMES`` playing frames only
    learn the floor — at playback onset the floor is still 0, so evaluating
    candidates immediately would let TARS's first syllable cancel itself.
    """

    #: Playing frames that only learn the bleed floor (1 s at 125 ms/frame).
    WARMUP_FRAMES = 8


    def __init__(self, silence_threshold, rms_scale, bleed_factor=2.5,
                 min_candidates=2, ema_alpha=0.2):
        self.silence_threshold = float(silence_threshold)
        self.rms_scale = float(rms_scale)
        self.bleed_factor = float(bleed_factor)
        self._base_bleed_factor = float(bleed_factor)  # configured value — backoff() raises against this, not the current one, so repeated vetoes cannot compound (review F5)
        self.min_candidates = int(min_candidates)
        self.ema_alpha = float(ema_alpha)
        self.bleed_floor = 0.0
        self._grace_remaining = 0          # frames
        self._streak = 0
        self._playing_frames = 0           # warm-up progress while playing
        self._backoff_until = 0.0

    @property
    def threshold(self) -> float:
        return max(self.silence_threshold * self.rms_scale,
                   self.bleed_floor * self.bleed_factor)

    def on_frame(self, rms, tts_playing) -> str:
        """Returns 'candidate' | 'bleed' | 'quiet' | 'grace'.

        While ``tts_playing`` the gate both learns the bleed floor and judges
        the frame against the floor learned BEFORE it (so the first frame of an
        over-talk is not swallowed by its own EMA update).  The verdict is
        suppressed during the warm-up frames only.
        """
        if tts_playing:
            warm = self._playing_frames >= self.WARMUP_FRAMES
            kind = self._judge(rms, self.threshold) if warm else "bleed"
            if kind == "quiet":
                # while playing, below-threshold is still TARS's own voice
                # bleeding into the mic — never report it as silence
                kind = "bleed"
            if not warm:
                self._playing_frames += 1
                self._streak = 0
            self.bleed_floor = ((1 - self.ema_alpha) * self.bleed_floor
                                + self.ema_alpha * max(rms, self.bleed_floor))
            self._grace_remaining = 2       # 2 frames = 250 ms echo grace
            return kind
        if self._grace_remaining > 0:
            self._grace_remaining -= 1
            self._streak = 0
            return "grace"
        return self._judge(rms, self.threshold)

    def _judge(self, rms, threshold) -> str:
        """Streak logic shared by the playing and non-playing paths."""
        if rms > threshold:
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
        # Idempotent (review F5): raise to a FIXED bar computed from the
        # configured base — `max` (not `*=`) so repeated vetoes inside the
        # window, or a restored persisted state plus a new veto, cannot
        # compound 2.5 -> 3.75 -> 5.6 -> ... and silently kill barge-in.
        self.bleed_factor = max(self.bleed_factor,
                                self._base_bleed_factor * 1.5)

    def in_backoff(self) -> bool:
        return time.monotonic() < self._backoff_until

    def backoff_state(self):
        """The raised bar as {'bleed_factor', 'min_candidates', 'until'}
        (monotonic deadline) — for persisting a backoff ACROSS gate
        instances: the monitor builds a fresh gate per run, so the 60 s
        raised bar must be carried at manager level and re-applied
        (review F3)."""
        return {'bleed_factor': self.bleed_factor,
                'min_candidates': self.min_candidates,
                'until': self._backoff_until}

    def restore_backoff(self, state):
        """Re-apply a persisted backoff onto a fresh gate (review F3).
        Never lowers the bar below this gate's configured values."""
        self.bleed_factor = max(self.bleed_factor,
                                float(state['bleed_factor']))
        self.min_candidates = max(self.min_candidates,
                                  int(state['min_candidates']))
        self._backoff_until = max(self._backoff_until,
                                   float(state['until']))
