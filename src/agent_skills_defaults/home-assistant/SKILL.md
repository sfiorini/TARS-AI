---
name: home-assistant
description: Control and inspect a Home Assistant smart home via REST: lights, switches, scenes, climate, covers, media players, states, listing. Params action=list_entities|get_state|list_services|call_service.
license: MIT
metadata:
  tars_parameters: '{"properties": {"action": {"type": "string", "description": "list_entities | get_state | list_services | call_service"}, "entity_id": {"type": "string", "description": "Entity id for get_state or call_service target, e.g. light.kitchen"}, "domain": {"type": "string", "description": "Service domain for call_service, e.g. light"}, "service": {"type": "string", "description": "Service name for call_service, e.g. turn_on"}, "data": {"type": "string", "description": "JSON object string of extra service data, e.g. {\"brightness_pct\": 50}"}}, "required": ["action"]}'
  tars_config: '{"url": {"type": "text", "description": "Home Assistant base URL, e.g. http://192.168.1.50:8123"}}'
---
# Home Assistant

Run actions against the Home Assistant REST API:

    run_skill_script: {"skill": "home-assistant", "script": "ha_rest.py", "args": ["<action>", "<entity_id>", "<domain>", "<service>", "<data>"]}

The script reads HA_TOKEN from the environment (never pass a token as an argument)
and the URL from [SKILL_STD:home-assistant] in config.ini (fallback: HA_URL env).
It prints a bounded speakable summary on stdout. On any error it prints a one-line
reason to stderr and exits non-zero. Start with list_entities or get_state (read-only)
to learn what's available before calling services.