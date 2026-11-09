#!/usr/bin/env python3
"""AST exporter: module_movements.py -> src/movements_defaults.json (S-M1-01).

Generates the tracked movement defaults file once, so it can become the
source of truth for the movement store (D5).  The existing regex parser
(_parse_movement_steps in module_chatui.py) cannot represent ARMS_PRESENT
branches, HOLD or skip semantics — this exporter walks the real AST instead:

  - for each id in module_movement_registry.MOVEMENTS, resolve the body
    function (turn_* ids map to their non-swapped *_impl functions; the
    swap logic stays runtime) and walk its try: body;
  - unroll `for _ in range(N):` by N repetitions and `for a, b, c, d in
    sequence:` loops over literal tuple lists (with slicing);
  - a move_legs(a, b, c, d, s) call opens a step; literal None or 0 leg
    values export as null (move_legs treats 0 as skip);
  - a move_arm(...) call attaches to the open step iff the step's arm
    fields are all null and no time.sleep intervened since the legs call,
    else it opens its own arm-only step (legs all null);
  - time.sleep(t) sets hold_time of the most recent step; HOLD exports
    as -1; disable_all_servos() terminates the branch;
  - `if servoctl.ARMS_PRESENT:` / `if not servoctl.ARMS_PRESENT:` blocks
    are emitted as both branches: steps = not-arms walk, steps_arms =
    arms walk.  A function with no arms branch gets steps_arms = null,
    except has_arms functions whose single body is the arms variant —
    they carry it as both steps and steps_arms.

Output: {"version": 1, "movements": {...}} with keys sorted.

Usage:
  python3 scripts/export_movement_defaults.py
"""
import ast
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
_MOVEMENTS_PY = _SRC / "modules" / "module_movements.py"
_OUT_PATH = _SRC / "movements_defaults.json"

sys.path.insert(0, str(_SRC))
from modules.module_movement_registry import MOVEMENTS, HAS_ARMS  # noqa: E402

# turn_* wrappers dispatch on the runtime swap flag — export the
# non-swapped implementation bodies (swap logic stays runtime).
_BODY_FUNC = {
    "turn_right": "_turn_right_impl",
    "turn_right_slow": "_turn_right_slow_impl",
    "turn_left": "_turn_left_impl",
    "turn_left_slow": "_turn_left_slow_impl",
}

_LEG_KEYS = ("left_height", "right_height", "left_leg", "right_leg")
_ARM_KEYS = ("left_main", "left_forearm", "left_hand",
             "right_main", "right_forearm", "right_hand")


class _Collector:
    """Accumulates steps for one branch of a movement function."""

    def __init__(self):
        self.steps = []
        self.terminated = False
        self._attachable = False  # arm may attach to the most recent step

    def _new_step(self):
        return {
            "left_height": None, "right_height": None,
            "left_leg": None, "right_leg": None,
            "left_main": None, "left_forearm": None, "left_hand": None,
            "right_main": None, "right_forearm": None, "right_hand": None,
            "speed": 0.0, "arm_speed": None, "hold_time": 0.0,
            "movement": None,
        }

    def legs(self, lh, rh, ll, rl, speed):
        step = self._new_step()
        step["left_height"] = lh
        step["right_height"] = rh
        step["left_leg"] = ll
        step["right_leg"] = rl
        step["speed"] = speed
        self.steps.append(step)
        self._attachable = True

    def arm(self, values, speed):
        last = self.steps[-1] if self.steps else None
        if (last is not None and self._attachable
                and all(last[k] is None for k in _ARM_KEYS)):
            for key, val in zip(_ARM_KEYS, values):
                last[key] = val
            # the step keeps the legs' speed for the legs call, but the
            # arm's OWN speed survives in arm_speed (audit A6: the deleted
            # functions passed each call its own speed)
            last["arm_speed"] = speed
        else:
            step = self._new_step()
            for key, val in zip(_ARM_KEYS, values):
                step[key] = val
            step["speed"] = speed
            step["arm_speed"] = speed
            self.steps.append(step)
        self._attachable = False

    def sleep(self, seconds):
        if self.steps:
            # consecutive sleeps accumulate (e.g. happy_dance's
            # time.sleep(0.1) + time.sleep(0.3) -> hold_time 0.4)
            self.steps[-1]["hold_time"] = round(
                self.steps[-1]["hold_time"] + float(seconds), 3)
        self._attachable = False

    def disable(self):
        self.terminated = True


def _is_arms_present(node):
    """True for the `servoctl.ARMS_PRESENT` attribute expression."""
    return (isinstance(node, ast.Attribute)
            and node.attr == "ARMS_PRESENT"
            and isinstance(node.value, ast.Name)
            and node.value.id == "servoctl")


def _eval_arg(node, env):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "HOLD":
            return -1
        if node.id in env:
            return env[node.id]
        raise ValueError(f"unbound name {node.id!r} in movement call")
    raise ValueError(f"unsupported argument node: {ast.dump(node)}")


def _leg_value(val):
    """move_legs treats None and 0 as skip — both export as JSON null."""
    return None if val is None or val == 0 else val


def _arm_value(val):
    """move_arm treats None and 0 as skip (null); HOLD exports as -1."""
    if val is None or val == 0:
        return None
    return val


def _walk(stmts, collector, env, branch):
    """Walk a statement list, feeding the collector.

    branch: None (single-body function), "arms" or "noarms" (function with
    ARMS_PRESENT branches — the non-matching branch's statements are
    skipped, everything else applies to both).
    """
    for stmt in stmts:
        if collector.terminated:
            return
        if isinstance(stmt, ast.Assign):
            _walk_assign(stmt, env)
        elif isinstance(stmt, ast.For):
            _walk_for(stmt, collector, env, branch)
        elif isinstance(stmt, ast.If):
            _walk_if(stmt, collector, env, branch)
        elif isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant):
                continue  # docstring / stray literal
            if isinstance(stmt.value, ast.Call):
                _walk_call(stmt.value, collector, env)
            else:
                raise ValueError("unsupported expression statement")
        elif isinstance(stmt, ast.Pass):
            continue
        else:
            raise ValueError(f"unsupported statement: {type(stmt).__name__}")


def _walk_assign(stmt, env):
    if not (len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
        raise ValueError("unsupported assignment target")
    name = stmt.targets[0].id
    value = stmt.value
    if isinstance(value, ast.List):
        tuples = []
        for element in value.elts:
            if not isinstance(element, ast.Tuple):
                raise ValueError(f"unsupported list element for {name!r}")
            tuples.append(tuple(_eval_arg(elt, env) for elt in element.elts))
        env[name] = tuples
    else:
        env[name] = _eval_arg(value, env)


def _walk_for(stmt, collector, env, branch):
    it = stmt.iter
    if (isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
            and it.func.id == "range" and len(it.args) == 1
            and isinstance(it.args[0], ast.Constant)
            and isinstance(it.args[0].value, int)):
        for _ in range(it.args[0].value):
            _walk(stmt.body, collector, env, branch)
            if collector.terminated:
                return
        return
    # for a, b, c, d in sequence[:N]:  — iterate a literal tuple list
    seq = None
    if isinstance(it, ast.Name):
        seq = env.get(it.id)
    elif (isinstance(it, ast.Subscript) and isinstance(it.value, ast.Name)
            and it.value.id in env):
        seq = env[it.value.id]
        if isinstance(it.slice, ast.Slice):
            seq = seq[it.slice.lower.value if it.slice.lower else None:
                      it.slice.upper.value if it.slice.upper else None]
        else:
            raise ValueError("unsupported loop subscript")
    if seq is None:
        raise ValueError("unsupported for-loop iterable")
    if not (isinstance(stmt.target, ast.Tuple)
            and all(isinstance(t, ast.Name) for t in stmt.target.elts)):
        raise ValueError("unsupported for-loop target")
    names = [t.id for t in stmt.target.elts]
    for values in seq:
        if len(values) != len(names):
            raise ValueError("tuple arity mismatch in for-loop")
        for name, val in zip(names, values):
            env[name] = val
        _walk(stmt.body, collector, env, branch)
        if collector.terminated:
            return
        for name in names:
            env.pop(name, None)


def _walk_if(stmt, collector, env, branch):
    test = stmt.test
    positive = _is_arms_present(test)
    negative = (isinstance(test, ast.UnaryOp)
                and isinstance(test.op, ast.Not)
                and _is_arms_present(test.operand))
    if not (positive or negative):
        raise ValueError("unsupported if-statement in movement body")
    matches = (branch is None
               or (positive and branch == "arms")
               or (negative and branch == "noarms"))
    if matches:
        _walk(stmt.body, collector, env, branch)
    elif stmt.orelse:
        _walk(stmt.orelse, collector, env, branch)


def _walk_call(call, collector, env):
    func = call.func
    if isinstance(func, ast.Name):
        if func.id == "move_legs":
            if len(call.args) != 5:
                raise ValueError("move_legs call must have 5 arguments")
            lh, rh, ll, rl, speed = (_eval_arg(a, env) for a in call.args)
            collector.legs(_leg_value(lh), _leg_value(rh),
                           _leg_value(ll), _leg_value(rl), speed)
            return
        if func.id == "move_arm":
            if len(call.args) != 7:
                raise ValueError("move_arm call must have 7 arguments")
            values = [_arm_value(_eval_arg(a, env)) for a in call.args[:6]]
            speed = _eval_arg(call.args[6], env)
            collector.arm(values, speed)
            return
        if func.id == "disable_all_servos":
            collector.disable()
            return
        raise ValueError(f"unsupported call: {func.id}()")
    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "time" and func.attr == "sleep"):
        if len(call.args) != 1:
            raise ValueError("time.sleep call must have 1 argument")
        collector.sleep(_eval_arg(call.args[0], env))
        return
    raise ValueError("unsupported call in movement body")


def _find_try(fn_def):
    """Locate the try: block inside the acquire_moving() guard."""
    for stmt in fn_def.body:
        if isinstance(stmt, ast.Try):
            return stmt
        if isinstance(stmt, ast.If):
            for inner in stmt.body:
                if isinstance(inner, ast.Try):
                    return inner
    raise ValueError(f"no try block found in {fn_def.name}()")


def _walk_branch(try_node, branch):
    collector = _Collector()
    _walk(try_node.body, collector, {}, branch)
    if not collector.steps:
        raise ValueError(f"no steps collected for branch {branch!r}")
    return collector.steps


def export_movement(tree):
    functions = {node.name: node for node in tree.body
                 if isinstance(node, ast.FunctionDef)}
    movements = {}
    for mid, meta in MOVEMENTS.items():
        fn_name = _BODY_FUNC.get(mid, mid)
        fn_def = functions.get(fn_name)
        if fn_def is None:
            raise ValueError(f"movement function {fn_name}() not found")
        try_node = _find_try(fn_def)
        has_arms_branch = any(
            (isinstance(stmt, ast.If)
             and (_is_arms_present(stmt.test)
                  or (isinstance(stmt.test, ast.UnaryOp)
                      and isinstance(stmt.test.op, ast.Not)
                      and _is_arms_present(stmt.test.operand))))
            for stmt in try_node.body)
        if has_arms_branch:
            steps = _walk_branch(try_node, "noarms")
            steps_arms = _walk_branch(try_node, "arms")
        else:
            steps = _walk_branch(try_node, None)
            # a has_arms function's single body is the arms variant — carry
            # it as both branches (execute() arm-gates has_arms entries)
            steps_arms = _walk_branch(try_node, None) \
                if meta["type"] == HAS_ARMS else None
        movements[mid] = {
            "name": meta["name"],
            "type": meta["type"],
            "description": meta["description"],
            "steps": steps,
            "steps_arms": steps_arms,
        }
    return {"version": 1, "movements": movements}


def main():
    try:
        tree = ast.parse(_MOVEMENTS_PY.read_text())
        payload = export_movement(tree)
        with _OUT_PATH.open("w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"export_movement_defaults: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {len(payload['movements'])} movements to "
          f"{_OUT_PATH.relative_to(_REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
