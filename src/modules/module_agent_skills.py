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
import shutil
import subprocess
import sys
import tempfile
import zipfile

from modules.module_messageQue import queue_message
from modules.module_config import load_config
from modules.module_toolregistry import ToolResult

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
            if key == "metadata" and value.strip() in ("", "{}"):
                data["metadata"] = {}
                in_metadata = not value.strip()  # {} has no nested entries
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


class AgentSkillStore:
    """The installed agent-skill pool (D3): install from folder/zip,
    list, uninstall; create/edit/rename/reset + seeding (S-M5-04+)."""

    def __init__(self, pool_dir, defaults_dir):
        self.pool_dir = str(pool_dir)
        self.defaults_dir = str(defaults_dir)

    # ── discovery ──────────────────────────────────────────────

    def _skill_path(self, name):
        return os.path.join(self.pool_dir, name)

    def _read_skill(self, name):
        """Parse + validate one installed skill; (data, errors)."""
        skill_md = os.path.join(self._skill_path(name), "SKILL.md")
        try:
            data = parse_skill_md(skill_md)
        except (OSError, ValueError) as exc:
            return None, [str(exc)]
        return data, validate_skill(self._skill_path(name), data)

    def list_skills(self):
        """[{name, description, has_parameters, enabled}] sorted by name."""
        result = []
        if not os.path.isdir(self.pool_dir):
            return result
        for name in sorted(os.listdir(self.pool_dir)):
            if not os.path.isdir(self._skill_path(name)):
                continue
            data, errors = self._read_skill(name)
            if data is None or errors:
                queue_message(f"SKILLS: installed skill '{name}' is invalid: "
                              f"{errors or 'unreadable'}")
                continue
            decoded = decode_tars_metadata(data)
            result.append({
                "name": name,
                "description": data.get("description", ""),
                "has_parameters": decoded["parameters"] is not None,
                "enabled": True,  # [SKILL_STD:<name>] persistence: S-M5-06
            })
        return result

    # ── install ─────────────────────────────────────────────────

    def install_from_folder(self, path, force=False):
        """Install a skill folder (must contain SKILL.md).
        Returns (ok, errors)."""
        path = str(path)
        skill_md = os.path.join(path, "SKILL.md")
        if not os.path.isfile(skill_md):
            return False, [f"{path}: SKILL.md is required"]
        try:
            data = parse_skill_md(skill_md)
        except (OSError, ValueError) as exc:
            return False, [str(exc)]
        errors = validate_skill(path, data)
        if errors:
            return False, errors
        name = data["name"]
        target = self._skill_path(name)
        if os.path.exists(target):
            if not force:
                return False, [f"skill '{name}' is already installed "
                               f"(use force/overwrite to replace it)"]
            shutil.rmtree(target)
        shutil.copytree(path, target)
        queue_message(f"SKILLS: installed agent skill '{name}'")
        return True, []

    def install_from_zip(self, path, force=False):
        """Install from a zip: reject absolute/.. entries, extract to a
        temp dir, validate, then move into the pool (all-or-nothing)."""
        try:
            with zipfile.ZipFile(str(path)) as zf:
                for entry in zf.namelist():
                    if entry.startswith("/") or ".." in entry.split("/"):
                        return False, [f"unsafe zip entry '{entry}' "
                                       f"(absolute path or .. traversal)"]
                tmp = tempfile.mkdtemp()
                try:
                    extract_root = os.path.join(tmp, "extract")
                    zf.extractall(extract_root)
                    # find the shallowest SKILL.md — its parent is the skill
                    candidates = sorted(
                        (name for name in zf.namelist()
                         if name.rstrip("/").endswith("SKILL.md")),
                        key=lambda n: n.count("/"))
                    if not candidates:
                        return False, ["no SKILL.md found in zip"]
                    skill_dir = os.path.join(
                        extract_root, os.path.dirname(candidates[0]))
                    # normalize the folder to <name> so the name/dir
                    # validation passes (a root-level SKILL.md extracts
                    # into the randomly-named temp root)
                    try:
                        data = parse_skill_md(
                            os.path.join(skill_dir, "SKILL.md"))
                    except (OSError, ValueError) as exc:
                        return False, [str(exc)]
                    name = data.get("name", "")
                    if not name:
                        return False, ["no name in SKILL.md"]
                    named_dir = os.path.join(tmp, name)
                    if os.path.abspath(skill_dir) != os.path.abspath(named_dir):
                        if os.path.exists(named_dir):
                            return False, [f"zip already contains a "
                                           f"'{name}' folder"]
                        shutil.move(skill_dir, named_dir)
                    return self.install_from_folder(named_dir, force=force)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
        except zipfile.BadZipFile as exc:
            return False, [f"not a zip file: {exc}"]

    # ── create / edit / rename / reset / seed (S-M5-04) ─────────

    def create(self, name, description, body, license="MIT"):
        """Create a new skill folder with spec-pure frontmatter."""
        if not name or not _NAME_RE.match(name) \
                or name.startswith("-") or name.endswith("-") \
                or "--" in name or not (1 <= len(name) <= 64):
            return False, [f"invalid skill name '{name}' (1-64 chars, "
                           "[a-z0-9-], no leading/trailing/consecutive "
                           "hyphens)"]
        if not (1 <= len(description or "") <= 1024):
            return False, ["description must be 1-1024 chars"]
        target = self._skill_path(name)
        if os.path.exists(target):
            return False, [f"skill '{name}' already exists"]
        os.makedirs(target)
        content = (f"---\nname: {name}\ndescription: {description}\n"
                   f"license: {license}\nmetadata:\n---\n{body}\n")
        with open(os.path.join(target, "SKILL.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(content)
        queue_message(f"SKILLS: created agent skill '{name}'")
        return True, []

    def edit(self, name, description=None, body=None):
        """Edit description and/or body in place (line-level rewrite)."""
        skill_md = os.path.join(self._skill_path(name), "SKILL.md")
        if not os.path.isfile(skill_md):
            return False, [f"unknown skill '{name}'"]
        with open(skill_md, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        try:
            close = next(i for i in range(1, len(lines))
                        if lines[i].strip() == "---")
        except StopIteration:
            return False, [f"'{name}': malformed SKILL.md"]
        if description is not None:
            if not (1 <= len(description) <= 1024):
                return False, ["description must be 1-1024 chars"]
            for i in range(1, close):
                if lines[i].startswith("description:"):
                    lines[i] = f"description: {description}"
                    break
            else:
                lines.insert(close, f"description: {description}")
        if body is not None:
            lines = lines[:close + 1] + [body]
        with open(skill_md, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return True, []

    def rename(self, old, new):
        """Rename: validate the new name, move the dir, fix frontmatter."""
        old_dir = self._skill_path(old)
        if not os.path.isdir(old_dir):
            return False, [f"unknown skill '{old}'"]
        if not new or not _NAME_RE.match(new) \
                or new.startswith("-") or new.endswith("-") \
                or "--" in new or not (1 <= len(new) <= 64):
            return False, [f"invalid skill name '{new}'"]
        new_dir = self._skill_path(new)
        if os.path.exists(new_dir):
            return False, [f"skill '{new}' already exists"]
        shutil.move(old_dir, new_dir)
        ok, errors = self.edit(new, description=None, body=None)
        if ok:
            # fix the frontmatter name line
            skill_md = os.path.join(new_dir, "SKILL.md")
            with open(skill_md, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            close = next(i for i in range(1, len(lines))
                         if lines[i].strip() == "---")
            for i in range(1, close):
                if lines[i].startswith("name:"):
                    lines[i] = f"name: {new}"
                    break
            with open(skill_md, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return ok, errors

    def reset(self):
        """Wipe the pool and re-copy from the shipped defaults (locked
        meaning: user skills die on reset — asserted, not fixed)."""
        if os.path.isdir(self.pool_dir):
            for name in os.listdir(self.pool_dir):
                target = os.path.join(self.pool_dir, name)
                if os.path.isdir(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
        else:
            os.makedirs(self.pool_dir)
        if os.path.isdir(self.defaults_dir):
            shutil.copytree(self.defaults_dir, self.pool_dir,
                            dirs_exist_ok=True)
        queue_message("SKILLS: agent skills reset from defaults")

    def seed_if_absent(self):
        """First app start: copy defaults → pool (only when absent)."""
        if os.path.exists(self.pool_dir):
            return
        shutil.copytree(self.defaults_dir, self.pool_dir)
        count = len([n for n in os.listdir(self.pool_dir)
                     if os.path.isdir(os.path.join(self.pool_dir, n))])
        queue_message(f"SKILLS: seeded agent_skills from defaults "
                      f"({count} skills)")

    # ── progressive disclosure: load_skill / run_skill_script ─────

    def load_skill(self, name):
        """The skill's SKILL.md body (after the frontmatter), truncated to
        [STANDARD_SKILLS] body_char_limit. Unknown skill -> is_error with
        the bounded valid-name list."""
        skill_md = os.path.join(self._skill_path(name), "SKILL.md")
        if not os.path.isfile(skill_md):
            valid = ", ".join(sorted(s["name"]
                                     for s in self.list_skills()))[:200]
            return ToolResult(
                is_error=True,
                content=f"Unknown skill '{name}'. Valid: {valid or '(none)'}")
        with open(skill_md, "r", encoding="utf-8") as fh:
            text = fh.read()
        lines = text.splitlines()
        try:
            close = next(i for i in range(1, len(lines))
                         if lines[i].strip() == "---")
        except StopIteration:
            close = 0
        body = "\n".join(lines[close + 1:]).strip()
        limit = load_config().get("STANDARD_SKILLS", {}).get(
            "body_char_limit", 4000)
        if len(body) > limit:
            queue_message(f"SKILLS: load_skill('{name}') body truncated "
                          f"{len(body)} -> {limit} chars")
            body = body[:limit]
        return ToolResult(content=body)

    def run_skill_script(self, skill, script, args):
        """Run python3 <pool>/<skill>/scripts/<script> <args...>.

        Exit 0 -> stdout is ToolResult.content; non-zero exit ->
        is_error with stderr; timeout -> is_error with a timeout message.
        Scripts run with the app's privileges — no sandbox.
        """
        # path-traversal guard: script must be a bare filename
        if (not script or "/" in script or "\\" in script or ".." in script
                or os.path.isabs(script)):
            return ToolResult(is_error=True,
                              content=f"invalid script name '{script}'")
        script_path = os.path.join(self._skill_path(skill), "scripts",
                                   script)
        if not os.path.isfile(script_path):
            return ToolResult(
                is_error=True,
                content=f"unknown script '{script}' in skill '{skill}'")
        timeout = load_config().get("LLM", {}).get("tool_call_timeout", 60)
        try:
            proc = subprocess.run(
                [sys.executable, script_path] + [str(a) for a in (args or [])],
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ToolResult(is_error=True, content="Script timed out")
        if proc.returncode != 0:
            return ToolResult(
                is_error=True,
                content=(proc.stderr or "").strip()
                          or f"script exited with code {proc.returncode}")
        return ToolResult(content=(proc.stdout or "").strip())

    def uninstall(self, name):
        """Remove an installed skill (its whole folder)."""
        target = self._skill_path(name)
        if not os.path.isdir(target):
            raise ValueError(f"unknown skill '{name}'")
        shutil.rmtree(target)
        queue_message(f"SKILLS: uninstalled agent skill '{name}'")


def _std_config_path():
    """The config.ini path (TARS_CONFIG_PATH-aware, like module_config)."""
    return os.environ.get("TARS_CONFIG_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.ini")


def get_std_config(name):
    """Read a skill's persisted [SKILL_STD:<name>] values ({} if absent)."""
    import configparser
    parser = configparser.ConfigParser()
    parser.read(_std_config_path())
    section = f"SKILL_STD:{name}"
    if not parser.has_section(section):
        return {}
    return dict(parser.items(section))


def set_std_config(name, values):
    """Persist values into [SKILL_STD:<name>] (merged with existing),
    mirroring SkillManager._save_skill_section's line-level editing so
    comments elsewhere in config.ini survive."""
    config_path = _std_config_path()
    if not os.path.exists(config_path):
        return
    merged = get_std_config(name)
    merged.update({k: str(v) for k, v in (values or {}).items()})
    section_header = f"[SKILL_STD:{name}]"
    lines = open(config_path, "r").readlines()
    new_lines, skip = [], False
    for line in lines:
        stripped = line.strip()
        if stripped == section_header:
            skip = True
            continue
        if skip and stripped.startswith("[") and stripped.endswith("]"):
            skip = False
        if not skip:
            new_lines.append(line)
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines.append("\n")
    if not new_lines or new_lines[-1].strip():
        new_lines.append("\n")
    new_lines.append(f"{section_header}\n")
    for field, value in sorted(merged.items()):
        new_lines.append(f"{field} = {value}\n")
    new_lines.append("\n")
    with open(config_path, "w") as fh:
        fh.writelines(new_lines)


_store_instance = None


def get_agent_skill_store():
    """Module-level singleton over src/agent_skills + defaults."""
    global _store_instance
    if _store_instance is None:
        src_dir = os.path.dirname(os.path.abspath(__file__))
        base = os.path.dirname(src_dir)
        _store_instance = AgentSkillStore(
            pool_dir=os.path.join(base, "agent_skills"),
            defaults_dir=os.path.join(base, "agent_skills_defaults"))
    return _store_instance


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
