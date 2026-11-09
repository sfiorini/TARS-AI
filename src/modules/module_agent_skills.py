"""
Module : Agent Skills (standard skills pool, D3)
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

Agent Skills are spec-pure SKILL.md folders (D3): the six frontmatter
fields only — name, description, license, compatibility, metadata,
allowed-tools.  Claude Code hard-errors on any other top-level key, so
TARS-specific data (parameters, config) lives under `metadata:` as
JSON-encoded strings (`tars_parameters`, `tars_config`).
"""
import json
import os
import re

from modules.module_messageQue import queue_message

SPEC_FIELDS = ("name", "description", "license", "compatibility",
               "metadata", "allowed-tools")

_NAME_RE = re.compile(r"^[a-z0-9-]+$")


def parse_skill_md(path):
    """Parse a SKILL.md's frontmatter (stdlib only).

    `---` fences; top-level `key: value` lines; `metadata:` holds indented
    `key: <json-string>` entries one level deep.  Returns {field: value}
    with metadata as a dict of raw strings.
    """
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path}: missing opening --- frontmatter fence")
    try:
        end = next(i for i in range(1, len(lines))
                   if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError(f"{path}: unterminated frontmatter (no closing ---)")
    data = {}
    in_metadata = False
    for line in lines[1:end]:
        if not line.strip():
            continue
        stripped = line.strip()
        if not line.startswith((" ", "\t")):
            in_metadata = False
            key, _, value = line.partition(":")
            key = key.strip()
            if key == "metadata" and not value.strip():
                data["metadata"] = {}
                in_metadata = True
            else:
                data[key] = value.strip()
        elif in_metadata:
            key, _, value = stripped.partition(":")
            value = value.strip()
            # YAML-style quoting: strip one pair of matching quotes so
            # `tars_parameters: '{...}'` yields the bare JSON string
            if len(value) >= 2 and value[0] == value[-1] \
                    and value[0] in ("'", '"'):
                value = value[1:-1]
            data.setdefault("metadata", {})[key.strip()] = value
    return data


def validate_skill(skill_dir, data):
    """Validate a parsed SKILL.md against its folder. Returns error list."""
    errors = []
    if not os.path.isfile(os.path.join(skill_dir, "SKILL.md")):
        errors.append("SKILL.md is required")
    for key in data:
        if key not in SPEC_FIELDS:
            errors.append(
                f"unknown top-level key '{key}' — Claude Code hard-errors "
                f"on any field outside: {', '.join(SPEC_FIELDS)}")
    name = data.get("name", "")
    dir_name = os.path.basename(os.path.normpath(skill_dir))
    if not name:
        errors.append("name is required (1-64 chars)")
    else:
        if not (1 <= len(name) <= 64):
            errors.append(f"name '{name}' must be 1-64 chars")
        if not _NAME_RE.match(name):
            errors.append(f"name '{name}' must use only [a-z0-9-]")
        if name.startswith("-") or name.endswith("-") or "--" in name:
            errors.append(f"name '{name}' must not have leading/trailing/"
                          "consecutive hyphens")
        if name != dir_name:
            errors.append(f"name '{name}' must equal the parent directory "
                          f"name '{dir_name}'")
    description = data.get("description", "")
    if not (1 <= len(description) <= 1024):
        errors.append("description must be 1-1024 chars")
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append("metadata must be a mapping of key: <json-string>")
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if key.startswith("tars_"):
                try:
                    json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    errors.append(f"metadata '{key}' is not valid JSON")
    return errors


def decode_tars_metadata(data):
    """Decode tars_parameters / tars_config JSON strings (or None)."""
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    def _decode(key):
        raw = metadata.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    return {"parameters": _decode("tars_parameters"),
            "config": _decode("tars_config")}
