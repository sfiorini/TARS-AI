# dice-roll

The shipped example Agent Skill (D7): a dependency-free, spec-pure SKILL.md
with `metadata.tars_parameters` (so it is directly callable in one hop) and a
real stdlib-only script. Verify on the robot or any checkout:

```bash
python3 src/app-skills.py list
python3 src/agent_skills/dice-roll/scripts/roll_dice.py 2 20
```

Voice test: "roll me a d20" — TARS speaks the rolled number.
