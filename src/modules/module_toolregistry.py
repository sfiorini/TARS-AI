"""
Module : Tool Registry
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

One interface for every tool pool (design §3): the deferred loop, the
prompt and the UI never know whether a tool is a builtin skill, an agent
skill or an MCP tool.
"""
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from modules.module_messageQue import queue_message


@dataclass
class ToolDescriptor:
    name: str                 # model-facing, unique per pool
    kind: str                 # "builtin" | "agent_skill" | "mcp"
    description: str          # for prompt assembly, pre-truncated by pool
    parameters: Optional[dict]  # {"properties": {...}, "required": [...]} | None
    result_bearing: bool      # True -> participates in the deferred loop
    timeout_s: float          # per-call override of tool_call_timeout
    execute: Callable[[dict, "ToolContext"], "ToolResult"]


@dataclass
class ToolResult:
    content: str              # text fed to the model (never None; "" allowed)
    is_error: bool = False    # MCP isError / raised exception -> fed back (D1)
    structured: Optional[dict] = None
    raw: Any = None


@dataclass
class ToolContext:
    """Superset of today's skill context dict."""
    bot_response: dict = field(default_factory=dict)
    user_input: str = ""
    source: str = "voice"
    has_image: bool = False
    config: dict = field(default_factory=dict)
    skill_config: Optional[dict] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class ToolRegistry:
    """Registry of ToolDescriptors; dispatch never raises."""

    def __init__(self):
        self._tools: Dict[str, ToolDescriptor] = {}

    def register(self, descriptor):
        """Register a tool. Duplicate names keep the first registration and
        disable the later one (defensive collision policy, design §3)."""
        existing = self._tools.get(descriptor.name)
        if existing is not None:
            queue_message(
                f"TOOLREGISTRY: duplicate tool '{descriptor.name}' from "
                f"{descriptor.kind}, disabling later")
            return
        self._tools[descriptor.name] = descriptor

    def get(self, name):
        return self._tools.get(name)

    def dispatch(self, name, params, ctx) -> ToolResult:
        """Dispatch a tool call. NEVER raises: unknown names and raised
        exceptions both come back as ToolResult(is_error=True)."""
        descriptor = self._tools.get(name)
        if descriptor is None:
            return ToolResult(content=f"Unknown function: {name}",
                              is_error=True)
        try:
            result = descriptor.execute(params, ctx)
        except Exception as exc:  # noqa: BLE001 — by contract
            return ToolResult(content=str(exc), is_error=True)
        if isinstance(result, ToolResult):
            return result
        if result is None:
            return ToolResult(content="")
        return ToolResult(content=str(result))

    def iter_descriptors(self):
        return list(self._tools.values())

    def get_prompt_summaries(self):
        """Prompt-facing summaries: (name, description, parameters)."""
        return [(d.name, d.description, d.parameters)
                for d in self._tools.values()]


_registry = None


def get_registry():
    """Module-level singleton."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def reset_registry():
    """Drop the singleton (tests)."""
    global _registry
    _registry = None
