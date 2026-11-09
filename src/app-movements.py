#!/usr/bin/env python3
"""TARS movements CLI — manage the movement store (D5).

Commands:
  list                 print `id<tab>type<tab>name` per movement
  show <id>            pretty-print one entry
  edit <id>            open $EDITOR on the entry; validates on save
  reset                restore every defaults id (user movements kept)
  prune                reset + delete ids absent from defaults
  export <path>        write the whole pool to <path>
  import <path>        replace the pool from <path> (all-or-nothing)
  validate             report per-entry errors without touching the file

Operates on src/movements.json via get_movement_store(); the
TARS_MOVEMENTS_STORE env var overrides the store path (used by tests
and offline tooling).  TARS_CONFIG_PATH is respected by the config
bootstrap.  House style: no argparse — sys.argv, sys.exit(0/1).
"""
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile

logging.basicConfig(level=logging.INFO, format="[MOVEMENTS] %(message)s")
log = logging.getLogger("movements")

USAGE = __doc__.strip()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULTS_PATH = os.path.join(_SCRIPT_DIR, "movements_defaults.json")


def _get_store(load=True):
    """The movement store (TARS_MOVEMENTS_STORE overrides the live path).

    ``load=False`` builds a bare store without load()/seeding — used by
    validate, which must never create the store file.
    """
    from modules.module_movementstore import (MovementStore,
                                              get_movement_store,
                                              _STORE_PATH)  # noqa: SLF001
    override = os.environ.get("TARS_MOVEMENTS_STORE")
    store_path = override or _STORE_PATH
    if not load:
        return MovementStore(store_path=store_path,
                              defaults_path=_DEFAULTS_PATH)
    if override:
        store = MovementStore(store_path=store_path,
                              defaults_path=_DEFAULTS_PATH)
        store.load()
        return store
    return get_movement_store()


def cmd_list(store, args):
    for mid, entry in sorted(store.get_all().items()):
        print(f"{mid}\t{entry['type']}\t{entry['name']}")
    return 0


def cmd_show(store, args):
    entry = store.get(args[0])
    if entry is None:
        print(f"movements: unknown movement '{args[0]}'", file=sys.stderr)
        return 1
    print(json.dumps(entry, indent=2, sort_keys=True))
    return 0


def cmd_edit(store, args):
    mid = args[0]
    entry = store.get(mid)
    if entry is None:
        print(f"movements: unknown movement '{mid}'", file=sys.stderr)
        return 1
    fd, tmp_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(tmp_path, "w") as fh:
            json.dump(entry, fh, indent=2, sort_keys=True)
        editor = os.environ.get("EDITOR", "vi")
        subprocess.run(shlex.split(editor) + [tmp_path], check=False)
        try:
            with open(tmp_path, "r") as fh:
                edited = json.load(fh)
            store.update(mid, edited)
        except ValueError as exc:
            print(f"movements: invalid entry '{mid}': {exc}",
                  file=sys.stderr)
            return 1
        print(f"Updated '{mid}'")
        return 0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def cmd_reset(store, args):
    prune = args and args[0] == "prune"
    before = set(store.get_all())
    store.reset_from_defaults(prune=prune)
    after = set(store.get_all())
    with open(_DEFAULTS_PATH, "r") as fh:
        defaults = set(json.load(fh)["movements"])
    restored = len(defaults)
    kept = len(after - defaults)
    if prune:
        removed = len(before - defaults)
        print(f"Restored {restored} movements from defaults, "
              f"pruned {removed} orphan(s)")
    else:
        print(f"Restored {restored} movements from defaults, "
              f"kept {kept} user movement(s)")
    return 0


def cmd_export(store, args):
    path = os.path.abspath(args[0])
    store.export_to(path)
    print(f"Exported {len(store.get_all())} movements to {path}")
    return 0


def cmd_import(store, args):
    path = os.path.abspath(args[0])
    errors = store.import_from(path)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"Imported {len(store.get_all())} movements from {path}")
    return 0


def cmd_validate(store, args):
    if not os.path.exists(store.store_path):
        print(f"no store file: {store.store_path} "
              f"(run 'reset' to seed it from the defaults)", file=sys.stderr)
        return 1
    ok, errors = store.is_valid()
    if ok:
        print("movements.json is valid")
        return 0
    for error in errors:
        print(error, file=sys.stderr)
    return 1


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if len(argv) >= 2 else 1
    command, args = argv[1], argv[2:]
    store = _get_store(load=command != "validate")
    if command == "list":
        return cmd_list(store, args)
    if command == "show" and args:
        return cmd_show(store, args)
    if command == "edit" and args:
        return cmd_edit(store, args)
    if command in ("reset", "prune"):
        return cmd_reset(store, [command])
    if command == "export" and args:
        return cmd_export(store, args)
    if command == "import" and args:
        return cmd_import(store, args)
    if command == "validate":
        return cmd_validate(store, args)
    print(USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
