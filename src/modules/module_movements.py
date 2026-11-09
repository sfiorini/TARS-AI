"""
Module: Movements
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

The 33 movement functions that used to live here are gone: movement data
now lives in src/movements_defaults.json (generated once by
scripts/export_movement_defaults.py) and playback goes through
module_movementstore.play_steps/execute.  This module keeps only the
runtime swap flag and the ventilate positions.
"""
import time
import modules.module_servoctl as servoctl

move_legs = servoctl.move_legs
move_arm = servoctl.move_arm
disable_all_servos = servoctl.disable_all_servos
HOLD = servoctl.HOLD

_swap_directions = False

def set_swap_turn_directions(swap: bool):
    """Set whether to swap left/right turn directions"""
    global _swap_directions
    _swap_directions = swap

def get_swap_turn_directions() -> bool:
    """Get current direction swap setting"""
    return _swap_directions


# Apply the configured turn-direction swap at import time (this used to
# be done by module_servoctl's now-deleted re-export block).
try:
    from modules.module_config import load_config
    set_swap_turn_directions(load_config()["CONTROLS"]["swap_turn_directions"])
except Exception:
    pass


def ventilate_on():
    """
    Position tars for better airflow
    """
    if servoctl.acquire_moving():
        servoctl._is_ventilate_operation = True

        servoctl._notify_movement_start()
        try:
            from modules.module_cputemp import set_ventilating

            move_legs(50, 50, 50, 50, 0.8)
            move_legs(25, 25, 50, 50, 0.75)
            move_legs(25, 25, 42, 42, 0.75)
            move_legs(55, 55, 30, 30, 0.75)

            set_ventilating(True)

            time.sleep(0.1)
            disable_all_servos()
        finally:
            servoctl._is_ventilate_operation = False
            servoctl.set_moving(False)
            servoctl._notify_movement_end()


def ventilate_off():
    from modules.module_cputemp import is_ventilating, set_ventilating

    if is_ventilating():
        was_moving = servoctl.MOVING

        servoctl.set_moving(True)
        servoctl._is_ventilate_operation = True

        if not was_moving:
            servoctl._notify_movement_start()

        try:
            move_legs(55, 55, 30, 30, 0.75)
            move_legs(25, 25, 30, 30, 0.75)
            move_legs(25, 25, 50, 50, 0.75)
            move_legs(50, 50, 50, 50, 0.75)

            set_ventilating(False)

            time.sleep(0.1)
            disable_all_servos()
        finally:
            servoctl._is_ventilate_operation = False
            servoctl.set_moving(was_moving)
            if not was_moving:
                servoctl._notify_movement_end()
