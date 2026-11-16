"""Home Assistant REST script (stdlib only).

Positional argv: action entity_id domain service data
Empty strings tolerated (absent).

URL order: TARS_CONFIG_PATH env →
  Path(__file__).resolve().parents[3] / "config.ini" [SKILL_STD:home-assistant] url →
  HA_URL env → exit 1.
Token: HA_TOKEN env only — never an arg, never echoed.
"""
import configparser
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ACTIONS = frozenset({"list_entities", "get_state", "list_services",
                     "call_service"})


def _usage():
    print("usage: ha_rest.py <action> [entity_id] [domain] [service] [data]",
          file=sys.stderr)
    sys.exit(1)


def _resolve_url():
    # D6 literal order: TARS_CONFIG_PATH if set (EXCLUSIVE — no fall-through
    # to the default config), ELSE the script-relative default config;
    # then HA_URL env; then exit 1. (M2 audit IB-F6: the delivered version
    # consulted the default config even when TARS_CONFIG_PATH was set.)
    config_path = os.environ.get("TARS_CONFIG_PATH")
    if config_path:
        parser = configparser.ConfigParser()
        parser.read(config_path)
        url = parser.get("SKILL_STD:home-assistant", "url", fallback="")
        if url:
            return url
    else:
        # default branch: script → home-assistant → agent_skills(_defaults) → src
        default_config = (Path(__file__).resolve().parents[3]
                          / "config.ini")
        if default_config.exists():
            parser = configparser.ConfigParser()
            parser.read(str(default_config))
            url = parser.get("SKILL_STD:home-assistant", "url", fallback="")
            if url:
                return url
    url_env = os.environ.get("HA_URL", "")
    if url_env:
        return url_env
    print("Home Assistant URL not configured "
          "(set [SKILL_STD:home-assistant] url or HA_URL)",
          file=sys.stderr)
    sys.exit(1)


def _get(path):
    return _request("GET", path)


def _post(path, body):
    return _request("POST", path, body)


def _request(method, path, body=None):
    # URL resolved BEFORE the token check (M2 audit IB-F5): config errors
    # surface before auth errors, and the real-subprocess self-sourcing
    # test becomes branch-sensitive (a deleted TARS_CONFIG_PATH branch
    # yields the URL error, not the token error).
    base = _resolve_url().rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    if not token:
        print("HA_TOKEN not set (add it to .env)", file=sys.stderr)
        sys.exit(1)
    url = f"{base}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        code = e.code
        msg = f"Home Assistant HTTP {code}"
        if code == 401:
            msg += " (unauthorized — check HA_TOKEN)"
        print(msg, file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        host_part = ""
        if hasattr(e, "reason") and hasattr(e.reason, "strerror"):
            host_part = str(e.reason.strerror)
        else:
            host_part = str(e)
        # Extract host only from the URL fragment (no userinfo/path)
        import re
        host_match = re.search(r'host\s+\'([^\']+)\'', str(e))
        if host_match:
            host_part = host_match.group(1)
        print(f"Cannot reach Home Assistant at {host_part}",
              file=sys.stderr)
        sys.exit(1)


def _list_entities():
    _, body = _get("/api/states")
    try:
        states = json.loads(body)
    except json.JSONDecodeError:
        print("Unexpected response from Home Assistant", file=sys.stderr)
        sys.exit(1)
    if not isinstance(states, list):
        print("Unexpected response from Home Assistant", file=sys.stderr)
        sys.exit(1)
    domains = {}
    for s in states:
        eid = s.get("entity_id", "")
        domain = eid.split(".", 1)[0] if "." in eid else "other"
        domains.setdefault(domain, {"count": 0, "names": []})
        domains[domain]["count"] += 1
        name = s.get("attributes", {}).get("friendly_name", eid)
        if name and len(domains[domain]["names"]) < 3:
            domains[domain]["names"].append(str(name))
    parts = []
    for domain in sorted(domains.keys())[:8]:
        info = domains[domain]
        entry = f"{domain}: {info['count']}"
        if info["names"]:
            entry += f" ({', '.join(info['names'])})"
        parts.append(entry)
    result = "; ".join(parts)
    if len(result) > 600:
        result = result[:597] + "..."
    print(result)


def _get_state(entity_id):
    if not entity_id:
        _usage()
    _, body = _get(f"/api/states/{entity_id}")
    try:
        state = json.loads(body)
    except json.JSONDecodeError:
        print("Unexpected response from Home Assistant", file=sys.stderr)
        sys.exit(1)
    if not isinstance(state, dict):
        print("Unexpected response from Home Assistant", file=sys.stderr)
        sys.exit(1)
    name = state.get("attributes", {}).get("friendly_name", entity_id)
    attrs = state.get("attributes", {})
    key_attrs = {k: v for k, v in attrs.items()
                 if k not in ("friendly_name", "icon", "supported_features",
                              "entity_picture", "assumed_state",
                              "attribution", "device_class",
                              "unit_of_measurement")}
    attr_strs = [f"{k}: {v}" for k, v in sorted(key_attrs.items())[:3]]
    result = f"{entity_id}: {state.get('state', 'unknown')} ({name}"
    if attr_strs:
        result += f"; {', '.join(attr_strs)}"
    result += ")"
    if len(result) > 300:
        result = result[:297] + "..."
    print(result)


def _list_services():
    _, body = _get("/api/services")
    try:
        services = json.loads(body)
    except json.JSONDecodeError:
        print("Unexpected response from Home Assistant", file=sys.stderr)
        sys.exit(1)
    if not isinstance(services, list):
        print("Unexpected response from Home Assistant", file=sys.stderr)
        sys.exit(1)
    parts = []
    count = 0
    for domain_info in services:
        if count >= 10:
            break
        domain = domain_info.get("domain", "unknown")
        svcs = domain_info.get("services", {})
        svc_names = []
        for svc_name in sorted(svcs.keys()
                               if isinstance(svcs, dict)
                               else [str(s) for s in svcs])[:6]:
            svc_names.append(str(svc_name))
        parts.append(f"{domain}: {', '.join(svc_names)}" if svc_names
                     else domain)
        count += 1
    result = "; ".join(parts)
    if len(result) > 600:
        result = result[:597] + "..."
    print(result)


def _call_service(domain, service, entity_id, data):
    if not domain or not service:
        _usage()
    body = {}
    if entity_id:
        body["entity_id"] = entity_id
    if data:
        try:
            parsed = json.loads(data)
            if not isinstance(parsed, dict):
                print("data must be a JSON object string", file=sys.stderr)
                sys.exit(1)
            body.update(parsed)
        except json.JSONDecodeError:
            print("data must be a JSON object string", file=sys.stderr)
            sys.exit(1)
    path = f"/api/services/{domain}/{service}"
    _, resp_body = _post(path, body)
    try:
        result = json.loads(resp_body)
    except json.JSONDecodeError:
        print("Unexpected response from Home Assistant", file=sys.stderr)
        sys.exit(1)
    changed = 0
    if isinstance(result, list):
        changed = len(result)
    target = entity_id if entity_id else "home"
    print(f"Called {domain}.{service} on {target}: "
          f"ok ({changed} entities changed)")


def main():
    args = [sys.argv[i] if i < len(sys.argv) else ""
            for i in range(1, 6)]
    action = args[0]
    if not action or action not in ACTIONS:
        _usage()
    entity_id = args[1]
    domain = args[2]
    service = args[3]
    data = args[4]
    if action == "list_entities":
        _list_entities()
    elif action == "get_state":
        _get_state(entity_id)
    elif action == "list_services":
        _list_services()
    elif action == "call_service":
        _call_service(domain, service, entity_id, data)


if __name__ == "__main__":
    main()