# Attribution

### TARS Project Attribution Guidelines

As we continue to build and expand the TARS project, below are the guidelines for attribution and best practices when sharing or publishing work related to the TARS project.

---

## General Attribution

- Clearly state that this project is based on the character TARS from the film *Interstellar*.  
- When referencing or sharing the CAD designs:  
  - Include a statement like:  
    “Based on the mechanical puppet designs by Christopher Nolan, Nathan Crowley, and the production team who originally brought TARS to life—miniaturized CAD by Charlie Diaz, with additional modifications by the TARS-AI Community.”

---

## Specific Guidelines for CAD, STL, Scripts, and Modules

- The original CAD files and scripts are provided by Charlie Diaz.  
- If you modify or extend these files:  
  - Credit Charlie Diaz as the original creator.  
  - Clearly specify your contributions or modifications.  

- Files authored or significantly modified by Charles-Olivier Dion (AtomikSpace) are **dual-licensed**:  
  - Free for non-commercial use under CC-BY-NC 4.0.  

---

## Usage of TARS’ Voice and Personality

- “This project includes AI-generated elements inspired by Bill Irwin’s portrayal of TARS in the film *Interstellar*.”

---

## Sharing Content Online

- When publishing content (e.g., YouTube videos, blog posts, or repository updates):  
  - Attribute the original film’s production team and Charlie Diaz where applicable.  
  - Include a disclaimer noting that this project is a fan-made initiative.  
  - For AtomikSpace contributions, note if commercial licensing applies.

---

## Legal Considerations

- Non-commercial use is free under CC-BY-NC 4.0.  
- Commercial use of AtomikSpace contributions **requires a separate license** 
- This project includes code/inspiration (module engine and memory functions) from AME by Exploding Cat, licensed under the MIT License. The original license and copyright notice are included in the source code.  
- The remainder of the project is licensed under CC-BY-NC 4.0.

---

## Community Contribution

- Contributors are encouraged to follow these attribution guidelines when making modifications or additions.  
- Maintain transparency in crediting the work of all contributors.  
- Respect the dual-license terms for AtomikSpace contributions.  

---

These guidelines ensure that the TARS project respects the intellectual property of the original creators while fostering a collaborative and innovative community. Let’s work together to keep this project thriving and inspiring!

---

## Voice Assets (TTS)

- **Fish.audio public voice “Tars”** by brenardspag — <https://fish.audio/m/6a57e35f5c8244dbb5d012a3c434ec16/> — used as `[TTS] fishaudio_reference_id = 6a57e35f5c8244dbb5d012a3c434ec16` (set on the robot by `scripts/configure_tars_client.sh`). Use of the voice model is subject to Fish.audio’s terms.
- **Optional own-clone sample**: freesound.org **#815828 “Things A Machine Might Say 2”** by **RICHERlandTV** — <https://freesound.org/people/RICHERlandTV/sounds/815828/> — licensed **CC BY 4.0** (<https://creativecommons.org/licenses/by/4.0/>). Attribution: *“Things A Machine Might Say 2” by RICHERlandTV, freesound.org #815828, CC BY 4.0*. `scripts/setup_tts_voice.sh` downloads the preview and uploads a 15–25 s segment to Fish.audio voice cloning (`POST https://api.fish.audio/v1/model`) if you want your own clone. The sample file is **not committed** to this repository. Do not use the CC0 derivative (#832090) without crediting the original.
- **Piper robot FX**: the default sox effect chain (`pitch -150 flanger 5 3 0 70 0.4 triangle 25 quad gain -n -3`) is a robot-voice recipe inspired by dsidlo/ComfyUI_SoX_Effects (SoxVeRobotNode), applied post-synthesis via `[TTS] piper_robot_fx` / `piper_fx`.
