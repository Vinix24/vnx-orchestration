#!/usr/bin/env python3
"""vnx gate-profile: the gate profile in force and the two stacks it yields (track
gate-profiel-per-project, P7, B9-B11).

    vnx gate-profile show [--json]
    vnx gate-profile --value <field> [--csv]

``show`` prints the profile, its source, the stack the merge door asks (``request_stack``,
``gate_profile.review_stack``) and the stack ``vnx gate`` verifies (``required_stack``,
``gate_profile.required_stack``, with the ``checks.*_required`` enforcement dials), each name
with its source. ``--value`` prints one field for a shell caller: ``gate.sh`` reads
``--value required_stack --csv`` instead of keeping a second copy of the rule.

Exit codes: 0 ok, 2 the configuration is refused (an invalid ``VNX_GOVERNANCE_PROFILE`` or an
unreadable enforcement dial) or a usage error. The reason goes to stderr; nothing is guessed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _path in (SCRIPT_DIR, SCRIPT_DIR / "lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import config_registry
import gate_profile
from dispatch_spec import ReviewGateConfigError

EXIT_OK = 0
EXIT_REFUSED = 2
STACK_VALUES = ("review_stack", "required_stack")
VALUE_FIELDS = gate_profile.FIELDS + ("required_stack",)


def _stack_record(names: List[str], provenance: Dict[str, str]) -> Dict[str, Any]:
    return {"names": list(names), "provenance": dict(provenance)}


def _value_source(field: str, contributed: Any, project_id: Optional[str]) -> str:
    """Where one profile field's value comes from: the profile, else the field's own key, else none."""
    if contributed is not None:
        return "profile"
    key = gate_profile.FIELD_REGISTRY_KEYS[field]
    if key is None:
        return "none"
    return config_registry.resolve_with_source(key, project_id)[1]


def build_report(project_id: Optional[str] = None) -> Dict[str, Any]:
    """The whole ``show --json`` record, read through the module's own functions."""
    resolution = gate_profile.resolve(project_id)
    names, stack_source = gate_profile.review_stack(project_id, resolution)
    required, required_prov = gate_profile.required_stack(project_id, resolution=resolution)
    values = {}
    for field in gate_profile.FIELDS:
        contributed = gate_profile.value(field, project_id, resolution=resolution)
        values[field] = {"value": contributed, "source": _value_source(field, contributed, project_id)}
    return {
        "profile": resolution.profile, "source": resolution.source, "raw": resolution.raw, "error": None,
        "request_stack": _stack_record(names, {name: stack_source for name in names}),
        "required_stack": _stack_record(required, required_prov),
        "values": values,
    }


def error_report(exc: gate_profile.GateProfileConfigError) -> Dict[str, Any]:
    return {
        "profile": None, "source": exc.source, "raw": exc.raw, "error": str(exc),
        "request_stack": None, "required_stack": None, "values": None,
    }


def stack_difference(report: Dict[str, Any]) -> Optional[str]:
    """The two-sources warning (B10), or ``None`` when the nominal profile stack equals the required one."""
    nominal = gate_profile.PROFILE_TABLE[report["profile"]]["review_stack"]
    nominal_names = [item.strip() for item in str(nominal).split(",") if item.strip()]
    required = report["required_stack"]["names"]
    if set(nominal_names) == set(required):
        return None
    added = [name for name in required if name not in nominal_names]
    removed = [name for name in nominal_names if name not in required]
    return (
        f"WARNING: the {report['profile']} profile names {','.join(nominal_names)} but vnx gate verifies "
        f"{','.join(required)} (added: {','.join(added) or '-'}; removed: {','.join(removed) or '-'}). "
        "The checks.*_required enforcement dials outrank the profile (B10)."
    )


def _format_stack(label: str, record: Dict[str, Any]) -> List[str]:
    lines = [f"{label}: {','.join(record['names'])}"]
    lines += [f"  {name}  (source: {record['provenance'][name]})" for name in record["names"]]
    return lines


def format_report(report: Dict[str, Any]) -> str:
    lines = [f"profile: {report['profile']}  (source: {report['source']})"]
    lines += _format_stack("request_stack (the merge door asks)", report["request_stack"])
    lines += _format_stack("required_stack (vnx gate verifies)", report["required_stack"])
    for field, entry in report["values"].items():
        shown = "-" if entry["value"] is None else entry["value"]
        lines.append(f"{field}: {shown}  (source: {entry['source']})")
    warning = stack_difference(report)
    if warning:
        lines.append(warning)
    return "\n".join(lines)


def _field_value(field: str) -> str:
    if field == "required_stack":
        names, _ = gate_profile.required_stack()
        return ",".join(names)
    contributed = gate_profile.value(field)
    if contributed is None:
        return ""
    return str(contributed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx gate-profile", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", nargs="?", choices=["show"], help="show the profile and both stacks")
    parser.add_argument("--json", action="store_true", help="show: machine-readable record")
    parser.add_argument("--value", choices=VALUE_FIELDS, help="print one field")
    parser.add_argument("--csv", action="store_true", help="--value: comma-separated on one line (the only format)")
    return parser


def _refused(exc: Exception, as_json: bool) -> int:
    if isinstance(exc, gate_profile.GateProfileConfigError):
        print(f"gate-profile: {gate_profile.PROFILE_KEY}={exc.raw!r} (source: {exc.source}) is refused: "
              f"{exc.reason}", file=sys.stderr)
        if as_json:
            print(json.dumps(error_report(exc), indent=2, sort_keys=True))
    else:
        print(f"gate-profile: refused: {exc}", file=sys.stderr)
    return EXIT_REFUSED


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if (args.command is None) == (args.value is None):
        _parser().error("give exactly one of: show, --value <field>")
    try:
        if args.value is not None:
            print(_field_value(args.value))
            return EXIT_OK
        report = build_report()
    except (gate_profile.GateProfileConfigError, ReviewGateConfigError) as exc:
        return _refused(exc, args.json)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
