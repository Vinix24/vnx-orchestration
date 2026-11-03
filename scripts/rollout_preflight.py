#!/usr/bin/env python3
"""rollout_preflight — can the pinned release be rolled out? Read-only.

``vnx preflight`` measures the fleet before a rollout and writes nothing: no
install, no reload, no change to a settings file, a plist or a store. Every
judgement is imported from its owner -- ``fleet_pin_audit.classify_project``
(pin), ``launchd_project_scope.check_pin_alignment`` (installed jobs), the
verify function of ``vnx_cli.commands.release`` (pinned release dir) and the
doctor check ``release:main-drift`` (a diverged main). Nothing is
re-implemented (plan risk: "PR-8 wordt een tweede pins-oordeel").

Four blocking classes per project: ``pin`` (a dead pin, or a pinned release
dir that fails verify), ``launchd`` (any ``check_pin_alignment`` violation,
state name kept literally), ``hooks`` (hook commands a ``regen_settings.sh
--dry-run`` render would change, old and new named; a failed dry-run is a
finding) and ``main_drift`` (the doctor's check; outside a git checkout its
skip is printed, never a finding). An unmeasured class is never a pass: a
project without a pin reports ``pin`` and ``launchd`` as ``unmeasured`` (it
does not itself block). Shim-routed project hooks are one ``info`` line.

Exit: 0 no finding, 1 at least one, 2 a measurement itself failed.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
# The IMPORT root is the directory that holds ``vnx_cli/``: the repo root in a
# checkout, ``site-packages`` in a wheel (``vnx_cli`` ships as a top-level
# package). It is put on ``sys.path`` only -- it is not the engine root. The
# file check keeps an unrelated ``vnx_cli`` off the path.
for _candidate in (_HERE.parent, _HERE.parent.parent):
    if (_candidate / "vnx_cli" / "_reexec.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break
for _path in (_HERE / "lib", _HERE, _HERE / "launchd"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import fleet_pin_audit
import hook_coverage
import launchd_project_scope
import pin_drift
from vnx_cli._engine import engine_root
from vnx_cli.commands import doctor, release

# The ENGINE root is where ``scripts/commands/`` lives. Its owner resolves it
# (packaged installs keep the engine under ``vnx_orchestration/``, a sibling of
# ``vnx_cli``), never a probe copied here: in a wheel the import root above is
# site-packages, and building engine paths under it would look for a
# ``scripts/commands/regen_settings.sh`` that does not exist. Every engine path
# in this module builds on this value.
_ENGINE_ROOT = engine_root()

BLOCKING_CLASSES = ("pin", "launchd", "hooks", "main_drift")
REPORT_CLASSES = BLOCKING_CLASSES + ("unmeasured", "info")
UNMEASURED_WITHOUT_PIN = ("launchd", "pin")
DEFAULT_REGISTRY = Path.home() / ".vnx" / "projects.json"
DEFAULT_STORE_ROOT = Path.home() / ".vnx-system"
DEFAULT_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
REGEN_TIMEOUT_SECONDS = 120
SHIM_HOME_MARKER = hook_coverage.DEFAULT_VNX_HOME.replace("$HOME/", "")

class MeasurementError(Exception):
    """A measurement could not be made; the caller exits 2, never 0."""

# ── hooks ──────────────────────────────────────────────────────────────────
def _hook_commands(settings: Optional[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """Every hook command, in settings order, with its event and matcher."""
    commands: List[Dict[str, str]] = []
    hooks = settings.get("hooks") if isinstance(settings, Mapping) else None
    if not isinstance(hooks, Mapping):
        return commands
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, Mapping):
                continue
            handlers = entry.get("hooks")
            for handler in handlers if isinstance(handlers, list) else ():
                if isinstance(handler, Mapping) and isinstance(handler.get("command"), str):
                    commands.append({"event": str(event),
                                     "matcher": str(entry.get("matcher", "")),
                                     "command": handler["command"]})
    return commands

def _change(event: Any, matcher: Any, old: Optional[str], new: Optional[str]) -> Dict[str, Any]:
    return {"state": "changed", "event": "" if event is None else str(event),
            "matcher": "" if matcher is None else str(matcher), "old": old, "new": new}

def _report_findings(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The merge engine's own replaced/added pairs, one finding each."""
    findings: List[Dict[str, Any]] = []
    for event, per_event in report.items():
        if not isinstance(per_event, Mapping):
            continue
        for replaced in per_event.get("replaced") or []:
            old, new = replaced["old_command"], replaced["new_command"]
            if old != new:
                findings.append(_change(event, replaced.get("matcher"), old, new))
        for added in per_event.get("added") or []:
            findings.append(_change(event, added.get("matcher"), None, added["command"]))
    return findings

def _set_findings(old: Optional[Mapping[str, Any]],
                  rendered: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Order-insensitive fallback for a render that carries no hook report.

    A render from a tree older than the report still carries the merged hooks;
    a multiset comparison never reports a hook that merely moved its slot.
    """
    before = Counter((i["event"], i["matcher"], i["command"]) for i in _hook_commands(old))
    after = Counter((i["event"], i["matcher"], i["command"]) for i in _hook_commands(rendered))
    findings = [_change(e, m, c, None) for e, m, c in sorted((before - after).elements())]
    findings += [_change(e, m, None, c) for e, m, c in sorted((after - before).elements())]
    return findings

def hook_findings(old: Optional[Mapping[str, Any]],
                  rendered: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Every hook command the render would change, old and new named.

    A project without settings.json gets the render's whole hook set written by
    a real rollout. With a file, the merge engine's ``_vnx_hook_report`` is the
    authority -- no second judgement of which handlers are VNX-owned.
    """
    if old is None:
        return [_change(i["event"], i["matcher"], None, i["command"])
                for i in _hook_commands(rendered)]
    report = rendered.get("_vnx_hook_report") if isinstance(rendered, Mapping) else None
    return _report_findings(report) if isinstance(report, Mapping) else _set_findings(old, rendered)

def read_settings(root: Path) -> Optional[Dict[str, Any]]:
    """The project's settings payload; ``None`` when the file does not exist.

    An existing but unreadable/unparseable file raises: the caller turns that
    into a hooks finding, never into "no hooks to compare".
    """
    path = root / ".claude" / "settings.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a JSON object")
    return payload

def parse_dry_run(stdout: str) -> Optional[Dict[str, Any]]:
    """The first JSON object a ``--dry-run --json`` run printed, else ``None``."""
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(stdout[start:])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None

def run_regen_settings_dry_run(project_root: Path,
                               engine_root: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Render the project from its pin without writing: the real wrapper.

    ``log``/``err`` go to stderr so stdout carries the merge engine's JSON
    (``bin/vnx`` sends ``log`` to stdout; the render itself is unchanged). A
    missing wrapper is a failed dry-run, never an exception.
    """
    tree = Path(engine_root) if engine_root is not None else _ENGINE_ROOT
    wrapper = tree / "scripts" / "commands" / "regen_settings.sh"
    if not wrapper.is_file():
        return subprocess.CompletedProcess(
            ["bash"], 127, "", f"regen-settings wrapper not found: {wrapper}")
    program = (
        "set -euo pipefail\n"
        f'source "{wrapper}"\n'
        "err() { printf '%s\\n' \"$*\" >&2; }\n"
        "log() { printf '%s\\n' \"$*\" >&2; }\n"
        'PROJECT_ROOT="$1"\n'
        f'VNX_HOME="{tree}"\n'
        "cmd_regen_settings --merge --dry-run --json\n"
    )
    return subprocess.run(["bash", "-c", program, "preflight", str(project_root)],
                          capture_output=True, text=True,
                          timeout=REGEN_TIMEOUT_SECONDS, check=False)

def _shim_markers(settings: Optional[Mapping[str, Any]]) -> List[str]:
    """Which shim routes the project's hook commands use (one marker per route)."""
    markers: List[str] = []
    for item in _hook_commands(settings):
        command = item["command"]
        if SHIM_HOME_MARKER in command and SHIM_HOME_MARKER not in markers:
            markers.append(SHIM_HOME_MARKER)
        if hook_coverage.SHIM_SUFFIX in command and hook_coverage.SHIM_NAME not in markers:
            markers.append(hook_coverage.SHIM_NAME)
    return markers

def hooks_findings(root: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Hook commands a render from the pin would change, plus the shim routes."""
    try:
        settings = read_settings(root)
    except (OSError, ValueError) as exc:
        return [{"state": "unreadable",
                 "detail": f".claude/settings.json cannot be read: {exc}"}], []
    try:
        proc = run_regen_settings_dry_run(root)
    except (OSError, subprocess.SubprocessError) as exc:
        return [{"state": "dry-run-failed",
                 "detail": f"regen_settings.sh --dry-run could not run: {exc}"}], _shim_markers(settings)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return [{"state": "dry-run-failed",
                 "detail": f"regen_settings.sh --dry-run failed (exit {proc.returncode}): "
                           f"{detail}"}], _shim_markers(settings)
    rendered = parse_dry_run(proc.stdout)
    if rendered is None:
        return [{"state": "dry-run-failed",
                 "detail": "regen_settings.sh --dry-run printed no settings JSON"}], _shim_markers(settings)
    return hook_findings(settings, rendered), _shim_markers(settings)

# ── pin, launchd, main drift ───────────────────────────────────────────────
def verify_release(store_root: Path, version: str) -> Optional[str]:
    """Run ``vnx release verify <version>`` on this store; the failure text or None.

    ``VNX_HOME_ROOT`` is set from the explicit store argument for the duration
    of the call and restored, so the judgement is made on the store this
    command was asked about.
    """
    saved = os.environ.get("VNX_HOME_ROOT")
    os.environ["VNX_HOME_ROOT"] = str(store_root)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = release.vnx_release_verify(
                argparse.Namespace(version=version, repo=str(_ENGINE_ROOT)))
    finally:
        if saved is None:
            os.environ.pop("VNX_HOME_ROOT", None)
        else:
            os.environ["VNX_HOME_ROOT"] = saved
    if code == 0:
        return None
    message = err.getvalue().strip() or out.getvalue().strip()
    return message.splitlines()[-1] if message else f"vnx release verify {version} exited {code}"

def pin_findings(row: Mapping[str, Any], store_root: Path,
                 versions_dir: Path) -> List[Dict[str, Any]]:
    """A dead pin or an unverifiable pinned release dir, judged by the two owners.

    A pin and its release dir need not spell the version the same way: a pin is
    commonly written ``1.0.0`` while the dir is ``versions/v1.0.0`` (OI-2270).
    The installed dir is therefore resolved first (``pin_drift`` owns the three
    spellings) and it is that dir's own NAME the verifier is asked about -- a
    raw pin would make it look for a dir that does not exist and report a valid
    pin as unverifiable. ``pinned_release_dir`` answers ``None`` only when no
    installed release responds to the pin; that stays the ``dead`` finding and
    never becomes a ``verify`` one.
    """
    pin = row.get("pin")
    if pin is None:
        return []
    installed = pin_drift.pinned_release_dir(versions_dir, pin)
    if row.get("verdict") == fleet_pin_audit.DEAD or installed is None:
        detail = (f"pin {pin!r} names no installed release dir under {versions_dir}"
                  if installed is None else
                  f"pin {pin!r} is installed at {installed}, but the project's engine "
                  "link dangles or points outside the store")
        return [{"state": "dead", "pin": pin, "detail": detail}]
    problem = verify_release(store_root, installed.name)
    return [] if problem is None else [
        {"state": "verify", "pin": pin,
         "detail": f"pin {pin!r}: vnx release verify {installed.name} failed: {problem}"}]

def launchd_findings(alignment: Mapping[str, Any], project_id: str) -> List[Dict[str, Any]]:
    """Every ``check_pin_alignment`` violation attributed to this project."""
    return [{"state": violation["kind"], "label": violation["label"],
             "plist": violation["plist"], "detail": violation["detail"]}
            for violation in alignment["violations"]
            if violation["project_id"] == project_id]

def main_drift_findings(root: Path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """The doctor's ``release:main-drift`` verdict: a finding, or its skip line."""
    check = doctor._check_release_main_drift(root)
    if check.status == doctor.PASS:
        return ([], check.detail) if check.detail.startswith("skipped:") else ([], None)
    # Any status that is not the documented PASS blocks, including an unknown
    # one this doctor does not know: fail closed.
    return [{"state": "diverged", "check": "release:main-drift",
             "detail": check.detail}], None

# ── measurement ────────────────────────────────────────────────────────────
def _project_entry(project_id: str, root: Path, store_root: Path, versions_dir: Path,
                   alignment: Mapping[str, Any], current: Optional[str]) -> Dict[str, Any]:
    row = fleet_pin_audit.classify_project(root, store_root, current)
    hooks, markers = hooks_findings(root)
    drift, skipped = main_drift_findings(root)
    return {
        "root": str(root),
        "pin": pin_findings(row, store_root, versions_dir),
        "launchd": launchd_findings(alignment, project_id),
        "hooks": hooks,
        "main_drift": drift,
        "unmeasured": [] if row.get("pin") is not None else list(UNMEASURED_WITHOUT_PIN),
        "info": ([f"project hooks route through the shim ({', '.join(markers)})"]
                 if markers else []),
        "_skipped": skipped,
    }

def measure(selected: Mapping[str, Path], fleet: Mapping[str, Path], *,
            store_root: Path, plist_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Measure the selected projects, judging launchd against the full fleet."""
    versions_dir = store_root / "versions"
    try:
        list(versions_dir.iterdir())
    except OSError as exc:
        raise MeasurementError(
            f"store versions/ not readable: {versions_dir} ({exc})") from exc
    try:
        alignment = launchd_project_scope.check_pin_alignment(plist_dir, dict(fleet), versions_dir)
    except Exception as exc:
        raise MeasurementError(
            f"check_pin_alignment failed: {type(exc).__name__}: {exc}") from exc
    current = pin_drift.current_version(store_root)
    projects: Dict[str, Dict[str, Any]] = {}
    for project_id, root in selected.items():
        try:
            projects[project_id] = _project_entry(
                project_id, root, store_root, versions_dir, alignment, current)
        except MeasurementError:
            raise
        except Exception as exc:
            raise MeasurementError(
                f"measuring {project_id!r} failed: {type(exc).__name__}: {exc}") from exc
    return projects

def _resolve_fleet(entries: Sequence[Mapping[str, Any]],
                   wanted: Sequence[str]) -> Tuple[Dict[str, Path], Dict[str, Path]]:
    """``(project_id -> root, selected)``; an exact ``project_id`` wins, as in launchd.

    An exact id is resolved first. The aliases -- the registry ``name`` and the
    directory basename -- are consulted only for a wanted name that is not an
    exact id, so an alias equal to another project's id can never shadow that
    id. An alias two projects carry is refused, naming both: it is never
    resolved to whichever project happened to be registered first. An unknown
    ``--project`` name is a measurement error, never a shrunken fleet.
    """
    fleet: Dict[str, Path] = {}
    alias_owners: Dict[str, List[str]] = {}
    for entry in entries:
        root = Path(entry["path"]).expanduser()
        project_id = str(entry.get("project_id") or entry.get("name") or root.name)
        if project_id in fleet:
            raise MeasurementError(f"duplicate project id {project_id!r} in the registry")
        fleet[project_id] = root
        for alias in (entry.get("name"), root.name):
            if not alias:
                continue
            owners = alias_owners.setdefault(str(alias), [])
            if project_id not in owners:
                owners.append(project_id)
    if not wanted:
        return fleet, dict(fleet)
    selected: Dict[str, Path] = {}
    unknown: List[str] = []
    for name in wanted:
        if name in fleet:
            selected[name] = fleet[name]
            continue
        owners = alias_owners.get(name) or []
        if len(owners) > 1:
            raise MeasurementError(
                f"ambiguous project name {name!r}: carried by "
                f"{', '.join(repr(owner) for owner in owners)}")
        if not owners:
            unknown.append(name)
            continue
        selected[owners[0]] = fleet[owners[0]]
    if unknown:
        raise MeasurementError(f"unknown project id(s): {', '.join(unknown)}")
    return fleet, selected

def summarize(projects: Mapping[str, Mapping[str, Any]]) -> Dict[str, int]:
    summary = {"projects": len(projects)}
    for name in REPORT_CLASSES:
        summary[name] = sum(len(entry[name]) for entry in projects.values())
    return summary

def public_projects(projects: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The JSON view of each project: exactly the six classes decision 5 names."""
    return {project_id: {name: entry[name] for name in REPORT_CLASSES}
            for project_id, entry in projects.items()}

# ── output ─────────────────────────────────────────────────────────────────
def _hook_line(finding: Mapping[str, Any]) -> str:
    if finding["state"] != "changed":
        return f"  hooks: {finding['detail']}"
    old = finding["old"] if finding["old"] is not None else "(none)"
    new = finding["new"] if finding["new"] is not None else "(none)"
    return f"  hooks: {finding['event']} [{finding['matcher']}] {old} -> {new}"

def _project_lines(project_id: str, entry: Mapping[str, Any]) -> List[str]:
    lines = [f"project: {project_id}  ({entry['root']})"]
    lines += [f"  pin: {finding['state']}: {finding['detail']}" for finding in entry["pin"]]
    lines += [f"  launchd: {finding['state']} {finding['label']} "
              f"({finding['plist']}): {finding['detail']}" for finding in entry["launchd"]]
    lines += [_hook_line(finding) for finding in entry["hooks"]]
    lines += [f"  main-drift: vnx doctor check {finding['check']}: {finding['detail']}"
              for finding in entry["main_drift"]]
    if entry["_skipped"]:
        lines.append(f"  main-drift: {entry['_skipped']}")
    if entry["unmeasured"]:
        lines.append(f"  unmeasured: {', '.join(entry['unmeasured'])}")
    lines += [f"  info: {info}" for info in entry["info"]]
    if not any(entry[name] for name in REPORT_CLASSES) and not entry["_skipped"]:
        lines.append("  ok: no findings")
    return lines

def render_text(projects: Mapping[str, Mapping[str, Any]]) -> str:
    """One block per project, one line per finding, then the counts per class."""
    summary = summarize(projects)
    lines = [line for project_id, entry in projects.items()
             for line in _project_lines(project_id, entry)]
    counts = ", ".join(f"{name.replace('_', '-')} {summary[name]}" for name in REPORT_CLASSES)
    lines.append(f"summary: {summary['projects']} projects, {counts}")
    return "\n".join(lines)

def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vnx preflight",
        description="Measure whether the pinned release can be rolled out (read-only).")
    parser.add_argument("--project", action="append", default=[], metavar="ID",
                        help="only this project (repeatable); an unknown id exits 2")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    return parser.parse_args(argv)

def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        entries = fleet_pin_audit._targets(
            argparse.Namespace(project_dir=[], registry=DEFAULT_REGISTRY))
        fleet, selected = _resolve_fleet(entries, args.project)
    except (OSError, ValueError, KeyError, MeasurementError) as exc:
        print(f"vnx preflight: {exc}", file=sys.stderr)
        return 2
    try:
        projects = measure(selected, fleet, store_root=DEFAULT_STORE_ROOT.expanduser(),
                           plist_dir=DEFAULT_PLIST_DIR.expanduser())
    except MeasurementError as exc:
        print(f"vnx preflight: {exc}", file=sys.stderr)
        return 2
    blocked = any(entry[name] for entry in projects.values() for name in BLOCKING_CLASSES)
    code = 1 if blocked else 0
    if args.json:
        print(json.dumps({"projects": public_projects(projects),
                          "summary": summarize(projects), "exit_code": code}, indent=2))
    else:
        print(render_text(projects))
    return code

if __name__ == "__main__":
    sys.exit(main())
