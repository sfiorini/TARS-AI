"""Tests for the background filler-phrase refresh (Milestone 4).

S-M4-02: raw_complete_llm gains an optional `timeout` param so refresh
calls can pass timeout=60 — a hung refresh request must not hold the
default 120 s.  The param threads into the request and is re-applied on
G9's retry loop (the retry must preserve it, not drop it).

S-M4-03: the refresh machinery itself — _validate_phrases (charset
fixed-point rule), _parse_phrase_payload (fenced/non-greedy JSON
tolerance + the anti-scavenging rule), per-category prompt with the
category's register, _refresh_once (LLM call OUTSIDE the pool lock,
atomic swap under it, failure keeps the old pool), and
_refresh_all_categories (survives a raising category and advances
_last_refresh after every cycle).

The LLM HTTP layer is always stubbed — no network, no real client.
"""
import ast
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from modules import module_llm as llm
from modules import module_thinking as mt
from modules.module_state import TarsState

# Absolute src/ path for source-scanning tests: importing module_llm (top
# of this file) chdir's the process to src/modules (pre-existing bug in
# module_config.load_config — do NOT fix here), so relative paths lie.
_SRC = Path(__file__).resolve().parent.parent


class TestRawCompleteTimeout(unittest.TestCase):
    def test_timeout_param_used_when_given(self):
        with mock.patch.object(llm, "_http_session") as sess:
            sess.post.return_value.json.return_value = {"choices": [
                {"message": {"content": "ok"}}]}
            llm.raw_complete_llm("hi", timeout=33)
        self.assertEqual(sess.post.call_args.kwargs["timeout"], 33)

    def test_timeout_defaults_to_config(self):
        with mock.patch.object(llm, "_http_session") as sess:
            sess.post.return_value.json.return_value = {"choices": [
                {"message": {"content": "ok"}}]}
            llm.raw_complete_llm("hi")
        self.assertEqual(sess.post.call_args.kwargs["timeout"],
                         llm.CONFIG['LLM'].get('request_timeout', 120))


class _RefreshStateTestCase(unittest.TestCase):
    """Isolate module_thinking's refresh state between tests.

    Same CONFIG-stub convention as test_thinking.py: replace mt.CONFIG
    with a plain dict (never read the repo's config.ini) and restore it
    in tearDown; save/restore the _refreshed pool and _last_refresh.
    """

    def setUp(self):
        self._cfg = mt.CONFIG
        mt.CONFIG = {"CHAR": {"thinking_responses": "[]"}}
        # F3: the fallback floor is read via load_config() — stub it to
        # return the same dict, never the repo's config.ini (dynamic
        # side_effect: mid-test mt.CONFIG reassignments count)
        p = mock.patch.object(mt, "load_config",
                              side_effect=lambda: mt.CONFIG)
        p.start()
        self.addCleanup(p.stop)
        self._saved_refreshed = dict(mt._refreshed)
        mt._refreshed.clear()
        self._had_last = hasattr(mt, "_last_refresh")
        if self._had_last:
            self._saved_last = mt._last_refresh

    def tearDown(self):
        mt.CONFIG = self._cfg
        mt._refreshed.clear()
        mt._refreshed.update(self._saved_refreshed)
        if self._had_last:
            mt._last_refresh = self._saved_last
        elif hasattr(mt, "_last_refresh"):
            del mt._last_refresh

    def _stub_llm(self, reply=None, side_effect=None):
        """Stub raw_complete_llm (no network, no real client) and silence
        queue_message; returns the stub for call-args inspection."""
        stub = mock.MagicMock(return_value=reply, side_effect=side_effect)
        patcher = mock.patch.object(llm, "raw_complete_llm", stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch.object(mt, "queue_message")
        quiet.start()
        self.addCleanup(quiet.stop)
        return stub


class TestRefreshedPoolChain(_RefreshStateTestCase):
    """Recovered coverage: the fallback chain (M1 built it, M4 keeps it
    pinned). Refreshed pool wins; then bank; then config; then None."""

    def test_generated_set_wins(self):
        mt._refreshed["search"] = ["A."]
        self.assertEqual(mt.get_filler_phrase("search"), "A.")

    def test_config_phrases_when_generated_empty(self):
        # No refreshed entry for the category; the bank is emptied so the
        # config leg of the chain is the one under test.
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            mt.CONFIG["CHAR"]["thinking_responses"] = '["cfg one", "cfg two"]'
            self.assertIn(mt.get_filler_phrase("search"),
                          {"cfg one", "cfg two"})

    def test_none_when_both_empty(self):
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            mt.CONFIG["CHAR"]["thinking_responses"] = "[]"
            self.assertIsNone(mt.get_filler_phrase("search"))

    def test_none_when_config_garbage(self):
        with mock.patch.dict(mt.PHRASE_BANK, clear=True):
            mt.CONFIG["CHAR"]["thinking_responses"] = "not json"
            self.assertIsNone(mt.get_filler_phrase("search"))


class TestValidatePhrases(unittest.TestCase):
    """S-M4-03: every phrase must be a fixed point of sanitize_phrase —
    the playback path plays text raw, so the charset rule lives here."""

    def test_validate_rejects_bad_charset(self):
        # Parentheses are stripped by the sanitizer, so such a phrase is
        # not a fixed point and must be dropped; the clean one survives.
        self.assertEqual(
            mt._validate_phrases(["No (parentheses)", "ok phrase"]),
            ["ok phrase"])


class TestParsePhrasePayload(unittest.TestCase):
    """S-M4-03: fenced/non-greedy JSON tolerance for the {"phrases": [...]}
    schema."""

    def test_fenced_payload_tolerated(self):
        # [F4] DISCRIMINATION (mutation-tested): the old payload used
        # THREE double-quotes ('"""json ... """'), which the backtick
        # fence regex never matches — it parsed via the body.find("{")
        # fallback, so deleting the fence-stripping kept the test green.
        # A bare ``` fence alone would STILL not discriminate (the
        # fallback finds the fenced object anyway — verified), so the
        # fence must be LOAD-BEARING: a reply-schema object sits BEFORE
        # the fence. The fallback would find THAT object first and
        # reject the whole payload (None); only the fence branch
        # correctly extracts the phrases from inside the fence.
        payload = ('Sure! Here you go: {"reply": "Let me look that up"}'
                   '\n```json\n{"phrases": ["One.", "Two."]}\n```')
        self.assertEqual(mt._parse_phrase_payload(payload),
                         ["One.", "Two."])


class TestRefreshOnce(_RefreshStateTestCase):
    """S-M4-03: per-category generation via raw_complete_llm(timeout=60),
    atomic swap under the pool lock, failure keeps the old pool."""

    def test_refresh_once_swaps_atomically(self):
        old = mt._refreshed["search"] = ["Old one."]
        self._stub_llm('{"phrases": ["Fresh one."]}')
        self.assertTrue(mt._refresh_once("search"))
        self.assertEqual(mt._refreshed["search"], ["Fresh one."])
        self.assertIsNot(mt._refreshed["search"], old,
                         "the swap must assign a NEW list object")

    def test_refresh_failure_keeps_old_pool(self):
        mt._refreshed["search"] = ["Old one."]
        self._stub_llm(side_effect=RuntimeError("network down"))
        self.assertFalse(mt._refresh_once("search"))
        self.assertEqual(mt._refreshed["search"], ["Old one."])

    def test_reply_schema_rejected_not_scavenged(self):
        # A payload in the conversation schema ("reply") is rejected, not
        # scavenged — at the parse level AND at the refresh level (the
        # old pool survives).
        # [F5] DISCRIMINATION (mutation-tested): the old payload had NO
        # "phrases" key, so .get("phrases") already returned None —
        # deleting the anti-scavenging rule kept the test green. The
        # payload must contain BOTH a "reply" key AND phrase-like
        # content, so the rule is the ONLY thing that decides: with it
        # the reply schema wins (None, old pool kept); without it the
        # phrases would be scavenged.
        payload = ('{"reply": "Let me look that up", '
                   '"phrases": ["One.", "Two."]}')
        self.assertIsNone(mt._parse_phrase_payload(payload))
        mt._refreshed["search"] = ["Old one."]
        self._stub_llm(payload)
        self.assertFalse(mt._refresh_once("search"))
        self.assertEqual(mt._refreshed["search"], ["Old one."])
        self.assertNotEqual(mt._refreshed["search"], ["One.", "Two."],
                            "the reply-schema rejection must win over the "
                            "phrase-like content — never scavenge")

    def test_prompt_carries_category_and_register(self):
        stub = self._stub_llm('{"phrases": ["Fresh one."]}')
        self.assertTrue(mt._refresh_once("music"))
        prompt = stub.call_args[0][0]
        self.assertIn("music", prompt)
        self.assertIn(mt.REGISTERS["music"], prompt)  # "playful"

    def test_raw_called_with_60s_timeout(self):
        stub = self._stub_llm('{"phrases": ["Fresh one."]}')
        mt._refresh_once("search")
        self.assertEqual(stub.call_args.kwargs.get("timeout"), 60)

    def test_lock_not_held_during_generation(self):
        """The pool lock must NOT be held while raw_complete_llm runs.
        A refresh call can take up to 60 s of network time — if the
        call were ever moved inside `with _pool_lock:`, every reader of
        the phrase bank (the mid-turn filler) would stall behind it.
        The stub asserts the lock state at call time, so a regression
        that moves the call under the lock fails HERE, not in prod."""
        observed = {}

        def _fake_llm(prompt, timeout=None):
            observed['locked'] = mt._pool_lock.locked()
            return '{"phrases": ["Thinking it through."]}'

        # _refresh_once imports raw_complete_llm function-locally from
        # modules.module_llm, so patch it at its source module.
        patcher = mock.patch.object(llm, "raw_complete_llm", _fake_llm)
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch.object(mt, "queue_message")
        quiet.start()
        self.addCleanup(quiet.stop)

        self.assertTrue(mt._refresh_once("general"))
        self.assertIn("locked", observed,
                      "the stubbed LLM call was never reached")
        self.assertFalse(observed["locked"],
                         "pool lock must NOT be held during generation")

    def test_swap_is_locked_not_in_place(self):
        old = mt._refreshed["search"] = ["Old one."]
        self._stub_llm('{"phrases": ["Fresh one."]}')
        self.assertTrue(mt._refresh_once("search"))
        self.assertIsNot(mt._refreshed["search"], old)
        self.assertEqual(old, ["Old one."],
                         "the old list must never be mutated in place")

    def test_swap_takes_pool_lock(self):
        """[F6] The pool swap (the only WRITE to _refreshed) must happen
        UNDER _pool_lock — every reader (get_filler_phrase, the mid-turn
        filler) takes the same lock, so an unguarded assignment is a
        torn read/write race. test_lock_not_held_during_generation pins
        only the INVERSE (the lock must not span the LLM call); nothing
        pinned the positive side — verified: replacing
        `with _pool_lock: _refreshed[category] = ...` with an unguarded
        assignment left all of TestRefreshOnce green. Same
        _RecordingLock pattern as test_read_and_write_both_take_meta_lock.
        DISCRIMINATION (mutation-tested): removing the `with _pool_lock:`
        (keeping the bare assignment) makes this test FAIL; restoring it
        makes it PASS again."""
        self._stub_llm('{"phrases": ["Fresh one."]}')

        class _RecordingLock:
            def __init__(self, inner):
                self._inner = inner
                self.count = 0
            def __enter__(self):
                self.count += 1
                return self._inner.__enter__()
            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        real = mt._pool_lock
        rec = _RecordingLock(real)
        mt._pool_lock = rec
        self.addCleanup(setattr, mt, "_pool_lock", real)

        self.assertTrue(mt._refresh_once("search"))
        self.assertGreaterEqual(
            rec.count, 1,
            "the pool swap must acquire _pool_lock — an unguarded "
            "assignment races every reader of the phrase bank")


class TestRefreshAllCategories(_RefreshStateTestCase):
    """S-M4-03: the cycle survives a raising category and advances
    _last_refresh after EVERY cycle (no hammering on failure)."""

    def test_loop_survives_failure_and_advances_last_refresh(self):
        mt._last_refresh = 100.0

        def _flaky(category):
            if category == "search":
                raise RuntimeError("boom")
            return True

        # [F3] the cycle now re-checks the idle gate between categories;
        # this test is about surviving a raising category, so hold the
        # gate open (the real _is_idle() is False on the dev Mac and
        # would abort the cycle after the first, raising category)
        with mock.patch.object(mt, "_refresh_once", side_effect=_flaky), \
             mock.patch.object(mt, "_is_idle", return_value=True):
            ok = mt._refresh_all_categories()
        self.assertTrue(ok, "one failing category must not fail the cycle")
        self.assertGreater(mt._last_refresh, 100.0,
                           "_last_refresh must advance after every cycle")

    def test_cycle_aborts_between_categories_when_stopped(self):
        """F1: a full cycle can take minutes (13 categories x 60 s), so
        stop()'s 5 s join routinely times out mid-cycle. The cycle must
        check the stop event BETWEEN categories and abort promptly —
        otherwise a stopping daemon keeps making LLM calls for minutes.
        A partial cycle still advances _last_refresh (no hammering)."""
        mt._last_refresh = 100.0
        calls = []

        def _recording(category):
            calls.append(category)
            if len(calls) == 2:
                mt._stop_event.set()
            return True

        saved_event = mt._stop_event
        mt._stop_event = threading.Event()
        self.addCleanup(setattr, mt, "_stop_event", saved_event)

        # [F3] hold the idle gate open — this test is about the STOP
        # event abort, and the real _is_idle() is False on the dev Mac
        # (it would otherwise abort the cycle before the stop event
        # ever gets set)
        with mock.patch.object(mt, "_refresh_once", side_effect=_recording), \
             mock.patch.object(mt, "_is_idle", return_value=True):
            ok = mt._refresh_all_categories()
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2,
                        "the cycle must abort once the stop event is set")
        self.assertGreater(mt._last_refresh, 100.0,
                           "a partial cycle must still advance "
                           "_last_refresh (no hammering)")

    def test_cycle_breaks_between_categories_when_not_idle(self):
        """[F3] the idle gate used to be sampled ONCE per cycle (in the
        daemon loop, before the cycle started). A cycle can run
        13 x 60 s, so a user waking TARS mid-cycle left the remaining
        categories firing LLM calls on the same API key, competing with
        the user's turn for the rate limit. The cycle must re-check
        _is_idle() BETWEEN categories and break when not idle. The
        FIRST category is not gated (a manual web-UI refresh bypasses
        the idle gate by design — explicit user intent). A partial
        cycle still advances _last_refresh (no hammering)."""
        mt._last_refresh = 100.0
        calls = []
        with mock.patch.object(mt, "_refresh_once",
                               side_effect=lambda c: calls.append(c)
                               or True), \
             mock.patch.object(mt, "_is_idle", return_value=False):
            ok = mt._refresh_all_categories()
        self.assertTrue(ok)
        self.assertEqual(calls, [mt.CATEGORIES[0]],
                         "a not-idle cycle must stop after the first "
                         "category — no LLM calls against a busy robot")
        self.assertGreater(mt._last_refresh, 100.0,
                           "a partial cycle must still advance "
                           "_last_refresh (no hammering)")

    def test_cycle_breaks_mid_cycle_when_user_wakes_tars(self):
        """[F3] the mid-cycle case: idle for the first checks, then the
        user wakes TARS — the remaining categories must not fire."""
        mt._last_refresh = 100.0
        calls = []

        def _idle_goes_busy():
            return len(calls) < 2  # busy from the 3rd category on

        with mock.patch.object(mt, "_refresh_once",
                               side_effect=lambda c: calls.append(c)
                               or True), \
             mock.patch.object(mt, "_is_idle", side_effect=_idle_goes_busy):
            ok = mt._refresh_all_categories()
        self.assertTrue(ok)
        self.assertEqual(calls, list(mt.CATEGORIES[:2]),
                         "the cycle must break the moment the idle gate "
                         "closes, mid-cycle")
        self.assertGreater(mt._last_refresh, 100.0)


# --- S-M4-04: idle gate, daemon loop, start/stop, boot wiring -----------

def _fake_servoctl(moving=False):
    """Stand-in for modules.module_servoctl (the real one imports Pi-only
    board/busio/adafruit_pca9685 at top level, so it CANNOT be imported on
    the dev Mac — and a string-target mock.patch would import it too).
    Installed in sys.modules by the idle/daemon test setUps; removed in
    cleanup (hard constraint: no sys.modules leak)."""
    fake = types.ModuleType("modules.module_servoctl")
    fake.MOVING = moving
    return fake


class TestIdleGate(unittest.TestCase):
    """S-M4-04: _is_idle is true ONLY when ALL four conditions hold; ANY
    import/check failure means False (cannot prove idle -> don't run)."""

    def setUp(self):
        self._saved = sys.modules.get("modules.module_servoctl")
        sys.modules["modules.module_servoctl"] = _fake_servoctl(moving=False)
        self.addCleanup(self._restore_servoctl)

    def _restore_servoctl(self):
        if self._saved is None:
            sys.modules.pop("modules.module_servoctl", None)
        else:
            sys.modules["modules.module_servoctl"] = self._saved

    def test_idle_when_all_clear(self):
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.STANDBY), \
             mock.patch("modules.module_tts.is_tts_playing",
                        return_value=False), \
             mock.patch("modules.module_cputemp.get_thermal_status",
                        return_value={"current_temp": 55.0}):
            self.assertTrue(mt._is_idle())

    def test_not_standby_blocks(self):
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.TALKING):
            self.assertFalse(mt._is_idle())

    def test_moving_blocks(self):
        sys.modules["modules.module_servoctl"].MOVING = True
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.STANDBY):
            self.assertFalse(mt._is_idle())

    def test_tts_playing_blocks(self):
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.STANDBY), \
             mock.patch("modules.module_tts.is_tts_playing",
                        return_value=True):
            self.assertFalse(mt._is_idle())

    def test_hot_cpu_blocks(self):
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.STANDBY), \
             mock.patch("modules.module_tts.is_tts_playing",
                        return_value=False), \
             mock.patch("modules.module_cputemp.get_thermal_status",
                        return_value={"current_temp": 74.7}):
            self.assertFalse(mt._is_idle())

    def test_no_sensor_blocks(self):
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.STANDBY), \
             mock.patch("modules.module_tts.is_tts_playing",
                        return_value=False), \
             mock.patch("modules.module_cputemp.get_thermal_status",
                        return_value={"current_temp": None}):
            self.assertFalse(mt._is_idle())

    def test_zero_temp_sensor_blocks(self):
        """[F2] CPUTempModule.__init__ leaves self.temperature = 0.0
        when _read_temp() raises (no /sys/class/thermal/... on the
        board), so get_thermal_status() reports current_temp == 0.0 —
        NOT None. 0.0 >= 70.0 is False, so the gate USED TO PASS while
        the CPU was hot. A non-positive reading cannot prove idle."""
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.STANDBY), \
             mock.patch("modules.module_tts.is_tts_playing",
                        return_value=False), \
             mock.patch("modules.module_cputemp.get_thermal_status",
                        return_value={"current_temp": 0.0}):
            self.assertFalse(mt._is_idle())

    def test_negative_temp_blocks(self):
        # [F2] the non-positive rule covers a nonsense negative reading
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.STANDBY), \
             mock.patch("modules.module_tts.is_tts_playing",
                        return_value=False), \
             mock.patch("modules.module_cputemp.get_thermal_status",
                        return_value={"current_temp": -12.5}):
            self.assertFalse(mt._is_idle())

    def test_real_sensor_45c_still_passes(self):
        # [F2] discrimination: the legitimate case must survive — a real
        # sensor reading 45 C is below the 70 C gate and must pass
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.STANDBY), \
             mock.patch("modules.module_tts.is_tts_playing",
                        return_value=False), \
             mock.patch("modules.module_cputemp.get_thermal_status",
                        return_value={"current_temp": 45.0}):
            self.assertTrue(mt._is_idle())


class TestRefreshDaemon(_RefreshStateTestCase):
    """S-M4-04: the daemon loop fires ONLY when enabled AND past the
    interval AND fully idle; start/stop are idempotent."""

    def setUp(self):
        super().setUp()
        self._saved_servo = sys.modules.get("modules.module_servoctl")
        sys.modules["modules.module_servoctl"] = _fake_servoctl(moving=False)
        self.addCleanup(self._restore_servoctl)

    def _restore_servoctl(self):
        if self._saved_servo is None:
            sys.modules.pop("modules.module_servoctl", None)
        else:
            sys.modules["modules.module_servoctl"] = self._saved_servo

    def _run_one_tick(self):
        """Fabricate exactly one _refresh_loop iteration: the fake event
        answers False (while-check), False (post-wait guard), then True
        (the next while-check) so the loop exits after one pass."""
        ev = mock.MagicMock()
        ev.is_set.side_effect = [False, False, True]
        ev.wait.return_value = False
        with mock.patch.object(mt, "_stop_event", ev):
            mt._refresh_loop()

    def test_loop_waits_when_disabled(self):
        # DISCRIMINATION (mutation-tested): with the REAL _is_idle()
        # (False on any machine that cannot prove idle), the loop
        # stops at the idle gate anyway — the test used to stay green
        # with the enabled gate deleted. Pin the gate DIRECTLY: hold
        # every OTHER gate open (idle True, interval elapsed), so the
        # enabled flag is the ONLY thing that can stop the refresh.
        mt._last_refresh = 0.0
        cfg = mock.patch.object(mt, "load_config", return_value={
            "CHAR": {"thinking_refresh_enabled": False,
                     "thinking_refresh_minutes": 120}})
        cfg.start()
        self.addCleanup(cfg.stop)
        refresh = mock.patch.object(mt, "_refresh_all_categories")
        stub = refresh.start()
        self.addCleanup(refresh.stop)
        with mock.patch.object(mt, "_is_idle", return_value=True):
            self._run_one_tick()
        stub.assert_not_called()
        self.assertEqual(stub.call_count, 0,
                         "the disabled gate must stop the refresh even "
                         "when idle and past the interval")

    def test_loop_fires_when_gates_open(self):
        mt._last_refresh = 0.0
        cfg = mock.patch.object(mt, "load_config", return_value={
            "CHAR": {"thinking_refresh_enabled": True,
                     "thinking_refresh_minutes": 120}})
        cfg.start()
        self.addCleanup(cfg.stop)
        refresh = mock.patch.object(mt, "_refresh_all_categories")
        stub = refresh.start()
        self.addCleanup(refresh.stop)
        with mock.patch("modules.module_state.get_tars_state",
                        return_value=TarsState.STANDBY), \
             mock.patch("modules.module_tts.is_tts_playing",
                        return_value=False), \
             mock.patch("modules.module_cputemp.get_thermal_status",
                        return_value={"current_temp": 55.0}):
            self._run_one_tick()
        stub.assert_called_once()

    def test_loop_skips_refresh_when_not_idle(self):
        """[F3] DISCRIMINATION (mutation-tested): nothing pinned that
        _refresh_loop CALLS _is_idle() — TestIdleGate unit-tests it in
        isolation and test_loop_fires_when_gates_open only covers the
        positive path, so deleting `if not _is_idle(): continue` from
        the loop kept the suite green. Hold every OTHER gate open
        (enabled, interval elapsed) and close ONLY the idle gate: zero
        refresh calls — a busy robot must never burn the API key."""
        mt._last_refresh = 0.0
        cfg = mock.patch.object(mt, "load_config", return_value={
            "CHAR": {"thinking_refresh_enabled": True,
                     "thinking_refresh_minutes": 120}})
        cfg.start()
        self.addCleanup(cfg.stop)
        refresh = mock.patch.object(mt, "_refresh_all_categories")
        stub = refresh.start()
        self.addCleanup(refresh.stop)
        with mock.patch.object(mt, "_is_idle", return_value=False):
            self._run_one_tick()
        stub.assert_not_called()
        self.assertEqual(stub.call_count, 0,
                         "a not-idle robot must not refresh — the loop "
                         "must consult _is_idle() before refreshing")

    def test_start_idempotent(self):
        saved_thread = mt._thread
        self.addCleanup(setattr, mt, "_thread", saved_thread)
        mt._thread = None
        fake_thread = mock.MagicMock()
        fake_thread.is_alive.return_value = True
        with mock.patch.object(mt, "threading") as fake_threading:
            fake_threading.Thread.return_value = fake_thread
            mt.start()
            first = mt._thread
            mt.start()
            self.assertIs(mt._thread, first,
                          "a second start() must not spawn another thread")
            self.assertEqual(fake_threading.Thread.call_count, 1)

    def test_stop_sets_event(self):
        saved_event = mt._stop_event
        self.addCleanup(setattr, mt, "_stop_event", saved_event)
        mt._stop_event = threading.Event()
        saved_thread = mt._thread
        self.addCleanup(setattr, mt, "_thread", saved_thread)
        mt._thread = None
        self.assertFalse(mt._stop_event.is_set())
        mt.stop()
        mt.stop()  # idempotent: a second stop() must not raise
        self.assertTrue(mt._stop_event.is_set())


class TestStopStartNoDuplicateDaemon(unittest.TestCase):
    """F1: stop() joins for only 5 s while one refresh cycle can take
    minutes (13 categories x 60 s), so the join routinely times out
    mid-cycle. stop() must NOT null _thread while it is still alive, and
    a later start() must refuse to double-spawn — otherwise TWO
    permanent daemons run and the first, having watched _stop_event
    get cleared, never exits (duplicate LLM calls forever).

    Real threads, not mocks: the stub blocks past the 5 s join timeout,
    exactly like a mid-cycle LLM call."""

    def setUp(self):
        self._saved_thread = mt._thread
        self._saved_event = mt._stop_event
        mt._thread = None
        mt._stop_event = threading.Event()
        self._release = threading.Event()
        self._entered = threading.Event()
        self._cycles = []

        def _slow_loop():
            self._cycles.append(1)          # one entry == one refresh loop
            self._entered.set()
            # model a cycle in progress; outlives stop()'s 5 s join
            self._release.wait(30)

        self._loop_patch = mock.patch.object(mt, "_refresh_loop", _slow_loop)
        self._loop_patch.start()

    def tearDown(self):
        self._release.set()
        # join every daemon this test may have spawned (a RED run
        # leaves two) so no thread leaks into the rest of the suite
        for t in threading.enumerate():
            if t.name == "ThinkingRefresh" and t.is_alive():
                t.join(5)
        self._loop_patch.stop()
        mt._thread = self._saved_thread
        mt._stop_event = self._saved_event

    @staticmethod
    def _daemon_count():
        return sum(1 for t in threading.enumerate()
                   if t.name == "ThinkingRefresh" and t.is_alive())

    def test_stop_then_start_never_two_live_daemons(self):
        mt.start()
        self.assertTrue(self._entered.wait(5),
                        "the first daemon never entered its loop")
        self.assertEqual(self._daemon_count(), 1)
        mt.stop()  # the 5 s join times out: the cycle is still running
        mt.start()  # must REFUSE: the first daemon is still alive
        self.assertEqual(self._daemon_count(), 1,
                         "stop()+start() must never leave two live "
                         "daemons")
        self.assertEqual(len(self._cycles), 1,
                         "a second start() must not run a second loop")
        self.assertTrue(mt._thread is not None and mt._thread.is_alive(),
                        "stop() must keep _thread while the thread lives, "
                        "so start() can see it and refuse")


class TestLastRefreshLocking(_RefreshStateTestCase):
    """F2: _last_refresh is written by every refresh cycle (daemon AND
    the manual-refresh bg thread) and read by the daemon loop — all
    accesses must go through the module-level _meta_lock."""

    def test_read_and_write_both_take_meta_lock(self):
        lock = getattr(mt, "_meta_lock", None)
        self.assertIsNotNone(lock, "module-level _meta_lock must exist")

        class _RecordingLock:
            def __init__(self, inner):
                self._inner = inner
                self.count = 0
            def __enter__(self):
                self.count += 1
                return self._inner.__enter__()
            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        real = mt._meta_lock
        rec = _RecordingLock(real)
        mt._meta_lock = rec
        self.addCleanup(setattr, mt, "_meta_lock", real)

        # read path: one daemon tick (gates open past the interval check;
        # the idle gate then blocks the refresh itself)
        mt._last_refresh = 0.0
        cfg = mock.patch.object(mt, "load_config", return_value={
            "CHAR": {"thinking_refresh_enabled": True,
                     "thinking_refresh_minutes": 120}})
        cfg.start()
        self.addCleanup(cfg.stop)
        ev = mock.MagicMock()
        ev.is_set.side_effect = [False, False, True]
        ev.wait.return_value = False
        # the refresh stub is scoped to the tick ONLY — the write path
        # below must call the REAL _refresh_all_categories
        with mock.patch.object(mt, "_refresh_all_categories"), \
             mock.patch.object(mt, "_stop_event", ev):
            mt._refresh_loop()
        reads = rec.count
        self.assertGreaterEqual(
            reads, 1,
            "the daemon loop's _last_refresh read must take _meta_lock")

        # write path: one full cycle
        with mock.patch.object(mt, "_refresh_once", return_value=True):
            mt._refresh_all_categories()
        self.assertGreaterEqual(
            rec.count, reads + 1,
            "the cycle's _last_refresh write must take the SAME lock")


class TestBootWiring(unittest.TestCase):
    """S-M4-04: boot wiring is config-gated (off by default) and sits in
    its own try/except so a daemon failure can never block boot. Source
    scan only — startup_initialization needs real hardware and cannot
    run on the dev Mac."""

    def _main_src(self):
        return (_SRC / "modules" / "module_main.py").read_text()

    def test_boot_wiring_gated_off(self):
        body = self._main_src()
        i_fn = body.index("def startup_initialization")
        body = body[i_fn:]
        self.assertIn("thinking_refresh_enabled", body)
        self.assertIn("thinking_refresh_enabled', False)", body,
                      "the gate must default to False (off by default)")

    def test_boot_never_blocks_boot(self):
        # module_main cannot be imported/run on the dev Mac (hardware
        # init) — assert structurally that the thinking-refresh boot
        # wiring sits inside its own try/except, so a daemon failure
        # can never block boot
        src = self._main_src()
        i_call = src.index("module_thinking.start()")
        i_try = src.rindex("try:", 0, i_call)
        i_except = src.index("except Exception as e:", i_call)
        self.assertLess(i_try, i_call)
        self.assertLess(i_call, i_except)
        self.assertIn("thinking_refresh_enabled", src[i_try:i_except])


class TestRefreshRoutes(unittest.TestCase):
    """S-M5-02: route presence + delegation. module_chatui CANNOT be
    imported on the dev Mac — module_chatui.py:55 imports
    module_servoctl, whose top level (module_servoctl.py:21-23) imports
    Pi-only board/busio/adafruit_pca9685; the existing suite never
    imports module_chatui, it AST-parses it (test_docs_webui_parity.py,
    test_movement_store.py:755-758). So, exactly like
    TestServoReinitRouteMapping (test_servo_watchdog.py:962), this is a
    source-structural (ast) test, NOT a Flask test-client test. Route
    BEHAVIOUR (lock, idempotency, state shape) is already unit-covered
    by S-M5-01's tests on request_manual_refresh/refresh_status."""

    def _route_fn(self, name):
        with open(_SRC / "modules" / "module_chatui.py",
                  encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"{name} not found in module_chatui.py")

    def _route_path(self, fn):
        for dec in fn.decorator_list:
            if (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "route"):
                return ast.literal_eval(dec.args[0])
        return None

    def test_post_refresh_route_exists_and_delegates(self):
        fn = self._route_fn("thinking_refresh_start")
        self.assertEqual(self._route_path(fn), "/api/thinking/refresh")
        methods = [c.value for dec in fn.decorator_list
                   if isinstance(dec, ast.Call)
                   for kw in dec.keywords if kw.arg == "methods"
                   for c in kw.value.elts]
        self.assertIn("POST", methods)
        self.assertIn("request_manual_refresh", ast.unparse(fn))

    def test_status_route_exists_and_delegates(self):
        fn = self._route_fn("thinking_refresh_status")
        self.assertEqual(self._route_path(fn),
                         "/api/thinking/refresh/status")
        self.assertIn("refresh_status", ast.unparse(fn))


class TestManualRefresh(unittest.TestCase):
    """S-M5-01: the manual (web-UI) trigger — idempotent (a second
    request while running never queues a second sweep), runs in a
    daemon thread, and a failure sets state/last_error while keeping
    the pools intact."""

    def test_manual_returns_starting_then_done(self):
        done = threading.Event()
        release = threading.Event()

        def _slow_refresh():
            # Hold 'running' until the idempotency check below has run —
            # an instant mock lets the bg thread finish first and the
            # second call would see a terminal state, not 'running'.
            release.wait(5)
            return done.set() or True

        with mock.patch.object(mt, "_refresh_all_categories",
                               side_effect=_slow_refresh):
            self.assertEqual(mt.request_manual_refresh()["state"], "starting")
            self.assertEqual(mt.request_manual_refresh()["state"], "running")
            release.set()
            done.wait(5)
            # _bg sets 'done' asynchronously; poll briefly
            for _ in range(50):
                if mt.refresh_status()["state"] == "done":
                    break
                time.sleep(0.02)
            self.assertEqual(mt.refresh_status()["state"], "done")

    def test_manual_failure_sets_error_keeps_pools(self):
        before = dict(mt._refreshed)
        with mock.patch.object(mt, "_refresh_all_categories",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(mt.request_manual_refresh()["state"], "starting")
            for _ in range(50):
                if mt.refresh_status()["state"] == "error":
                    break
                time.sleep(0.02)
        info = mt.refresh_status()
        self.assertEqual(info["state"], "error")
        self.assertEqual(info["last_error"], "boom")
        self.assertEqual(mt._refreshed, before)  # pools intact on failure

    def test_status_shape(self):
        info = mt.refresh_status()
        self.assertIn("state", info)
        self.assertIn("generated", info)

    def test_second_cycle_attempt_while_one_runs_does_not_stack(self):
        """[F4] At most one _refresh_all_categories() runs at a time —
        the daemon and the manual path share ONE guard. A second attempt
        while a cycle is in flight must return False immediately
        instead of stacking a second loop (up to 26 concurrent
        raw_complete_llm calls on the same API key)."""
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def _slow_refresh_once(category):
            calls.append(category)
            if len(calls) == 1:   # hold the first cycle open mid-flight
                entered.set()
                release.wait(10)
            return True

        saved_last = mt._last_refresh
        self.addCleanup(setattr, mt, "_last_refresh", saved_last)

        with mock.patch.object(mt, "_refresh_once",
                               side_effect=_slow_refresh_once), \
             mock.patch.object(mt, "_is_idle", return_value=True), \
             mock.patch.object(mt, "queue_message"):
            results = {}

            def _first_cycle():
                results["first"] = mt._refresh_all_categories()

            t = threading.Thread(target=_first_cycle, daemon=True,
                                 name="F4FirstCycle")
            t.start()
            self.assertTrue(entered.wait(5),
                            "the first cycle never started")
            # a second attempt while the first is mid-cycle: no stacking
            self.assertFalse(mt._refresh_all_categories(),
                            "a second cycle must not run while one is "
                            "in flight")
            release.set()
            t.join(5)
            self.assertTrue(results["first"])
            self.assertEqual(calls, list(mt.CATEGORIES),
                             "exactly ONE cycle's worth of categories "
                             "must run — no second sweep")

    def test_manual_click_while_daemon_cycle_runs_reports_running(self):
        """[F4] The audited failure: request_manual_refresh guarded only
        against another MANUAL run, so a web-UI click while the daemon
        was mid-cycle stacked a SECOND _refresh_all_categories loop.
        The manual path must share the cycle guard and REPORT ('a
        cycle is already running') instead of stacking."""
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def _slow_refresh_once(category):
            calls.append(category)
            if len(calls) == 1:   # hold the daemon cycle open mid-flight
                entered.set()
                release.wait(10)
            return True

        saved_state = dict(mt._refresh_state)
        self.addCleanup(lambda: mt._refresh_state.update(saved_state))
        saved_last = mt._last_refresh
        self.addCleanup(setattr, mt, "_last_refresh", saved_last)

        with mock.patch.object(mt, "_refresh_once",
                               side_effect=_slow_refresh_once), \
             mock.patch.object(mt, "_is_idle", return_value=True), \
             mock.patch.object(mt, "queue_message"):
            cycle_done = threading.Event()

            def _daemon_cycle():
                mt._refresh_all_categories()
                cycle_done.set()

            t = threading.Thread(target=_daemon_cycle, daemon=True,
                                 name="F4DaemonCycle")
            t.start()
            self.assertTrue(entered.wait(5),
                            "the daemon cycle never started")

            # the manual click arrives mid-cycle: REPORT, don't stack
            result = mt.request_manual_refresh()
            self.assertEqual(result["state"], "running",
                             "a manual click while a cycle is in flight "
                             "must report 'running', not start a second "
                             "sweep")
            release.set()
            self.assertTrue(cycle_done.wait(5))
            t.join(5)
            self.assertEqual(calls, list(mt.CATEGORIES),
                             "exactly ONE cycle ran — the manual click "
                             "must not stack a second sweep on the "
                             "daemon's")


if __name__ == "__main__":
    unittest.main()
