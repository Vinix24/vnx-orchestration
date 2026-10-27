#!/usr/bin/env python3
"""guard_check — `vnx guard check`: is the T0 context guard in place for every VNX project?

Plan t0-guard-gebruikersniveau, § "User-level registration". Read-only: it opens settings files
and the project registry and writes nothing. The report has two layers.

PER ACCOUNT (``~/.claude`` and every ``~/.claude-*`` account): the three user-level entries, the
shim and the guard in ``~/.vnx-system/current``, and the root the user-owned registry trusts to
run repo code (shim step 2), including whether that first ``vnx-dev`` entry exists on disk. An
account whose ``settings.json`` cannot be read, is not a JSON object or holds a malformed
``hooks`` tree is ``unknown``, with the reason (for the tree: the path to the bad node).

PER PROJECT. The inventory is the registry ``~/.vnx/projects.json`` plus every folder holding a
``.vnx-project-id`` OR a ``.claude/terminals/T0/CLAUDE.md`` under each ``--scan-root`` (depth 3,
``.vnx-data`` skipped); a scan hit the registry does not list is marked. Without ``--scan-root``
the report says so in a ``scope:`` line, with the number of projects it did see. A project
without ``.claude/terminals/T0/CLAUDE.md`` is ``not guarded (no T0)``. A T0 folder with no usable
``.vnx-project-id`` is ``not guardable: no .vnx-project-id``: rule 1 turns the shim away there,
no user-level entry can run, and it awaits an operator decision. It is listed and counted in one
closing line, and never changes the exit code (rule 1 itself is unchanged). A project with a T0
that fails the rest of the shim's rule 1 (``hook_coverage.t0_project_root``: a
``.vnx-project-id`` with a first line, a ``.git`` entry at or above, not a dispatch worktree) is
``not guarded (<reason>)`` and makes ``check`` exit 1: the installed command exits there without
running the guard. For a project that passes, each event is answered by ``hook_coverage.coverage`` (the function the
guard itself uses), PreToolUse once per tool of the user matcher. ``coverage`` runs with the
environment of the account being reported (``account_env``), because what Claude Code records
per project (the trust decision) lives per account:

  ``project (direct|shim)``  the project's own settings run the guard; the user level stays silent
  ``user``                   nothing in the project covers and this account's entry is in place
  ``missing``                nothing in the project covers and this account has no entry
  ``duplicate``              this account has more than one entry of ours in the event
  ``unknown``                a settings file of the project cannot be loaded (the guard counts it
                             as not covered), or this account's own settings are ``unknown``

``--require-user`` makes the user level the only registration that counts: for every project
that passes rule 1, an event answered by the project's own settings (``project (...)``) is a
failure, named per project and per event as ``covered by the project only, not at user level``.
Without the flag the behaviour and the exit code are those described below.

``check`` exits 0 when every project with a T0 folder is ``user`` or ``project`` on every event
in every account, and the shim and guard exist; otherwise 1. A usage error is argparse's exit 2.
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
NOT_GUARDED = "not guarded"
NOT_GUARDABLE = "not guardable"
NO_PROJECT_ID = f"no {PROJECT_ID_FILE}"
PROJECT_ONLY = "covered by the project only, not at user level"
ALL_EVENTS = "all events"


@dataclass
class ProjectEntry:
    path: Path
    project_id: Optional[str]
    in_registry: bool
    has_t0: bool
    # why the shim's rule 1 turns this project away (None: the shim runs the guard here)
    rule1_reason: Optional[str] = None
    # why this T0 folder cannot be guarded at all (no usable .vnx-project-id), or None
    not_guardable: Optional[str] = None


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
    scan_roots: List[Path] = field(default_factory=list)

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
    """Directories holding a ``.vnx-project-id`` or a T0 marker at most ``depth`` levels below
    ``root``. The T0 marker is what makes an ID-less folder visible: it is exactly the folder
    that cannot be guarded and that the report has to list."""
    found: List[Path] = []

    def walk(directory: Path, level: int) -> None:
        if (directory / PROJECT_ID_FILE).is_file() or (directory / T0_MARKER).is_file():
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


def rule1_reason(path: Path) -> Optional[str]:
    """Why the hook shim exits 0 for a session in ``path`` although the folder has a T0 file, or
    None when rule 1 passes. Rule 1 is ``hook_coverage.t0_project_root``, the function the guard
    uses; this only names the first failing part."""
    if hc.t0_project_root(str(path)) is not None:
        return None
    if hc.project_id(path) is None:
        return (f"{NO_PROJECT_ID} with a non-empty first line, so the shim exits without "
                "running the guard")
    if hc._repo_root(str(path)) is None:
        return "no .git entry at or above the folder, so the shim finds no repository root"
    return "the folder lies in a repository root or a dispatch worktree that rule 1 refuses"


def not_guardable_reason(path: Path) -> Optional[str]:
    """Why a T0 folder is outside the guard for good: it has no usable ``.vnx-project-id``, the
    first part of rule 1 (``rule1_reason``). No user-level entry can run there, so no account
    can repair it: it is listed and awaits an operator decision (it never fails the check)."""
    return None if hc.project_id(path) is not None else NO_PROJECT_ID


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
        root = Path(real)
        has_t0 = (root / T0_MARKER).is_file()
        not_guardable = not_guardable_reason(root) if has_t0 else None
        inventory.append(ProjectEntry(
            root, hc.project_id(real), real in registered, has_t0,
            rule1_reason(root) if has_t0 and not not_guardable else None, not_guardable))
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


def account_env(home: Path, config_dir: Path, base: Mapping[str, str]) -> Dict[str, str]:
    """The environment Claude Code runs with for the account in ``config_dir``. The account's
    global config (where trust is recorded) is ``$CLAUDE_CONFIG_DIR/.claude.json`` when that
    variable is set, else ``$HOME/.claude.json``. So ``~/.claude`` runs without the variable,
    which also makes it read ``~/.claude.json`` and not ``~/.claude/.claude.json``, and every
    other account runs with it set to its own folder."""
    env = dict(base)
    env["HOME"] = str(home)
    if os.path.realpath(config_dir) == os.path.realpath(home / ".claude"):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def _coverage_text(project: Path, event: str, tool: Optional[str], user_state: Optional[str],
                   env: Mapping[str, str]) -> str:
    cov = hc.coverage(project, event, tool, env=env)
    if cov.covered:
        return f"project ({cov.form})"
    if cov.state == hc.UNKNOWN or user_state == gi.STATE_UNKNOWN:
        return "unknown"
    if user_state == gi.STATE_DUPLICATE:
        return "duplicate"
    return "user" if user_state == gi.STATE_OK else "missing"


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
    """The report of one account; ``env`` is that account's own (``account_env``)."""
    report = AccountReport(config_dir, "ok", trusted_root=trusted_root_text(env))
    try:
        _, settings = gi.read_settings(gi.settings_path(config_dir))
        report.entries = gi.entry_states(settings)
    except gi.GuardInstallError as exc:
        report.settings_state = f"unknown, {exc}"
        report.entries = {event: gi.STATE_UNKNOWN for event, _ in gi.GUARD_EVENTS}
    for project in projects:
        if not project.has_t0:
            continue
        if project.not_guardable:
            report.coverage[str(project.path)] = {
                ALL_EVENTS: f"{NOT_GUARDABLE}: {project.not_guardable}"}
            continue
        if project.rule1_reason:
            report.coverage[str(project.path)] = {
                ALL_EVENTS: f"{NOT_GUARDED} ({project.rule1_reason})"}
            continue
        rows: Dict[str, str] = {}
        for event, tool in _event_keys():
            label = event if tool is None else f"{event}:{tool}"
            rows[label] = _coverage_text(project.path, event, tool, report.entries.get(event), env)
        report.coverage[str(project.path)] = rows
    return report


def run_check(*, home: Path, config_dirs: Sequence[str] = (), scan_roots: Sequence[str] = (),
              env: Optional[Mapping[str, str]] = None,
              require_user: bool = False) -> CheckReport:
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
    report.scan_roots = [Path(r).expanduser() for r in scan_roots]
    report.projects = build_inventory(home, report.scan_roots)
    for account in accounts:
        acc = account_report(account, report.projects, account_env(home, account, mapping))
        report.accounts.append(acc)
        report.problems.extend(_account_problems(acc))
        if require_user:
            report.problems.extend(require_user_problems(acc))
    return report


def _entry_problem(config_dir: Path, event: str, state: str) -> str:
    if state == gi.STATE_DUPLICATE:
        return (f"{config_dir}: {event} has more than one entry of ours (duplicate); "
                "run `vnx guard install` to leave exactly one")
    return f"{config_dir}: {event} entry is {state}"


def _account_problems(acc: AccountReport) -> List[str]:
    problems = []
    if acc.settings_state != "ok":
        problems.append(f"{acc.config_dir}: {acc.settings_state}")
    else:
        problems.extend(_entry_problem(acc.config_dir, event, state)
                        for event, state in acc.entries.items() if state != gi.STATE_OK)
    for project, rows in acc.coverage.items():
        unguarded = rows.get(ALL_EVENTS, "")
        if unguarded.startswith(NOT_GUARDABLE):
            continue
        if unguarded.startswith(NOT_GUARDED):
            problems.append(f"{acc.config_dir}: {project} is {unguarded}")
            continue
        bad = sorted(label for label, text in rows.items() if text in ("missing", "unknown"))
        if bad:
            problems.append(f"{acc.config_dir}: {project} is not covered on {', '.join(bad)}")
    return problems


def require_user_problems(acc: AccountReport) -> List[str]:
    """Under ``--require-user``: the user level is the only registration that counts, so every
    event a project answers with its own settings is a failure. A project that is not guardable
    (no ``.vnx-project-id``) never fails this flag: it awaits an operator decision, and one that
    fails rule 1 is already reported by the plain check."""
    problems: List[str] = []
    for project, rows in acc.coverage.items():
        project_covered = {label.split(":", 1)[0] for label, text in rows.items()
                           if text.startswith("project (")}
        problems.extend(f"{acc.config_dir}: {project}: {event} is {PROJECT_ONLY}"
                        for event, _ in gi.GUARD_EVENTS if event in project_covered)
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
    lines.append(_scope_line(report))
    for project in report.projects:
        lines.extend(_project_lines(report, project))
    if not report.projects:
        lines.append("  (none in the registry or under a --scan-root)")
    closing = _not_guardable_line(report)
    if closing:
        lines.append(closing)
    lines.append("OK" if report.ok else "NOT OK")
    lines.extend(f"  - {problem}" for problem in report.problems)
    return "\n".join(lines) + "\n"


def _scope_line(report: CheckReport) -> str:
    """What the inventory read: the registry alone, or the registry plus named scan roots."""
    if report.scan_roots:
        roots = ", ".join(str(root) for root in report.scan_roots)
        return f"scope: registry plus every .vnx-project-id and T0 folder under: {roots}"
    return (f"scope: registry only ({len(report.projects)} projects); "
            "pass --scan-root DIR to include unregistered projects")


def _not_guardable_line(report: CheckReport) -> Optional[str]:
    """One closing line for the T0 folders no account can repair (no ``.vnx-project-id``)."""
    count = sum(1 for project in report.projects if project.not_guardable)
    if not count:
        return None
    noun = "folder" if count == 1 else "folders"
    return (f"{count} {noun} with a T0 but no usable {PROJECT_ID_FILE}: not guardable; "
            "they await an operator decision.")


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
                      out, require_user: bool = False) -> int:
    report = run_check(home=home, config_dirs=config_dirs, scan_roots=scan_roots,
                       require_user=require_user)
    out.write(render_report(report))
    return 0 if report.ok else 1
