"""
Module : Tool Loop (deferred reply, D1)
Author: Charles-Olivier Dion (AtomikSpace)
Contact: atomikspace.labs@gmail.com
Copyright (c) 2026 Charles-Olivier Dion

This file is authored by Charles-Olivier Dion and is dual-licensed.

Non-Commercial License:
This file is licensed under Creative Commons Attribution-NonCommercial 4.0 International (CC-BY-NC 4.0).
You may use, modify, and redistribute this file for NON-COMMERCIAL purposes only, with attribution.

Commercial License:
Commercial use (including selling products, paid services, SaaS, subscriptions, Patreon rewards, or derivatives)
requires a separate written license from Charles-Olivier Dion (AtomikSpace).

This license applies only to this file and does not override licenses of other files in the repository.

The deferred reply loop (design §2): result-bearing tool calls trigger a
holding line, run, then a follow-up LLM completion composes the final
spoken reply from the results.  Fire-and-forget tools NEVER enter the
loop — the caller's existing side-effect thread stays the single
dispatcher for them (a loop-side dispatch would run every movement
twice).
"""
import concurrent.futures
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from modules.module_messageQue import queue_message
from modules.module_config import load_config
from modules.module_toolregistry import (get_registry, ToolContext,
                                          ToolResult)

FALLBACK_LINE = "I hit a snag finishing that — check the logs."
TOOL_RESULT_CHAR_LIMIT = 10000  # result text truncated before the follow-up

STATES = ("IDLE", "CLASSIFY", "TOOLS_RUNNING", "FOLLOWUP_LLM",
          "SPEAK_FOLLOWUP", "CANCELLED", "TIMEOUT")


@dataclass
class TurnCallbacks:
    """Per-turn delivery: speak is the harness's TTS/emit/send channel;
    on_state is a UI/log hook (default no-op)."""
    speak: Callable[[str], None]
    on_state: Callable[[str], None] = lambda state: None


# ── epoch cancellation ──────────────────────────────────────────────
# A process-global counter per source; every utterance callback entry
# bumps it.  A turn thread that sees a stale epoch is CANCELLED — its
# results are logged and discarded, never injected into the new turn.

_epochs = {}
_epoch_lock = threading.Lock()


def bump_epoch(source):
    """Invalidate any in-flight turn for this source (new utterance)."""
    with _epoch_lock:
        _epochs[source] = _epochs.get(source, 0) + 1
        return _epochs[source]


def _current_epoch(source):
    with _epoch_lock:
        return _epochs.get(source, 0)


class ToolLoopRunner:
    """Owns everything after `parsed` returns, for result-bearing turns."""

    def __init__(self, followup_fn):
        """followup_fn(user_input, prior_reply, tool_results_text, source)
        -> parsed dict | str | None — injected; the loop never imports
        module_llm (testable without network)."""
        self._followup_fn = followup_fn

    # ── turn entry ────────────────────────────────────────────────

    def start_turn(self, parsed, user_text, source, callbacks):
        """Classify the turn's function calls.

        Returns True iff the turn entered the loop (a daemon turn thread
        now owns it).  Returns False immediately when no call is
        result-bearing — classification only, ZERO dispatch: the
        caller's existing side-effect thread stays the single dispatcher
        for fire-and-forget calls.
        """
        if not isinstance(parsed, dict):
            return False
        callbacks.on_state("CLASSIFY")
        calls = self._result_bearing_calls(parsed.get("function_calls"))
        if not calls:
            return False
        epoch = _current_epoch(source)
        threading.Thread(
            target=self._run_turn,
            args=(parsed, user_text, source, callbacks, epoch, calls),
            daemon=True, name=f"toolloop-{source}").start()
        return True

    # ── the turn thread ───────────────────────────────────────────

    def _run_turn(self, parsed, user_text, source, callbacks, epoch,
                  initial_calls):
        config = load_config()
        llm = config.get("LLM", {})
        max_hops = llm.get("tool_loop_max_hops", 2)
        default_timeout = llm.get("tool_call_timeout", 60)
        request_timeout = llm.get("request_timeout", 120)
        deadline = (time.monotonic()
                    + max_hops * (default_timeout + request_timeout))
        ctx = ToolContext(bot_response=parsed, user_input=user_text,
                          source=source, config=config)

        parts = []
        pending = initial_calls
        hops_used = 0
        while True:
            if not self._epoch_ok(source, epoch):
                return self._cancel(callbacks, source)
            callbacks.on_state("TOOLS_RUNNING")
            for name, result in self._run_tools(pending, ctx,
                                                default_timeout):
                text = (result.content if not result.is_error
                        else f"ERROR: {result.content}")
                parts.append(f"{name}: {text}")
            if not self._epoch_ok(source, epoch):
                return self._cancel(callbacks, source)
            if time.monotonic() > deadline:
                return self._timeout(callbacks, parts)
            callbacks.on_state("FOLLOWUP_LLM")
            hops_used += 1
            tool_text = "\n".join(parts)[-TOOL_RESULT_CHAR_LIMIT:]
            followup = self._safe_followup(
                user_text, parsed.get("reply", "") or "", tool_text,
                source)
            if not self._epoch_ok(source, epoch):
                return self._cancel(callbacks, source)
            if followup is None:
                # request failure -> timeout fallback
                return self._timeout(callbacks, parts)
            new_calls = self._result_bearing_calls(
                followup.get("function_calls")
                if isinstance(followup, dict) else None)
            if new_calls and hops_used < max_hops:
                pending = new_calls
                continue
            if new_calls:
                # hop limit with still-pending work
                return self._timeout(callbacks, parts)
            reply = (followup.get("reply", "")
                     if isinstance(followup, dict) else str(followup))
            if not str(reply).strip():
                reply = FALLBACK_LINE
            return self._speak(callbacks, source, epoch, reply)

    # ── helpers ────────────────────────────────────────────────────

    def _result_bearing_calls(self, calls):
        if not calls:
            return []
        out = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            descriptor = get_registry().get(call.get("function", ""))
            if descriptor is not None and descriptor.result_bearing:
                out.append(call)
        return out

    def _run_tools(self, calls, ctx, default_timeout):
        """Run each result-bearing call in its own thread, per-call
        timeout from descriptor.timeout_s; results in call order."""
        results = []
        if not calls:
            return results
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(calls)))
        try:
            futures = []
            for call in calls:
                name = call.get("function", "")
                descriptor = get_registry().get(name)
                timeout = (descriptor.timeout_s if descriptor
                           else default_timeout)
                futures.append((name, timeout, pool.submit(
                    get_registry().dispatch, name,
                    call.get("parameters", {}) or {}, ctx)))
            for name, timeout, future in futures:
                try:
                    result = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    result = ToolResult(content="timeout", is_error=True)
                except Exception as exc:  # noqa: BLE001 — by contract
                    result = ToolResult(content=str(exc), is_error=True)
                results.append((name, result))
        finally:
            pool.shutdown(wait=False)
        return results

    def _safe_followup(self, user_text, prior_reply, tool_text, source):
        try:
            return self._followup_fn(user_text, prior_reply, tool_text,
                                     source)
        except Exception as exc:  # noqa: BLE001 — never raise to caller
            queue_message(f"TOOLLOOP: follow-up completion failed: {exc}")
            return None

    @staticmethod
    def _epoch_ok(source, epoch):
        return _current_epoch(source) == epoch

    @staticmethod
    def _cancel(callbacks, source):
        queue_message(
            f"TOOLLOOP: turn cancelled (source={source}) — results "
            f"discarded, not injected into the new turn")
        callbacks.on_state("CANCELLED")
        return None

    @staticmethod
    def _timeout(callbacks, parts):
        queue_message("TOOLLOOP: turn hit its limit — speaking fallback")
        callbacks.on_state("TIMEOUT")
        if parts:
            queue_message(f"TOOLLOOP: raw tool text: "
                          f"{''.join(parts)[:500]}")
        try:
            callbacks.speak(FALLBACK_LINE)
        except Exception as exc:  # noqa: BLE001
            queue_message(f"TOOLLOOP: speak callback failed: {exc}")
        callbacks.on_state("IDLE")
        return None

    @staticmethod
    def _speak(callbacks, source, epoch, reply):
        if _current_epoch(source) != epoch:
            return ToolLoopRunner._cancel(callbacks, source)
        callbacks.on_state("SPEAK_FOLLOWUP")
        try:
            callbacks.speak(reply)
        except Exception as exc:  # noqa: BLE001
            queue_message(f"TOOLLOOP: speak callback failed: {exc}")
        callbacks.on_state("IDLE")
        return None


_runner = None


def get_loop_runner(followup_fn=None):
    """Module-level singleton; lazily wired to module_llm's
    followup_completion when no followup_fn is supplied (tests inject
    their own)."""
    global _runner
    if _runner is None:
        if followup_fn is None:
            from modules.module_llm import followup_completion
            followup_fn = followup_completion
        _runner = ToolLoopRunner(followup_fn)
    return _runner
