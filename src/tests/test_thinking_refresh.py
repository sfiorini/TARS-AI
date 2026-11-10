"""S-M4 tests: self-refreshing thinking-phrase set (module_thinking).

Covers:
- raw_complete_llm's new optional timeout parameter (S-M4-02)
- module_thinking validation / refresh / loop / daemon (S-M4-03)
- _maybe_play_thinking_response consuming get_filler_phrase (S-M4-04)
- boot wiring of the refresh daemon (S-M4-05)

The LLM HTTP layer is always stubbed — no network, no hardware.
"""
import json
import sys
import threading
import types
import unittest
from contextlib import ExitStack
from unittest import mock

from modules import module_llm  # noqa: E402
from modules import module_thinking  # noqa: E402

# Fake Pi-only hardware so module_main (via module_servoctl) imports on any
# dev machine — same pattern as test_audio_recovery.py. setdefault keeps
# this idempotent even when another test module imported the stubs first.
_board = types.ModuleType("board")
_board.SCL = "SCL"
_board.SDA = "SDA"
_busio = types.ModuleType("busio")
_busio.I2C = mock.MagicMock()
_pca9685 = types.ModuleType("adafruit_pca9685")
_pca9685.PCA9685 = mock.MagicMock()
for _name, _mod in (("board", _board), ("busio", _busio),
                    ("adafruit_pca9685", _pca9685)):
    sys.modules.setdefault(_name, _mod)

import modules.module_main as module_main  # noqa: E402
from modules.module_llm import raw_complete_llm  # noqa: E402


def _completion_json(text):
    """Non-streaming chat-completion body whose content is the given text."""
    return json.dumps({"choices": [{"message": {"content": text}}]})


class _QueueCapture:
    """Capture queue_message calls instead of printing them."""

    def __init__(self):
        self.messages = []

    def __call__(self, message, stream=False):
        self.messages.append(str(message))


def _recording_session(reply_text):
    """Fake _http_session recording post kwargs, returning a JSON reply."""
    session = mock.MagicMock()
    resp = mock.MagicMock()
    resp.text = reply_text
    resp.json = lambda: json.loads(reply_text)
    session.post.return_value = resp
    return session


class TestRawCompleteTimeout(unittest.TestCase):
    """S-M4-02: raw_complete_llm(user_prompt, istext=True, timeout=None)."""

    def test_timeout_forwarded(self):
        """An explicit timeout=30 must reach the HTTP post call."""
        session = _recording_session(_completion_json("ok"))
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", _QueueCapture()):
            raw_complete_llm("x", timeout=30)
        self.assertEqual(session.post.call_args[1]["timeout"], 30)

    def test_timeout_default_uses_config(self):
        """Omitting timeout keeps the [LLM] request_timeout default (120)."""
        session = _recording_session(_completion_json("ok"))
        with mock.patch.object(module_llm, "_http_session", session), \
             mock.patch.object(module_llm, "queue_message", _QueueCapture()):
            raw_complete_llm("x")
        self.assertEqual(session.post.call_args[1]["timeout"], 120)


class _ThinkingStateTestCase(unittest.TestCase):
    """Isolate module_thinking's module-level state between tests."""

    def setUp(self):
        self._old_generated = module_thinking._generated
        self._old_stop = module_thinking._stop_event
        self._old_thread = module_thinking._thread
        module_thinking._generated = []
        module_thinking._stop_event = threading.Event()
        module_thinking._thread = None

    def tearDown(self):
        module_thinking.stop_thinking_refresh()
        if module_thinking._thread is not None:
            module_thinking._thread.join(timeout=5)
        module_thinking._generated = self._old_generated
        module_thinking._stop_event = self._old_stop
        module_thinking._thread = self._old_thread


class _FakeEvent:
    """Event stand-in that records wait() delays instead of sleeping."""

    def __init__(self):
        self._set = False
        self.waits = []

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self._set

    def set(self):
        self._set = True


class TestValidatePhrases(unittest.TestCase):
    """S-M4-03: _validate_phrases pipeline — non-empty list -> strings only
    -> strip -> drop >40 chars or >6 words -> dedupe (case-insensitive) ->
    require >=3 survivors else None."""

    def test_non_list_returns_none(self):
        self.assertIsNone(module_thinking._validate_phrases("hello"))
        self.assertIsNone(module_thinking._validate_phrases({"a": 1}))
        self.assertIsNone(module_thinking._validate_phrases(None))

    def test_empty_list_returns_none(self):
        self.assertIsNone(module_thinking._validate_phrases([]))

    def test_non_strings_dropped(self):
        self.assertEqual(
            module_thinking._validate_phrases(["a", 1, "b", "c"]),
            ["a", "b", "c"])
        # dropping the int leaves only 2 survivors -> None
        self.assertIsNone(module_thinking._validate_phrases(["a", 1, "b"]))

    def test_over_40_chars_dropped(self):
        self.assertEqual(
            module_thinking._validate_phrases(["a" * 41, "b", "c", "d"]),
            ["b", "c", "d"])
        # exactly 40 chars survives
        self.assertEqual(
            module_thinking._validate_phrases(["a" * 40, "b", "c"]),
            ["a" * 40, "b", "c"])

    def test_over_6_words_dropped(self):
        seven = "one two three four five six seven"
        self.assertEqual(
            module_thinking._validate_phrases([seven, "b", "c", "d"]),
            ["b", "c", "d"])
        six = "one two three four five six"
        self.assertEqual(
            module_thinking._validate_phrases([six, "b", "c"]),
            [six, "b", "c"])

    def test_phrases_stripped(self):
        self.assertEqual(
            module_thinking._validate_phrases(["  a  ", "b", "c"]),
            ["a", "b", "c"])

    def test_dupes_deduped_case_insensitive(self):
        self.assertEqual(
            module_thinking._validate_phrases(
                ["Alpha", "ALPHA", "beta", "gamma"]),
            ["Alpha", "beta", "gamma"])

    def test_two_survivors_none_three_kept(self):
        self.assertIsNone(module_thinking._validate_phrases(["a", "b"]))
        self.assertEqual(module_thinking._validate_phrases(["a", "b", "c"]),
                         ["a", "b", "c"])


class TestFillerPhraseFallback(_ThinkingStateTestCase):
    """S-M4-03: fallback chain generated -> config -> None."""

    def test_generated_set_wins(self):
        module_thinking._generated = ["gen one", "gen two"]
        self.assertIn(module_thinking.get_filler_phrase(),
                      {"gen one", "gen two"})

    def test_config_phrases_when_generated_empty(self):
        module_thinking._generated = []
        with mock.patch.dict(module_thinking.CONFIG["CHAR"],
                             {"thinking_responses": '["cfg one", "cfg two"]'}):
            self.assertIn(module_thinking.get_filler_phrase(),
                          {"cfg one", "cfg two"})

    def test_none_when_both_empty(self):
        module_thinking._generated = []
        with mock.patch.dict(module_thinking.CONFIG["CHAR"],
                             {"thinking_responses": "[]"}):
            self.assertIsNone(module_thinking.get_filler_phrase())

    def test_none_when_config_garbage(self):
        module_thinking._generated = []
        with mock.patch.dict(module_thinking.CONFIG["CHAR"],
                             {"thinking_responses": "not json"}):
            self.assertIsNone(module_thinking.get_filler_phrase())


class TestBuildPrompt(unittest.TestCase):
    """S-M4-03: _build_prompt requests {"phrases": [...]} and names the
    character (the request forces response_format=json_object, so the prompt
    must ask for that exact shape)."""

    def test_prompt_has_phrases_shape_and_character_name(self):
        with mock.patch.dict(module_thinking.CONFIG["CHAR"],
                             {"character_name": "R2D2"}):
            prompt = module_thinking._build_prompt()
        self.assertIn("phrases", prompt)
        self.assertIn("R2D2", prompt)


class TestRefreshOnce(_ThinkingStateTestCase):
    """S-M4-03: _refresh_once parses every tolerated schema, validates, and
    swaps under the lock; any failure keeps the old set."""

    def _stub_llm(self, reply, **call_kwargs):
        stub = mock.MagicMock(return_value=reply, **call_kwargs)
        patcher = mock.patch.object(module_llm, "raw_complete_llm", stub)
        self.addCleanup(patcher.stop)
        patcher.start()
        capture = _QueueCapture()
        qpatcher = mock.patch(
            "modules.module_messageQue.queue_message", capture)
        self.addCleanup(qpatcher.stop)
        qpatcher.start()
        return stub, capture

    def test_phrases_schema_swaps(self):
        self._stub_llm('{"phrases": ["p1", "p2", "p3"]}')
        self.assertTrue(module_thinking._refresh_once())
        self.assertEqual(module_thinking.generated_phrase_count(), 3)
        self.assertEqual(module_thinking._generated, ["p1", "p2", "p3"])

    def test_main_reply_schema_keeps_old_set(self):
        module_thinking._generated = ["old1", "old2", "old3"]
        _, capture = self._stub_llm('{"reply": "here you go"}')
        self.assertFalse(module_thinking._refresh_once())
        self.assertEqual(module_thinking._generated,
                         ["old1", "old2", "old3"])
        self.assertEqual(capture.messages, [])

    def test_bare_list_swaps(self):
        self._stub_llm('["a", "b", "c"]')
        self.assertTrue(module_thinking._refresh_once())
        self.assertEqual(module_thinking._generated, ["a", "b", "c"])

    def test_markdown_fences_tolerated(self):
        self._stub_llm('```json\n["f1", "f2", "f3"]\n```')
        self.assertTrue(module_thinking._refresh_once())
        self.assertEqual(module_thinking._generated, ["f1", "f2", "f3"])

    def test_garbage_keeps_old_set(self):
        module_thinking._generated = ["old1", "old2", "old3"]
        self._stub_llm("not json at all")
        self.assertFalse(module_thinking._refresh_once())
        self.assertEqual(module_thinking._generated,
                         ["old1", "old2", "old3"])

    def test_none_reply_keeps_old_set_no_raise(self):
        """A missing LLM key makes raw_complete_llm return None — the
        refresh must fail cleanly and keep the old set."""
        module_thinking._generated = ["old1", "old2", "old3"]
        self._stub_llm(None)
        self.assertFalse(module_thinking._refresh_once())
        self.assertEqual(module_thinking._generated,
                         ["old1", "old2", "old3"])

    def test_timeout_forwarded(self):
        stub, _ = self._stub_llm('{"phrases": ["p1", "p2", "p3"]}')
        module_thinking._refresh_once()
        self.assertEqual(stub.call_args[1].get("timeout"), 30)
        module_thinking._refresh_once(timeout=7)
        self.assertEqual(stub.call_args[1].get("timeout"), 7)

    def test_swap_is_locked_not_in_place(self):
        """The swap assigns a new list under the lock — a caller holding the
        old list object is never mutated mid-iteration."""
        module_thinking._generated = ["old1", "old2", "old3"]
        old_ref = module_thinking._generated
        self._stub_llm('{"phrases": ["n1", "n2", "n3"]}')
        self.assertTrue(module_thinking._refresh_once())
        self.assertIsNot(module_thinking._generated, old_ref)
        self.assertEqual(old_ref, ["old1", "old2", "old3"])
        self.assertEqual(module_thinking._generated, ["n1", "n2", "n3"])

    def test_success_queues_status_message(self):
        _, capture = self._stub_llm('{"phrases": ["p1", "p2", "p3"]}')
        module_thinking._refresh_once()
        self.assertIn("THINKING: refreshed 3 phrases", capture.messages)

    def test_invalid_phrases_keep_old_set(self):
        """The LLM answers the phrases schema but with junk — validation
        rejects it and the old set survives."""
        module_thinking._generated = ["old1", "old2", "old3"]
        self._stub_llm('{"phrases": ["only one", 2, ""]}')
        self.assertFalse(module_thinking._refresh_once())
        self.assertEqual(module_thinking._generated,
                         ["old1", "old2", "old3"])


class TestRefreshLoop(_ThinkingStateTestCase):
    """S-M4-03: the loop refreshes immediately, waits minutes*60, and one
    failure never kills the cycle."""

    def test_loop_survives_failure_and_waits_configured_delay(self):
        calls = []

        def _flaky():
            calls.append(len(calls))
            if len(calls) == 1:
                raise RuntimeError("boom")
            return True

        with mock.patch.object(module_thinking, "_refresh_once",
                               side_effect=_flaky):
            event = _FakeEvent()
            event.is_set = lambda: len(calls) >= 2
            module_thinking._refresh_loop(event)
        self.assertEqual(len(calls), 2,
                         "a raising refresh must not kill the loop")
        self.assertEqual(event.waits, [30 * 60, 30 * 60],
                         "each iteration waits thinking_refresh_minutes * 60")

    def test_loop_honors_preset_stop_event(self):
        """A stop set before the loop starts means no refresh at all."""
        with mock.patch.object(module_thinking, "_refresh_once") as refresh:
            event = _FakeEvent()
            event.set()
            module_thinking._refresh_loop(event)
        refresh.assert_not_called()
        self.assertEqual(event.waits, [])

    def test_loop_respects_configured_minutes(self):
        with mock.patch.object(module_thinking, "_refresh_once",
                               return_value=True), \
             mock.patch.dict(module_thinking.CONFIG["CHAR"],
                             {"thinking_refresh_minutes": 5}):
            event = _FakeEvent()
            event.is_set = lambda: len(event.waits) >= 1
            module_thinking._refresh_loop(event)
        self.assertEqual(event.waits, [5 * 60])


class TestLlmFillerWiring(_ThinkingStateTestCase):
    """S-M4-04: _maybe_play_thinking_response consumes get_filler_phrase
    (imported lazily inside the function — the module_llm <->
    module_thinking cycle is broken on BOTH sides)."""

    def _call(self):
        """Run _maybe_play_thinking_response with queue_message captured and
        no real playback thread spawned; returns (result, messages, targets)."""
        capture = _QueueCapture()
        targets = []

        class _NoThread:
            def __init__(self, target=None, daemon=None, name=None):
                self.target = target

            def start(self):
                targets.append(self.target)

        with mock.patch.object(module_llm, "queue_message", capture), \
             mock.patch.object(module_llm.threading, "Thread", _NoThread), \
             mock.patch.dict(module_llm.CONFIG["CHAR"],
                             {"enable_thinking_responses": True}):
            result = module_llm._maybe_play_thinking_response()
        return result, capture.messages, targets

    def test_filler_uses_generated_set(self):
        """A non-empty generated set wins: the filler plays a generated
        phrase even when the config list is empty."""
        module_thinking._generated = ["gen phrase"]
        with mock.patch.dict(module_thinking.CONFIG["CHAR"],
                             {"thinking_responses": "[]"}):
            result, messages, targets = self._call()
        self.assertTrue(result)
        self.assertEqual(messages, ["gen phrase"])
        self.assertEqual(len(targets), 1, "playback thread must start")

    def test_filler_none_returns_false(self):
        """get_filler_phrase() None -> _maybe_play_thinking_response
        returns False (the tool-loop HOLDING_LINE fallback fires)."""
        with mock.patch.dict(module_thinking.CONFIG["CHAR"],
                             {"thinking_responses": '["cfg phrase"]'}), \
             mock.patch.object(module_thinking, "get_filler_phrase",
                               return_value=None):
            result, messages, targets = self._call()
        self.assertFalse(result)
        self.assertEqual(messages, [])
        self.assertEqual(targets, [])

    def test_config_phrases_still_play_when_generated_empty(self):
        """Fallback chain through the new module: empty generated set,
        config phrases present -> a config phrase plays (pre-S-M4-04
        behavior preserved)."""
        module_thinking._generated = []
        with mock.patch.dict(module_thinking.CONFIG["CHAR"],
                             {"thinking_responses": '["cfg one", "cfg two"]'}):
            result, messages, targets = self._call()
        self.assertTrue(result)
        self.assertIn(messages[0], {"cfg one", "cfg two"})
        self.assertEqual(len(targets), 1)


class TestBootWiring(unittest.TestCase):
    """S-M4-05: startup_initialization() starts the refresh daemon after
    the thermal block, guarded by try/except and the
    thinking_refresh_enabled key — a missing LLM key or import failure
    never blocks boot."""

    def _patches(self):
        return [
            mock.patch.object(module_main, "initialize_servos"),
            mock.patch("modules.module_servoctl.start_servo_watchdog"),
            mock.patch("modules.module_audio_recovery.reprobe_audio"),
            mock.patch("modules.module_mic.start_hub_watchdog"),
            mock.patch("modules.module_cputemp.CPUTempModule"),
            mock.patch("modules.module_cputemp.set_cpu_temp_instance"),
            mock.patch("modules.module_cputemp.set_ventilate_callback"),
            mock.patch("modules.module_cputemp.start_thermal_monitoring"),
            mock.patch("modules.module_movements.ventilate_on"),
            mock.patch.object(module_main, "queue_message"),
        ]

    def test_startup_starts_refresh_when_enabled(self):
        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            refresh = stack.enter_context(mock.patch(
                "modules.module_thinking.start_thinking_refresh"))
            stack.enter_context(mock.patch.dict(
                module_main.CONFIG["CHAR"],
                {"thinking_refresh_enabled": True}))
            module_main.startup_initialization()
        refresh.assert_called_once()

    def test_startup_skips_refresh_when_disabled(self):
        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            refresh = stack.enter_context(mock.patch(
                "modules.module_thinking.start_thinking_refresh"))
            stack.enter_context(mock.patch.dict(
                module_main.CONFIG["CHAR"],
                {"thinking_refresh_enabled": False}))
            module_main.startup_initialization()
        refresh.assert_not_called()

    def test_refresh_failure_never_blocks_boot(self):
        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            stack.enter_context(mock.patch.dict(
                module_main.CONFIG["CHAR"],
                {"thinking_refresh_enabled": True}))
            stack.enter_context(mock.patch(
                "modules.module_thinking.start_thinking_refresh",
                side_effect=RuntimeError("no llm key")))
            module_main.startup_initialization()   # must not raise


class TestDaemonLifecycle(_ThinkingStateTestCase):
    """S-M4-03: start/stop follow the cputemp daemon pattern — named daemon
    thread, is_alive guard, idempotent start, explicit stop."""

    def test_start_idempotent(self):
        with mock.patch.object(module_thinking, "_refresh_once",
                               return_value=True):
            self.assertTrue(module_thinking.start_thinking_refresh())
            self.assertFalse(module_thinking.start_thinking_refresh(),
                             "a second start must be a no-op")
            thread = module_thinking._thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
            self.assertEqual(thread.name, "thinking-refresh")
            self.assertTrue(thread.daemon)
            module_thinking.stop_thinking_refresh()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_stop_sets_event_and_thread_exits(self):
        with mock.patch.object(module_thinking, "_refresh_once",
                               return_value=True):
            module_thinking.start_thinking_refresh()
            module_thinking.stop_thinking_refresh()
            self.assertTrue(module_thinking._stop_event.is_set())
            module_thinking._thread.join(timeout=5)
            self.assertFalse(module_thinking._thread.is_alive())

    def test_generated_phrase_count_tracks_state(self):
        self.assertEqual(module_thinking.generated_phrase_count(), 0)
        module_thinking._generated = ["a", "b"]
        self.assertEqual(module_thinking.generated_phrase_count(), 2)


if __name__ == "__main__":
    unittest.main()
