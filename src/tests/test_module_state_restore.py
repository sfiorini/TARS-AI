"""State restoration for work that temporarily takes the TARS state.

Regression (observed on the robot, 2026-11-10): a deferred tool-loop
follow-up spoke AFTER the STT thread had already parked in the wake
detector, then unconditionally wrote LISTENING. Nothing backed that state —
no recording round ever ran — so the robot displayed LISTENING forever while
functionally sitting in STANDBY (the wake word worked, plain speech did not).

Runtime evidence: `web_search` took 7.1 s, longer than the whole listening
round, so the follow-up arrived after the session had ended:

    PROBE-STATE LISTENING->STANDBY thread=STTThread
    PROBE-PARK  tts=False state=STANDBY
    PROBE-STATE STANDBY->TALKING  thread=toolloop-voice   (6.3 s later)
    PROBE-STATE TALKING->LISTENING thread=toolloop-voice  (last write)
    (no PROBE-STT afterwards — no round ever started)

The rule: work that only borrows the state must give it back, and must not
clobber a transition another thread made while it ran.
"""

import os


import pytest

from modules.module_state import (
    TarsState,
    get_tars_state,
    restore_state_if_owned,
    set_tars_state,
)

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _clean_state():
    """The state machine is module-global; isolate every test."""
    set_tars_state(TarsState.STANDBY)
    yield
    set_tars_state(TarsState.STANDBY)


class TestRestoreStateIfOwned:
    def test_late_followup_returns_to_standby(self):
        """THE BUG: the follow-up speaks after the STT thread parked.
        The state must go back to STANDBY, never to LISTENING."""
        set_tars_state(TarsState.STANDBY)
        prev = get_tars_state()
        set_tars_state(TarsState.TALKING)          # follow-up starts speaking

        assert restore_state_if_owned(prev, TarsState.TALKING) is True
        assert get_tars_state() == TarsState.STANDBY

    def test_followup_during_a_live_round_returns_to_listening(self):
        """Normal path: a listening round is active, so LISTENING is correct."""
        set_tars_state(TarsState.LISTENING)
        prev = get_tars_state()
        set_tars_state(TarsState.TALKING)

        assert restore_state_if_owned(prev, TarsState.TALKING) is True
        assert get_tars_state() == TarsState.LISTENING

    def test_does_not_clobber_a_park_during_playback(self):
        """The STT thread parked while the follow-up was speaking — its
        STANDBY wins, because the follow-up no longer owns the state."""
        set_tars_state(TarsState.LISTENING)
        prev = get_tars_state()
        set_tars_state(TarsState.TALKING)
        set_tars_state(TarsState.STANDBY)          # STT thread parks mid-play

        assert restore_state_if_owned(prev, TarsState.TALKING) is False
        assert get_tars_state() == TarsState.STANDBY

    def test_does_not_clobber_a_wake_word_during_playback(self):
        """A wake word fired mid-playback and started a round."""
        set_tars_state(TarsState.STANDBY)
        prev = get_tars_state()
        set_tars_state(TarsState.TALKING)
        set_tars_state(TarsState.LISTENING)        # wake word fires mid-play

        assert restore_state_if_owned(prev, TarsState.TALKING) is False
        assert get_tars_state() == TarsState.LISTENING


class TestFollowupUsesConditionalRestore:
    """Static guard, same pattern as TestSideEffectImportOrderGuard: the
    unconditional LISTENING write is what caused the bug, so its return
    must fail loudly."""

    def _speak_followup_body(self):
        """Return only _speak_followup's own source.

        Boundary is indentation-based: the body ends at the first non-blank
        line indented at or below the `def` itself. (A regex for the next
        `def` at the same indent over-grabs, because _speak_followup is the
        last nested def in utterance_callback.)
        """
        path = os.path.join(_SRC, "modules", "module_main.py")
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        start = next(i for i, ln in enumerate(lines)
                     if "def _speak_followup(" in ln)
        base = len(lines[start]) - len(lines[start].lstrip())
        out = [lines[start]]
        for ln in lines[start + 1:]:
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= base:
                break
            out.append(ln)
        return "".join(out)

    def test_followup_restores_instead_of_forcing_listening(self):
        body = self._speak_followup_body()
        assert "restore_state_if_owned" in body, (
            "_speak_followup must hand the state back conditionally; an "
            "unconditional write leaves the UI stuck on LISTENING"
        )
        assert "set_tars_state(TarsState.LISTENING)" not in body, (
            "_speak_followup still forces LISTENING — this is the bug: it "
            "writes LISTENING even when the STT thread has already parked "
            "and no recording round will follow"
        )


class TestStateMachineStillNotifies:
    """The atomic-CAS refactor split set_tars_state into a locked swap plus an
    outside-the-lock announce. These guard that contract."""

    def test_transition_notifies_listeners(self):
        from modules.module_state import on_state_change, remove_state_change
        seen = []
        cb = lambda old, new: seen.append((old, new))
        on_state_change(cb)
        try:
            set_tars_state(TarsState.TALKING)
        finally:
            remove_state_change(cb)
        assert seen == [(TarsState.STANDBY, TarsState.TALKING)]

    def test_same_state_does_not_notify(self):
        from modules.module_state import on_state_change, remove_state_change
        seen = []
        cb = lambda old, new: seen.append((old, new))
        on_state_change(cb)
        try:
            set_tars_state(TarsState.STANDBY)      # already STANDBY
        finally:
            remove_state_change(cb)
        assert seen == []

    def test_restore_notifies_listeners(self):
        from modules.module_state import on_state_change, remove_state_change
        set_tars_state(TarsState.STANDBY)
        prev = get_tars_state()
        set_tars_state(TarsState.TALKING)
        seen = []
        cb = lambda old, new: seen.append((old, new))
        on_state_change(cb)
        try:
            assert restore_state_if_owned(prev, TarsState.TALKING) is True
        finally:
            remove_state_change(cb)
        assert seen == [(TarsState.TALKING, TarsState.STANDBY)]

    def test_failed_restore_does_not_notify(self):
        from modules.module_state import on_state_change, remove_state_change
        set_tars_state(TarsState.TALKING)
        seen = []
        cb = lambda old, new: seen.append((old, new))
        on_state_change(cb)
        try:
            assert restore_state_if_owned(TarsState.LISTENING, TarsState.TALKING) is True
        finally:
            remove_state_change(cb)
        # restored TALKING->LISTENING (we owned it)
        assert seen == [(TarsState.TALKING, TarsState.LISTENING)]
