"""Voice-turn side-effect dispatch tests (P0 regression).

Robot evidence (2026-11-09, every voice turn with a function call):

    ERROR: cannot access local variable 'llm_execute_side_effects'
    where it is not associated with a value

Root cause: ``module_main.utterance_callback`` had a LOCAL
``from modules.module_llm import (llm_execute_side_effects, ...)``
inside the ``if started:`` branch. Any import anywhere in a function
makes that name LOCAL for the whole function, so the ``elif`` (start_turn
returned False → pure fire-and-forget turn) read it before assignment →
UnboundLocalError, swallowed by the function-wide ``except`` and logged.
Result: NO tool ever executed on voice turns — movements, volume,
discord sends, everything.

``module_chatui._process_chat_message`` and
``skills.skill_discord._run_side_effects_sync`` place the same local
import ABOVE the ``started = ...`` branch and are correct; the static
source-order guard below pins that pattern in all three files.
"""
import json
import os
import sys
import threading
import types
import unittest
from unittest import mock

# Fake Pi-only hardware so module_servoctl (via module_main) imports on
# any dev machine — same pattern as test_tool_loop.py.
board = types.ModuleType("board")
board.SCL = "SCL"
board.SDA = "SDA"
busio = types.ModuleType("busio")
busio.I2C = mock.MagicMock()
pca9685 = types.ModuleType("adafruit_pca9685")
pca9685.PCA9685 = mock.MagicMock()
for _name, _mod in (("board", board), ("busio", busio),
                    ("adafruit_pca9685", pca9685)):
    sys.modules.setdefault(_name, _mod)

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

import modules.module_speed as module_speed  # noqa: E402
import modules.module_toolloop as module_toolloop  # noqa: E402
import modules.module_llm as module_llm  # noqa: E402
import modules.module_main as module_main  # noqa: E402


class _FakeRunner:
    """Stands in for get_loop_runner(): start_turn() classifies only."""

    def __init__(self, started):
        self.started = started
        self.start_turn_calls = []

    def start_turn(self, parsed, user_text, source, callbacks):
        self.start_turn_calls.append((parsed, user_text, source))
        return self.started


class _VoiceTurnTestCase(unittest.TestCase):
    """Shared harness: drives utterance_callback via the preemptive-LLM
    path (no streaming, no real TTS) with every heavy collaborator
    patched, then inspects the side-effect dispatch."""

    def setUp(self):
        # module-level globals other tests may have touched
        for name in ("ui_manager", "stt_manager", "memory_manager"):
            saved = getattr(module_main, name)
            setattr(module_main, name, None)
            self.addCleanup(setattr, module_main, name, saved)

        # speed profiler
        for attr in ("mark_utterance_start", "start", "stop"):
            p = mock.patch.object(module_speed, attr)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(module_speed, "enabled", False)
        p.start()
        self.addCleanup(p.stop)

        # tool loop: epoch bump + runner classification
        p = mock.patch.object(module_toolloop, "bump_epoch")
        p.start()
        self.addCleanup(p.stop)

        # side effects (imported locally inside utterance_callback →
        # patching the module attribute is what the function sees)
        self.side_effect_calls = []
        self.side_effect_ran = threading.Event()

        def _fake_side_effects(parsed, user_text, **kwargs):
            self.side_effect_calls.append((parsed, user_text, kwargs))
            self.side_effect_ran.set()

        p = mock.patch.object(module_llm, "llm_execute_side_effects",
                              _fake_side_effects)
        p.start()
        self.addCleanup(p.stop)

        self.memories_calls = []
        self.memories_written = threading.Event()

        def _fake_write_memories(parsed, user_text):
            self.memories_calls.append((parsed, user_text))
            self.memories_written.set()

        p = mock.patch.object(module_llm, "write_turn_memories",
                              _fake_write_memories)
        p.start()
        self.addCleanup(p.stop)

        # emotion detection (CONFIG has it enabled)
        for attr in ("detect_emotion", "detect_emotion_from_llm"):
            p = mock.patch.object(module_main, attr,
                                  return_value=(None, None, {}))
            p.start()
            self.addCleanup(p.stop)

        # TTS: pipeline construction + preemptive reply playback
        p = mock.patch.object(module_main, "SentenceTTSPipeline")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(module_main, "play_audio_chunks",
                              new=mock.AsyncMock(return_value=False))
        p.start()
        self.addCleanup(p.stop)

        # state + queue capture (the robot-visible error channel)
        p = mock.patch.object(module_main, "set_tars_state")
        p.start()
        self.addCleanup(p.stop)
        self.queued = []
        p = mock.patch.object(module_main, "queue_message",
                              side_effect=self.queued.append)
        p.start()
        self.addCleanup(p.stop)

    def _run_voice_turn(self, parsed, runner):
        p = mock.patch.object(module_toolloop, "get_loop_runner",
                              return_value=runner)
        p.start()
        self.addCleanup(p.stop)
        message = json.dumps({
            "text": "move forward",
            "preemptive_llm_result": parsed,
        })
        module_main.utterance_callback(message)

    def _assert_no_error_queued(self):
        errors = [m for m in self.queued
                  if isinstance(m, str) and m.startswith("ERROR:")]
        self.assertEqual([], errors,
                         f"utterance_callback swallowed an error: {errors!r}")


class TestVoiceFireAndForgetDispatch(_VoiceTurnTestCase):
    """P0: a pure fire-and-forget voice turn (start_turn → False) must
    dispatch llm_execute_side_effects — the branch that raised
    UnboundLocalError on the robot."""

    def test_fire_and_forget_turn_dispatches_side_effects(self):
        parsed = {
            "reply": "Moving forward.",
            "function_calls": [
                {"name": "move_robot",
                 "parameters": {"direction": "forward"}},
            ],
        }
        runner = _FakeRunner(started=False)
        self._run_voice_turn(parsed, runner)

        self.assertTrue(
            self.side_effect_ran.wait(timeout=5),
            "llm_execute_side_effects was never invoked; queued messages: "
            f"{self.queued!r}")
        self._assert_no_error_queued()
        self.assertEqual(1, len(self.side_effect_calls))
        dispatched_parsed, dispatched_text, kwargs = self.side_effect_calls[0]
        self.assertEqual("move forward", dispatched_text)
        self.assertEqual(parsed["function_calls"],
                         dispatched_parsed["function_calls"])


class TestVoiceResultBearingDispatch(_VoiceTurnTestCase):
    """A result-bearing turn (start_turn → True): memories written once,
    fire-and-forget share dispatched exactly once via the side-effect
    thread (design §2 — the loop only runs result-bearing calls)."""

    def test_result_bearing_turn_writes_memories_and_dispatches_ff_once(self):
        result_call = {"name": "get_weather",
                       "parameters": {"city": "Rome"}}
        ff_call = {"name": "move_robot",
                   "parameters": {"direction": "forward"}}
        parsed = {
            "reply": "Checking.",
            "function_calls": [result_call, ff_call],
        }
        runner = _FakeRunner(started=True)
        p = mock.patch.object(module_toolloop, "fire_and_forget_calls",
                              return_value=[ff_call])
        p.start()
        self.addCleanup(p.stop)
        self._run_voice_turn(parsed, runner)

        self.assertTrue(self.memories_written.wait(timeout=5),
                        "write_turn_memories was never invoked")
        self.assertEqual(1, len(self.memories_calls))
        self.assertTrue(
            self.side_effect_ran.wait(timeout=5),
            "fire-and-forget share was never dispatched; queued: "
            f"{self.queued!r}")
        self._assert_no_error_queued()
        self.assertEqual(1, len(self.side_effect_calls),
                         "fire-and-forget share must dispatch exactly once")
        dispatched_parsed, _text, _kw = self.side_effect_calls[0]
        self.assertEqual([ff_call], dispatched_parsed["function_calls"],
                         "only the fire-and-forget calls go to the "
                         "side-effect thread")
        self.assertTrue(dispatched_parsed.get("_memories_written"))


def _function_body(source, def_line, next_def_line):
    start = source.index(def_line)
    end = source.index(next_def_line, start)
    return source[start:end]


class TestSideEffectImportOrderGuard(unittest.TestCase):
    """Static guard (cheap, catches this whole bug class): in each
    dispatcher, the LOCAL ``from modules.module_llm import ...`` that
    binds llm_execute_side_effects must appear BEFORE the first
    ``start_turn`` call — otherwise a later branch reads the name before
    assignment (UnboundLocalError).

    module_chatui and skill_discord already follow this pattern; this
    prevents the same regression there.
    """

    CASES = [
        ("modules/module_main.py",
         "def utterance_callback(message):",
         "def post_utterance_callback():"),
        ("modules/module_chatui.py",
         "def _process_chat_message(msg, img_b64):",
         "def receive_user_message():"),
        ("skills/skill_discord.py",
         "def _run_side_effects_sync(reply, user_message, send_followup=None):",
         "@bot.event"),
    ]

    def test_local_llm_import_precedes_first_start_turn_call(self):
        for relpath, def_line, next_line in self.CASES:
            with self.subTest(relpath=relpath):
                with open(os.path.join(_SRC, relpath),
                          encoding="utf-8") as fh:
                    body = _function_body(fh.read(), def_line, next_line)
                import_pos = body.find("from modules.module_llm import")
                self.assertGreater(
                    import_pos, -1,
                    f"{relpath}: local module_llm import not found")
                self.assertIn("llm_execute_side_effects",
                              body[import_pos:body.find("\n", import_pos)],
                              f"{relpath}: local import must bind "
                              "llm_execute_side_effects")
                start_turn_pos = body.find("start_turn(")
                self.assertGreater(
                    start_turn_pos, -1,
                    f"{relpath}: start_turn call not found")
                self.assertLess(
                    import_pos, start_turn_pos,
                    f"{relpath}: the local llm_execute_side_effects "
                    "import must execute BEFORE the first start_turn "
                    "call — an import below it makes the name local for "
                    "the whole function and later branches read it "
                    "before assignment (UnboundLocalError)")
