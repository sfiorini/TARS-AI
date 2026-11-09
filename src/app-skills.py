#!/usr/bin/env python3
"""TARS agent-skills CLI — manage the Agent Skills pool (D3).

Commands:
  list                     one line per skill: name — description [params]
  install <path> [--force] install a skill folder or .zip (conflict needs --force)
  uninstall <name>         remove an installed skill
  validate <path>           validate a skill folder (prints errors, exit 1 if any)
  reset [--yes]              wipe the pool and re-copy from the shipped defaults

Operates on src/agent_skills via get_agent_skill_store(); the
TARS_SKILLS_POOL env var overrides the pool path (tests and offline
tooling).  TARS_CONFIG_PATH is respected by the config bootstrap.
House style: no argparse — sys.argv, sys.exit(0/1).
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="[SKILLS] %(message)s")
log = logging.getLogger("skills")

USAGE = __doc__.strip()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULTS_DIR = os.path.join(_SCRIPT_DIR, "agent_skills_defaults")


def _get_store():
    """The agent-skill store (TARS_SKILLS_POOL overrides the live path)."""
    from modules.module_agent_skills import (AgentSkillStore,
                                             get_agent_skill_store)
    override = os.environ.get("TARS_SKILLS_POOL")
    if override:
        return AgentSkillStore(pool_dir=override,
                               defaults_dir=_DEFAULTS_DIR)
    return get_agent_skill_store()


def cmd_list(store, args):
    skills = store.list_skills()
    if not skills:
        print("(no skills installed)")
        return 0
    for skill in skills:
        marker = " [params]" if skill["has_parameters"] else ""
        print(f"{skill['name']} — {skill['description']}{marker}")
    return 0


def cmd_install(store, args):
    paths = [a for a in args if not a.startswith("--")]
    if not paths:
        print("skills: install needs a folder or .zip path",
              file=sys.stderr)
        return 1
    path = paths[0]
    force = "--force" in args
    if path.lower().endswith(".zip"):
        ok, errors = store.install_from_zip(path, force=force)
    else:
        ok, errors = store.install_from_folder(path, force=force)
    if not ok:
        for error in errors:
            print(f"skills: {error}", file=sys.stderr)
        return 1
    print(f"Installed {os.path.basename(path.rstrip('/'))}")
    return 0


def cmd_uninstall(store, args):
    if not args:
        print("skills: uninstall needs a skill name", file=sys.stderr)
        return 1
    try:
        store.uninstall(args[0])
    except ValueError as exc:
        print(f"skills: {exc}", file=sys.stderr)
        return 1
    print(f"Uninstalled '{args[0]}'")
    return 0


def cmd_validate(store, args):
    from modules.module_agent_skills import parse_skill_md, validate_skill
    if not args:
        print("skills: validate needs a skill folder", file=sys.stderr)
        return 1
    path = args[0]
    skill_md = os.path.join(path, "SKILL.md")
    if not os.path.isfile(skill_md):
        print(f"skills: {path}: SKILL.md is required", file=sys.stderr)
        return 1
    try:
        data = parse_skill_md(skill_md)
    except (OSError, ValueError) as exc:
        print(f"skills: {exc}", file=sys.stderr)
        return 1
    errors = validate_skill(path, data)
    if errors:
        for error in errors:
            print(f"skills: {error}", file=sys.stderr)
        return 1
    print(f"{data.get('name', os.path.basename(path))} is valid")
    return 0


def cmd_reset(store, args):
    if "--yes" not in args:
        answer = input("Reset the agent-skills pool from the shipped "
                       "defaults? User-created skills are DELETED. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    store.reset()
    print(f"Pool reset from {_DEFAULTS_DIR}")
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if len(argv) >= 2 else 1
    command, args = argv[1], argv[2:]
    store = _get_store()
    if command == "list":
        return cmd_list(store, args)
    if command == "install":
        return cmd_install(store, args)
    if command == "uninstall":
        return cmd_uninstall(store, args)
    if command == "validate":
        return cmd_validate(store, args)
    if command == "reset":
        return cmd_reset(store, args)
    print(USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
