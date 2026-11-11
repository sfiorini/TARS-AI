"""Deferred reply loop tests (M4): config keys, runner state machine,
holding line, followup completion, memory preservation, rollback switch.

The loop is the riskiest behavioural change in the plan (D1): these tests
pin the state machine with a fake followup_fn and fake registry
descriptors — no network, no LLM.
"""
import os
import shutil
import sys
import types
import unittest
from unittest import mock


# Bootstrap gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_INI = os.path.join(_SRC, "config.ini")
if not os.path.exists(_CONFIG_INI):
    _TEMPLATE = os.path.join(_SRC, "config.ini.template")
    try:
        shutil.copyfile(_TEMPLATE, _CONFIG_INI)
    except OSError as exc:
        raise RuntimeError(
            "Cannot bootstrap src/config.ini from src/config.ini.template "
            f"(required for module imports): {exc}"
        ) from exc


def _install_fake_hardware():
    """board/busio/adafruit_pca9685 only exist on the Pi — inject fakes so
    module_servoctl can be imported on any dev machine."""
    board = types.ModuleType("board")
    board.SCL = "SCL"
    board.SDA = "SDA"
    busio = types.ModuleType("busio")
    busio.I2C = mock.MagicMock()
    pca9685 = types.ModuleType("adafruit_pca9685")
    pca9685.PCA9685 = mock.MagicMock()
    for name, mod in (("board", board), ("busio", busio),
                      ("adafruit_pca9685", pca9685)):
        sys.modules.setdefault(name, mod)


_install_fake_hardware()

import tempfile  # noqa: E402

from modules import module_config  # noqa: E402


class TestLoopConfig(unittest.TestCase):
    """[LLM] loop/timeout keys with code defaults (S-M4-01)."""

    def test_llm_loop_keys_have_defaults(self):
        # a config WITHOUT the new keys must still expose the defaults
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        minimal = os.path.join(tmp, "config.ini")
        with open(os.path.join(_SRC, "config.ini.template")) as fh:
            text = fh.read()
        for key in ("tool_loop_max_hops", "tool_call_timeout",
                    "request_timeout", "deferred_loop_enabled"):
            text = "\n".join(ln for ln in text.splitlines()
                             if not ln.startswith(key))
        with open(minimal, "w") as fh:
            fh.write(text)
        saved_path = os.environ.get("TARS_CONFIG_PATH")
        saved_cache = module_config._config_cache
        os.environ["TARS_CONFIG_PATH"] = minimal
        module_config._config_cache = None
        self.addCleanup(module_config.__setattr__, "_config_cache",
                        saved_cache)
        def _restore():
            if saved_path is None:
                os.environ.pop("TARS_CONFIG_PATH", None)
            else:
                os.environ["TARS_CONFIG_PATH"] = saved_path
        self.addCleanup(_restore)
        config = module_config.load_config()
        self.assertEqual(config['LLM']['tool_loop_max_hops'], 2)
        self.assertEqual(config['LLM']['tool_call_timeout'], 60)
        self.assertEqual(config['LLM']['request_timeout'], 120)
        self.assertIs(config['LLM']['deferred_loop_enabled'], True)


import time  # noqa: E402
import threading  # noqa: E402

from modules.module_toolregistry import (  # noqa: E402
    ToolDescriptor, ToolRegistry, ToolResult, ToolContext, reset_registry,
)
from modules.module_toolloop import (  # noqa: E402
    ToolLoopRunner, TurnCallbacks, bump_epoch, FALLBACK_LINE,
)


def _descriptor(name, result_bearing=True, execute=None, timeout_s=5.0,
                kind="builtin", speakable=False):
    return ToolDescriptor(
        name=name, kind=kind, description="", parameters=None,
        result_bearing=result_bearing, timeout_s=timeout_s,
        speakable=speakable,
        execute=execute or (lambda params, ctx: "tool result"))


class _LoopTestCase(unittest.TestCase):
    """A runner with a fake followup_fn over a fresh registry."""

    TERMINAL = ("IDLE", "CANCELLED", "TIMEOUT")

    def setUp(self):
        super().setUp()
        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = ToolRegistry()
        # NOTE: the runner reads get_registry() — patch it to our fresh one
        import modules.module_toolloop as toolloop
        self._patcher = mock.patch.object(toolloop, "get_registry",
                                         return_value=self.registry)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.followup_calls = []
        self.spoken = []
        self.states = []
        self.done = threading.Event()
        self.callbacks = TurnCallbacks(
            speak=self.spoken.append,
            on_state=self._on_state)

    def _on_state(self, state):
        self.states.append(state)
        if state in self.TERMINAL:
            self.done.set()

    def _followup(self, user_input, prior_reply, tool_results_text, source):
        call = (user_input, prior_reply, tool_results_text, source)
        self.followup_calls.append(call)
        return self._next_followup(*call)

    def _next_followup(self, *args):
        return {"reply": "final answer"}

    def _runner(self):
        return ToolLoopRunner(self._followup)

    def _wait_turn(self, timeout=5.0):
        if not self.done.wait(timeout):
            self.fail(f"turn did not finish; states={self.states}")

    @staticmethod
    def _parsed(calls, reply="checking"):
        return {"reply": reply, "function_calls": calls,
                "new_memories": []}


class TestToolLoopRunner(_LoopTestCase):
    """Runner state machine (S-M4-03)."""

    def test_fire_and_forget_bypasses_loop(self):
        exec_calls = []
        self.registry.register(_descriptor(
            "move", result_bearing=False,
            execute=lambda p, c: exec_calls.append(p) or "moved"))
        started = self._runner().start_turn(
            self._parsed([{"function": "move", "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self.assertFalse(started)  # classification only — ZERO dispatch
        self.assertEqual(exec_calls, [])
        self.assertEqual(self.followup_calls, [])
        self.assertEqual(self.spoken, [])

    def test_result_bearing_runs_followup(self):
        self.registry.register(_descriptor("search"))
        started = self._runner().start_turn(
            self._parsed([{"function": "search", "parameters": {"q": 1}}],
                         reply="let me check"),
            "what is the weather", "voice", self.callbacks)
        self.assertTrue(started)
        self._wait_turn()
        self.assertEqual(len(self.followup_calls), 1)
        user_input, prior_reply, tool_text, source = self.followup_calls[0]
        self.assertEqual(user_input, "what is the weather")
        self.assertEqual(prior_reply, "let me check")
        self.assertIn("search: tool result", tool_text)
        self.assertEqual(source, "voice")
        self.assertEqual(self.spoken, ["final answer"])
        self.assertIn("SPEAK_FOLLOWUP", self.states)

    def test_is_error_fed_to_model(self):
        def boom(params, ctx):
            raise ValueError("search exploded")
        self.registry.register(_descriptor("search", execute=boom))
        self._runner().start_turn(
            self._parsed([{"function": "search", "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertIn("ERROR: search exploded",
                      self.followup_calls[0][2])

    def test_timeout_fallback(self):
        def slow(params, ctx):
            time.sleep(1.0)
            return "late"
        self.registry.register(_descriptor("slow", execute=slow,
                                           timeout_s=0.1))
        self._runner().start_turn(
            self._parsed([{"function": "slow", "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertIn("ERROR: timeout", self.followup_calls[0][2])
        # the follow-up still runs and is spoken
        self.assertEqual(self.spoken, ["final answer"])

    def test_hop_bound(self):
        # followup_fn always returns NEW result-bearing calls
        self.registry.register(_descriptor("search"))
        def next_followup(user_input, prior_reply, tool_text, source):
            self.followup_calls.append((user_input, prior_reply, tool_text,
                                        source))
            return {"reply": "partial",
                    "function_calls": [{"function": "search",
                                        "parameters": {}}]}
        runner = ToolLoopRunner(next_followup)
        runner.start_turn(
            self._parsed([{"function": "search", "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        # max_hops default 2 -> exactly 2 follow-up completions, then the
        # fallback line is spoken
        self.assertEqual(len(self.followup_calls), 2)
        self.assertEqual(self.spoken, [FALLBACK_LINE])
        self.assertIn("TIMEOUT", self.states)

    def test_epoch_cancellation(self):
        def interrupting(params, ctx):
            bump_epoch("voice")  # user spoke again mid-tool
            return "result"
        self.registry.register(_descriptor("search",
                                           execute=interrupting))
        started = self._runner().start_turn(
            self._parsed([{"function": "search", "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self.assertTrue(started)
        self._wait_turn()
        self.assertEqual(self.spoken, [])  # stale results discarded
        self.assertEqual(self.followup_calls, [])
        self.assertIn("CANCELLED", self.states)

    def test_followup_garbage_survived(self):
        self.registry.register(_descriptor("search"))
        for garbage in (None, 42, {"no_reply": True}):
            self.done.clear()
            self.states.clear()
            runner = ToolLoopRunner(lambda *a: garbage)
            started = runner.start_turn(
                self._parsed([{"function": "search", "parameters": {}}]),
                "hello", "voice", self.callbacks)
            self.assertTrue(started)
            self._wait_turn()
        # never raised, and a speak happened for every garbage turn
        self.assertEqual(len(self.spoken), 3)
        # G9: the None iteration routes through the followup-None rescue,
        # but the descriptor is unspeakable by default -> FALLBACK_LINE
        # non-dict/non-str garbage is NEVER spoken verbatim (review F5):
        # 42 must not become the spoken answer "42"
        self.assertEqual(self.spoken, [FALLBACK_LINE, FALLBACK_LINE,
                                       FALLBACK_LINE])


class TestSuccessLogging(_LoopTestCase):
    """Successful tool turns are verifiable in the log (robot defect 6):
    the loop must log turn start, per-tool outcome, follow-up outcome and
    turn done — failures-only logging made a successful turn
    indistinguishable from a turn that called no tool at all."""

    def _lines(self, qm):
        return [call.args[0] for call in qm.call_args_list
                if call.args and isinstance(call.args[0], str)]

    @staticmethod
    def _matching(lines, pattern):
        import re
        return [line for line in lines
                if re.fullmatch(pattern, line)]

    def test_successful_turn_logs_success_lines(self):
        import modules.module_toolloop as toolloop
        self.registry.register(_descriptor("search"))
        with mock.patch.object(toolloop, "queue_message") as qm:
            started = self._runner().start_turn(
                self._parsed([{"function": "search", "parameters": {}}]),
                "hello", "voice", self.callbacks)
            self.assertTrue(started)
            self._wait_turn()
        lines = self._lines(qm)
        # turn start names the source and the tools
        self.assertIn(
            "TOOLLOOP: turn start (source=voice) — tools: search", lines)
        # per-tool ok line: name, duration (1 decimal), outcome
        self.assertTrue(
            self._matching(lines, r"TOOLLOOP: tool search ok in "
                             r"\d+\.\ds \(11 chars\)"),
            "per-tool ok line missing")
        # follow-up ok line: hop number and reply size
        self.assertIn("TOOLLOOP: hop 1 follow-up ok (reply 12 chars)",
                      lines)
        # turn done line: source, hops, duration
        self.assertTrue(
            self._matching(lines, r"TOOLLOOP: turn done "
                             r"\(source=voice, hops=1, \d+\.\ds\)"),
            "turn done line missing")

    def test_turn_start_line_lists_all_tools(self):
        import modules.module_toolloop as toolloop
        self.registry.register(_descriptor("search"))
        self.registry.register(_descriptor("lookup"))
        with mock.patch.object(toolloop, "queue_message") as qm:
            started = self._runner().start_turn(
                self._parsed([{"function": "search", "parameters": {}},
                              {"function": "lookup", "parameters": {}}]),
                "hello", "voice", self.callbacks)
            self.assertTrue(started)
            self._wait_turn()
        lines = self._lines(qm)
        self.assertIn(
            "TOOLLOOP: turn start (source=voice) — tools: search, lookup",
            lines)

    def test_error_tool_logs_error_line(self):
        import modules.module_toolloop as toolloop

        def boom(params, ctx):
            raise ValueError("search exploded")
        self.registry.register(_descriptor("search", execute=boom))
        with mock.patch.object(toolloop, "queue_message") as qm:
            started = self._runner().start_turn(
                self._parsed([{"function": "search", "parameters": {}}]),
                "hello", "voice", self.callbacks)
            self.assertTrue(started)
            self._wait_turn()
        lines = self._lines(qm)
        self.assertTrue(
            self._matching(lines, r"TOOLLOOP: tool search ERROR in "
                             r"\d+\.\ds: search exploded"),
            "per-tool ERROR line missing")

    def test_followup_failure_keeps_failure_lines(self):
        import modules.module_toolloop as toolloop
        self.registry.register(_descriptor("search"))

        def broken_followup(*args):
            raise RuntimeError("LLM down")
        runner = ToolLoopRunner(broken_followup)
        with mock.patch.object(toolloop, "queue_message") as qm:
            started = runner.start_turn(
                self._parsed([{"function": "search", "parameters": {}}]),
                "hello", "voice", self.callbacks)
            self.assertTrue(started)
            self._wait_turn()
        lines = self._lines(qm)
        # the pre-existing failure line stays
        self.assertTrue(any(line.startswith(
            "TOOLLOOP: follow-up completion failed: LLM down")
            for line in lines), "existing failure line missing")
        # and the new hop line reports the failed follow-up
        self.assertIn(
            "TOOLLOOP: hop 1 follow-up FAILED — speaking fallback", lines)

    def test_fire_and_forget_turn_logs_nothing(self):
        import modules.module_toolloop as toolloop
        self.registry.register(_descriptor(
            "move", result_bearing=False,
            execute=lambda p, c: "moved"))
        with mock.patch.object(toolloop, "queue_message") as qm:
            started = self._runner().start_turn(
                self._parsed([{"function": "move", "parameters": {}}]),
                "hello", "voice", self.callbacks)
            self.assertFalse(started)
        toolloop_lines = [line for line in self._lines(qm)
                          if "TOOLLOOP" in line]
        self.assertEqual(toolloop_lines, [])


HOLDING_LINE = "On it — I'll report back."


class TestDataSpeak(_LoopTestCase):
    """G9: the followup-None rescue — speak trimmed speakable tool data
    (fake followup_fn returning None; no module_llm involved)."""

    def test_trim_speakable_unit(self):
        from modules.module_toolloop import _trim_speakable
        # prefix strip
        self.assertEqual(
            _trim_speakable(["weather: sunny, 21C"], [True]),
            "sunny, 21C")
        # flag filtering (and space-join of the survivors)
        self.assertEqual(
            _trim_speakable(["a: one", "b: two", "c: three"],
                            [True, False, True]),
            "one three")
        # None when nothing qualifies
        self.assertIsNone(_trim_speakable(["a: x", "b: "],
                                          [False, True]))
        # sentence-boundary cap
        content = "word. " * 100  # 600 chars
        result = _trim_speakable([f"search: {content}"], [True])
        self.assertLessEqual(len(result), 500)
        self.assertTrue(result.endswith("."))
        self.assertTrue(content.startswith(result))
        # hard cut when no boundary within the cap (fixture must have no
        # trailing whitespace at char 500 or rstrip shortens it)
        self.assertEqual(_trim_speakable([f"search: {'x' * 600}"], [True]),
                         "x" * 500)

    def test_data_speak_epoch_recheck_cancels(self):
        # DIRECT call — deliberate: the in-loop race (a bump between the
        # follow-up and the speak) is not deterministically testable
        epoch = bump_epoch("voice")
        bump_epoch("voice")  # stale the first value
        ToolLoopRunner._speak_tool_data(
            self.callbacks, "voice", epoch,
            ["search: found it"], [True])
        self.assertEqual(self.spoken, [])
        self.assertIn("CANCELLED", self.states)

    def test_data_speak_waits_for_reply0_gate(self):
        gate = threading.Event()
        callbacks = TurnCallbacks(speak=self.spoken.append,
                                  on_state=self._on_state,
                                  wait_for_reply0=gate)
        epoch = bump_epoch("voice")
        thread = threading.Thread(
            target=ToolLoopRunner._speak_tool_data,
            args=(callbacks, "voice", epoch,
                  ["search: found it"], [True]),
            daemon=True)
        thread.start()
        thread.join(0.3)
        # blocked on the unset gate — nothing spoken yet
        self.assertTrue(thread.is_alive(),
                        "data speak must block on the reply-0 gate")
        self.assertEqual(self.spoken, [])
        gate.set()
        thread.join(5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.spoken, ["found it"])

    def _none_followup_runner(self):
        return ToolLoopRunner(lambda *a: None)

    def test_followup_none_speaks_speakable_data(self):
        self.registry.register(_descriptor(
            "web_search", speakable=True,
            execute=lambda p, c: "sunny, 21C"))
        self._none_followup_runner().start_turn(
            self._parsed([{"function": "web_search",
                           "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertEqual(self.spoken, ["sunny, 21C"])  # prefix stripped
        self.assertNotIn(FALLBACK_LINE, self.spoken)
        self.assertIn("TIMEOUT", self.states)
        self.assertEqual(self.states[-1], "IDLE")

    def test_followup_none_unspeakable_keeps_fallback(self):
        # speakable omitted -> default False -> FALLBACK_LINE (pins the
        # default; byte-identical to today's behavior)
        self.registry.register(_descriptor("web_search"))
        self._none_followup_runner().start_turn(
            self._parsed([{"function": "web_search",
                           "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertEqual(self.spoken, [FALLBACK_LINE])

    def test_followup_none_error_entry_not_spoken(self):
        def boom(params, ctx):
            raise ValueError("tool exploded")
        self.registry.register(_descriptor(
            "web_search", speakable=True, execute=boom))
        self._none_followup_runner().start_turn(
            self._parsed([{"function": "web_search",
                           "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertEqual(self.spoken, [FALLBACK_LINE])

    def test_followup_none_empty_content_not_spoken(self):
        self.registry.register(_descriptor(
            "web_search", speakable=True,
            execute=lambda p, c: ""))
        self._none_followup_runner().start_turn(
            self._parsed([{"function": "web_search",
                           "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertEqual(self.spoken, [FALLBACK_LINE])

    def test_data_speak_capped_at_sentence_boundary(self):
        content = "word. " * 100  # 600 chars
        self.registry.register(_descriptor(
            "web_search", speakable=True,
            execute=lambda p, c: content))
        self._none_followup_runner().start_turn(
            self._parsed([{"function": "web_search",
                           "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertEqual(len(self.spoken), 1)
        self.assertLessEqual(len(self.spoken[0]), 500)
        self.assertTrue(self.spoken[0].endswith("."))
        self.assertTrue(content.startswith(self.spoken[0]))

    def test_data_speak_hard_cut_without_boundary(self):
        # no ". "/"! "/"? " anywhere and no trailing whitespace at the
        # cut point (rstrip would otherwise shorten it below 500)
        content = "x" * 600
        self.registry.register(_descriptor(
            "web_search", speakable=True,
            execute=lambda p, c: content))
        self._none_followup_runner().start_turn(
            self._parsed([{"function": "web_search",
                           "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertEqual(self.spoken, ["x" * 500])

    def test_data_speak_joins_multiple_tools(self):
        self.registry.register(_descriptor(
            "web_search", speakable=True,
            execute=lambda p, c: "first text"))
        self.registry.register(_descriptor(
            "other", speakable=True,
            execute=lambda p, c: "second text"))
        self.registry.register(_descriptor(
            "search", execute=lambda p, c: "private data"))
        self._none_followup_runner().start_turn(
            self._parsed([{"function": "web_search",
                           "parameters": {}},
                          {"function": "other", "parameters": {}},
                          {"function": "search", "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertEqual(self.spoken, ["first text second text"])

    def test_data_speak_logs_lines(self):
        import modules.module_toolloop as toolloop
        self.registry.register(_descriptor(
            "web_search", speakable=True,
            execute=lambda p, c: "found it"))
        with mock.patch.object(toolloop, "queue_message") as qm:
            self._none_followup_runner().start_turn(
                self._parsed([{"function": "web_search",
                               "parameters": {}}]),
                "hello", "voice", self.callbacks)
            self._wait_turn()
        lines = [call.args[0] for call in qm.call_args_list
                 if call.args and isinstance(call.args[0], str)]
        # the pinned failure line stays VERBATIM
        self.assertIn(
            "TOOLLOOP: hop 1 follow-up FAILED — speaking fallback", lines)
        self.assertTrue(
            any(line.startswith("TOOLLOOP: speaking tool data (")
                for line in lines),
            "speaking-tool-data line missing")
        self.assertTrue(
            any(line.startswith("TOOLLOOP: raw tool text: ")
                for line in lines),
            "raw tool text line missing")


class TestHoldingLine(_LoopTestCase):
    """reply-0 doubles as holding line; canned line only when reply-0 is
    empty and the thinking mechanism yields nothing (S-M4-04)."""

    def test_empty_reply0_gets_holding_line(self):
        self.registry.register(_descriptor("search"))
        with mock.patch("modules.module_llm._maybe_play_thinking_response",
                        return_value=False) as think:
            started = self._runner().start_turn(
                self._parsed([{"function": "search", "parameters": {}}],
                             reply=""),
                "hello", "voice", self.callbacks)
        self.assertTrue(started)
        think.assert_called_once()
        self._wait_turn()
        self.assertEqual(self.spoken[0], HOLDING_LINE)

    def test_thinking_mechanism_suppresses_fallback(self):
        self.registry.register(_descriptor("search"))
        with mock.patch("modules.module_llm._maybe_play_thinking_response",
                        return_value=True):
            self._runner().start_turn(
                self._parsed([{"function": "search", "parameters": {}}],
                             reply=""),
                "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertNotIn(HOLDING_LINE, self.spoken)

    def test_nonempty_reply0_not_spoken_by_loop(self):
        self.registry.register(_descriptor("search"))
        with mock.patch("modules.module_llm._maybe_play_thinking_response"
                        ) as think:
            self._runner().start_turn(
                self._parsed([{"function": "search", "parameters": {}}],
                             reply="let me check on that"),
                "hello", "voice", self.callbacks)
        think.assert_not_called()
        self._wait_turn()
        # the first (and only) speak is the follow-up reply — no holding
        self.assertEqual(self.spoken, ["final answer"])


class TestFollowupCompletion(unittest.TestCase):
    """followup_completion prompt shape (S-M4-05) — the model's behavior
    depends on the augmentation format, so it is pinned exactly."""

    def test_followup_completion_prompt_shape(self):
        from modules import module_llm
        captured = {}

        def fake_build_prompt(user_text, *args, **kwargs):
            captured["user_text"] = user_text
            return "stub-prompt"

        canned = '{"reply": "the answer", "function_calls": [], "new_memories": []}'
        with mock.patch.object(module_llm, "build_prompt",
                              side_effect=fake_build_prompt), \
             mock.patch.object(module_llm, "raw_complete_llm",
                               return_value=canned) as post:
            result = module_llm.followup_completion(
                "what is the weather", "checking", "search: sunny, 21C",
                "voice")
        self.assertIn("[Your previous reply: checking]",
                      captured["user_text"])
        self.assertIn("[Tool results:\nsearch: sunny, 21C\n]",
                      captured["user_text"])
        self.assertIn("Produce the final short spoken reply for the user "
                      "using these results.", captured["user_text"])
        self.assertIn("Do not call functions again unless strictly "
                      "necessary.", captured["user_text"])
        self.assertTrue(captured["user_text"].startswith(
            "what is the weather"))
        post.assert_called_once_with("stub-prompt")
        self.assertEqual(result, {"reply": "the answer",
                                  "function_calls": [],
                                  "new_memories": []})

    def test_followup_completion_request_failure_returns_none(self):
        from modules import module_llm
        with mock.patch.object(module_llm, "build_prompt",
                              return_value="stub"), \
             mock.patch.object(module_llm, "raw_complete_llm",
                               return_value=None):
            result = module_llm.followup_completion("u", "r", "t", "voice")
        self.assertIsNone(result)  # runner speaks the timeout fallback


class _RecordingMemory:
    """Event-setting recorder standing in for memory_manager."""

    def __init__(self):
        self.longterm = []
        self.topic_index = []
        self.activity = []
        self.longterm_event = threading.Event()
        self.topic_event = threading.Event()
        self.activity_event = threading.Event()

    def write_longterm_memory(self, user_input, reply, activity):
        self.longterm.append((user_input, reply, activity))
        self.longterm_event.set()

    def update_topic_index_with_ai_response(self, json_str):
        self.topic_index.append(json_str)
        self.topic_event.set()

    def save_current_activity(self, activity):
        self.activity.append(activity)
        self.activity_event.set()


class TestTurnMemories(unittest.TestCase):
    """Memory writes must never be lost (S-M4-06, plan-review F3):
    llm_execute_side_effects is the ONLY memory path for webui, discord,
    reminder and legacy llm_process — it keeps writing via the extracted
    write_turn_memories, guarded to exactly one write per turn."""

    def test_write_turn_memories_persists_all_three(self):
        from modules import module_llm
        mem = _RecordingMemory()
        with mock.patch.object(module_llm, "memory_manager", mem):
            module_llm.write_turn_memories(
                {"reply": "r", "new_memories": ["fact"],
                 "current_activity": "walking"}, "u")
            self.assertTrue(mem.longterm_event.wait(2))
            self.assertTrue(mem.topic_event.wait(2))
            self.assertTrue(mem.activity_event.wait(2))
        self.assertEqual(mem.longterm, [("u", "r", "walking")])
        self.assertIn("fact", mem.topic_index[0])
        self.assertEqual(mem.activity, ["walking"])
        # empty new_memories -> the topic index is NOT called
        mem2 = _RecordingMemory()
        with mock.patch.object(module_llm, "memory_manager", mem2):
            module_llm.write_turn_memories(
                {"reply": "r", "new_memories": []}, "u2")
            self.assertTrue(mem2.longterm_event.wait(2))
        self.assertEqual(mem2.topic_index, [])

    def test_side_effects_write_memories_non_voice_source(self):
        from modules import module_llm
        mem = _RecordingMemory()
        parsed = {"reply": "r", "new_memories": ["fact"],
                  "current_activity": "walking"}
        with mock.patch.object(module_llm, "memory_manager", mem):
            # the path webui (:783), discord (skill_discord.py:155),
            # reminder (skill_reminder.py:82) and legacy llm_process (:711)
            # all rely on
            module_llm.llm_execute_side_effects(parsed, "u",
                                                source="webui")
            self.assertTrue(mem.longterm_event.wait(2))
            self.assertTrue(mem.topic_event.wait(2))
            self.assertTrue(mem.activity_event.wait(2))
        self.assertEqual(mem.longterm, [("u", "r", "walking")])
        self.assertIn("fact", mem.topic_index[0])
        # idempotence: a second write on the SAME parsed dict fires nothing
        mem_again = _RecordingMemory()
        with mock.patch.object(module_llm, "memory_manager", mem_again):
            module_llm.write_turn_memories(parsed, "u")
        self.assertEqual(mem_again.longterm + mem_again.topic_index
                         + mem_again.activity, [])

    def test_module_main_memory_wiring(self):
        # source pin (S-M2-11 pattern): the started-path memory write and
        # the not-started dispatch thread must both survive refactors
        with open(os.path.join(_SRC, "modules", "module_main.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("bump_epoch('voice')", src)
        self.assertNotIn("has_blocking_tool", src)
        start_idx = src.index("started = get_loop_runner().start_turn")
        tail = src[start_idx:]
        self.assertIn("if started:", tail)
        self.assertIn("threading.Thread(target=write_turn_memories", tail)
        self.assertIn("elif func_calls or new_mems:", tail)
        self.assertIn("target=llm_execute_side_effects", tail)


LEGACY_UNAVAILABLE = "That tool isn't available in legacy mode."


class TestRollbackSwitch(_LoopTestCase):
    """deferred_loop_enabled = false → one-release live rollback (S-M4-08)."""

    def _rollback_config(self):
        return mock.patch("modules.module_toolloop.load_config",
                          return_value={"LLM": {
                              "deferred_loop_enabled": False}})

    def test_rollback_switch_bypasses_loop(self):
        self.registry.register(_descriptor(
            "search", execute=lambda p, c: "search results here"))
        with self._rollback_config():
            started = self._runner().start_turn(
                self._parsed([{"function": "search", "parameters": {}}]),
                "hello", "voice", self.callbacks)
        self.assertTrue(started)
        self._wait_turn()  # legacy turn runs on a daemon thread
        self.assertEqual(self.followup_calls, [])  # the loop is bypassed
        self.assertIn("search results here", self.spoken)

    def test_rollback_mcp_result_tools_canned_fallback(self):
        self.registry.register(_descriptor(
            "mcp_example_get_time", kind="mcp",
            execute=lambda p, c: "12:00"))
        with self._rollback_config():
            started = self._runner().start_turn(
                self._parsed([{"function": "mcp_example_get_time",
                                "parameters": {}}]),
                "hello", "voice", self.callbacks)
        self.assertTrue(started)
        self._wait_turn()  # legacy turn runs on a daemon thread
        self.assertEqual(self.followup_calls, [])
        self.assertIn(LEGACY_UNAVAILABLE, self.spoken)
        self.assertNotIn("12:00", self.spoken)

    def test_rollback_fire_and_forget_still_returns_false(self):
        exec_calls = []
        self.registry.register(_descriptor(
            "move", result_bearing=False,
            execute=lambda p, c: exec_calls.append(p) or "moved"))
        with self._rollback_config():
            started = self._runner().start_turn(
                self._parsed([{"function": "move", "parameters": {}}]),
                "hello", "voice", self.callbacks)
        self.assertFalse(started)  # caller's side-effect thread dispatches
        self.assertEqual(exec_calls, [])
        self.assertEqual(self.spoken, [])


class TestMixedTurnDispatch(unittest.TestCase):
    """Review F1: a turn with BOTH a fire-and-forget call and a
    result-bearing call must dispatch each EXACTLY once — the loop only
    runs the result-bearing ones, so the started-branch still dispatches
    the fire-and-forget share through llm_execute_side_effects."""

    def setUp(self):
        super().setUp()
        from modules.module_toolregistry import get_registry, reset_registry
        reset_registry()
        self.addCleanup(reset_registry)
        self.movement_calls = []
        self.search_calls = []
        get_registry().register(_descriptor(
            "move", result_bearing=False,
            execute=lambda p, c: self.movement_calls.append(p) or "moved"))
        get_registry().register(_descriptor(
            "search", result_bearing=True,
            execute=lambda p, c: self.search_calls.append(p) or "sunny"))

    def _wired_turn(self, source):
        """The exact started-branch wiring the three call sites run."""
        from modules.module_toolloop import (ToolLoopRunner, TurnCallbacks,
                                             fire_and_forget_calls)
        from modules.module_llm import (llm_execute_side_effects,
                                        write_turn_memories)
        parsed = {"reply": "checking", "new_memories": [],
                  "function_calls": [
                      {"function": "move", "parameters": {"d": 1}},
                      {"function": "search", "parameters": {"q": 2}}]}
        spoken = []
        done = threading.Event()

        def on_state(state):
            if state in ("IDLE", "CANCELLED", "TIMEOUT"):
                done.set()

        runner = ToolLoopRunner(lambda *a: {"reply": "final"})
        started = runner.start_turn(
            parsed, "hello", source,
            TurnCallbacks(speak=spoken.append, on_state=on_state))
        self.assertTrue(started)
        # started-branch (the module_main/module_chatui/skill_discord pattern)
        threading.Thread(target=write_turn_memories,
                         args=(parsed, "hello"), daemon=True).start()
        ff_calls = fire_and_forget_calls(parsed)
        if ff_calls:
            ff_parsed = dict(parsed)
            ff_parsed["_memories_written"] = True  # the direct write owns memories
            ff_parsed["function_calls"] = ff_calls
            threading.Thread(target=llm_execute_side_effects,
                            args=(ff_parsed, "hello"), daemon=True).start()
        self.assertTrue(done.wait(5), "turn did not finish")
        deadline = time.time() + 5
        while not self.movement_calls and time.time() < deadline:
            time.sleep(0.01)
        return spoken

    def test_mixed_turn_dispatches_each_call_exactly_once(self):
        spoken = self._wired_turn("voice")
        self.assertEqual(len(self.movement_calls), 1)  # not dropped
        self.assertEqual(len(self.search_calls), 1)    # not doubled
        self.assertEqual(spoken, ["final"])

    def test_mixed_turn_webui_path(self):
        spoken = self._wired_turn("webui")
        self.assertEqual(len(self.movement_calls), 1)
        self.assertEqual(len(self.search_calls), 1)
        self.assertEqual(spoken, ["final"])

    def test_call_sites_wire_fire_and_forget(self):
        for rel in ("modules/module_main.py", "modules/module_chatui.py",
                    "skills/skill_discord.py"):
            with open(os.path.join(_SRC, rel), encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn("fire_and_forget_calls(", src,
                          f"{rel} must dispatch the fire-and-forget share "
                          f"in its started-branch")


class TestReply0Gate(_LoopTestCase):
    """Review F2: the follow-up must never speak over reply-0 TTS —
    speak blocks on the caller's reply-0-drained gate (bounded wait)."""

    def test_speak_waits_for_reply0_gate(self):
        import modules.module_toolloop as toolloop
        self.registry.register(_descriptor("search"))
        gate = threading.Event()
        callbacks = TurnCallbacks(speak=self.spoken.append,
                                  on_state=self._on_state,
                                  wait_for_reply0=gate)
        started = self._runner().start_turn(
            self._parsed([{"function": "search", "parameters": {}}]),
            "hello", "voice", callbacks)
        self.assertTrue(started)
        # turn reaches the gate and blocks — speak NOT called yet
        deadline = time.time() + 2
        while time.time() < deadline and not self.states:
            time.sleep(0.01)
        time.sleep(0.2)
        self.assertEqual(self.spoken, [],
                         "speak must not fire before reply-0 drains")
        gate.set()
        self._wait_turn()
        self.assertEqual(self.spoken, ["final answer"])  # exactly once

    def test_gate_never_set_bounded_wait(self):
        """A missing set can never hang the turn thread forever."""
        import modules.module_toolloop as toolloop
        self.registry.register(_descriptor("search"))
        gate = threading.Event()  # never set
        callbacks = TurnCallbacks(speak=self.spoken.append,
                                  on_state=self._on_state,
                                  wait_for_reply0=gate)
        patcher = mock.patch.object(toolloop, "REPLY0_GATE_TIMEOUT", 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)  # stays patched until the test ends
        started = self._runner().start_turn(
            self._parsed([{"function": "search", "parameters": {}}]),
            "hello", "voice", callbacks)
        self.assertTrue(started)
        self._wait_turn(timeout=5)
        self.assertEqual(self.spoken, ["final answer"])  # spoke anyway

    def test_reply0_gate_finally_guarded(self):
        """M4 review P3: a TTS exception between gate creation and the set
        must not leave the follow-up waiting the full 120 s — the set is
        finally-guarded."""
        with open(os.path.join(_SRC, "modules", "module_main.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        idx = src.index("_reply0_drained.set()")
        tail = src[:idx]
        last_finally = tail.rfind("finally:")
        self.assertNotEqual(last_finally, -1,
                            "_reply0_drained.set() must live in a finally "
                            "block")
        between = tail[last_finally + len("finally:"):]
        code_lines = [ln for ln in between.splitlines()
                      if ln.strip() and not ln.strip().startswith("#")]
        self.assertEqual(
            code_lines, [],
            "the gate set must sit directly inside the finally block "
            "(only comments may precede it)")

    def test_voice_wiring_passes_gate(self):
        # source pin: module_main creates the gate, passes it in TurnCallbacks
        # and sets it after the reply-0 TTS drains
        with open(os.path.join(_SRC, "modules", "module_main.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("wait_for_reply0=", src)
        self.assertIn("_reply0_drained.set()", src)
        # the set happens AFTER the pipeline join, not before start_turn
        self.assertLess(src.index("started = get_loop_runner().start_turn"),
                        src.index("_reply0_drained.set()"))


class TestConfigToolTimeout(_LoopTestCase):
    """Review F3: [LLM] tool_call_timeout must govern builtin tools —
    descriptors with timeout_s=None fall back to the configured default."""

    def test_tool_call_timeout_config_honored(self):
        import modules.module_toolloop as toolloop

        def slow(params, ctx):
            time.sleep(1.0)
            return "late"

        # timeout_s=None — the builtin registration shape (review F3)
        self.registry.register(_descriptor("slow", execute=slow,
                                           timeout_s=None))
        small = mock.patch.object(toolloop, "load_config",
                                  return_value={"LLM": {
                                      "tool_call_timeout": 0.2}})
        small.start()
        self.addCleanup(small.stop)
        self._runner().start_turn(
            self._parsed([{"function": "slow", "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertIn("ERROR: timeout", self.followup_calls[0][2])

    def test_tool_call_timeout_config_generous(self):
        import modules.module_toolloop as toolloop
        self.registry.register(_descriptor(
            "quick", execute=lambda p, c: "fast result", timeout_s=None))
        generous = mock.patch.object(toolloop, "load_config",
                                     return_value={"LLM": {
                                         "tool_call_timeout": 30}})
        generous.start()
        self.addCleanup(generous.stop)
        self._runner().start_turn(
            self._parsed([{"function": "quick", "parameters": {}}]),
            "hello", "voice", self.callbacks)
        self._wait_turn()
        self.assertIn("quick: fast result", self.followup_calls[0][2])
        self.assertNotIn("ERROR", self.followup_calls[0][2])


class TestLegacyMixedTurnDispatch(unittest.TestCase):
    """F1-REG1: under rollback (deferred_loop_enabled = false) the legacy
    thread must run ONLY the result-bearing calls — the caller stays the
    single owner of fire-and-forget dispatch on EVERY path.  A movement
    must physically run exactly once."""

    def setUp(self):
        super().setUp()
        reset_registry()
        self.addCleanup(reset_registry)
        from modules.module_toolregistry import get_registry
        self.movement_calls = []
        self.search_calls = []
        get_registry().register(_descriptor(
            "move", result_bearing=False,
            execute=lambda p, c: self.movement_calls.append(p) or "moved"))
        get_registry().register(_descriptor(
            "search", result_bearing=True,
            execute=lambda p, c: self.search_calls.append(p) or "sunny"))

    def _rollback(self):
        import modules.module_toolloop as toolloop
        patcher = mock.patch.object(
            toolloop, "load_config",
            return_value={"LLM": {"deferred_loop_enabled": False}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _caller_wiring(self, parsed, source="voice"):
        """The exact started-branch wiring the call sites run."""
        from modules.module_toolloop import (ToolLoopRunner, TurnCallbacks,
                                             fire_and_forget_calls)
        from modules.module_llm import (llm_execute_side_effects,
                                        write_turn_memories)
        spoken = []
        done = threading.Event()

        def on_state(state):
            if state in ("IDLE", "CANCELLED", "TIMEOUT"):
                done.set()

        started = ToolLoopRunner(lambda *a: {"reply": "final"}).start_turn(
            parsed, "hello", source,
            TurnCallbacks(speak=spoken.append, on_state=on_state))
        self.assertTrue(started)
        threading.Thread(target=write_turn_memories,
                         args=(parsed, "hello"), daemon=True).start()
        ff_calls = fire_and_forget_calls(parsed)
        ff_thread = None
        if ff_calls:
            ff_parsed = dict(parsed)
            ff_parsed["_memories_written"] = True
            ff_parsed["function_calls"] = ff_calls
            ff_thread = threading.Thread(target=llm_execute_side_effects,
                                          args=(ff_parsed, "hello"),
                                          daemon=True)
            ff_thread.start()
        self.assertTrue(done.wait(5), "legacy turn did not finish")
        if ff_thread is not None:
            ff_thread.join(timeout=5)  # deterministic: both paths settled
        return spoken

    def test_legacy_path_dispatches_fire_and_forget_exactly_once(self):
        self._rollback()
        parsed = {"reply": "checking", "new_memories": [],
                  "function_calls": [
                      {"function": "move", "parameters": {"d": 1}},
                      {"function": "search", "parameters": {"q": 2}}]}
        spoken = self._caller_wiring(parsed)
        self.assertEqual(len(self.movement_calls), 1,
                         "movement must run exactly once under rollback "
                         "(no double dispatch)")
        self.assertEqual(len(self.search_calls), 1)
        self.assertIn("sunny", spoken)  # result-bearing builtin spoken

    def test_legacy_path_pure_fire_and_forget_unchanged(self):
        self._rollback()
        from modules.module_toolloop import ToolLoopRunner, TurnCallbacks
        from modules.module_llm import llm_execute_side_effects
        parsed = {"reply": "ok", "new_memories": [],
                  "function_calls": [{"function": "move",
                                      "parameters": {}}]}
        spoken = []
        started = ToolLoopRunner(lambda *a: {"reply": "x"}).start_turn(
            parsed, "hello", "voice",
            TurnCallbacks(speak=spoken.append))
        self.assertFalse(started)  # the legacy thread does not run
        # the caller's llm_execute_side_effects dispatches it exactly once
        llm_execute_side_effects(parsed, "hello")  # synchronous dispatch
        self.assertEqual(len(self.movement_calls), 1)
        self.assertEqual(self.search_calls, [])
        self.assertEqual(spoken, [])


class TestFireAndForgetReplies(unittest.TestCase):
    """Audit A3: non-followup builtin skills that RETURN a string must
    still surface it in the reply on the inline (fire-and-forget) path —
    the pre-M4 behavior webui/discord rely on (e.g. home_assistant's
    'Lights on' / error text). The loop path is unchanged."""

    def test_fire_and_forget_reply_surfaces(self):
        import concurrent.futures
        from modules.module_toolregistry import (ToolDescriptor,
                                                 ToolResult, reset_registry)
        from modules import module_llm
        reset_registry()
        self.addCleanup(reset_registry)

        def fake_exec(params, ctx):
            return "Lights on"

        descriptor = ToolDescriptor(
            name="home_assistant", kind="builtin", description="",
            parameters=None, result_bearing=False, timeout_s=None,
            execute=fake_exec)
        from modules.module_toolregistry import get_registry
        get_registry().register(descriptor)
        parsed = {"reply": "",
                  "function_calls": [{"function": "home_assistant",
                                      "parameters": {}}],
                  "new_memories": []}
        # the webui fire-and-forget path: llm_execute_side_effects inline
        module_llm.llm_execute_side_effects(parsed, "turn on the lights",
                                             source="webui")
        self.assertEqual(parsed.get("reply"), "Lights on",
                         "the fire-and-forget skill's return string must "
                         "surface in the reply")
        self.assertTrue(parsed.get("_skill_replied"))


if __name__ == "__main__":
    unittest.main()
