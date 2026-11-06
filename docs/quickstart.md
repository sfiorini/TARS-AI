# Quickstart

From a blank SD card to a talking robot: flash the OS, clone this fork's `V3` branch, run `Install.sh`, fill in `.env` and `src/config.ini`, launch, and open the web UI. Everything is scripted except the two config files.

## 1. Hardware and OS image

- **Raspberry Pi 5** is the reference build (Pi 4 / 3 / Zero 2 are also handled by `Install.sh`, with reduced local features — see its interactive Pi-model menu).
- Flash **Raspberry Pi OS 64-bit** with Raspberry Pi Imager. The reference robot runs **Debian 13 "Trixie" aarch64**; `Install.sh` includes a Trixie `lgpio` dev symlink fix, so use a current 64-bit image.
- Boot once and finish first-run setup (user, Wi-Fi, SSH if you want remote access).
- Enable I2C for the PCA9685/INA260 boards: `sudo raspi-config` → *Interface Options → I2C → Yes* (equivalently `dtparam=i2c_arm=on` in `/boot/firmware/config.txt`). Verify later with `i2cdetect -y 1` — you should see `0x40` and `0x41`.
- Wire the servo driver and battery sensor before first power-on — see [Hardware & Wiring](hardware.md), including the known PCA9685 batch defect.

## 2. Clone the fork

```bash
cd ~
git clone -b V3 https://github.com/sfiorini/TARS-AI.git
cd TARS-AI
```

!!! warning "Use branch V3"
    `V3` is the only branch of this fork. Cloning upstream TARS-AI-Community gives you the old code without the servo-watchdog fixes, the movement registry, and the LLM delta fix documented here.

## 3. Run the installer

```bash
sudo ./Install.sh
```

`Install.sh` (interactive) asks for the Pi model and optional extras (RetroPie, Raspotify), installs system packages (`python3-pip python3-venv python3-dev portaudio19-dev espeak-ng libcap-dev sox libsox-fmt-all git …`), creates the virtualenv at **`src/.venv`**, installs the per-model requirements, and seeds config files if missing:

```bash
cp src/config.ini.template src/config.ini   # Install.sh ~line 1119
cp .env.template .env                        # Install.sh ~line 1150 (chmod 664)
```

It also expands `dphys-swapfile` and can create a desktop shortcut.

## 4. Secrets and config

Edit **`.env`** (hidden — `ls -la` to see it) with at least:

```bash
ELEVENLABS_API_KEY="..."   # Scribe STT + ElevenLabs TTS
OTHER_API_KEY="..."        # LLM key for [LLM] llm_backend = other (e.g. Mistral)
```

Full key list: `OPENAI_API_KEY`, `GROK_API_KEY`, `DEEPINFRA_API_KEY`, `OTHER_API_KEY`, `ELEVENLABS_API_KEY`, `EXTERNAL_API_KEY` (companion server Bearer key), `VISION_API_KEY`, `DISCORD_TOKEN`, `HA_TOKEN` — see [Configuration](configuration.md).

!!! warning "Never commit secrets"
    `.env` and `src/config.ini` are gitignored. Do not add them to git, issues, or chat.

Edit **`src/config.ini`**. Minimum for first run:

```ini
[LLM]
llm_backend = other
base_url = https://api.mistral.ai
other_model = zai-glm-5-3-flash

[ACCESS]
webui_password = <change-me>

[SERVO]
arms_present = false        # true if your build has arm servos
```

If you run the companion server on a server machine, point the robot at it after install:

```bash
./scripts/configure_tars_client.sh <EXTERNAL_API_KEY>   # api_key from the server host's config-server.ini
```

## 5. Launch

```bash
./tars-launcher.sh
```

The launcher activates `src/.venv`, runs `sudo setcap 'cap_net_bind_service=+ep'` on the Python binary (so the web UI can bind port 80 without root), then runs `App-Start.py`, which spawns:

```bash
cd src && source .venv/bin/activate && python app.py show_ui=true
```

On a desktop Pi a terminal window shows the boot log; on the robot the on-device display shows the UI. Over SSH, detach: `ssh -f pi 'nohup ~/TARS-AI/tars-launcher.sh >/dev/null 2>&1 &'`. Enable boot autostart with `./toggle-autostart.sh` (XDG entry — see [Running TARS](running.md)).

## 6. Verify hardware on the Pi

```bash
i2cdetect -y 1                 # expect 0x40 (PCA9685) and 0x41 (INA260)
cd src && source .venv/bin/activate
python ina260_Battery_Sensor_Test.py   # battery voltage/current, if fitted
```

## 7. Open the web UI

Browse to **`http://<pi-ip>/`** (port from `[ACCESS] webui_port`, default `80`, so no port in the URL) and log in with **`webui_password`** from `[ACCESS]`. From the dashboard: chat, config editor, motion tab, logs. Check servo health at `GET /servo_status` (expect `mode2: 4`). See [Web UI](web-ui.md).

Say the wake word (`hey tars` by default) and talk to TARS.

## 8. Run the tests

```bash
python3 -m pytest src/tests -q
```

Expected on V3: **62 passed** in ~1 s. `pytest.ini` sets `testpaths = src/tests` and `pythonpath = src`; the tests fake the GPIO/I2C modules and bootstrap `src/config.ini` from the template, so they run on any machine. On the robot, use the app venv: `src/.venv/bin/python -m pytest src/tests -q`.

## Next steps

- [Hardware & Wiring](hardware.md) — if you are assembling the body
- [Configuration](configuration.md) — tune voice, LLM, servos, skills
- [Server Mode](server-mode.md) — offload STT/TTS/vision to a PC
- [Troubleshooting](troubleshooting.md) — when something misbehaves
