# Servo Watchdog & Recovery

`src/modules/module_servoctl.py` is **self-healing**: a daemon watchdog thread — started at app startup (`module_main.startup_initialization()` → `start_servo_watchdog()`), one tick every **15 s** — keeps the PCA9685 (I2C `0x40`) alive and repairs the failure modes that otherwise look like "the app says MOVING but the servos don't move", all **without rebooting the Pi**.

## What each watchdog tick does

1. **Dead handle** (`pca is None`, e.g. boot-time I2C error 121) → re-run `initialize_pca9685()`. Servo commands also trigger a **lazy re-init** (`_try_lazy_pca_init()`), rate-limited to once per 5 s, so a recovered bus is picked up on the next command. Failed PWM writes increment `_i2c_error_count` and drop the handle (`_invalidate_pca()`) so the next command re-creates the bus.
2. **Chip register state** (`_repair_chip_state_if_needed()`):
   - **MODE1 bit `0x20` must be set** (`_mode1_healthy`). If the chip's logic resets, MODE1 falls back to `0x01` while the device still ACKs on I2C — multi-byte PWM writes then land on the wrong registers and **all servos freeze** until the driver state is restored.
   - **MODE2 OUTNE bits (`0x0C`) must equal `0x04`** (`_mode2_healthy`). On this board **MODE2 = 0x04 is the only live-verified output-drive state**: `0x01` (the chip default) leaves the servos *silent* (no movement, no motor noise) while still ACKing on I2C; `0x0B`/`0x1E` (seen after chip-logic glitches) put all PWM pins in high-impedance. `initialize_pca9685()` therefore forces `_write_register(0x01, 0x04)` on every init.
   - On unhealthy state: re-init the driver, re-verify MODE1/MODE2, then **re-apply the saved positions** from `~/.servo_positions.json` so the servos hold instead of going limp.
3. **Stuck MOVING flag** (`_is_moving_stuck`): `MOVING=True` with no PWM write for **> 180 s** means an exception left the flag set and every later movement would silently no-op → the watchdog resets `MOVING = False` and logs an ERROR.

Register access uses the driver's `mode1_reg`/`mode2_reg` attributes when present, else raw `writeto`/`writeto_then_readfrom` on the stored I2C handle — never a single library internal.

## Live status and forced recovery (fork web endpoints)

Served by `module_chatui.py`; they need the web UI session cookie:

```bash
curl -c /tmp/tars.cookie -d "password=$WEBUI_PASSWORD" http://<pi-ip>/login

curl -b /tmp/tars.cookie http://<pi-ip>/servo_status
# {"pca_alive": true, "moving": false, "last_pwm_age_s": 12.3,
#  "i2c_error_count": 0, "watchdog_running": true, "mode1": 161, "mode2": 4}

curl -b /tmp/tars.cookie -X POST http://<pi-ip>/servo_reinit
```

`POST /servo_reinit` calls `force_servo_recovery()`: clears a stuck `MOVING`, re-creates the I2C/PCA9685 device, re-applies saved positions (20 ms apart, no neutral jerk), and returns the new `servo_status()`.

## Reading mode1 / mode2

| Field | Healthy value | Meaning |
|---|---|---|
| `pca_alive` | `true` | PCA9685 object exists; the chip ACKs on I2C |
| `mode1` | bit `0x20` set (e.g. `161` = 0xA1) | output-drive bit OK; `0x01`/`null` = chip logic reset → frozen servos |
| `mode2` | `4` (0x04; OUTNE bits `0x0C` == `0x04`) | totem-pole drive — the only state that moves servos on this board; `0x01` = silent, `0x0B`/`0x1E` = high-impedance |
| `moving` | `false` when idle | stuck `true` with large `last_pwm_age_s` → the 180 s watchdog reset clears it |
| `last_pwm_age_s` | small | seconds since the last PWM write; large + `moving: true` = stuck |
| `i2c_error_count` | 0 / slowly rising | rising fast = flaky wiring or servo-rail power |
| `watchdog_running` | `true` | watchdog started at boot (only when movement is enabled) |

## Manual no-reboot recovery (app stopped)

If the app is down and the servos are limp, re-assert the registers by hand on I2C bus 1, chip address `0x40`:

```bash
sudo i2cset -y 1 0x40 0x01 0x04   # MODE2 = 0x04  (output drive — required on this board)
sudo i2cset -y 1 0x40 0x00 0xA0   # MODE1 = 0xA0  (bit 0x20 set, chip running)
sudo i2cdump -y 1 0x40            # verify: reg 0x00 = a0, reg 0x01 = 04
```

While the app runs, the watchdog performs the equivalent automatically every 15 s, and `POST /servo_reinit` triggers it on demand — you normally never need the manual commands.

## Tests

`src/tests/test_servo_watchdog.py` is the executable spec (run `python3 -m pytest src/tests/test_servo_watchdog.py -q` for the current count): `_mode1_healthy`/`_mode2_healthy`/`_chip_state_healthy` truth tables, `_is_moving_stuck` timeout logic, fake-I2C register read/write (including the atomic `writeto_then_readfrom` fallback path), watchdog tick repair + position re-apply, and `force_servo_recovery()`. Run the whole suite with `python3 -m pytest src/tests -q` (green on V3 — the count grows with every feature and is not hardcoded here).

## Constants and failure scenarios

| Constant | Value | Role |
|---|---|---|
| `_watchdog_interval` | 15 s | watchdog tick period (`start_servo_watchdog(interval=15)`) |
| lazy re-init rate limit | 5 s | `_try_lazy_pca_init()` won't hammer a dead bus |
| `MAX_RETRIES` | 3 | PWM writes retry on errno 121 (I2C remote I/O error) before invalidating |
| stuck-MOVING timeout | 180 s | no PWM write while `MOVING=True` → flag reset |
| position persistence | `~/.servo_positions.json` | re-applied after every chip repair / re-init |

| Symptom | Cause | Watchdog action |
|---|---|---|
| Servos freeze mid-move, app still logs MOVING | MODE1 lost bit `0x20` (chip logic reset, still ACKs) | re-init + position re-apply within 15 s |
| Servos silent, no motor noise, app thinks it moves | MODE2 ≠ `0x04` (`0x01` default / `0x0B`/`0x1E` high-Z) | re-init forces MODE2=0x04 + re-apply |
| All movement no-ops after an exception | `MOVING` stuck `True` | reset after 180 s without PWM |
| `pca_alive: false` after boot | I2C bus dead at import | lazy re-init on next command / watchdog tick |
| `i2c_error_count` climbing | wiring / servo-rail power | handle invalidated; bus re-created per command |

Note: the PCA9685 is initialized at **module import time** only when `[CONTROLS] enabled` or `voicemovement` is true; otherwise `pca` stays `None` until the watchdog/lazy init brings it up. `start_servo_watchdog()` is called from `module_main.startup_initialization()` after `initialize_servos()`.

## Constants and rate limits

| Constant | Value | Meaning |
|---|---|---|
| `_watchdog_interval` | 15 s | watchdog tick (`start_servo_watchdog(interval=15)`, started from `module_main.py`) |
| lazy re-init rate limit | 5 s | `_try_lazy_pca_init()` retries a dead `pca` at most once per 5 s |
| `MAX_RETRIES` | 3 | I2C write attempts in `set_servo_pwm()` before `_invalidate_pca()` |
| stuck-MOVING timeout | 180 s | `_is_moving_stuck()`: `MOVING` true with no PWM for > 180 s → flag reset |
| log rate limits | 30 s | "PCA9685 unavailable" / register-read-failure warnings |
| positions file | `~/.servo_positions.json` | last known channel positions, re-applied after repair |

## What a repair looks like in the log

`src/logs/tars-ai.log`:

```
SERVO: watchdog started (15s interval)
SERVO: PCA9685 state unhealthy (MODE1=1, MODE2=11) - re-initializing
LOAD: PCA9685 initialized successfully
SERVO: PCA9685 state repair complete
ERROR: servo watchdog - MOVING stuck for more than 180s, resetting movement state
```

If you instead see `WARNING: servo watchdog - PCA9685 still unavailable` repeating, the **bus** is down (wiring/power) — check `i2cdetect -y 1` for `0x40`; the watchdog cannot fix a dead bus by itself.
