#!/usr/bin/env python3
"""vnx gate-profile: the gate profile in force and the two stacks it yields (track
gate-profiel-per-project, P7, B9-B11).

    vnx gate-profile show [--json] [--project-id <pid>]
    vnx gate-profile --value <field> [--csv] [--project-id <pid>]

``show`` prints the profile, its source, the stack the merge door asks (``request_stack``,
``gate_profile.review_stack``) and the stack ``vnx gate`` verifies (``required_stack``,
``gate_profile.required_stack``, with the ``checks.*_required`` enforcement dials), each name
with its source. ``--value`` prints one field for a shell caller: ``gate.sh`` reads
``--value required_stack --csv`` instead of keeping a second copy of the rule.

``--project-id`` reads that project's profile instead of the process's; ``show`` names the project it
read (``project_id``). The command is read-only.

Exit codes: 0 ok, 2 the configuration is refused (an invalid ``VNX_GOVERNANCE_PROFILE`` or an
unreadable enforcement dial) or a usage error. The reason goes to stderr; nothing is guessed. With
``--json`` every config error also prints a JSON error record on stdout.
"""
from __future__ import annotations

import argparse
import json
import os
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
from vnx_ids import PROJECT_ID_RE

EXIT_OK = 0
EXIT_REFUSED = 2
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


def _resolved_project_id(project_id: Optional[str]) -> Optional[str]:
    """The id the readers used: the argument, else the process's project (wired by ``resolve``)."""
    if project_id:
        return project_id
    return config_registry._default_project_id or os.environ.get("VNX_PROJECT_ID") or None


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
        "project_id": _resolved_project_id(project_id),
        "profile": resolution.profile, "source": resolution.source, "raw": resolution.raw, "error": None,
        "request_stack": _stack_record(names, {name: stack_source for name in names}),
        "required_stack": _stack_record(required, required_prov),
        "values": values,
    }


def error_report(exc: ReviewGateConfigError, project_id: Optional[str] = None) -> Dict[str, Any]:
    """The ``--json`` error record. A non-profile config error has no raw value or source: both ``None``."""
    return {
        "project_id": project_id,
        "profile": None, "source": getattr(exc, "source", None), "raw": getattr(exc, "raw", None),
        "error": str(exc),
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
    lines = [f"project_id: {report['project_id']}"]
    lines += [f"profile: {report['profile']}  (source: {report['source']})"]
    lines += _format_stack("request_stack (the merge door asks)", report["request_stack"])
    lines += _format_stack("required_stack (vnx gate verifies)", report["required_stack"])
    for field, entry in report["values"].items():
        shown = "-" if entry["value"] is None else entry["value"]
        lines.append(f"{field}: {shown}  (source: {entry['source']})")
    warning = stack_difference(report)
    if warning:
        lines.append(warning)
    return "\n".join(lines)


def _field_value(field: str, project_id: Optional[str] = None) -> str:
    if field == "required_stack":
        names, _ = gate_profile.required_stack(project_id)
        return ",".join(names)
    contributed = gate_profile.value(field, project_id)
    if contributed is None:
        return ""
    return str(contributed)


def _project_id_arg(raw: str) -> str:
    if not PROJECT_ID_RE.match(raw):
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not a project id (lowercase letters, digits and '-', 2 to 32 characters, starting with a letter)")
    return raw


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx gate-profile", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", nargs="?", choices=["show"], help="show the profile and both stacks")
    parser.add_argument("--json", action="store_true", help="show: machine-readable record")
    parser.add_argument("--value", choices=VALUE_FIELDS, help="print one field")
    parser.add_argument("--project-id", type=_project_id_arg, default=None,
                        help="read this project's profile instead of the process's")
    parser.add_argument("--csv", action="store_true",
                        help="--value: accepted for gate.sh only; comma-separated on one line is the default "
                             "and only format, so this flag changes nothing")
    return parser


def _refused(exc: ReviewGateConfigError, as_json: bool, project_id: Optional[str]) -> int:
    if isinstance(exc, gate_profile.GateProfileConfigError):
        print(f"gate-profile: {gate_profile.PROFILE_KEY}={exc.raw!r} (source: {exc.source}) is refused: "
              f"{exc.reason}", file=sys.stderr)
    else:
        print(f"gate-profile: refused: {exc}", file=sys.stderr)
    if as_json:
        print(json.dumps(error_report(exc, project_id), indent=2, sort_keys=True))
    return EXIT_REFUSED


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if (args.command is None) == (args.value is None):
        _parser().error("give exactly one of: show, --value <field>")
    try:
        if args.value is not None:
            print(_field_value(args.value, args.project_id))
            return EXIT_OK
        report = build_report(args.project_id)
    except (gate_profile.GateProfileConfigError, ReviewGateConfigError) as exc:
        return _refused(exc, args.json, args.project_id)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
