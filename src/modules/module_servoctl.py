"""
Module: Servo Controller V3.1
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

from __future__ import division
import time
import os
import threading
import board
import busio
from adafruit_pca9685 import PCA9685

from modules.module_messageQue import queue_message
from modules.module_config import load_config

config = load_config()

NEUTRAL_LEFT_HEIGHT = 350
NEUTRAL_RIGHT_HEIGHT = 350
NEUTRAL_LEFT_LEG = 300
NEUTRAL_RIGHT_LEG = 300

global_arm_speed = 0.5
global_easing_strength = 0.6

SERVO_POSITIONS_FILE = os.path.expanduser("~/.servo_positions.json")

def _load_servo_positions():
    
    import json
    try:
        with open(SERVO_POSITIONS_FILE, 'r') as f:
            positions = json.load(f)
            return {int(k): v for k, v in positions.items()}
    except Exception:
        print(f"[SERVO] No saved positions found, starting fresh")
        return {}

def _save_servo_positions():
    
    import json
    try:
        with open(SERVO_POSITIONS_FILE, 'w') as f:
            json.dump(servo_positions, f)
    except Exception as e:
        print(f"[SERVO] Warning: Could not save positions: {e}")

servo_positions = _load_servo_positions()

_channels_initialized = set(servo_positions.keys())

pca = None
MAX_RETRIES = 3

# Self-healing state: lazy PCA re-init (rate-limited), I2C error accounting,
# last PWM activity timestamp (stuck-MOVING detection) and the watchdog.
_last_init_attempt = 0.0
_i2c_error_count = 0
_last_pwm_time = 0.0
_last_pca_none_log = 0.0
_last_reg_read_log = 0.0
_watchdog_running = False
_i2c = None  # raw I2C handle kept for register reads (fallback if driver attrs missing)
_watchdog_thread = None
_watchdog_interval = 2
# Channels holding a live PWM command (set on successful writes, cleared by
# disable_all_servos). The repair path re-applies ONLY these — idle
# (deliberately de-energized) channels must stay de-energized.
_engaged_channels = set()
_unhealthy_chip_reads = 0  # consecutive unhealthy MODE1/MODE2 reads (repair needs 2)

# One lock around PCA handle swaps and MOVING transitions so the watchdog,
# movement threads and servo_status never race on shared state. RLock (not
# Lock): the watchdog holds it across a whole tick and calls helpers
# (initialize_pca9685, servo reads) that acquire it again on the same thread.
# Movement threads reach this lock through the set_moving()/acquire_moving()
# helpers — never with bare MOVING assignments (F1).
_state_lock = threading.RLock()

battery_module = None

def set_battery_module(battery_mod):
    global battery_module
    battery_module = battery_mod

def signal_servo_activity():
    if battery_module is not None:
        battery_module.signal_servo_activity()

def _write_register(reg, value):
    """Write a raw PCA9685 register; uses the driver struct attribute when
    present, else raw writeto on the stored I2C handle.
    NOTE: must be defined BEFORE initialize_pca9685() — that function is
    called at module import time and references this helper."""
    if pca is None:
        return
    attr = {0x00: "mode1_reg", 0x01: "mode2_reg"}.get(reg)
    try:
        if attr is not None and hasattr(pca, attr):
            setattr(pca, attr, value)
        elif _i2c is not None:
            _i2c.writeto(0x40, bytes([reg, value]))
    except Exception as e:
        queue_message(f"WARNING: PCA9685 register 0x{reg:02X} write failed: {e}")

def initialize_pca9685():
    
    global pca, _last_init_attempt, _i2c
    with _state_lock:
        _last_init_attempt = time.time()
        pca = None  # clear any stale handle first: a failed re-init must be honestly reported
        
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            _i2c = i2c
            pca = PCA9685(i2c, address=0x40)
            pca.frequency = 50
            _write_register(0x01, 0x04)  # MODE2: output-drive state verified working on this board (0x01 = silent, no motor drive)
            queue_message("LOAD: PCA9685 initialized successfully")
            return True
            
        except OSError as e:
            if e.errno == 121:
                queue_message(f"ERROR: I2C Remote I/O error - Check connections and power!")
            else:
                queue_message(f"ERROR: I2C error {e.errno}: {e}")
            return False
            
        except Exception as e:
            queue_message(f"ERROR: Failed to initialize PCA9685: {e}")
            return False

def _try_lazy_pca_init():
    """Attempt to re-create the PCA9685 device object, at most once per 5s.
    Covers the boot-failure case: a dead `pca` no longer bricks all movement
    until a reboot — the next servo command retries the I2C bus."""
    global _last_init_attempt
    
    if time.time() - _last_init_attempt < 5.0:
        return
    
    if initialize_pca9685():
        queue_message("SERVO: PCA9685 re-initialized on demand")

def _log_pca_unavailable():
    """Rate-limited warning while servo commands are being dropped."""
    global _last_pca_none_log
    
    now = time.time()
    if now - _last_pca_none_log >= 30.0:
        _last_pca_none_log = now
        queue_message("WARNING: PCA9685 unavailable - servo commands ignored until I2C bus recovers")

def _invalidate_pca():
    """Drop a dead PCA9685 handle so the next command re-creates the I2C
    bus (bus recovery without a reboot)."""
    global pca, _i2c_error_count
    
    with _state_lock:
        _i2c_error_count += 1
        pca = None

controls = config.get('CONTROLS', {})
_movement_enabled = controls.get('enabled', False) or controls.get('voicemovement', False)

if _movement_enabled:
    if not initialize_pca9685():
        queue_message("WARNING: PCA9685 initialization failed - check hardware")
else:
    pca = None

leftMainMin = int(config["SERVO"]["leftMainMin"])
leftMainMax = int(config["SERVO"]["leftMainMax"])
leftForarmMin = int(config["SERVO"]["leftForarmMin"])
leftForarmMax = int(config["SERVO"]["leftForarmMax"])
leftHandMin = int(config["SERVO"]["leftHandMin"])
leftHandMax = int(config["SERVO"]["leftHandMax"])

rightMainMin = int(config["SERVO"]["rightMainMin"])
rightMainMax = int(config["SERVO"]["rightMainMax"])
rightForarmMin = int(config["SERVO"]["rightForarmMin"])
rightForarmMax = int(config["SERVO"]["rightForarmMax"])
rightHandMin = int(config["SERVO"]["rightHandMin"])
rightHandMax = int(config["SERVO"]["rightHandMax"])

leftMainOffset = int(config["SERVO"]["leftMainOffset"])
leftMainMin = leftMainMin + leftMainOffset
leftMainMax = leftMainMax + leftMainOffset

leftForearmOffset = int(config["SERVO"]["leftForearmOffset"])
leftForarmMin = leftForarmMin + leftForearmOffset
leftForarmMax = leftForarmMax + leftForearmOffset

leftHandOffset = int(config["SERVO"]["leftHandOffset"])
leftHandMin = leftHandMin + leftHandOffset
leftHandMax = leftHandMax + leftHandOffset

rightMainOffset = int(config["SERVO"]["rightMainOffset"])
rightMainMin = rightMainMin + rightMainOffset
rightMainMax = rightMainMax + rightMainOffset

rightForearmOffset = int(config["SERVO"]["rightForearmOffset"])
rightForarmMin = rightForarmMin + rightForearmOffset
rightForarmMax = rightForarmMax + rightForearmOffset

rightHandOffset = int(config["SERVO"]["rightHandOffset"])
rightHandMin = rightHandMin + rightHandOffset
rightHandMax = rightHandMax + rightHandOffset

perfectLeftHeightOffset = int(config["SERVO"]["perfectLeftHeightOffset"])
leftUpHeight = int(config["SERVO"]["leftUpHeight"]) + perfectLeftHeightOffset
leftNeutralHeight = NEUTRAL_LEFT_HEIGHT + perfectLeftHeightOffset
leftDownHeight = int(config["SERVO"]["leftDownHeight"]) + perfectLeftHeightOffset

perfectRightHeightOffset = int(config["SERVO"]["perfectRightHeightOffset"])
rightUpHeight = int(config["SERVO"]["rightUpHeight"]) - perfectRightHeightOffset
rightNeutralHeight = NEUTRAL_RIGHT_HEIGHT - perfectRightHeightOffset
rightDownHeight = int(config["SERVO"]["rightDownHeight"]) - perfectRightHeightOffset

perfectLeftLegOffset = int(config["SERVO"]["perfectLeftLegOffset"])
forwardLeftLeg = int(config["SERVO"]["forwardLeftLeg"]) + perfectLeftLegOffset
neutralLeftLeg = NEUTRAL_LEFT_LEG + perfectLeftLegOffset
backLeftLeg = int(config["SERVO"]["backLeftLeg"]) + perfectLeftLegOffset

perfectRightLegOffset = int(config["SERVO"]["perfectRightLegOffset"])
forwardRightLeg = int(config["SERVO"]["forwardRightLeg"]) + perfectRightLegOffset
neutralRightLeg = NEUTRAL_RIGHT_LEG + perfectRightLegOffset
backRightLeg = int(config["SERVO"]["backRightLeg"]) + perfectRightLegOffset

MOVING = False

def set_moving(value):
    """F1: locked MOVING write for code outside this module (movement
    threads). The watchdog reads MOVING / _last_pwm_time under _state_lock;
    a bare `servoctl.MOVING = ...` races those checks — e.g. the watchdog
    could see a movement as idle in the window before its first MOVING=True
    write and re-init the chip mid-walk. Every MOVING transition in
    module_movements.py goes through this helper (or acquire_moving)."""
    global MOVING
    with _state_lock:
        MOVING = value

def acquire_moving():
    """F1: atomic test-and-set of MOVING under _state_lock. Replaces the
    bare `if not servoctl.MOVING: servoctl.MOVING = True` pattern, whose
    check-then-set window let two movements start concurrently (or the
    watchdog see a just-starting movement as idle). Returns False — and
    leaves the flag untouched — when a movement is already in progress."""
    global MOVING
    with _state_lock:
        if MOVING:
            return False
        MOVING = True
        return True

HOLD = -1
ARMS_PRESENT = config["SERVO"]["arms_present"]

if not servo_positions:
    print("[SERVO] No saved positions - initializing to neutral estimates")
    servo_positions[0] = leftNeutralHeight
    servo_positions[1] = rightNeutralHeight
    servo_positions[2] = neutralLeftLeg
    servo_positions[3] = neutralRightLeg
    servo_positions[4] = leftMainMin
    servo_positions[5] = leftForarmMin
    servo_positions[6] = leftHandMin
    servo_positions[7] = rightMainMin
    servo_positions[8] = rightForarmMin
    servo_positions[9] = rightHandMin
else:
    print("[SERVO] Loaded saved positions", flush=True)

_on_movement_start = None
_on_movement_end = None
_is_ventilate_operation = False


def refresh_config():
    """Re-read config and update all offset-dependent servo variables without reinitializing PCA."""
    global leftMainMin, leftMainMax, leftForarmMin, leftForarmMax, leftHandMin, leftHandMax
    global rightMainMin, rightMainMax, rightForarmMin, rightForarmMax, rightHandMin, rightHandMax
    global leftMainOffset, leftForearmOffset, leftHandOffset
    global rightMainOffset, rightForearmOffset, rightHandOffset
    global perfectLeftHeightOffset, leftUpHeight, leftNeutralHeight, leftDownHeight
    global perfectRightHeightOffset, rightUpHeight, rightNeutralHeight, rightDownHeight
    global perfectLeftLegOffset, forwardLeftLeg, neutralLeftLeg, backLeftLeg
    global perfectRightLegOffset, forwardRightLeg, neutralRightLeg, backRightLeg
    global ARMS_PRESENT

    cfg = load_config()
    servo = cfg["SERVO"]

    # Arm base values
    leftMainMin = int(servo["leftMainMin"])
    leftMainMax = int(servo["leftMainMax"])
    leftForarmMin = int(servo["leftForarmMin"])
    leftForarmMax = int(servo["leftForarmMax"])
    leftHandMin = int(servo["leftHandMin"])
    leftHandMax = int(servo["leftHandMax"])
    rightMainMin = int(servo["rightMainMin"])
    rightMainMax = int(servo["rightMainMax"])
    rightForarmMin = int(servo["rightForarmMin"])
    rightForarmMax = int(servo["rightForarmMax"])
    rightHandMin = int(servo["rightHandMin"])
    rightHandMax = int(servo["rightHandMax"])

    # Arm offsets
    leftMainOffset = int(servo["leftMainOffset"])
    leftMainMin += leftMainOffset
    leftMainMax += leftMainOffset
    leftForearmOffset = int(servo["leftForearmOffset"])
    leftForarmMin += leftForearmOffset
    leftForarmMax += leftForearmOffset
    leftHandOffset = int(servo["leftHandOffset"])
    leftHandMin += leftHandOffset
    leftHandMax += leftHandOffset
    rightMainOffset = int(servo["rightMainOffset"])
    rightMainMin += rightMainOffset
    rightMainMax += rightMainOffset
    rightForearmOffset = int(servo["rightForearmOffset"])
    rightForarmMin += rightForearmOffset
    rightForarmMax += rightForearmOffset
    rightHandOffset = int(servo["rightHandOffset"])
    rightHandMin += rightHandOffset
    rightHandMax += rightHandOffset

    # Leg offsets
    perfectLeftHeightOffset = int(servo["perfectLeftHeightOffset"])
    leftUpHeight = int(servo["leftUpHeight"]) + perfectLeftHeightOffset
    leftNeutralHeight = NEUTRAL_LEFT_HEIGHT + perfectLeftHeightOffset
    leftDownHeight = int(servo["leftDownHeight"]) + perfectLeftHeightOffset

    perfectRightHeightOffset = int(servo["perfectRightHeightOffset"])
    rightUpHeight = int(servo["rightUpHeight"]) - perfectRightHeightOffset
    rightNeutralHeight = NEUTRAL_RIGHT_HEIGHT - perfectRightHeightOffset
    rightDownHeight = int(servo["rightDownHeight"]) - perfectRightHeightOffset

    perfectLeftLegOffset = int(servo["perfectLeftLegOffset"])
    forwardLeftLeg = int(servo["forwardLeftLeg"]) + perfectLeftLegOffset
    neutralLeftLeg = NEUTRAL_LEFT_LEG + perfectLeftLegOffset
    backLeftLeg = int(servo["backLeftLeg"]) + perfectLeftLegOffset

    perfectRightLegOffset = int(servo["perfectRightLegOffset"])
    forwardRightLeg = int(servo["forwardRightLeg"]) + perfectRightLegOffset
    neutralRightLeg = NEUTRAL_RIGHT_LEG + perfectRightLegOffset
    backRightLeg = int(servo["backRightLeg"]) + perfectRightLegOffset

    ARMS_PRESENT = servo["arms_present"]


def set_movement_callbacks(on_start=None, on_end=None):

    global _on_movement_start, _on_movement_end
    _on_movement_start = on_start
    _on_movement_end = on_end

def _notify_movement_start():
    global _is_ventilate_operation

    # Bulletproof: this is called OUTSIDE the try block of every movement
    # function — if it raised, MOVING would stay True forever and every later
    # movement would silently no-op. Never let an exception escape.
    try:
        signal_servo_activity()

        if not _is_ventilate_operation:
            try:
                from modules.module_cputemp import is_ventilating
                if is_ventilating():
                    from modules.module_movements import ventilate_off
                    ventilate_off()
            except Exception as e:
                pass

        if _on_movement_start:
            try:
                _on_movement_start()
            except Exception as e:
                queue_message(f"ERROR: Failed to pause UI/STT: {e}")
    except Exception as e:
        queue_message(f"ERROR: movement-start notification failed: {e}")

def _notify_movement_end():

    # Bulletproof: same reasoning as _notify_movement_start.
    try:
        signal_servo_activity()

        if _on_movement_end:
            try:
                _on_movement_end()
            except Exception as e:
                queue_message(f"ERROR: Failed to resume UI/STT: {e}")
    except Exception as e:
        queue_message(f"ERROR: movement-end notification failed: {e}")

def pulse_to_duty_cycle(pulse_value):
    
    MAX_PULSE = 600
    pulse_us = 500 + (pulse_value / MAX_PULSE) * 2000
    duty_cycle = int((pulse_us / 20000.0) * 65535)
    return duty_cycle

def set_servo_pwm(channel, pwm_value):
    global _last_pwm_time

    if pca is None:
        _try_lazy_pca_init()
        if pca is None:
            _log_pca_unavailable()
            return False
    
    duty_cycle = pulse_to_duty_cycle(pwm_value)

    # Snapshot the handle: the watchdog may swap the global mid-write; writing
    # to the old handle is harmless, a None-dereference crash is not.
    pca_handle = pca

    for attempt in range(MAX_RETRIES):
        try:
            pca_handle.channels[channel].duty_cycle = duty_cycle
            with _state_lock:
                _last_pwm_time = time.time()
                _engaged_channels.add(channel)
            return True
            
        except OSError as e:
            if e.errno == 121:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(0.05)
                    continue
            queue_message(f"I2C error on channel {channel} after {attempt + 1} attempts: {e} - invalidating PCA9685 for bus recovery")
            _invalidate_pca()
            return False
            
        except Exception as e:
            queue_message(f"Error setting PWM on channel {channel}: {e} - invalidating PCA9685 for bus recovery")
            _invalidate_pca()
            return False
    
    return False

def _is_moving_stuck(moving, last_pwm_time, now, timeout=30):
    """Pure helper: MOVING with no PWM activity for more than `timeout`
    seconds is stuck (an exception left the flag set)."""
    if not moving:
        return False
    if not last_pwm_time:
        return True
    return (now - last_pwm_time) > timeout

def _mode1_healthy(mode1):
    """Pure helper: MODE1 bit 0x20 must be set. If the chip's logic resets,
    MODE1 falls back to 0x01 while the device still ACKs on I2C — multi-byte
    PWM writes then land on wrong registers and all servos freeze until the
    driver state is restored (no reboot needed, just re-init)."""
    if mode1 is None:
        return False
    return (mode1 & 0x20) != 0

def _read_register(reg):
    """Read a raw PCA9685 register (0x00=MODE1, 0x01=MODE2); None if unreadable.
    Uses the driver's struct attribute when present (adafruit-circuitpython-pca9685
    3.4.22 verified on tars), else falls back to a raw writeto/readfrom on the
    stored I2C handle — never depends on a single library internals."""
    if pca is None:
        return None
    attr = {0x00: "mode1_reg", 0x01: "mode2_reg"}.get(reg)
    try:
        if attr is not None and hasattr(pca, attr):
            return int(getattr(pca, attr))
        if _i2c is not None:
            return _i2c.writeto_then_readfrom(0x40, bytes([reg]), 1)[0]
    except Exception as e:
        global _last_reg_read_log
        now = time.time()
        if now - _last_reg_read_log >= 30.0:
            _last_reg_read_log = now
            queue_message(f"WARNING: PCA9685 register 0x{reg:02X} read failed: {e}")
    return None

def _read_mode1():
    """Read the PCA9685 MODE1 register; None if unreadable."""
    return _read_register(0x00)

def _read_mode2():
    """Read the PCA9685 MODE2 register; None if unreadable."""
    return _read_register(0x01)

def _mode2_healthy(mode2):
    """Pure helper: MODE2 must be EXACTLY 0x04 — the only empirically working
    state on this board. Verified empirically on the robot: MODE2=0x04 drives
    the servos (physical movement confirmed); 0x01 leaves them silent (no
    movement, no motor noise) while the chip still ACKs on I2C; 0x0B and 0x1E
    (seen after chip-logic glitches) put ALL PWM output pins in
    high-impedance. The old masked check (mode2 & 0x0C) == 0x04 classified
    hi-Z glitch modes 0x06/0x07/0x16/0x17 (and silent 0x05) as healthy, so
    they were never repaired."""
    if mode2 is None:
        return False
    return mode2 == 0x04

def _chip_state_healthy(mode1, mode2):
    return _mode1_healthy(mode1) and _mode2_healthy(mode2)

def _repair_chip_state_if_needed():
    """If MODE1/MODE2 lost their driver-set bits (chip-logic reset), re-run
    driver init to restore them. Repairs the 'thinks it moves but doesn't'
    state without a reboot.
    A repair requires TWO CONSECUTIVE unhealthy reads: a single transient
    I2C NACK must never abort a movement with a mid-walk re-init (which
    clears the PWM output registers). A healthy read resets the streak."""
    global _unhealthy_chip_reads
    
    mode1 = _read_mode1()
    mode2 = _read_mode2()
    if _chip_state_healthy(mode1, mode2):
        _unhealthy_chip_reads = 0
        return
    _unhealthy_chip_reads += 1
    if _unhealthy_chip_reads < 2:
        queue_message(f"SERVO: PCA9685 state unhealthy (MODE1={mode1}, MODE2={mode2}) - waiting for a second consecutive unhealthy read before repair")
        return
    _unhealthy_chip_reads = 0
    queue_message(f"SERVO: PCA9685 state unhealthy (MODE1={mode1}, MODE2={mode2}) - re-initializing")
    if initialize_pca9685():
        m1, m2 = _read_mode1(), _read_mode2()
        if _chip_state_healthy(m1, m2):
            queue_message("SERVO: PCA9685 state repair complete")
            # re-init cleared the PWM output registers — restore the commanded
            # pose of ENGAGED channels only; deliberately de-energized channels
            # (idle after a movement's disable_all_servos) stay untouched.
            _reapply_engaged_positions()
        else:
            queue_message(f"WARNING: servo watchdog - MODE1/MODE2 still unhealthy after re-init (MODE1={m1}, MODE2={m2})")
    else:
        queue_message("WARNING: servo watchdog - chip state repair re-init failed")

def _watchdog_tick():
    """One watchdog pass: retry a dead PCA9685 (rate-limited), repair an
    unhealthy MODE1/MODE2 (only when idle, after two consecutive unhealthy
    reads), reset a stuck MOVING flag (unconditionally)."""
    global MOVING
    
    with _state_lock:
        now = time.time()
        
        if pca is None:
            # Reuse the 5s rate-limited lazy init — never hammer the I2C bus
            # every tick when the board is absent.
            _try_lazy_pca_init()
            if pca is None:
                _log_pca_unavailable()  # 30s-throttled warning
        else:
            # Never touch the chip mid-movement: a re-init clears the PWM
            # output registers and can abort a walk. Only health-check when
            # idle (not MOVING, no PWM activity in the last 2s).
            pwm_recent = _last_pwm_time and (now - _last_pwm_time) < 2.0
            if not MOVING and not pwm_recent:
                _repair_chip_state_if_needed()
        
        # Unconditional: a stuck MOVING flag (an exception left it set)
        # blocks ALL future movement until reboot — always check it.
        if _is_moving_stuck(MOVING, _last_pwm_time, now):
            queue_message("ERROR: servo watchdog - MOVING stuck for more than 30s, resetting movement state")
            MOVING = False

def _watchdog_loop():
    while _watchdog_running:
        time.sleep(_watchdog_interval)
        try:
            _watchdog_tick()
        except Exception as e:
            queue_message(f"ERROR: servo watchdog tick failed: {e}")

def start_servo_watchdog(interval=2):
    """Start the servo self-healing watchdog (daemon thread)."""
    global _watchdog_running, _watchdog_thread, _watchdog_interval
    
    if _watchdog_running:
        return
    
    _watchdog_interval = interval
    _watchdog_running = True
    _watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True, name="servo-watchdog")
    _watchdog_thread.start()
    queue_message(f"SERVO: watchdog started ({interval}s interval)")

def servo_status():
    """Live servo controller health snapshot (consistent under _state_lock)."""
    with _state_lock:
        now = time.time()
        return {
            "pca_alive": pca is not None,
            "moving": MOVING,
            "last_pwm_age_s": round(now - _last_pwm_time, 1) if _last_pwm_time else None,
            "i2c_error_count": _i2c_error_count,
            "watchdog_running": _watchdog_running,
            "mode1": _read_mode1(),
            "mode2": _read_mode2(),
        }

def force_servo_recovery():
    """No-reboot recovery: re-create the I2C/PCA9685 device, then gently
    re-apply the saved positions of ENGAGED channels only (no neutral jerk).
    REJECTED while a movement is in progress (the /servo_reinit route maps
    this to HTTP 409): a manual re-init mid-walk aborts the walk, so defer
    instead — a stuck MOVING flag is the watchdog's job (30 s timeout)."""
    with _state_lock:
        if MOVING:
            status = servo_status()
            status["rejected"] = "moving"
            return status  # do NOT clear MOVING — state is left untouched
        
        if initialize_pca9685():
            mode1, mode2 = _read_mode1(), _read_mode2()
            if not _chip_state_healthy(mode1, mode2):
                queue_message(f"WARNING: servo recovery - MODE1/MODE2 still unhealthy after re-init (MODE1={mode1}, MODE2={mode2})")
            # Restore the commanded pose of engaged channels only (legs first,
            # 20 ms apart); idle channels stay de-energized — no neutral jerk.
            _reapply_engaged_positions()
    
    return servo_status()

def initialize_servos():
    if pca is None:
        controls = config.get('CONTROLS', {})
        if controls.get('enabled', False) or controls.get('voicemovement', False):
            queue_message("WARNING: Cannot initialize servos - PCA9685 not available")
        return

    try:
        for channel in range(16):
            pca.channels[channel].duty_cycle = 0
    except Exception as e:
        queue_message(f"Error initializing servos: {e}")

    time.sleep(0.1)

    # Gently re-engage servos at their saved positions before moving to
    # neutral (0.1 s per channel — soft-start, one channel at a time)
    if servo_positions:
        for channel, value in servo_positions.items():
            set_servo_pwm(int(channel), value)
            time.sleep(0.1)
        time.sleep(0.3)

    reset_positions(soft=True)
    print("All servos initialized")

def disable_all_servos():
    if pca is None:
        return
    
    try:
        for channel in range(16):
            pca.channels[channel].duty_cycle = 0
    except Exception as e:
        queue_message(f"Error disabling servos: {e}")

    with _state_lock:
        _engaged_channels.clear()

    time.sleep(0.05)

def _reapply_engaged_positions():
    """Re-apply ONLY engaged channels from servo_positions, legs (0-3) first,
    0.02 s inter-write sleeps. An empty engaged set writes ZERO PWM registers.

    Rationale: engaged-only re-apply restores the commanded pose of
    genuinely-engaged channels (hold, ventilate); idle-after-disable — the
    common path, since the movement-store and playback paths run
    disable_all_servos() after every movement (move_legs/move_arm themselves
    do NOT disable) — means deliberately de-energized servos, and idle repair
    writing zero PWM registers is exactly why the idle-repair jump disappears.
    No ramp through neutral: there is no position feedback and servo_positions
    IS the commanded pose."""
    with _state_lock:
        engaged = sorted(_engaged_channels)
    legs = [c for c in engaged if c <= 3]
    others = [c for c in engaged if c > 3]
    for channel in legs + others:
        value = servo_positions.get(channel)
        if value is not None:
            set_servo_pwm(int(channel), value)
            time.sleep(0.02)

def _start_boot_current_sampler():
    """Informational INA260 logging during the boot movement (S-M2-06):
    a daemon thread samples the battery module's current every 0.5 s until
    the returned stop-Event is set. INA260 is NOT a health signal — idle
    servos are deliberately de-energized and draw ~0 either way — so this
    gates nothing; a read failure is swallowed and never breaks the boot
    path. Returns None when no battery module is present (no thread)."""
    if battery_module is None:
        return None
    done = threading.Event()

    def _sample():
        while not done.wait(0.5):
            try:
                current = battery_module.current
                queue_message(f"INFO: boot current={current:.2f}A")
            except Exception:
                pass  # logging only — never gate the boot path on INA260

    thread = threading.Thread(target=_sample, daemon=True,
                              name="ina260-boot-sampler")
    thread.start()
    return done


def reset_positions(soft=False):
    """Move all servos to neutral. soft=True (the boot path) energizes one
    servo group at a time — leg pairs, then LEFT arm only, then RIGHT arm
    only, 0.3 s settles between phases — halving peak simultaneous inrush
    vs the single 6-servo move_arm. The hard path (default) is unchanged
    for every non-boot caller."""
    global servo_positions

    stop_sampler = _start_boot_current_sampler()
    try:
        move_legs(20, 20, None, None, 0.6)

        # Use saved positions as starting points (don't overwrite them)
        # This ensures smooth movement from last known position to neutral
        neutral_defaults = {
            0: leftNeutralHeight,
            1: rightNeutralHeight,
            2: neutralLeftLeg,
            3: neutralRightLeg,
            4: leftMainMin,
            5: leftForarmMin,
            6: leftHandMin,
            7: rightMainMin,
            8: rightForarmMin,
            9: rightHandMin,
        }
        for ch, val in neutral_defaults.items():
            if ch not in servo_positions:
                servo_positions[ch] = val

        disable_all_servos()

        if soft:
            # Boot soft-start: one servo group at a time, 0.3 s settles
            move_legs(30, 30, 50, 50, 0.5)
            time.sleep(0.3)
            move_legs(50, 50, 50, 50, 0.5)
            time.sleep(0.3)
            move_arm(left_main=1, left_forearm=1, left_hand=1,
                     speed_factor=0.3)
            time.sleep(0.3)
            move_arm(right_main=1, right_forearm=1, right_hand=1,
                     speed_factor=0.3)
            time.sleep(0.3)
        else:
            move_legs(30, 30, 50, 50, 0.5)
            time.sleep(0.2)
            move_legs(50, 50, 50, 50, 0.5)
            time.sleep(0.3)

            move_arm(1, 1, 1, 1, 1, 1, 0.3)
            time.sleep(0.5)

        disable_all_servos()
    finally:
        if stop_sampler is not None:
            stop_sampler.set()

def move_servos_synchronized(movements, speed_factor, easing_strength=None):
    """
    Move multiple servos simultaneously.
    
    Parameters:
    - movements: List of (channel, target_value) tuples
    - speed_factor: Speed multiplier (0.0-1.0, higher is faster)
    - easing_strength: Easing amount (None uses global default, 0 = linear, higher = more ease in/out)
    """
    global _channels_initialized
    
    effective_easing = easing_strength if easing_strength is not None else global_easing_strength
    
    signal_servo_activity()
    
    servo_data = []
    hold_channels = []
    has_uninitialized_channel = False
    
    for channel, target_value in movements:
        if target_value is None:
            continue
        
        if channel not in _channels_initialized:
            has_uninitialized_channel = True
            _channels_initialized.add(channel)
            
        current_value = servo_positions.get(channel, None)
        
        if current_value is None:
            neutral_positions = {
                0: leftNeutralHeight,
                1: rightNeutralHeight,
                2: neutralLeftLeg,
                3: neutralRightLeg,
                4: leftMainMin,
                5: leftForarmMin,
                6: leftHandMin,
                7: rightMainMin,
                8: rightForarmMin,
                9: rightHandMin,
            }
            current_value = neutral_positions.get(channel, 300)
            servo_positions[channel] = current_value
        
        if target_value == -1:
            hold_channels.append((channel, current_value))
            continue
        
        if current_value == target_value:
            continue
        
        distance = abs(target_value - current_value)
        step = 1 if target_value > current_value else -1
        
        servo_data.append({
            'channel': channel,
            'current': current_value,
            'target': target_value,
            'step': step,
            'distance': distance,
            'steps_taken': 0
        })
    
    for channel, value in hold_channels:
        set_servo_pwm(channel, value)
    
    if not servo_data:
        return
    
    try:
        from modules.module_cputemp import record_movement
        record_movement()
    except Exception:
        pass
    
    max_distance = max(s['distance'] for s in servo_data)
    
    if has_uninitialized_channel:
        effective_speed = min(speed_factor, 0.3)
    else:
        effective_speed = speed_factor
    
    base_delay = 0.02 * (1.0 - effective_speed)
    
    while any(s['current'] != s['target'] for s in servo_data):
        for servo in servo_data:
            if servo['current'] != servo['target']:
                servo['current'] += servo['step']
                set_servo_pwm(servo['channel'], servo['current'])
                servo['steps_taken'] += 1
        
        if max_distance > 0:
            progress = min(s['steps_taken'] for s in servo_data if s['current'] != s['target'] or s['steps_taken'] > 0) / max_distance
        else:
            progress = 1.0
        
        if effective_easing > 0:
            if progress < 0.5:
                eased = 2 * progress * progress
            else:
                eased = 1 - 2 * (1 - progress) * (1 - progress)
            delay_multiplier = 1.0 + effective_easing * (1.0 - 4 * (eased - 0.5) ** 2)
        else:
            delay_multiplier = 1.0
        
        time.sleep(base_delay * delay_multiplier)
    
    for servo in servo_data:
        servo_positions[servo['channel']] = servo['target']
    
    _save_servo_positions()
    
    signal_servo_activity()
    
    time.sleep(0.05)

def move_legs(left_height_percent=None, right_height_percent=None, left_leg_percent=None, right_leg_percent=None, speed_factor=1.0):
    """
    Move leg servos to specified positions.
    
    Parameters:
    - left_height_percent: Left leg height (1-100, None to skip)
    - right_height_percent: Right leg height (1-100, None to skip)
    - left_leg_percent: Left leg forward/back (1-100, None to skip)
    - right_leg_percent: Right leg forward/back (1-100, None to skip)
    - speed_factor: Speed multiplier (0.0-1.0, higher is faster)
    """
    
    def percentage_to_value(percent, min_val, max_val):
        if percent == 0:
            return None
        normalized = (percent - 1) / 99.0
        value = min_val + (max_val - min_val) * normalized
        return int(round(value))

    movements = []
    
    if left_height_percent is not None and left_height_percent != 0:
        target_value = percentage_to_value(left_height_percent, leftUpHeight, leftDownHeight)
        movements.append((0, target_value))
    
    if right_height_percent is not None and right_height_percent != 0:
        target_value = percentage_to_value(right_height_percent, rightUpHeight, rightDownHeight)
        movements.append((1, target_value))
    
    if left_leg_percent is not None and left_leg_percent != 0:
        target_value = percentage_to_value(left_leg_percent, forwardLeftLeg, backLeftLeg)
        movements.append((2, target_value))
    
    if right_leg_percent is not None and right_leg_percent != 0:
        target_value = percentage_to_value(right_leg_percent, forwardRightLeg, backRightLeg)
        movements.append((3, target_value))

    move_servos_synchronized(movements, speed_factor)

def move_arm(left_main=None, left_forearm=None, left_hand=None,
             right_main=None, right_forearm=None, right_hand=None, speed_factor=1.0):
    """
    Move arm servos to specified positions.
    
    Parameters:
    - left_main: Left main arm position (1-100, None to skip, -1 to hold)
    - left_forearm: Left forearm position (1-100, None to skip, -1 to hold)
    - left_hand: Left hand position (1-100, None to skip, -1 to hold)
    - right_main: Right main arm position (1-100, None to skip, -1 to hold)
    - right_forearm: Right forearm position (1-100, None to skip, -1 to hold)
    - right_hand: Right hand position (1-100, None to skip, -1 to hold)
    - speed_factor: Speed multiplier (0.0-1.0, higher is faster)
    """
    
    arm_speed_curve = 0.2
    adjusted_speed = speed_factor ** arm_speed_curve
    arm_easing_strength = 0.85
    
    def percentage_to_value(percent, min_val, max_val):
        if percent == 0:
            return None
        if percent == -1:
            return -1
        if percent == 1:
            return min_val
        if percent == 100:
            return max_val
        if max_val > min_val:
            value = min_val + ((max_val - min_val) * (percent - 1) / 99)
        else:
            value = min_val - ((min_val - max_val) * (percent - 1) / 99)
        return int(round(value))

    def get_value(val, min_val, max_val):
        if val is None or val == 0:
            return None
        if val == -1:
            return -1
        return percentage_to_value(val, min_val, max_val)

    movements = [
        (4, get_value(left_main, leftMainMin, leftMainMax)),
        (5, get_value(left_forearm, leftForarmMin, leftForarmMax)),
        (6, get_value(left_hand, leftHandMin, leftHandMax)),
        (7, get_value(right_main, rightMainMin, rightMainMax)),
        (8, get_value(right_forearm, rightForarmMin, rightForarmMax)),
        (9, get_value(right_hand, rightHandMin, rightHandMax)),
    ]

    move_servos_synchronized(movements, adjusted_speed, easing_strength=arm_easing_strength)

def cleanup():
    
    disable_all_servos()

from modules.module_movement_registry import (
    MOVEMENTS,
    LEGS_ONLY,
    HAS_ARMS,
    get_all,
    get_by_type,
    get_legs_only,
    get_has_arms,
    get_names,
    get_names_by_type
)

if __name__ == "__main__":
    initialize_servos()