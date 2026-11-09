---
name: dice-roll
description: Roll dice for the user. Use when the user asks to roll a die or dice, e.g. "roll a d20", "roll 3 dice", "give me a random number between 1 and 6".
license: MIT
metadata:
  tars_parameters: '{"properties": {"count": {"type": "number", "description": "How many dice to roll (1-10)", "default": 1}, "sides": {"type": "number", "description": "Sides per die (2-100)", "default": 6}}, "required": []}'
---
# Dice Roll

Roll dice by running the script:

    run_skill_script: {"skill": "dice-roll", "script": "roll_dice.py", "args": ["<count>", "<sides>"]}

The script prints one line like "Rolled 3d6: 4, 2, 5 (total 11)". Speak that result
to the user. If the user says "roll a d20", use sides=20. Defaults: count=1, sides=6.
