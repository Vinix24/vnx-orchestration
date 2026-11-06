"""Characterisation of the argument surface ``planning_cli._build_parser`` builds.

Written against the unchanged code (before the plan-gate builders move out of
``_build_parser``) and kept green across that move: the refactor is
behaviour-neutral only if this snapshot does not change. The snapshot freezes,
per subcommand path, its aliases and, per action, the option strings, dest,
default, choices, required, nargs, const, help, metavar and action class name;
for subparsers it recurses. A handful of real command lines are parsed as well
and their ``Namespace`` is compared to the one the old code produced.

Regenerate only deliberately (this rewrites the fixture):

    python3 -c "import sys; sys.path.insert(0, 'tests'); \\
        import test_planning_cli_parser_shape as m; m.write_fixture()"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import planning_cli  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "planning_cli_parser_shape.json"

# Real command lines whose parsed Namespace is frozen alongside the surface.
COMMAND_LINES: List[List[str]] = [
    ["objective", "list"],
    ["objective", "show", "x"],
    ["horizon", "show", "x"],
    ["plan-gate", "run", "t", "--doc", "d", "--panel-seats", "codex"],
    ["plan-gate", "status", "t"],
    ["plan-gate", "missing-reasons"],
    ["plan-gate", "backfill-reason", "t", "--reason", "why", "--approval-id", "AP-1"],
    ["plan-gate", "reblock", "t", "--reason", "why", "--approval-id", "AP-2"],
    [
        "plan-gate", "batch", "--track", "a", "--track", "b", "--limit", "2",
        "--restart", "--dry-run", "--panel-seats", "codex", "--seat-timeout", "120",
        "--dispatch-paths", "scripts/x.py", "--task-class", "03_refactoring",
        "--irreversible",
    ],
]


def _jsonable(value: Any) -> Any:
    """A plain, sorted, JSON-safe rendering of an argparse default/choices value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items())}
    return "<%s>" % type(value).__name__


def _handler_name(parser: argparse.ArgumentParser) -> str | None:
    func = parser._defaults.get("func")
    return getattr(func, "__name__", None) if func is not None else None


def _action_entry(action: argparse.Action) -> Dict[str, Any]:
    return {
        "class": type(action).__name__,
        "dest": action.dest,
        "option_strings": list(action.option_strings),
        "default": _jsonable(action.default),
        "choices": None if action.choices is None else _jsonable(list(action.choices)),
        "required": bool(action.required),
        "nargs": _jsonable(action.nargs),
        "const": _jsonable(action.const),
        "help": action.help,
        "metavar": _jsonable(action.metavar),
        "type": getattr(action.type, "__name__", None),
    }


def _snapshot_parser(parser: argparse.ArgumentParser, path: List[str]) -> Dict[str, Any]:
    """Recursive, order-preserving snapshot of one parser and its subparsers."""
    entries: List[Dict[str, Any]] = []
    subcommands: Dict[str, Any] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            entries.append({
                "class": type(action).__name__,
                "dest": action.dest,
                "required": bool(action.required),
                "help": action.help,
                "metavar": _jsonable(action.metavar),
            })
            groups: Dict[int, Dict[str, Any]] = {}
            for name, child in action.choices.items():
                group = groups.setdefault(id(child), {"names": [], "parser": child})
                group["names"].append(name)
            for group in groups.values():
                names = group["names"]
                child_path = path + [names[0]]
                node = _snapshot_parser(group["parser"], child_path)
                node["aliases"] = names[1:]
                subcommands[names[0]] = node
            continue
        entries.append(_action_entry(action))
    return {
        "path": path,
        "prog": parser.prog,
        "description": parser.description,
        "aliases": [],
        "handler": _handler_name(parser),
        "actions": entries,
        "subcommands": subcommands,
    }


def _namespace(parser: argparse.ArgumentParser, argv: List[str]) -> Dict[str, Any]:
    namespace = parser.parse_args(argv)
    rendered = vars(namespace)
    func = rendered.get("func")
    if func is not None:
        rendered = dict(rendered, func=getattr(func, "__name__", repr(func)))
    return rendered


def build_snapshot() -> Dict[str, Any]:
    """The live structure of ``_build_parser()`` under a pinned ``VNX_PROJECT_ID``."""
    parser = planning_cli._build_parser()
    return {
        "env": {"VNX_PROJECT_ID": os.environ.get("VNX_PROJECT_ID", "<unset>")},
        "parser": _snapshot_parser(parser, []),
        "namespaces": {" ".join(argv): _namespace(parser, argv) for argv in COMMAND_LINES},
    }


def write_fixture() -> None:
    """Rewrite the fixture from the current code (deliberate regeneration only)."""
    saved = os.environ.pop("VNX_PROJECT_ID", None)
    try:
        snapshot = build_snapshot()
    finally:
        if saved is not None:
            os.environ["VNX_PROJECT_ID"] = saved
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _first_difference(actual: Any, expected: Any, where: str = "root") -> str | None:
    """The path + values of the first difference between two snapshots."""
    if type(actual) is not type(expected):
        return "%s: %r (%s) != %r (%s)" % (
            where, actual, type(actual).__name__, expected, type(expected).__name__,
        )
    if isinstance(actual, dict):
        for key in sorted(set(actual) | set(expected)):
            if key not in actual:
                return "%s.%s: missing in live parser (snapshot has %r)" % (where, key, expected[key])
            if key not in expected:
                return "%s.%s: extra in live parser (%r)" % (where, key, actual[key])
            difference = _first_difference(actual[key], expected[key], "%s.%s" % (where, key))
            if difference is not None:
                return difference
        return None
    if isinstance(actual, list):
        if len(actual) != len(expected):
            return "%s: %d entries in the live parser, %d in the snapshot" % (
                where, len(actual), len(expected),
            )
        for index, (live, frozen) in enumerate(zip(actual, expected)):
            difference = _first_difference(live, frozen, "%s[%d]" % (where, index))
            if difference is not None:
                return difference
        return None
    if actual != expected:
        return "%s: %r != %r" % (where, actual, expected)
    return None


@pytest.fixture()
def frozen_snapshot() -> Dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_snapshot_fixture_exists() -> None:
    assert FIXTURE.is_file(), "missing characterisation fixture: %s" % FIXTURE


def test_parser_shape_matches_snapshot(monkeypatch, frozen_snapshot) -> None:
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    live = build_snapshot()
    assert frozen_snapshot["env"] == {"VNX_PROJECT_ID": "<unset>"}
    difference = _first_difference(live["parser"], frozen_snapshot["parser"], "parser")
    assert difference is None, "parser surface changed: %s" % difference


def test_command_lines_parse_to_the_snapshot_namespace(monkeypatch, frozen_snapshot) -> None:
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    live = build_snapshot()
    difference = _first_difference(live["namespaces"], frozen_snapshot["namespaces"], "namespaces")
    assert difference is None, "parsed Namespace changed: %s" % difference


def _all_parsers(parser: argparse.ArgumentParser) -> List[argparse.ArgumentParser]:
    parsers = [parser]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                parsers.extend(_all_parsers(child))
    return parsers


def test_snapshot_detects_a_changed_help_text(monkeypatch, frozen_snapshot) -> None:
    """Negative control: the comparison must fail when one help text moves."""
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    parser = planning_cli._build_parser()
    changed = False
    for sub_parser in _all_parsers(parser):
        for action in sub_parser._actions:
            if not isinstance(action, argparse._SubParsersAction) and isinstance(action.help, str):
                action.help = "help text deliberately changed"
                changed = True
                break
        if changed:
            break
    assert changed, "no help text found to mutate"
    live = _snapshot_parser(parser, [])
    difference = _first_difference(live, frozen_snapshot["parser"], "parser")
    assert difference is not None
    assert "help text deliberately changed" in difference
