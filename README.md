# TARS-AI

<p align="center">
  <a href="https://github.com/TARS-AI-Community/TARS-AI">
    <img width=90% alt="" src="https://github.com/TARS-AI-Community/TARS-AI/blob/V2/media/tars-banner.png" />
  </a>
</p>

<p align="center">
  A recreation of the robot TARS from <i>Interstellar</i>, featuring AI capabilities.
</p>

<p align="center">
    <a href="https://discord.gg/AmE2Gv9EUt">
      <img alt="Discord Invitation Link" src="https://img.shields.io/discord/1311295890182508605" align="center" />
    </a>
    <a href="https://sfiorini.github.io/TARS-AI/">
        <img src="https://img.shields.io/badge/Docs-grey?style=flat-square&logo=readthedocs&logoColor=white" alt="Documentation" align="center" />
    </a>
    <a href="https://github.com/sfiorini/TARS-AI/actions/workflows/docs.yml">
      <img src="https://github.com/sfiorini/TARS-AI/actions/workflows/docs.yml/badge.svg?branch=V3" alt="Docs CI (V3)" align="center" />
    </a>
</p>

---

## What this fork adds (V3)

- **Servo self-healing watchdog** — continuously verifies the PCA9685 MODE1/MODE2 registers and re-initializes the chip when they drift, including the MODE2 = 0x04 output-drive quirk of this board; recovery endpoints `GET /servo_status` and `POST /servo_reinit`.
- **Full movement registry exposed to the LLM** via the `execute_movement` skill.
- **ElevenLabs Scribe (`scribe_v2`) STT support** (set `stt_processor = elevenlabs`), with automatic fallback to the external server.
- **Mistral streaming fix** — `delta.content` sent as a list of content blocks no longer produces empty replies.
- **TTS fallback chain** (ElevenLabs → external server Piper → local → espeak).
- **App-Stop TERM-to-KILL escalation** — a surviving process gets SIGKILL, so the app actually exits.
- **Docker server mode** (`TARS-AI_Server/`) with a `voices` bind mount.
- **Wake-word sensitivity doc correction** (1 = strict, 10 = lenient).
- **Docs site + CI** — MkDocs on GitHub Pages with a test + build workflow on branch V3.

> **Note:** `V3` is the one and only branch of this fork; it diverges permanently from upstream.

---

## Developer quickstart

```bash
git clone -b V3 https://github.com/sfiorini/TARS-AI.git
cd TARS-AI
```

On the Pi:

```bash
./Install.sh
cp .env.template .env                              # fill in API keys
cp src/config.ini.template src/config.ini          # edit to taste
./tars-launcher.sh
```

Web UI at `http://<pi-ip>/` (set `webui_password` in `[ACCESS]`).

Tests:

```bash
python -m pytest src/tests -q
```

---

## Links

- Full documentation: https://sfiorini.github.io/TARS-AI/
- Server mode (mini): https://sfiorini.github.io/TARS-AI/server-mode/
- Troubleshooting: https://sfiorini.github.io/TARS-AI/troubleshooting/

---

## Repo layout

```text
TARS-AI/
├── Install.sh, tars-launcher.sh, App-Start.py, App-Stop.py, aec.py, ...  # root scripts
├── src/
│   ├── app.py, app_cms.py, ...                 # core apps
│   ├── modules/  skills/  stt/  tts/  vision/  character/  memory/  music/
│   ├── config.ini.template                    # copy to config.ini and edit
│   ├── tests/                                 # pytest suite
│   └── www/                                   # web UI
├── TARS-AI_Server/                            # docker server ("mini"): app-server.py, Dockerfile, docker-compose.yml, www/
├── docs/                                      # MkDocs source for the docs site
├── Documentation/                             # wiring diagrams, ComfyUI workflows
└── scripts/                                   # client setup helper (configure_tars_client.sh)
```

---

## Contributing

This fork works only on **`V3`** — open issues and PRs against `sfiorini/TARS-AI` branch `V3`.
Join the community on Discord: 👉 https://discord.gg/AmE2Gv9EUt

---

## 📜 License

This project is licensed under **Creative Commons Attribution-NonCommercial 4.0 International (CC-BY-NC 4.0)**.

You may:
- Build and modify your own TARS robot
- Share improvements and derivatives
- Use the project for personal, educational, and research purposes

You may **not** use this project for commercial purposes without explicit permission from the authors.
Commercial use includes, but is not limited to:

- Selling 3D printed parts, kits, or complete robots  
- Selling or distributing STL / CAD files for money  
- Offering paid assembly, customization, or installation services  
- Monetized YouTube, Social Media, Patreon, or subscription content that distributes project files or derivatives  
- Using this project in paid products, commercial research, or corporate projects  
- Integrating this project into commercial software or hardware products  
- Selling derivatives or modified versions of the hardware or software  

If you are unsure whether your use case is commercial, assume it is and request permission from the authors.

See the [LICENSE](./LICENSE) file for details.
See [DUAL-LICENSE.md](./DUAL-LICENSE.md) for the dual-license notice covering AtomikSpace contributions.

---

## 🧾 Attribution

Please follow the attribution guidelines when sharing or publishing derivative work:

👉 [ATTRIBUTION.md](./ATTRIBUTION.md)

---

