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
from dataclasses import dataclass
from typing import Callable, Optional

from modules.module_messageQue import queue_message
from modules.module_config import load_config
from modules.module_toolregistry import (get_registry, ToolContext,
                                          ToolResult)

FALLBACK_LINE = "I hit a snag finishing that — check the logs."
HOLDING_LINE = "On it — I'll report back."
LEGACY_UNAVAILABLE_LINE = "That tool isn't available in legacy mode."
TOOL_RESULT_CHAR_LIMIT = 10000  # result text truncated before the follow-up
REPLY0_GATE_TIMEOUT = 120  # bounded wait on the reply-0 gate (seconds)


@dataclass
class TurnCallbacks:
    """Per-turn delivery: speak is the harness's TTS/emit/send channel;
    on_state is a UI/log hook (default no-op); wait_for_reply0 is the
    voice harness's reply-0-drained gate — speak blocks on it (bounded)
    so the follow-up never talks over reply-0 TTS (None = no gate)."""
    speak: Callable[[str], None]
    on_state: Callable[[str], None] = lambda state: None
    wait_for_reply0: Optional[threading.Event] = None


def _wait_for_reply0(callbacks):
    """Block until the caller's reply-0 TTS has drained (bounded wait —
    a missing set can never hang the turn thread forever)."""
    gate = callbacks.wait_for_reply0
    if gate is None:
        return
    if not gate.wait(timeout=REPLY0_GATE_TIMEOUT):
        queue_message("TOOLLOOP: reply-0 gate never set — speaking anyway")


# ── epoch cancellation ──────────────────────────────────────────────
# A process-global counter per source; every utterance callback entry
# bumps it.  A turn thread that sees a stale epoch is CANCELLED — its
# results are logged and discarded, never injected into the new turn.

_epochs = {}
_epoch_lock = threading.Lock()


def fire_and_forget_calls(parsed):
    """The turn's NON-result-bearing function calls — the caller's
    side-effect thread must still dispatch these when the deferred loop
    owns the result-bearing ones (a mixed turn drops nothing; design §2
    fire-and-forget dispatch is immediate and unconditional)."""
    if not isinstance(parsed, dict):
        return []
    calls = parsed.get("function_calls") or []
    out = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        descriptor = get_registry().get(call.get("function", ""))
        if descriptor is None or not descriptor.result_bearing:
            out.append(call)
    return out


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
        # Rollback switch (checked once per turn): config false → the
        # one-release legacy blocking path, no loop
        if not load_config().get("LLM", {}).get("deferred_loop_enabled",
                                                 True):
            return self._run_blocking_legacy(parsed, user_text, source,
                                             callbacks)
        callbacks.on_state("CLASSIFY")
        calls = self._result_bearing_calls(parsed.get("function_calls"))
        if not calls:
            return False
        # Holding line (design §2 step 3): reply-0 doubles as the natural
        # holding line (the caller's pipeline speaks it — no double-speak);
        # only when reply-0 is empty does the loop supply one.
        reply0 = parsed.get("reply", "") or ""
        if not reply0.strip():
            played = False
            try:
                from modules.module_llm import _maybe_play_thinking_response
                played = bool(_maybe_play_thinking_response())
            except Exception:  # noqa: BLE001 — holding line must never fail a turn
                played = False
            if not played:
                try:
                    callbacks.speak(HOLDING_LINE)
                except Exception as exc:  # noqa: BLE001
                    queue_message(f"TOOLLOOP: speak callback failed: {exc}")
        epoch = _current_epoch(source)
        threading.Thread(
            target=self._run_turn,
            args=(parsed, user_text, source, callbacks, epoch, calls),
            daemon=True, name=f"toolloop-{source}").start()
        return True

    def _run_blocking_legacy(self, parsed, user_text, source, callbacks):
        """One-release rollback (deferred_loop_enabled = false).

        Reproduces today's behavior: when a result-bearing tool is present,
        ALL calls dispatch inline (blocking); result-bearing builtin
        results are spoken directly; MCP/agent-skill result tools (not yet
        existing) speak the canned fallback.  Returns False for turns with
        no result-bearing calls — the caller's background side-effect
        thread handles those exactly as today.
        """
        all_calls = parsed.get("function_calls") or []
        result_calls = self._result_bearing_calls(all_calls)
        if not result_calls:
            return False
        # Daemon thread, NOT inline: the caller (module_main) cannot reach
        # pipeline.join — which sets the reply-0 gate — until start_turn
        # returns; a synchronous gate wait would deadlock the voice harness.
        threading.Thread(target=self._blocking_legacy_turn,
                         args=(parsed, user_text, source, callbacks),
                         daemon=True, name=f"toolloop-legacy-{source}").start()
        return True

    def _blocking_legacy_turn(self, parsed, user_text, source, callbacks):
        all_calls = parsed.get("function_calls") or []
        ctx = ToolContext(bot_response=parsed, user_input=user_text,
                          source=source, config=load_config())
        # the follow-up must never speak over reply-0 TTS (review F2)
        _wait_for_reply0(callbacks)
        result_results = []
        for call in all_calls:
            name = call.get("function", "")
            descriptor = get_registry().get(name)
            result = get_registry().dispatch(
                name, call.get("parameters", {}) or {}, ctx)
            if descriptor is not None and descriptor.result_bearing:
                result_results.append((descriptor, result))
        for descriptor, result in result_results:
            if (descriptor.kind == "builtin"
                    and result.content and not result.is_error):
                line = result.content
            else:
                line = LEGACY_UNAVAILABLE_LINE
            try:
                callbacks.speak(line)
            except Exception as exc:  # noqa: BLE001
                queue_message(f"TOOLLOOP: speak callback failed: {exc}")
        callbacks.on_state("IDLE")
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
            if isinstance(followup, dict):
                new_calls = self._result_bearing_calls(
                    followup.get("function_calls"))
                reply = followup.get("reply", "")
            elif isinstance(followup, str):
                new_calls = []
                reply = followup
            else:
                # non-dict/non-str garbage is never spoken verbatim (F5)
                new_calls = []
                reply = FALLBACK_LINE
            if new_calls and hops_used < max_hops:
                pending = new_calls
                continue
            if new_calls:
                # hop limit with still-pending work
                return self._timeout(callbacks, parts)
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
                timeout = (descriptor.timeout_s
                           if descriptor is not None
                           and descriptor.timeout_s is not None
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
        # the follow-up must never speak over reply-0 TTS (review F2)
        _wait_for_reply0(callbacks)
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
