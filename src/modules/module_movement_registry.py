"""
Module : Movement Registry
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
"""

LEGS_ONLY = "legs_only"
HAS_ARMS = "has_arms"

# Store-backed shim (S-M2-01): MOVEMENTS is rebuilt from the movement
# store, so movements created/edited via the UI or CLI are immediately
# dispatchable without a restart.  The dict is mutated IN PLACE by
# reload_from_store() so every existing `from ... import MOVEMENTS`
# binding (servoctl, chatui, btcontroller, skill) stays live.
MOVEMENTS = {}


def reload_from_store():
    """Rebuild MOVEMENTS (id -> {name, type, description}) from the store."""
    from modules.module_movementstore import get_movement_store
    entries = get_movement_store().get_all()
    rebuilt = {mid: {"name": e["name"],
                    "type": e["type"],
                    "description": e["description"]}
               for mid, e in entries.items()}
    MOVEMENTS.clear()
    MOVEMENTS.update(rebuilt)


def get_all():
    return MOVEMENTS

def get_by_type(movement_type):
    return {k: v for k, v in MOVEMENTS.items() if v["type"] == movement_type}

def get_legs_only():
    return get_by_type(LEGS_ONLY)

def get_has_arms():
    return get_by_type(HAS_ARMS)

def get_names():
    return [(v["name"], k) for k, v in MOVEMENTS.items()]

def get_names_by_type(movement_type):
    return [(v["name"], k) for k, v in MOVEMENTS.items() if v["type"] == movement_type]


# Load from the store at import time; a missing/broken store must never
# crash the import chain (test_servo_watchdog.py imports it) — an empty
# registry + one log line is the fallback.
try:
    reload_from_store()
except Exception as _exc:  # noqa: BLE001
    from modules.module_messageQue import queue_message
    queue_message(f"WARNING: movement registry could not load from the "
                  f"movement store: {_exc}")