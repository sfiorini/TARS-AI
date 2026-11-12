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
        payload = '"""json\n{"phrases": ["One.", "Two."]}\n"""'
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
        self.assertIsNone(
            mt._parse_phrase_payload('{"reply": "Let me look that up"}'))
        mt._refreshed["search"] = ["Old one."]
        self._stub_llm('{"reply": "Let me look that up"}')
        self.assertFalse(mt._refresh_once("search"))
        self.assertEqual(mt._refreshed["search"], ["Old one."])

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


class TestRefreshAllCategories(_RefreshStateTestCase):
    """S-M4-03: the cycle survives a raising category and advances
    _last_refresh after EVERY cycle (no hammering on failure)."""

    def test_loop_survives_failure_and_advances_last_refresh(self):
        mt._last_refresh = 100.0

        def _flaky(category):
            if category == "search":
                raise RuntimeError("boom")
            return True

        with mock.patch.object(mt, "_refresh_once", side_effect=_flaky):
            ok = mt._refresh_all_categories()
        self.assertTrue(ok, "one failing category must not fail the cycle")
        self.assertGreater(mt._last_refresh, 100.0,
                           "_last_refresh must advance after every cycle")


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
        cfg = mock.patch.object(mt, "load_config", return_value={
            "CHAR": {"thinking_refresh_enabled": False}})
        cfg.start()
        self.addCleanup(cfg.stop)
        refresh = mock.patch.object(mt, "_refresh_all_categories")
        stub = refresh.start()
        self.addCleanup(refresh.stop)
        self._run_one_tick()
        stub.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
