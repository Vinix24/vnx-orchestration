#!/usr/bin/env python3
"""vnx providers — the operator's provider switch.

    vnx providers [list] [--json] [--project <pid>]
    vnx providers off|on <family> [--project <pid>] [--reason <text>]
    vnx providers reset <family> --project <pid> [--reason <text>]
    vnx providers repair [--empty]
    vnx providers check <identifier> [--project <pid>] [--consumer <name>]

One module behind both entrances: ``bin/vnx providers`` runs this script and the pip
``vnx providers`` (``vnx_cli/commands/providers.py``) runs it as a subprocess.

Exit codes: 0 ok, 1 write failure, 2 usage (unknown family or a non-switchable class),
3 unreadable switch file, 4 refused by ``check``, 5 write refused (the caller is in a linked
worktree, carries a dispatch marker, or the switch is not enforced yet).
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import os
import plistlib
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
for _path in (SCRIPT_DIR, SCRIPT_DIR / "lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import provider_switch as ps  # noqa: E402

EXIT_OK = 0
EXIT_WRITE_FAILED = 1
EXIT_USAGE = 2
EXIT_UNREADABLE = 3
EXIT_REFUSED = ps.EXIT_PROVIDER_SWITCHED_OFF
EXIT_WRITE_REFUSED = 5

CONSUMER = "vnx providers"
APPLICATION_CODE_LINE = (
    "application code: not covered; the switch governs the VNX engine's calls only. "
    "Use `vnx providers check <identifier>` as the hook in the project's own code (OI-2048)"
)


def _home_short(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def _entry_dict(entry: Optional[ps.SwitchEntry]) -> Optional[Dict[str, Any]]:
    return dataclasses.asdict(entry) if entry is not None else None


# ---------------------------------------------------------------------------
# list: families, enforcement, projects, overrides, consequences
# ---------------------------------------------------------------------------

def family_rows(state: ps.SwitchState, project: Optional[str]) -> List[Dict[str, Any]]:
    rows = []
    for family in ps.SWITCHABLE_FAMILIES:
        overrides = {pid: _entry_dict(section[family])
                     for pid, section in sorted(state.project_switches.items()) if family in section}
        decision = ps.decide(family, consumer=CONSUMER, project_id=project)
        rows.append({
            "family": family,
            "global": _entry_dict(state.global_switches.get(family)),
            "projects": overrides,
            "effective": dataclasses.asdict(decision),
        })
    return rows


def enforcement_status() -> Dict[str, Any]:
    pending = ps.pending_launch_functions()
    return {
        "enforced": not any(pending.values()),
        "launch_functions": len(ps.LAUNCH_FUNCTIONS),
        "pending": {deliverable: list(entries) for deliverable, entries in pending.items()},
    }


def _off_families_for(state: ps.SwitchState, project_id: str) -> List[str]:
    """Families the file switches off for ``project_id`` (an unreadable file refuses them all)."""
    if not state.readable:
        return list(ps.SWITCHABLE_FAMILIES)
    off = []
    for family in ps.SWITCHABLE_FAMILIES:
        entry = state.project_switches.get(project_id, {}).get(family) or state.global_switches.get(family)
        if entry is not None and not entry.enabled:
            off.append(family)
    return off


def engine_root_for(project_root: Path) -> Tuple[Optional[Path], str]:
    """The engine a project runs: the engine link under its ``.claude`` folder, or itself when it is the fabric."""
    link = project_root / ".claude" / "vnx-system"
    if link.is_symlink():
        return link.resolve(), "linked engine"
    if (project_root / "scripts" / "lib" / "vnx_paths.py").is_file():
        return project_root, "fabric checkout"
    return None, f"no {'/'.join(('.claude', 'vnx-system'))} link and not the fabric checkout"


def engine_families(engine_root: Path) -> Optional[Tuple[str, ...]]:
    """``SWITCHABLE_FAMILIES`` as declared in that engine's provider_switch.py, parsed with ast."""
    source = engine_root / "scripts" / "lib" / "provider_switch.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "SWITCHABLE_FAMILIES" for t in targets):
            value = node.value
            if isinstance(value, (ast.Tuple, ast.List, ast.Set)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
            ):
                return tuple(e.value for e in value.elts)
    return None


def registry_path() -> Path:
    return Path.home() / ".vnx" / "projects.json"


def project_rows(state: ps.SwitchState) -> Dict[str, Any]:
    path = registry_path()
    result: Dict[str, Any] = {"registry": _home_short(path), "error": None, "projects": []}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result["error"] = "no project registry"
        return result
    except (OSError, ValueError) as exc:
        result["error"] = f"unreadable project registry: {exc}"
        return result
    entries = registry.get("projects", []) if isinstance(registry, dict) else []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        pid = entry.get("project_id") if isinstance(entry.get("project_id"), str) else "?"
        engine, source = engine_root_for(Path(entry["path"]).expanduser())
        knows = engine is not None and (engine / "scripts" / "lib" / "provider_switch.py").is_file()
        families = engine_families(engine) if knows else None
        off = _off_families_for(state, pid)
        result["projects"].append({
            "project_id": pid,
            "engine_root": _home_short(engine),
            "engine_source": source,
            "knows_switch": knows,
            "engine_families": list(families) if families is not None else None,
            "off_families": off,
            "off_families_unknown_to_engine": [f for f in off if knows and (families is None or f not in families)],
            "application_code": APPLICATION_CODE_LINE,
        })
    return result


def _override_meaning(name: str, value: str) -> str:
    family = name[len(ps.OVERRIDE_ENV_PREFIX):].lower()
    if family not in ps.SWITCHABLE_FAMILIES:
        return f"unknown family {family!r}: no effect"
    meaning = ps.interpret_override(value)
    if meaning == ps.OVERRIDE_OFF:
        return f"switches {family} off for that process"
    if meaning == ps.OVERRIDE_IGNORED:
        return f"ignored: an override only switches off; `vnx providers on {family}` re-enables"
    return f"invalid token: refuses {family} for that process"


def override_rows() -> Dict[str, Any]:
    env = {k: {"value": v, "meaning": _override_meaning(k, v)}
           for k, v in sorted(os.environ.items()) if k.startswith(ps.OVERRIDE_ENV_PREFIX)}
    launchd: Dict[str, Any] = {}
    agents = Path.home() / "Library" / "LaunchAgents"
    for plist in sorted(agents.glob("com.vnx.*.plist")) if agents.is_dir() else []:
        try:
            with plist.open("rb") as fh:
                data = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            launchd[plist.name] = {"error": f"unreadable: {exc}"}
            continue
        variables = data.get("EnvironmentVariables") if isinstance(data, dict) else None
        found = {k: {"value": str(v), "meaning": _override_meaning(k, str(v))}
                 for k, v in sorted((variables or {}).items()) if k.startswith(ps.OVERRIDE_ENV_PREFIX)}
        if found:
            launchd[plist.name] = found
    return {"environment": env, "launchd": launchd}


def _allowed(identifier: str, project: Optional[str]) -> bool:
    return ps.decide(identifier, consumer=CONSUMER, project_id=project).allowed


def _config():
    """The process's config view, wired once (``config_runtime.get`` re-wires on every read)."""
    import config_registry  # noqa: PLC0415
    import config_runtime  # noqa: PLC0415
    config_runtime.autowire()
    return config_registry


def _review_chain_consequence(project: Optional[str], config: Any) -> Optional[str]:
    from gate_request_handler import _parse_review_gate_takeover_chain  # noqa: PLC0415
    chain = _parse_review_gate_takeover_chain(config.get("VNX_REVIEW_GATE_TAKEOVER_CHAIN") or "")
    after: List[str] = []
    gate = "codex_gate"
    while gate in chain and chain[gate] not in after:
        gate = chain[gate]
        after.append(gate)
    if after and not any(_allowed(g, project) for g in after):
        return (f"no second review seat: every gate after codex_gate in the takeover chain "
                f"({', '.join(after)}) is switched off, so a PR gets codex alone")
    return None


def _tiebreaker_consequence(project: Optional[str], config: Any) -> Optional[str]:
    from plan_gate_tiebreaker import load_tiebreaker_config  # noqa: PLC0415
    provider = load_tiebreaker_config()["provider"]
    if not _allowed(provider, project):
        return f"the plan-gate tiebreaker provider {provider} is switched off: the tiebreaker will refuse"
    return None


def _panel_consequence(project: Optional[str], config: Any) -> Optional[str]:
    from plan_gate_panel import load_panel_seats  # noqa: PLC0415
    seats = load_panel_seats()
    enabled = [s["label"] for s in seats if _allowed(s["provider"], project)]
    if len(enabled) < 2:
        return (f"only {len(enabled)} of {len(seats)} plan-gate seat(s) enabled "
                f"({', '.join(enabled) or 'none'}): a plan-gate cannot certify")
    return None


def _intelligence_consequence(project: Optional[str], config: Any) -> Optional[str]:
    lines = []
    for step, provider_key, enabled_key in (
        ("tagger", "VNX_TAGGER_PROVIDER", "VNX_TAGGER_ENABLED"),
        ("scout", "VNX_SCOUT_PROVIDER", "VNX_SCOUT_PREPASS"),
    ):
        provider = (config.get(provider_key) or "deepseek").strip().lower()
        if not _allowed(provider, project):
            state = "enabled" if config.get_bool(enabled_key) else "currently disabled"
            lines.append(f"the {step} provider {provider} is switched off: {step} calls will be "
                         f"skipped ({step} {state})")
    return "\n".join(lines) or None


def consequences(project: Optional[str]) -> List[str]:
    """What the effective switches mean for reviews, panels and the intelligence steps."""
    try:
        config = _config()
    except Exception as exc:  # an unreadable fabric config must show up here, not abort the listing
        return [f"could not read the fabric config: {exc}"]
    lines: List[str] = []
    for name, compute in (("review takeover chain", _review_chain_consequence),
                          ("plan-gate tiebreaker", _tiebreaker_consequence),
                          ("plan-gate panel", _panel_consequence),
                          ("tagger/scout providers", _intelligence_consequence)):
        try:
            text = compute(project, config)
        except Exception as exc:  # a malformed config must show up here, not abort the listing
            text = f"could not evaluate the {name}: {exc}"
        if text:
            lines.extend(text.splitlines())
    return lines


def build_report(project: Optional[str]) -> Dict[str, Any]:
    state = ps.load_state()
    return {
        "switch_file": str(state.path),
        "exists": state.exists,
        "readable": state.readable,
        "error": state.error,
        "version": state.version,
        "project": project,
        "families": family_rows(state, project),
        "non_switchable": dict(ps.NON_SWITCHABLE_CLASSES),
        "enforcement": enforcement_status(),
        "projects": project_rows(state),
        "overrides": override_rows(),
        "consequences": consequences(project),
    }


def _provenance(item: Dict[str, Any]) -> str:
    """``<when> by <who>: <why>`` for whichever of the three are present, or ""."""
    head = " ".join(part for part in (
        item.get("changed_at"), f"by {item['changed_by']}" if item.get("changed_by") else None,
    ) if part)
    reason = item.get("reason")
    return f"{head}: {reason}" if head and reason else head or reason or ""


def _entry_text(entry: Optional[Dict[str, Any]]) -> str:
    if entry is None:
        return "-"
    provenance = _provenance(entry)
    return ("on" if entry["enabled"] else "OFF") + (f" {provenance}" if provenance else "")


def _render_header(report: Dict[str, Any]) -> List[str]:
    if not report["readable"]:
        out = [f"Provider switch: UNREADABLE {report['switch_file']}", f"  {report['error']}",
               "  every switchable family is refused until `vnx providers repair`"]
    elif not report["exists"]:
        out = [f"Provider switch: {report['switch_file']} (missing: no switches, everything on)"]
    else:
        out = [f"Provider switch: {report['switch_file']} (version {report['version']})"]
    out.append(f"Current project: {report['project'] or 'none (global section only)'}")
    return out


def _render_families(report: Dict[str, Any]) -> List[str]:
    out = ["Families:"]
    for row in report["families"]:
        eff = row["effective"]
        state = "on " if eff["allowed"] else "OFF"
        detail = eff["source"] + (f" {eff['scope']}" if eff["source"] == "project" else "")
        provenance = _provenance(eff)
        if provenance:
            detail += f", {provenance}"
        if eff.get("error"):
            detail += f", {eff['error']}"
        out.append(f"  {row['family']:<11} effective {state} ({detail})")
        out.append(f"  {'':<11} global: {_entry_text(row['global'])}")
        for pid, entry in row["projects"].items():
            out.append(f"  {'':<11} project {pid}: {_entry_text(entry)}")
    out.append("Never switchable:")
    out.extend(f"  {name:<11} {reason}" for name, reason in report["non_switchable"].items())
    return out


def _render_enforcement(report: Dict[str, Any]) -> List[str]:
    enf = report["enforcement"]
    if enf["enforced"]:
        return [f"Enforcement: live, all {enf['launch_functions']} launch functions call provider_switch.require"]
    pending_total = sum(len(v) for v in enf["pending"].values())
    out = [f"Enforcement: NOT enforced yet. {pending_total} of {enf['launch_functions']} launch "
           "functions do not call provider_switch.require; the write verbs refuse until they do"]
    for deliverable, entries in enf["pending"].items():
        if entries:
            out.append(f"  pending {deliverable} ({len(entries)}):")
            out.extend(f"    {e}" for e in entries)
    return out


def _render_projects(report: Dict[str, Any]) -> List[str]:
    projects = report["projects"]
    out = [f"Projects ({projects['registry']}):"]
    if projects["error"]:
        out.append(f"  {projects['error']}")
    for row in projects["projects"]:
        if row["knows_switch"]:
            out.append(f"  {row['project_id']:<18} engine knows the switch ({row['engine_source']}: {row['engine_root']})")
        else:
            where = row["engine_root"] or row["engine_source"]
            out.append(f"  {row['project_id']:<18} engine does NOT know the switch ({where}): every provider stays on there")
        for family in row["off_families_unknown_to_engine"]:
            out.append(f"  {'':<18} WARNING: {family} is off but this engine does not know the family: it keeps calling it")
        out.append(f"  {'':<18} {row['application_code']}")
    return out


def _render_overrides(report: Dict[str, Any]) -> List[str]:
    out = ["Overrides (VNX_OVERRIDE_PROVIDER_*):"]
    env = report["overrides"]["environment"]
    launchd = report["overrides"]["launchd"]
    if not env and not launchd:
        out.append("  none")
    for name, item in env.items():
        out.append(f"  environment: {name}={item['value']} ({item['meaning']})")
    for plist, items in launchd.items():
        if "error" in items:
            out.append(f"  launchd {plist}: {items['error']}")
            continue
        for name, item in items.items():
            out.append(f"  launchd {plist}: {name}={item['value']} ({item['meaning']})")
    return out


def render_text(report: Dict[str, Any]) -> str:
    sections = (_render_header(report), _render_families(report), _render_enforcement(report),
                _render_projects(report), _render_overrides(report),
                _consequence_lines(report["consequences"]))
    return "\n\n".join("\n".join(section) for section in sections)


def _consequence_lines(lines: List[str]) -> List[str]:
    return ["Consequences:", *(f"  - {line}" for line in lines)] if lines else ["Consequences: none"]


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    report = build_report(ps.resolve_project(args.project))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(report))
    return EXIT_OK if report["readable"] else EXIT_UNREADABLE


def _write_refused(exc: ps.SwitchWriteRefused) -> int:
    print("vnx providers: write refused:", file=sys.stderr)
    for reason in exc.reasons:
        print(f"  - {reason}", file=sys.stderr)
    for deliverable, entries in ps.pending_launch_functions().items():
        if entries:
            print(f"  pending {deliverable} ({len(entries)}):", file=sys.stderr)
            for entry in entries:
                print(f"    {entry}", file=sys.stderr)
    return EXIT_WRITE_REFUSED


def _unreadable(exc: ps.SwitchFileUnreadable) -> int:
    print(f"vnx providers: the switch file is unreadable: {exc}", file=sys.stderr)
    print("  run `vnx providers repair` (or `vnx providers repair --empty`)", file=sys.stderr)
    return EXIT_UNREADABLE


def _validate(family: str, project: Optional[str]) -> Optional[int]:
    try:
        ps.check_switchable(family)
        if project is not None:
            ps.check_project_id(project)
    except ValueError as exc:
        print(f"vnx providers: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return None


def cmd_switch(args: argparse.Namespace) -> int:
    enabled = args.verb == "on"
    usage = _validate(args.family, args.project)
    if usage is not None:
        return usage
    scope = args.project or ps.SCOPE_GLOBAL
    try:
        result = ps.set_switch(args.family, enabled, scope=scope, actor=ps.default_actor(), reason=args.reason)
    except ps.SwitchWriteRefused as exc:
        return _write_refused(exc)
    except ps.SwitchFileUnreadable as exc:
        return _unreadable(exc)
    except OSError as exc:
        print(f"vnx providers: write failed: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED
    where = "globally" if scope == ps.SCOPE_GLOBAL else f"for project {scope}"
    was = _entry_text(result.old) if result.old else "unset"
    print(f"{result.family} switched {'on' if enabled else 'OFF'} {where} (was {was}). "
          f"Audit: {ps.audit_log_path(result.path)}")
    print("\n".join(_consequence_lines(consequences(ps.resolve_project(args.project)))))
    return EXIT_OK


def cmd_reset(args: argparse.Namespace) -> int:
    usage = _validate(args.family, args.project)
    if usage is not None:
        return usage
    try:
        result = ps.clear_project_override(args.family, args.project, actor=ps.default_actor(), reason=args.reason)
    except ps.SwitchWriteRefused as exc:
        return _write_refused(exc)
    except ps.SwitchFileUnreadable as exc:
        return _unreadable(exc)
    except OSError as exc:
        print(f"vnx providers: write failed: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED
    if not result.changed:
        print(f"no project override for {result.family} in {result.scope}: nothing changed")
        return EXIT_OK
    print(f"{result.family} override removed for project {result.scope} (was {_entry_text(result.old)}); "
          f"it follows the global section again. Audit: {ps.audit_log_path(result.path)}")
    print("\n".join(_consequence_lines(consequences(args.project))))
    return EXIT_OK


def cmd_repair(args: argparse.Namespace) -> int:
    try:
        result = ps.repair(actor=ps.default_actor(), empty=args.empty)
    except ps.SwitchWriteRefused as exc:
        return _write_refused(exc)
    except ps.RepairNotNeeded as exc:
        print(f"vnx providers: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED
    except ps.AuditLogUnreadable as exc:
        print(f"vnx providers: cannot rebuild the state: {exc}", file=sys.stderr)
        print("  rerun with --empty to write an empty version 1 file (every family on)", file=sys.stderr)
        return EXIT_UNREADABLE
    except OSError as exc:
        print(f"vnx providers: repair failed: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED
    how = "an empty version 1 file" if args.empty else "the state replayed from the audit log"
    print(f"repaired {result.path}: the unreadable file is kept as {result.quarantined_to.name}; wrote {how}:")
    print(json.dumps(result.new, indent=2, ensure_ascii=False))
    print("\n".join(_consequence_lines(consequences(ps.resolve_project(None)))))
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    decision = ps.decide(args.identifier, consumer=args.consumer, project_id=args.project)
    if decision.allowed:
        print(decision.detail())
        return EXIT_OK
    print(decision.detail(), file=sys.stderr)
    return EXIT_REFUSED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx providers", description="Turn model providers on and off.")
    sub = parser.add_subparsers(dest="verb", metavar="VERB")

    p_list = sub.add_parser("list", help="families, enforcement, projects, overrides and consequences")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.add_argument("--project", default=None, metavar="PID", help="project for the effective state")

    for verb, text in (("off", "switch a family off"), ("on", "switch a family on")):
        p = sub.add_parser(verb, help=text)
        p.add_argument("family", metavar="FAMILY", help=", ".join(ps.SWITCHABLE_FAMILIES))
        p.add_argument("--project", default=None, metavar="PID", help="only for this project")
        p.add_argument("--reason", default="", metavar="TEXT")

    p_reset = sub.add_parser("reset", help="remove a project override")
    p_reset.add_argument("family", metavar="FAMILY")
    p_reset.add_argument("--project", required=True, metavar="PID")
    p_reset.add_argument("--reason", default="", metavar="TEXT")

    p_repair = sub.add_parser("repair", help="rebuild an unreadable switch file from the audit log")
    p_repair.add_argument("--empty", action="store_true", help="write an empty version 1 file instead")

    p_check = sub.add_parser("check", help="exit 0 when the identifier may be called, 4 when refused")
    p_check.add_argument("identifier", metavar="IDENTIFIER")
    p_check.add_argument("--project", default=None, metavar="PID")
    p_check.add_argument("--consumer", default="vnx providers check", metavar="NAME")
    return parser


_HANDLERS = {
    "list": cmd_list, "off": cmd_switch, "on": cmd_switch, "reset": cmd_reset,
    "repair": cmd_repair, "check": cmd_check,
}


def main(argv: Optional[List[str]] = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if not args_in or (args_in[0].startswith("-") and args_in[0] not in ("-h", "--help")):
        args_in = ["list", *args_in]
    args = build_parser().parse_args(args_in)
    return _HANDLERS[args.verb](args)


if __name__ == "__main__":
    sys.exit(main())
