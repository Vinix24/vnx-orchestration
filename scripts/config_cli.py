#!/usr/bin/env python3
"""vnx config list|get|explain|set|unset — the registry view and its operator writer (config-een-oppervlak).

``get --raw`` is the bash contract (plan V3.5): one stdout line per key in order, an empty line
for no value, nothing else on stdout; exit 0 ok, 2 unknown key, 3 a storage layer unreadable
(``strict_resolver=True``); diagnostics on stderr.

``set``/``unset`` (PR-10b) write the project row through ``config_store_db`` or, with ``--global``,
the global file through ``config_global``; both need a non-blank ``--reason`` and operator mode.
The layer that refuses names why: exit 2 unknown key, 3 a store unreadable, 4 refused.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
for _path in (SCRIPT_DIR, SCRIPT_DIR / "lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import config_registry as cr  # noqa: E402

EXIT_OK = 0
EXIT_UNKNOWN_KEY = 2
EXIT_UNREADABLE = 3
EXIT_REFUSED = 4
ACTOR = "operator"  # the operator user, as dashboard/api_config.py records a write without an actor


def _strict_resolver(state_dir: Path) -> cr.DbResolver:
    import config_store_db
    return config_store_db.make_db_resolver(lambda _pid: state_dir, raise_on_error=True)


def _project_store(project_id: Optional[str]) -> Tuple[Optional[str], Path]:
    """``(project_id, state dir)`` for the named project, else the active store's."""
    if project_id:
        from project_root import resolve_central_data_dir
        state_dir: Optional[Path] = resolve_central_data_dir(project_id) / "state"
    else:
        from main_window import active_store
        state_dir, project_id = active_store()
        if not project_id:
            from project_root import resolve_project_id
            project_id = resolve_project_id()
    return project_id, Path(state_dir)


def _resolve(key: str, pid: Optional[str], resolver: Optional[cr.DbResolver],
             layers: Sequence[str] = cr.CHAIN_LAYERS) -> Tuple[Optional[str], str]:
    return cr.resolve_with_source(key, pid, resolver, use_default_resolver=False, strict_resolver=True,
                                  layers=tuple(layers))


def _layer_error(source: str) -> bool:
    return source in cr.ERROR_SOURCES

def _row(key: str, pid: Optional[str], resolver: Optional[cr.DbResolver]) -> dict:
    entry = cr.CONFIG_REGISTRY[key]
    value, source = _resolve(key, pid, resolver)
    return {"key": key, "value": value, "source": source, "scope": entry.scope, "type": entry.type,
            "allowed_values": list(entry.allowed_values) if entry.allowed_values else None}


def _fmt(row: dict) -> str:
    value = "(unreadable)" if _layer_error(row["source"]) else ("" if row["value"] is None else row["value"])
    allowed = f" allowed={','.join(row['allowed_values'])}" if row["allowed_values"] else ""
    return f"{row['key']}={value}  [{row['source']}] scope={row['scope']} type={row['type']}{allowed}"


def cmd_list(args, pid, resolver, out) -> int:
    rows = [_row(key, pid, resolver) for key in cr.CONFIG_REGISTRY]
    out(json.dumps(rows, indent=2) if args.json else "\n".join(_fmt(r) for r in rows))
    return EXIT_UNREADABLE if any(_layer_error(r["source"]) for r in rows) else EXIT_OK


def cmd_get(args, pid, resolver, out, err) -> int:
    if args.raw:
        return _get_raw(args.keys, pid, resolver, out, err)
    key = args.keys[0]
    if len(args.keys) != 1 or key not in cr.CONFIG_REGISTRY:
        err(f"vnx config get: need exactly one known KEY (several only with --raw): {' '.join(args.keys)}")
        return EXIT_UNKNOWN_KEY
    row = _row(key, pid, resolver)
    out(json.dumps(row, indent=2) if args.json else _fmt(row))
    return EXIT_UNREADABLE if _layer_error(row["source"]) else EXIT_OK


def _get_raw(keys: List[str], pid, resolver, out, err) -> int:
    unknown = [k for k in keys if k not in cr.CONFIG_REGISTRY]
    if unknown:
        err(f"vnx config get --raw: unknown key {', '.join(unknown)}")
        return EXIT_UNKNOWN_KEY
    lines: List[str] = []
    for key in keys:
        value, source = _resolve(key, pid, resolver)
        if _layer_error(source):
            err(f"vnx config get --raw: {key}: storage layer unreadable ({source})")
            return EXIT_UNREADABLE
        if value is not None and ("\n" in value or "\r" in value):
            err(f"vnx config get --raw: {key}: value holds a line break, cannot be one line")
            return EXIT_UNREADABLE
        lines.append("" if value is None else value)
    out("\n".join(lines))
    return EXIT_OK


def cmd_explain(args, pid, resolver, out, err) -> int:
    key = args.key
    entry = cr.CONFIG_REGISTRY.get(key)
    if entry is None:
        err(f"vnx config explain: unknown key {key}")
        return EXIT_UNKNOWN_KEY
    lines, winner, unreadable = [], None, False
    for layer in cr.CHAIN_LAYERS:
        if layer == "project_config" and entry.scope == "global":
            lines.append(f"{layer}: not applicable (scope global, only the global file counts)")
            continue
        value, source = _resolve(key, pid, resolver, (layer,))
        if _layer_error(source):
            unreadable = True
            lines.append(f"{layer}: ERROR layer unreadable ({source})")
        elif value is None:
            lines.append(f"{layer}: not set")
        else:
            if winner is None and not unreadable:
                winner = layer
            lines.append(f"{layer}: {value}")
    verdict = ("winner: unknown, a layer is unreadable" if unreadable
               else f"winner: {winner}" if winner else "winner: none (no layer has a value)")
    out(f"{key} (scope={entry.scope}, type={entry.type})\n" + "\n".join(lines) + "\n" + verdict)
    return EXIT_UNREADABLE if unreadable else EXIT_OK


def cmd_write(args, pid, state_dir: Optional[Path], out, err) -> int:
    """``set``/``unset``: refuse before any write unless every precondition holds; then show ``get``."""
    from vnx_mode import VNXMode, read_mode

    verb = f"vnx config {args.verb}"
    if not args.reason.strip():
        err(f"{verb}: --reason must not be blank")
        return EXIT_REFUSED
    entry = cr.CONFIG_REGISTRY.get(args.key)
    if entry is None:
        err(f"{verb}: unknown key {args.key}")
        return EXIT_UNKNOWN_KEY
    if state_dir is None:
        err(f"{verb}: no project store resolved, nothing written")
        return EXIT_UNREADABLE
    mode = read_mode(str(state_dir.parent))  # mode.json sits in the data dir beside state/
    if mode != VNXMode.OPERATOR:
        err(f"{verb}: operator mode only (mode is {mode.value if mode else 'not initialized'}); "
            "run 'vnx init --operator' first. Nothing written.")
        return EXIT_REFUSED
    try:
        result = _write_global(args, entry) if args.glob else _write_project(args, pid, state_dir)
    except (ValueError, TypeError, PermissionError) as exc:
        err(f"{verb}: refused: {exc}")
        return EXIT_REFUSED
    except (sqlite3.Error, OSError, RuntimeError) as exc:  # GlobalConfigUnreadable is a RuntimeError
        err(f"{verb}: store unreadable, nothing written: {type(exc).__name__}: {exc}")
        return EXIT_UNREADABLE
    if result is not None:
        err(result)
    row = _row(args.key, pid, _strict_resolver(state_dir))
    out(_fmt(row))
    return EXIT_UNREADABLE if _layer_error(row["source"]) else EXIT_OK


def _write_global(args, entry: cr.ConfigEntry) -> Optional[str]:
    import config_global
    import config_store_db

    if entry.scope != "global":
        raise PermissionError(f"{args.key} has scope {entry.scope}: it has no global value; drop --global")
    if args.verb == "unset":
        config_global.unset_global(args.key, actor=ACTOR, reason=args.reason, approval_id=args.approval_id)
        return None
    # The enum check is G3's validator, the same one a project row goes through (config_global defers it).
    value = config_store_db._coerce(entry.type, args.value, args.key, entry.allowed_values)  # shared G3 validator
    config_global.set_global(args.key, value, actor=ACTOR, reason=args.reason, approval_id=args.approval_id)
    return None


def _write_project(args, pid: Optional[str], state_dir: Path) -> Optional[str]:
    import config_store_db

    if not pid:
        raise RuntimeError("no project id resolved")
    db = state_dir / "runtime_coordination.db"
    if not db.is_file():
        raise RuntimeError(f"{db} does not exist")
    conn = sqlite3.connect(str(db))
    try:
        if args.verb == "set":
            config_store_db.set_config(conn, pid, args.key, args.value, actor=ACTOR,
                                       approval_id=args.approval_id, reason=args.reason)
            return None
        result = config_store_db.unset_config(conn, pid, args.key, actor=ACTOR,
                                              approval_id=args.approval_id, reason=args.reason)
    finally:
        conn.close()
    return None if result["removed"] else f"{args.key}: no project row for {pid}, nothing removed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx config", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    lst = sub.add_parser("list")
    get = sub.add_parser("get")
    exp = sub.add_parser("explain")
    for p in (lst, get, exp):
        p.add_argument("--project-id")
    for p in (lst, get):
        p.add_argument("--json", action="store_true")
    get.add_argument("--raw", action="store_true")
    get.add_argument("keys", nargs="+", metavar="KEY")
    exp.add_argument("key", metavar="KEY")
    for verb in ("set", "unset"):
        w = sub.add_parser(verb)
        w.add_argument("key", metavar="KEY")
        if verb == "set":
            w.add_argument("value", metavar="VALUE")
        w.add_argument("--reason", required=True)
        w.add_argument("--global", dest="glob", action="store_true")
        w.add_argument("--approval-id")
        w.add_argument("--project-id")
    return parser


def main(argv: Optional[Sequence[str]] = None, *, resolver: Optional[cr.DbResolver] = None,
         project_id: Optional[str] = None, state_dir: Optional[Path] = None,
         out: Callable[[str], Any] = print, err: Optional[Callable[[str], Any]] = None) -> int:
    err = err or (lambda msg: print(msg, file=sys.stderr))
    args = build_parser().parse_args(argv)
    if resolver is None and state_dir is None:
        try:
            project_id, state_dir = _project_store(args.project_id)
        except Exception as exc:
            err(f"vnx config: cannot resolve the project store ({type(exc).__name__}: {exc})")
            return EXIT_UNREADABLE
    elif args.project_id:
        project_id = args.project_id
    if resolver is None:
        resolver = _strict_resolver(state_dir)
    if args.verb in ("set", "unset"):
        return cmd_write(args, project_id, state_dir, out, err)
    if args.verb == "list":
        return cmd_list(args, project_id, resolver, out)
    if args.verb == "get":
        return cmd_get(args, project_id, resolver, out, err)
    return cmd_explain(args, project_id, resolver, out, err)


if __name__ == "__main__":
    sys.exit(main())
