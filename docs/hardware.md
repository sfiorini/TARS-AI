# Hardware & Wiring

What you need to build the physical robot and how to wire the two I2C boards the software talks to: the **PCA9685 servo driver at I2C 0x40** and the **INA260 battery sensor at I2C 0x41**. Print files ship in this repo; the detailed bill of materials is maintained in the [upstream wiki](https://github.com/TARS-AI-Community/TARS-AI/wiki/Home).

## Bill of materials (summary)

| Item | Notes |
|---|---|
| Raspberry Pi 5 + SD card + official PSU | reference build; see [Quickstart](quickstart.md) |
| PCA9685 16-channel PWM servo driver | I2C **0x40**, 50 Hz — see defect warning below |
| INA260 current/voltage sensor | I2C **0x41**, optional battery monitoring |
| Buck (step-down) converter | feeds the servo rail, e.g. battery → 5–6 V |
| 10× hobby servos | channels 0–3 legs, 4–9 arms (arms optional) |
| 1000 µF electrolytic capacitor | across the servo rail V+/GND |
| Microphone + speaker | USB audio works |
| Optional: 3.5"/5" display, Pi CSI camera | `[UI] UI_enabled`, `use_camera_module` |

Verify the I2C addresses after wiring: `i2cdetect -y 1` should show `0x40` and `0x41`.

## 3D-print files

- **`3d Printer Files/`** — the motorized build:
  - `A. Required/` — `A. Electronic Support`, `B. Torso`, `C. Left Leg`, `D. Right Leg`
  - `B. Mods/` — 5" lid with cutout letters, base, anti-vibration lower lid, upper lid without screen, one-piece upper chassis (`upper_chassis_one_piece.stl`)
  - `Tools/` — TARS 3D calibration tool, leg spacer
- **`Static Version (no motors)/`** — display-only statue: `STL_Static_Version/`, `Static_BOM.xlsx`, `Static_assembly_instructions.doc`, `TARS_static_v1.step`
- Check `3d Printer Files/license.txt` before printing or sharing parts (CC-BY-NC: no commercial sale of parts or files).

## Servo driver: PCA9685 at I2C 0x40

`src/modules/module_servoctl.py` drives a PCA9685 at address **0x40**, 50 Hz, 16 PWM channels:

| Channels | Function |
|---|---|
| 0, 1 | left / right leg height |
| 2, 3 | left / right leg swing (forward/back) |
| 4, 5, 6 | left arm main / forearm / hand |
| 7, 8, 9 | right arm main / forearm / hand |

Per-servo calibration lives in `[SERVO]` in `src/config.ini` (`leftUpHeight`, `leftDownHeight`, `forwardLeftLeg`, …, arm min/max/offsets) — tune them with `src/app-servotester.py`, the pygame calibration GUI (see [Movements](movements.md)).

!!! warning "Known batch defect on some PCA9685 boards"
    On affected boards the **green screw terminal's V+ is not connected internally**. Workaround: power the servo rail through the **V+ header pin** instead — connect the buck converter's **OUT+ to the V+ pin** and **OUT− to the green GND terminal**. If your servos get no power with the usual wiring, check this first.

- Power the servo rail from a **buck converter** stepped down from the battery (typically 5–6 V). Never from the Pi's 5 V pin alone — servo current spikes will brown out the Pi (check `vcgencmd get_throttled`).
- Recommended: feed the PCA9685 **VCC from the Pi 5 V pin** and place a **1000 µF electrolytic capacitor across V+/GND** on the servo rail to absorb spikes.
- I2C: SDA/SCL to the Pi GPIO header, shared bus with the INA260; GND common.

!!! warning "MODE2 must be 0x04 on this board"
    The PCA9685's MODE2 register default (`0x01`, open-drain outputs) leaves the servos **completely silent** on this board even though the chip still ACKs on I2C. The driver forces **MODE2 = 0x04** at every init (`initialize_pca9685()` in `module_servoctl.py`) and the watchdog re-asserts it every 2 s. Do not "restore" 0x01 — see [Servo Watchdog & Recovery](servo-watchdog.md).

## Battery monitoring: INA260 at I2C 0x41

`src/modules/module_battery.py` reads an **INA260 at 0x41** every 0.5 s (`adafruit_ina260` on `board.I2C()`): current, voltage, power; percentage computed linearly between `[BATTERY] battery_cutoff_voltage` and `battery_initial_voltage`, smoothed over a 10-sample window; charge-state detection (CHARGING/DISCHARGING/IDLE) with a 10 s servo-activity cooldown so servo current doesn't masquerade as charging.

- `[BATTERY] battery_enabled = True` starts the monitor thread; `battery_auto_shutdown = True` runs `sudo shutdown now` after 60 s at 0 % (protects the SD card and the LiPo).
- `battery_capacity_mAh` is used for reporting; set `battery_initial_voltage`/`battery_cutoff_voltage` per pack (3S LiPo: 12.6 V / 10.5 V).

Test the sensor standalone on the Pi:

```bash
cd src && source .venv/bin/activate
python ina260_Battery_Sensor_Test.py
```

## Wiring diagrams

Original SVGs live in `Documentation/`; copies are embedded below from `docs/assets/`.

![Wiring diagram — data/signal](assets/wiring%20diagram%20data.svg)

![Wiring diagram — power](assets/wiring%20digram%20power.svg)

!!! note "Polarity checklist"
    Buck **OUT+ → PCA9685 V+ pin** (header pin, not the green terminal on defective batches), buck **OUT− → green GND**, 1000 µF cap across V+/GND, INA260 in series with the battery **positive** lead, Pi and PCA9685 share GND. Double-check before powering on — reversed servo-rail polarity kills the driver instantly.

## I2C map

| Device | Address | Used by |
|---|---|---|
| PCA9685 PWM servo driver | `0x40` | `src/modules/module_servoctl.py` (`PCA9685(i2c, address=0x40)`, 50 Hz) |
| INA260 battery sensor | `0x41` | `src/modules/module_battery.py` (`INA260(i2c, address=0x41)`) |

Enable and verify the bus:

```bash
sudo raspi-config nonint do_i2c 0        # enable I2C
sudo apt install -y i2c-tools
i2cdetect -y 1                            # expect 0x40 and 0x41
```

## Power budget

- The Pi is powered from its own USB-C supply; the **servo rail is powered by the buck converter** from the battery (set buck output to ~5–6 V before connecting; measure at the V+ pin).
- PCA9685 **VCC** comes from the Pi **5V pin** (logic level + chip supply), with a **1000 µF electrolytic across V+/GND** to buffer servo current spikes. Do not feed servo current through the Pi's 5V pin.
- INA260 sits in series on the battery **+** lead so it measures pack voltage and current; configure the pack in `[BATTERY]` (`battery_initial_voltage = 12.6`, `battery_cutoff_voltage = 10.5` for 3S LiPo).
- Check undervoltage any time servos behave erratically: `vcgencmd get_throttled` (see [Troubleshooting](troubleshooting.md)).

## Display, camera, audio

- Display: 3.5" 480x320 or 5–7" 800px HDMI/LCD; set `[UI] screen_width`/`screen_height`, `UI_enabled = False` for headless.
- Camera: Pi CSI module (`[UI] use_camera_module = True`, `camera_rotation` in `[VISION]`).
- Audio: USB mic + USB speaker are auto-detected (`module_tts._resolve_output_device()` prefers USB audio); 3.5 mm jack works too.
