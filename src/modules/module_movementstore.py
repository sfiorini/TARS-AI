"""
Module : Movement Store
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
import copy
import json
import os
import re
import shutil
import threading
import time

from modules.module_messageQue import queue_message

LEGS_ONLY = "legs_only"
HAS_ARMS = "has_arms"
_TYPES = (LEGS_ONLY, HAS_ARMS)

_ID_RE = re.compile(r"^[a-z0-9_]+$")

_STEP_KEYS = frozenset((
    "left_height", "right_height", "left_leg", "right_leg",
    "left_main", "left_forearm", "left_hand",
    "right_main", "right_forearm", "right_hand",
    "speed", "hold_time", "movement",
))
# optional step keys (audit A6): arm_speed overrides the arm call's speed
# on merged leg+arm steps; absent/None -> the step's speed
_OPTIONAL_STEP_KEYS = frozenset(("arm_speed",))
_LEG_KEYS = ("left_height", "right_height", "left_leg", "right_leg")
_ARM_KEYS = ("left_main", "left_forearm", "left_hand",
             "right_main", "right_forearm", "right_hand")

# turn_* wrappers dispatch on the runtime swap flag — execute() must
# consult the same table (module_movements.get_swap_turn_directions)
_SWAP_TURNS = {
    "turn_right": "turn_left",
    "turn_right_slow": "turn_left_slow",
    "turn_left": "turn_right",
    "turn_left_slow": "turn_right_slow",
}

# Motion D-pad directions (chatui /robot_move) → store movement ids
ROBOT_MOVE_IDS = {
    "forward": "step_forward",
    "backward": "step_backward",
    "left": "turn_left",
    "right": "turn_right",
    "forward_slow": "walk_forward",
    "backward_slow": "walk_backward",
    "left_slow": "turn_left_slow",
    "right_slow": "turn_right_slow",
}

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STORE_PATH = os.path.join(_SRC_DIR, "movements.json")
_DEFAULTS_PATH = os.path.join(_SRC_DIR, "movements_defaults.json")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class MovementStore:
    """JSON-backed movement pool (live store + tracked defaults).

    The live store (``movements.json``, gitignored) is seeded from the
    tracked defaults (``movements_defaults.json``).  A corrupt or invalid
    live file never stops the robot: the offending entry is logged and
    the defaults content is used instead.
    """

    def __init__(self, store_path, defaults_path):
        self.store_path = str(store_path)
        self.defaults_path = str(defaults_path)
        self.movements = {}
        # per-thread playback depth: a composed movement ref recurses
        # into execute() -> play_steps() while this thread already holds
        # the MOVING claim — the inner call must not re-acquire it
        self._tls = threading.local()

    # ------------------------------------------------------------------
    # loading

    def _read_json(self, path):
        with open(path, "r") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")
        return data

    def _load_defaults(self):
        data = self._read_json(self.defaults_path)
        entries = data.get("movements")
        if not isinstance(entries, dict) or not entries:
            raise ValueError(
                f"defaults file has no movements: {self.defaults_path}")
        return entries

    def load(self):
        """Load the live store; fall back to defaults on any problem.

        First start: if the live store file is absent it is seeded from
        the tracked defaults (D5).  On a parse error or an invalid entry
        the reason is logged via queue_message and the defaults content
        is loaded instead (the robot keeps moving).  Returns the loaded
        movements dict.
        """
        self._seed_from_defaults_if_absent()
        entries = None
        if os.path.exists(self.store_path):
            candidate = None
            try:
                data = self._read_json(self.store_path)
                candidate = data.get("movements")
                if not isinstance(candidate, dict) or not candidate:
                    raise ValueError("no movements object")
            except ValueError as exc:
                queue_message(
                    f"MOVEMENTS: invalid movements.json: {exc} — using defaults")
                candidate = None
            if candidate is not None:
                # first pass: structural validation of every entry
                for mid, entry in candidate.items():
                    try:
                        self.validate_entry(mid, entry)
                    except ValueError as exc:
                        queue_message(
                            f"MOVEMENTS: invalid entry '{mid}': {exc}")
                        candidate = None
                        break
            if candidate is not None:
                # second pass: movement refs resolve once all ids are
                # known, and the ref graph must be acyclic (audit
                # A2-remainder — a hand-edited cyclic movements.json
                # must fall back to defaults, not recurse at playback)
                known = set(candidate)
                for mid, entry in candidate.items():
                    try:
                        self.validate_entry(mid, entry, known_ids=known)
                        self._check_acyclic(mid, entries=candidate)
                    except ValueError as exc:
                        queue_message(
                            f"MOVEMENTS: invalid entry '{mid}': {exc}")
                        candidate = None
                        break
            entries = candidate
        if entries is None:
            entries = self._load_defaults()
        self.movements = entries
        return self.movements

    # ------------------------------------------------------------------
    # CRUD

    def get_all(self):
        """A deep copy of every entry, id -> entry."""
        return copy.deepcopy(self.movements)

    def get(self, mid):
        entry = self.movements.get(mid)
        return copy.deepcopy(entry) if entry is not None else None

    def add(self, mid, entry):
        self.validate_entry(mid, entry)
        if mid in self.movements:
            raise ValueError(f"duplicate movement id '{mid}'")
        # audit A2: refs must resolve in the POST-WRITE id set, and the
        # ref graph must stay acyclic (a self-ref/cycle recurses forever)
        post_ids = set(self.movements) | {mid}
        self.validate_entry(mid, entry, known_ids=post_ids)
        self.movements[mid] = copy.deepcopy(entry)
        self._check_acyclic(mid)
        self.save()
        self._refresh_registry()

    def update(self, mid, entry):
        self.validate_entry(mid, entry)
        if mid not in self.movements:
            raise ValueError(f"unknown movement id '{mid}'")
        self.validate_entry(mid, entry, known_ids=set(self.movements))
        self.movements[mid] = copy.deepcopy(entry)
        self._check_acyclic(mid)
        self.save()
        self._refresh_registry()

    def delete(self, mid):
        if mid not in self.movements:
            raise ValueError(f"unknown movement id '{mid}'")
        del self.movements[mid]
        self.save()
        self._refresh_registry()

    def save(self):
        """Atomic write: tmp file + os.replace."""
        payload = json.dumps({"version": 1, "movements": self.movements},
                             indent=2, sort_keys=True)
        tmp_path = self.store_path + ".tmp"
        with open(tmp_path, "w") as fh:
            fh.write(payload)
            fh.write("\n")
        os.replace(tmp_path, self.store_path)

    def reset_from_defaults(self, prune=False):
        """D5: for every id in defaults, delete the store entry and
        re-insert fresh from defaults; ids absent from defaults are
        untouched (``prune=True`` additionally deletes those)."""
        defaults = self._load_defaults()
        for mid in defaults:
            self.movements.pop(mid, None)
            self.movements[mid] = copy.deepcopy(defaults[mid])
        if prune:
            for mid in list(self.movements):
                if mid not in defaults:
                    del self.movements[mid]
        self.save()
        self._refresh_registry()

    def export_to(self, path):
        """Write the whole pool to ``path`` (atomic)."""
        payload = json.dumps({"version": 1, "movements": self.movements},
                             indent=2, sort_keys=True)
        tmp_path = str(path) + ".tmp"
        with open(tmp_path, "w") as fh:
            fh.write(payload)
            fh.write("\n")
        os.replace(tmp_path, str(path))

    def import_from(self, path):
        """Replace the pool from ``path`` — all or nothing.

        Every entry is validated first (movement refs must resolve
        within the imported set); on any invalid entry nothing is
        imported and the error list is returned.  Returns [] on
        success.
        """
        data = self._read_json(path)
        entries = data.get("movements")
        if not isinstance(entries, dict) or not entries:
            return [f"{path}: no movements object"]
        errors = []
        known = set(entries)
        for mid, entry in entries.items():
            try:
                self.validate_entry(mid, entry, known_ids=known)
            except ValueError as exc:
                errors.append(f"{mid}: {exc}")
        if errors:
            return errors
        # audit A2-remainder: reject cyclic imports wholesale — the DFS
        # runs on the CANDIDATE set before anything is committed
        for mid in entries:
            try:
                self._check_acyclic(mid, entries=entries)
            except ValueError as exc:
                return [str(exc)]
        self.movements = copy.deepcopy(entries)
        self.save()
        self._refresh_registry()
        return []

    # ------------------------------------------------------------------
    # validation

    def validate_entry(self, mid, entry, known_ids=None):
        """Raise ValueError with a reason if the entry is invalid.

        Structural checks always run; movement references are only
        checked when ``known_ids`` is provided (the second pass, after
        all entries have loaded).
        """
        if not isinstance(mid, str) or not _ID_RE.match(mid):
            raise ValueError(f"id {mid!r} must match [a-z0-9_]+")
        if not isinstance(entry, dict):
            raise ValueError("entry must be an object")
        etype = entry.get("type")
        if etype not in _TYPES:
            raise ValueError(
                f"type {etype!r} must be one of {sorted(_TYPES)}")
        for field in ("name", "description"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
        steps = entry.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps must be a non-empty list")
        steps_arms = entry.get("steps_arms")
        if steps_arms is not None and not isinstance(steps_arms, list):
            raise ValueError("steps_arms must be a list or null")
        for branch_name, branch in (("steps", steps),
                                    ("steps_arms", steps_arms)):
            if branch is None:
                continue
            for idx, step in enumerate(branch):
                self._validate_step(f"{branch_name}[{idx}]", step, known_ids)

    def _validate_step(self, where, step, known_ids):
        if not isinstance(step, dict):
            raise ValueError(f"{where} must be an object")
        missing = _STEP_KEYS - set(step)
        unknown = set(step) - _STEP_KEYS - _OPTIONAL_STEP_KEYS
        if missing or unknown:
            raise ValueError(
                f"{where} has missing keys {sorted(missing)} "
                f"and unknown keys {sorted(unknown)}")
        speed = step["speed"]
        if not _is_number(speed) or not 0 <= speed <= 1:
            raise ValueError(
                f"{where} speed {speed!r} must be a number in 0..1")
        arm_speed = step.get("arm_speed")
        if arm_speed is not None and (not _is_number(arm_speed)
                                      or not 0 <= arm_speed <= 1):
            raise ValueError(
                f"{where} arm_speed {arm_speed!r} must be null or a "
                f"number in 0..1")
        for key in _LEG_KEYS:
            value = step[key]
            if value is None:
                continue
            if not _is_number(value) or not 0 <= value <= 100:
                raise ValueError(
                    f"{where} {key} {value!r} must be null or 0..100")
        for key in _ARM_KEYS:
            value = step[key]
            if value is None or value == -1:
                continue
            if not _is_number(value) or not 0 <= value <= 100:
                raise ValueError(
                    f"{where} {key} {value!r} must be null, -1 or 0..100")
        hold = step["hold_time"]
        if not _is_number(hold) or hold < 0:
            raise ValueError(f"{where} hold_time {hold!r} must be >= 0")
        ref = step["movement"]
        if ref is None:
            return
        if not isinstance(ref, str):
            raise ValueError(f"{where} movement must be null or an id string")
        if known_ids is not None and ref not in known_ids:
            raise ValueError(
                f"{where} references unknown movement '{ref}'")

    # ------------------------------------------------------------------
    # playback

    # the legacy builder defaults (audit B4): raw client steps from
    # /play_sequence may miss keys — the old _execute_steps used .get()
    # with these defaults; a KeyError mid-playback would 500 with the
    # servos stuck at a partial pose
    _STEP_DEFAULTS = {"left_height": 50, "right_height": 50,
                      "left_leg": 50, "right_leg": 50,
                      "left_main": None, "left_forearm": None,
                      "left_hand": None, "right_main": None,
                      "right_forearm": None, "right_hand": None,
                      "speed": 0.85, "arm_speed": None,
                      "hold_time": 0.0, "movement": None}

    @classmethod
    def _step_defaults(cls, step):
        # audit B4-residual: a present-but-wrong-typed value must be
        # rejected HERE (a clean ValueError), not at move_legs as a
        # TypeError mid-playback with the servos stuck mid-pose
        for key in ("left_height", "right_height", "left_leg",
                    "right_leg", "speed", "arm_speed", "hold_time"):
            value = step.get(key)
            if value is not None and not (isinstance(value, (int, float))
                                          and not isinstance(value, bool)):
                raise ValueError(
                    f"step field '{key}' must be a number or null, "
                    f"got {value!r}")
        for key in ("left_main", "left_forearm", "left_hand",
                    "right_main", "right_forearm", "right_hand",
                    "movement"):
            value = step.get(key)
            if value is not None and not isinstance(value, (int, float,
                                                            str)):
                raise ValueError(
                    f"step field '{key}' must be a number, string or "
                    f"null, got {value!r}")
        if all(key in step for key in cls._STEP_DEFAULTS):
            return step
        merged = dict(cls._STEP_DEFAULTS)
        merged.update(step)
        return merged

    def play_steps(self, steps, tail_neutral=False):
        """Play a step list on the servos.  Returns False if the robot
        is already moving (zero servo calls in that case).

        MOVING transitions go through the locked helpers
        (acquire_moving/set_moving); a movement ref inside the list
        recurses into execute() under the same MOVING claim.
        """
        import modules.module_servoctl as servoctl
        depth = getattr(self._tls, "play_depth", 0)
        if depth == 0:
            if not servoctl.acquire_moving():
                return False
            servoctl._notify_movement_start()
        self._tls.play_depth = depth + 1
        try:
            for step in steps:
                if not isinstance(step, dict):
                    raise ValueError(
                        "each step must be an object "
                        "(use the movement builder format)")
                step = self._step_defaults(step)
                if step.get("movement") is not None:
                    if step["movement"] in self.movements:
                        self.execute(step["movement"])
                    else:
                        queue_message(
                            f"MOVEMENTS: unknown movement ref "
                            f"'{step['movement']}' - skipped")
                    continue
                legs = (step["left_height"], step["right_height"],
                        step["left_leg"], step["right_leg"])
                if any(value is not None for value in legs):
                    servoctl.move_legs(step["left_height"],
                                       step["right_height"],
                                       step["left_leg"], step["right_leg"],
                                       step["speed"])
                if servoctl.ARMS_PRESENT and any(
                        step[key] is not None for key in _ARM_KEYS):
                    # audit A6: a merged leg+arm step carries the arm's
                    # OWN speed; absent/None falls back to the step speed
                    # (pre-arm_speed movements.json files keep working)
                    arm_speed = step.get("arm_speed")
                    if arm_speed is None:
                        arm_speed = step["speed"]
                    servoctl.move_arm(step["left_main"],
                                      step["left_forearm"],
                                      step["left_hand"],
                                      step["right_main"],
                                      step["right_forearm"],
                                      step["right_hand"],
                                      arm_speed)
                if step["hold_time"] and step["hold_time"] > 0:
                    time.sleep(step["hold_time"])
            if tail_neutral:
                servoctl.move_legs(50, 50, 50, 50, 0.8)
            return True
        finally:
            self._tls.play_depth = depth
            if depth == 0:
                # audit B4-residual: disable runs on the SUCCESS path AND
                # the error path — a mid-playback failure must never
                # leave the servos energised at a partial pose
                try:
                    servoctl.disable_all_servos()
                except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                    queue_message(
                        f"MOVEMENTS: disable_all_servos after playback "
                        f"failed: {exc}")
                servoctl.set_moving(False)
                servoctl._notify_movement_end()

    def execute(self, mid):
        """Play a stored movement by id.  Returns True when the movement
        played (or was arm-gated: has_arms entry with no arms installed).
        """
        import modules.module_servoctl as servoctl
        entry = self.get(mid)
        if entry is None:
            queue_message(f"MOVEMENTS: unknown movement '{mid}'")
            return False
        if entry["type"] == HAS_ARMS and not servoctl.ARMS_PRESENT:
            # audit B5: the deleted functions still ran their LEG
            # choreography on legs-only builds (only move_arm was
            # ineffective) — play the steps' leg portion instead of
            # silently no-op-ing, and log what was skipped
            queue_message(f"MOVEMENTS: '{mid}' - arm servos not "
                          f"installed; playing the leg portion only")
            return self.play_steps(entry["steps"])
        if mid in _SWAP_TURNS:
            import modules.module_movements as movements
            if movements.get_swap_turn_directions():
                swapped = _SWAP_TURNS[mid]
                entry = self.get(swapped)
                if entry is None:
                    queue_message(
                        f"MOVEMENTS: unknown movement '{swapped}'")
                    return False
        branch = (entry["steps_arms"]
                  if servoctl.ARMS_PRESENT and entry["steps_arms"]
                  else entry["steps"])
        return self.play_steps(branch)

    def _seed_from_defaults_if_absent(self):
        """Seed the live store from the tracked defaults on first start."""
        if os.path.exists(self.store_path):
            return
        if not os.path.exists(self.defaults_path):
            return
        try:
            shutil.copyfile(self.defaults_path, self.store_path)
            queue_message("MOVEMENTS: seeded movements.json from defaults")
        except OSError as exc:
            # read-only fs: warn and continue (defaults load in-memory)
            queue_message(f"WARNING: could not seed movements.json: {exc}")

    def _check_acyclic(self, start_mid, entries=None):
        """DFS over movement refs from start_mid — a cycle would recurse
        forever at playback (audit A2). Raises ValueError naming the
        cycle.  `entries` defaults to the live store (import checks pass
        the candidate set before committing it)."""
        entries = self.movements if entries is None else entries
        visited = set()
        path = [start_mid]

        def refs_of(mid):
            entry = entries.get(mid)
            if not entry:
                return []
            out = []
            for branch in (entry.get("steps"), entry.get("steps_arms")):
                if not branch:
                    continue
                for step in branch:
                    ref = step.get("movement")
                    if ref is not None:
                        out.append(ref)
            return out

        stack = [(start_mid, iter(refs_of(start_mid)))]
        visited.add(start_mid)
        while stack:
            mid, it = stack[-1]
            advanced = False
            for ref in it:
                if ref == start_mid or (ref in visited and ref in path):
                    raise ValueError(
                        f"movement '{start_mid}' creates a reference "
                        f"cycle: {' -> '.join(path + [ref])}")
                if ref not in visited:
                    visited.add(ref)
                    path.append(ref)
                    stack.append((ref, iter(refs_of(ref))))
                    advanced = True
                    break
            if not advanced:
                stack.pop()
                if path:
                    path.pop()

    def _refresh_registry(self):
        """Keep module_movement_registry in sync after any mutation so a
        movement created/edited via UI or CLI is immediately dispatchable
        and prompt-visible without a restart."""
        try:
            from modules.module_movement_registry import reload_from_store
            reload_from_store()
        except Exception:
            pass

    def is_valid(self):
        """Validate the store file without touching it: (ok, errors).

        Collects every entry's errors instead of raising (the CLI
        ``validate`` path).
        """
        errors = []
        try:
            data = self._read_json(self.store_path)
        except (OSError, ValueError) as exc:
            return (False, [f"movements.json: {exc}"])
        entries = data.get("movements")
        if not isinstance(entries, dict):
            return (False, ["movements.json: no movements object"])
        known = set(entries)
        for mid, entry in entries.items():
            try:
                self.validate_entry(mid, entry, known_ids=known)
            except ValueError as exc:
                errors.append(f"{mid}: {exc}")
        return (not errors, errors)


_store_instance = None


def get_movement_store():
    """Module-level singleton over src/movements.json + defaults."""
    global _store_instance
    if _store_instance is None:
        _store_instance = MovementStore(_STORE_PATH, _DEFAULTS_PATH)
        _store_instance.load()
    return _store_instance
