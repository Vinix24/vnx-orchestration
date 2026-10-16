#!/usr/bin/env python3
"""guard_check — `vnx guard check`: is the T0 context guard in place for every VNX project?

Plan t0-guard-gebruikersniveau, § "User-level registration". Read-only: it opens settings files
and the project registry and writes nothing. The report has two layers.

PER ACCOUNT (``~/.claude`` and every ``~/.claude-*`` account): the three user-level entries, the
shim and the guard in ``~/.vnx-system/current``, and the root the user-owned registry trusts to
run repo code (shim step 2), including whether that first ``vnx-dev`` entry exists on disk.

PER PROJECT. The inventory is the registry ``~/.vnx/projects.json`` plus every ``.vnx-project-id``
under each ``--scan-root`` (depth 3, ``.vnx-data`` skipped); a scan hit the registry does not
list is marked. A project without ``.claude/terminals/T0/CLAUDE.md`` is ``not guarded (no T0)``.
For a project with one, each event is answered by ``hook_coverage.coverage`` (the function the
guard itself uses), PreToolUse once per tool of the user matcher:

  ``project (direct|shim)``  the project's own settings run the guard; the user level stays silent
  ``user``                   nothing in the project covers and this account's entry is in place
  ``missing``                nothing in the project covers and this account has no entry
  ``unknown``                a settings file of the project cannot be loaded; the guard counts it
                             as not covered

``check`` exits 0 when every project with a T0 folder is ``user`` or ``project`` on every event
in every account, and the shim and guard exist; otherwise 1.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import guard_install as gi
import hook_coverage as hc

SCAN_DEPTH = 3
SKIPPED_DIRS = frozenset({".vnx-data", ".git"})
PROJECT_ID_FILE = ".vnx-project-id"
T0_MARKER = Path(".claude") / "terminals" / "T0" / "CLAUDE.md"
NO_T0 = "not guarded (no T0)"


@dataclass
class ProjectEntry:
    path: Path
    project_id: Optional[str]
    in_registry: bool
    has_t0: bool


@dataclass
class AccountReport:
    config_dir: Path
    settings_state: str  # "ok" or why the file cannot be read
    entries: Dict[str, str] = field(default_factory=dict)
    trusted_root: str = ""
    # project path -> "Event[:Tool]" -> state text
    coverage: Dict[str, Dict[str, str]] = field(default_factory=dict)


@dataclass
class CheckReport:
    home: Path
    problems: List[str] = field(default_factory=list)
    accounts: List[AccountReport] = field(default_factory=list)
    projects: List[ProjectEntry] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# ── inventory ────────────────────────────────────────────────────────────────


def registry_projects(home: Path) -> List[Path]:
    """Absolute project paths listed in ``~/.vnx/projects.json``; unreadable means none."""
    try:
        with open(home / ".vnx" / "projects.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    entries = data.get("projects") if isinstance(data, dict) else None
    paths = []
    for entry in entries if isinstance(entries, list) else ():
        path = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(path, str) and os.path.isabs(path):
            paths.append(Path(path))
    return paths


def scan_projects(root: Path, depth: int = SCAN_DEPTH) -> List[Path]:
    """Directories holding a ``.vnx-project-id`` at most ``depth`` levels below ``root``."""
    found: List[Path] = []

    def walk(directory: Path, level: int) -> None:
        if (directory / PROJECT_ID_FILE).is_file():
            found.append(directory)
        if level >= depth:
            return
        try:
            children = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for child in children:
            if child.name not in SKIPPED_DIRS and child.is_dir(follow_symlinks=False):
                walk(Path(child.path), level + 1)

    walk(root, 0)
    return found


def build_inventory(home: Path, scan_roots: Sequence[Path]) -> List[ProjectEntry]:
    registered = {os.path.realpath(p) for p in registry_projects(home)}
    ordered: List[Path] = list(registry_projects(home))
    for root in scan_roots:
        ordered.extend(scan_projects(root))
    seen: set = set()
    inventory: List[ProjectEntry] = []
    for path in ordered:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        inventory.append(ProjectEntry(Path(real), hc.project_id(real), real in registered,
                                      (Path(real) / T0_MARKER).is_file()))
    return inventory


# ── per account ──────────────────────────────────────────────────────────────


def trusted_root_text(env: Mapping[str, str]) -> str:
    root = hc.registered_fabric_root(env)
    if root is None:
        return ("none: no usable first vnx-dev entry in the registry, so every project runs "
                "~/.vnx-system/current")
    if not os.path.isdir(root):
        return (f"{root} (this path does not exist on disk: the guard falls back to "
                "~/.vnx-system/current for every project)")
    return root


def _coverage_text(project: Path, event: str, tool: Optional[str], installed: bool,
                   env: Mapping[str, str]) -> str:
    cov = hc.coverage(project, event, tool, env=env)
    if cov.covered:
        return f"project ({cov.form})"
    if cov.state == hc.UNKNOWN:
        return "unknown"
    return "user" if installed else "missing"


def _event_keys() -> List[tuple]:
    keys = []
    for event, _ in gi.GUARD_EVENTS:
        if event == "PreToolUse":
            keys.extend((event, tool) for tool in gi.USER_TOOLS)
        else:
            keys.append((event, None))
    return keys


def account_report(config_dir: Path, projects: Sequence[ProjectEntry],
                   env: Mapping[str, str]) -> AccountReport:
    report = AccountReport(config_dir, "ok", trusted_root=trusted_root_text(env))
    path = gi.settings_path(config_dir)
    try:
        _, settings = gi._read_settings(path)
    except gi.GuardInstallError as exc:
        report.settings_state = str(exc)
        settings = {}
    report.entries = gi.entry_states(settings)
    for project in projects:
        if not project.has_t0:
            continue
        rows: Dict[str, str] = {}
        for event, tool in _event_keys():
            label = event if tool is None else f"{event}:{tool}"
            installed = report.entries.get(event) == gi.STATE_OK
            rows[label] = _coverage_text(project.path, event, tool, installed, env)
        report.coverage[str(project.path)] = rows
    return report


def run_check(*, home: Path, config_dirs: Sequence[str] = (), scan_roots: Sequence[str] = (),
              env: Optional[Mapping[str, str]] = None) -> CheckReport:
    mapping = dict(os.environ if env is None else env)
    mapping["HOME"] = str(home)
    report = CheckReport(home)
    report.problems.extend(gi.preflight(home))
    try:
        accounts = gi.resolve_accounts(home, list(config_dirs))
    except gi.GuardInstallError as exc:
        report.problems.append(str(exc))
        accounts = []
    if not accounts:
        report.problems.append(f"no Claude Code account found under {home}")
    report.projects = build_inventory(home, [Path(r).expanduser() for r in scan_roots])
    for account in accounts:
        acc = account_report(account, report.projects, mapping)
        report.accounts.append(acc)
        report.problems.extend(_account_problems(acc))
    return report


def _account_problems(acc: AccountReport) -> List[str]:
    problems = []
    if acc.settings_state != "ok":
        problems.append(f"{acc.config_dir}: {acc.settings_state}")
    for event, state in acc.entries.items():
        if state != gi.STATE_OK:
            problems.append(f"{acc.config_dir}: {event} entry is {state}")
    for project, rows in acc.coverage.items():
        bad = sorted(label for label, text in rows.items() if text in ("missing", "unknown"))
        if bad:
            problems.append(f"{acc.config_dir}: {project} is not covered on {', '.join(bad)}")
    return problems


# ── output ───────────────────────────────────────────────────────────────────


def render_report(report: CheckReport) -> str:
    lines: List[str] = []
    for acc in report.accounts:
        lines.append(f"Account {acc.config_dir}")
        if acc.settings_state != "ok":
            lines.append(f"  settings: {acc.settings_state}")
        for event, state in acc.entries.items():
            lines.append(f"  user entry {event}: {state}")
        lines.append(f"  trusted repo root: {acc.trusted_root}")
    lines.extend(_shim_lines(report.home))
    lines.append("Projects")
    for project in report.projects:
        lines.extend(_project_lines(report, project))
    if not report.projects:
        lines.append("  (none in the registry or under a --scan-root)")
    lines.append("OK" if report.ok else "NOT OK")
    lines.extend(f"  - {problem}" for problem in report.problems)
    return "\n".join(lines) + "\n"


def _shim_lines(home: Path) -> List[str]:
    hooks = gi.current_root(home) / "scripts" / "hooks"
    return [f"{name} in {gi.CURRENT_REL.as_posix()}: "
            f"{'present' if (hooks / name).is_file() else 'MISSING ' + str(hooks / name)}"
            for name in (hc.SHIM_NAME, hc.GUARD_ENTRY)]


def _project_lines(report: CheckReport, project: ProjectEntry) -> List[str]:
    tag = f"{project.project_id or 'no id'}"
    note = "" if project.in_registry else ", found by scan, not in the registry"
    lines = [f"  {project.path} [{tag}{note}]"]
    if not project.has_t0:
        lines.append(f"    {NO_T0}")
        return lines
    for acc in report.accounts:
        lines.append(f"    {acc.config_dir.name}:")
        for label, text in acc.coverage.get(str(project.path), {}).items():
            lines.append(f"      {label}: {text}")
    return lines


def run_check_command(*, home: Path, config_dirs: Sequence[str], scan_roots: Sequence[str],
                      out) -> int:
    report = run_check(home=home, config_dirs=config_dirs, scan_roots=scan_roots)
    out.write(render_report(report))
    return 0 if report.ok else 1
