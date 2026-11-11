"""
Core application state — thread-safe, importable from any module.

Usage:
    from modules.module_state import set_tars_state, get_tars_state, TarsState

    set_tars_state(TarsState.TALKING)
    if get_tars_state() == TarsState.LISTENING:
        ...
"""

import threading
from enum import Enum
from typing import Callable, List

from modules.module_config import load_config


class TarsState(str, Enum):
    BOOTING = "BOOTING"
    STANDBY = "STANDBY"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    TALKING = "TALKING"


_state: TarsState = TarsState.BOOTING
_lock = threading.Lock()
_listeners: List[Callable[[TarsState, TarsState], None]] = []


def get_tars_state() -> TarsState:
    """Get current TARS state. Thread-safe."""
    with _lock:
        return _state


def _swap_state_locked(new_state: TarsState):
    """Set the state. Caller MUST hold `_lock`.

    Returns (old_state, listeners) when the state actually changed, or None
    when it was already `new_state`. Kept separate so callers that need the
    check and the write to be atomic (e.g. restore_state_if_owned) can do
    both under a single lock acquisition — set_tars_state re-acquires `_lock`
    and `_lock` is a plain non-reentrant Lock, so calling it while holding the
    lock would deadlock.
    """
    global _state
    old = _state
    if old == new_state:
        return None
    _state = new_state
    return old, _listeners.copy()


def _announce_state_change(old: TarsState, new_state: TarsState, listeners) -> None:
    """Log and notify listeners. MUST be called OUTSIDE `_lock`."""
    # PROBE P1 (temporary instrumentation — stuck-LISTENING investigation).
    # Records EVERY accepted transition with the writing thread, so the log
    # shows which thread wrote the last state before a stall. Remove after use.
    try:
        from modules.module_messageQue import queue_message as _qm
        import time as _t
        _qm(f"PROBE-STATE {old.value}->{new_state.value} "
            f"thread={threading.current_thread().name} t={_t.monotonic():.3f}")
    except Exception:
        pass
    # Log state change in debug mode
    try:
        if load_config().get('STT', {}).get('debug', False):
            from modules.module_messageQue import queue_message
            queue_message(f"DEBUG: State: {old.value} → {new_state.value}")
    except Exception:
        pass

    # Notify outside lock to avoid deadlocks
    for fn in listeners:
        try:
            fn(old, new_state)
        except Exception:
            pass


def set_tars_state(new_state: TarsState) -> None:
    """Set TARS state. Notifies all registered listeners. Thread-safe."""
    with _lock:
        changed = _swap_state_locked(new_state)
    if changed is None:
        return
    _announce_state_change(changed[0], new_state, changed[1])


def restore_state_if_owned(prev_state: TarsState, owned: TarsState) -> bool:
    """Put the state back to `prev_state`, but only if it is still `owned`.

    For work that temporarily takes the state and must give it back — e.g. a
    deferred follow-up speaking on the tool-loop thread. If another thread
    wrote a transition while that work ran, that write wins: the borrower no
    longer owns the state and must not clobber it.

    The check and the write happen under ONE acquisition of `_lock`, so no
    competing writer can slip a transition in between them (a plain
    get-then-set would leave exactly that window open).

    Returns True if the state was restored, False if it was left alone.
    """
    with _lock:
        if _state != owned:
            return False
        changed = _swap_state_locked(prev_state)
    if changed is not None:
        _announce_state_change(changed[0], prev_state, changed[1])
    return True


def on_state_change(callback: Callable[[TarsState, TarsState], None]) -> None:
    """Register a listener called on state changes: callback(old_state, new_state)."""
    with _lock:
        _listeners.append(callback)


def remove_state_change(callback: Callable[[TarsState, TarsState], None]) -> None:
    """Unregister a previously registered state change listener."""
    with _lock:
        try:
            _listeners.remove(callback)
        except ValueError:
            pass


# Reference to STT manager, set by app.py at startup
_stt_manager = None


def register_stt_manager(stt_mgr) -> None:
    """Register the STT manager so force_standby() can cancel it."""
    global _stt_manager
    _stt_manager = stt_mgr


def force_standby() -> None:
    """Kill any active STT session and force TARS into STANDBY. Safe to call from anywhere."""
    if _stt_manager:
        _stt_manager.cancel()
    set_tars_state(TarsState.STANDBY)
