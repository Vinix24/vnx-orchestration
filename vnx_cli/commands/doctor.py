#!/usr/bin/env python3
"""vnx doctor — validate prerequisites and project structure."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# Minimum runtime_schema_version for runtime_coordination.db
MIN_RUNTIME_SCHEMA_VERSION = 10


class Check(NamedTuple):
    name: str
    status: str
    detail: str


def _check_tools() -> list[Check]:
    results = []
    for tool in ("python3", "git", "jq"):
        found = shutil.which(tool)
        results.append(Check(
            name=f"tool:{tool}",
            status=PASS if found else FAIL,
            detail=found or f"{tool} not found in PATH",
        ))
    return results


def _check_directories(project_dir: Path) -> list[Check]:
    results = []

    vnx_dir = project_dir / ".vnx"
    results.append(Check(
        name="dir:.vnx",
        status=PASS if vnx_dir.is_dir() else FAIL,
        detail=str(vnx_dir) if vnx_dir.is_dir() else ".vnx/ missing — run `vnx init`",
    ))

    vnx_data = project_dir / ".vnx-data"
    results.append(Check(
        name="dir:.vnx-data",
        status=PASS if vnx_data.is_dir() else FAIL,
        detail=str(vnx_data) if vnx_data.is_dir() else ".vnx-data/ missing — run `vnx init`",
    ))

    agents_dir = project_dir / "agents"
    if agents_dir.is_dir():
        agent_dirs = [d for d in agents_dir.iterdir() if d.is_dir()]
        if agent_dirs:
            results.append(Check(
                name="agents",
                status=PASS,
                detail=f"{len(agent_dirs)} agent dir(s) found",
            ))
        else:
            results.append(Check(
                name="agents",
                status=WARN,
                detail="agents/ exists but contains no subdirectories",
            ))
    else:
        results.append(Check(
            name="agents",
            status=WARN,
            detail="agents/ directory not found",
        ))

    return results


def _resolve_central_pin(central_path: Path) -> str:
    """Resolve the version pin for a central install directory."""
    try:
        if central_path.is_symlink():
            return central_path.resolve().name
    except OSError:
        pass
    for fname in ("VERSION", "version.txt", ".vnx-version"):
        vf = central_path / fname
        if vf.is_file():
            try:
                first = vf.read_text(encoding="utf-8").strip().splitlines()[0]
                if first:
                    return first
            except OSError:
                pass
    return "unknown"


def _check_install_mode(project_dir: Path) -> Check:
    """Detect embedded vs central VNX install mode and report the active pin."""
    embedded_path = project_dir / ".claude" / "vnx-system"
    central_path = Path.home() / ".vnx-system" / "current"

    central_active = (central_path / "scripts").is_dir()
    embedded_active = (embedded_path / "scripts").is_dir()

    if central_active:
        pin = _resolve_central_pin(central_path)
        return Check(
            name="install:mode",
            status=PASS,
            detail=f"mode: central, pin: {pin}",
        )
    if embedded_active:
        return Check(
            name="install:mode",
            status=PASS,
            detail=f"mode: embedded, path: {embedded_path}",
        )
    return Check(
        name="install:mode",
        status=WARN,
        detail="no VNX install detected (neither embedded nor central scripts/ tree found)",
    )


def _check_dual_install(project_dir: Path) -> Check:
    """Fail if both embedded and central installs are present with a scripts/ tree."""
    embedded_path = project_dir / ".claude" / "vnx-system"
    central_path = Path.home() / ".vnx-system" / "current"

    embedded_active = (embedded_path / "scripts").is_dir()
    central_active = (central_path / "scripts").is_dir()

    if embedded_active and central_active:
        return Check(
            name="install:dual",
            status=FAIL,
            detail=(
                f"dual install conflict: embedded at {embedded_path} "
                f"AND central at {central_path} — "
                "remove embedded install before using central mode"
            ),
        )
    return Check(
        name="install:dual",
        status=PASS,
        detail="no dual install conflict",
    )


def _check_schema_versions(project_dir: Path) -> list[Check]:
    """Check PRAGMA user_version and runtime_schema_version on coordination databases."""
    state_dir = project_dir / ".vnx-data" / "state"
    db_specs = [
        ("runtime_coordination.db", MIN_RUNTIME_SCHEMA_VERSION),
        ("quality_intelligence.db", 0),
    ]
    results = []

    for db_name, min_version in db_specs:
        db_path = state_dir / db_name
        if not db_path.exists():
            results.append(Check(
                name=f"schema:{db_name}",
                status=WARN,
                detail=f"{db_name} not found (skipping schema check)",
            ))
            continue

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                user_version = conn.execute("PRAGMA user_version").fetchone()[0]
                schema_version: int | None = None
                try:
                    row = conn.execute(
                        "SELECT MAX(version) FROM runtime_schema_version"
                    ).fetchone()
                    if row and row[0] is not None:
                        schema_version = int(row[0])
                except sqlite3.OperationalError:
                    pass

                effective = schema_version if schema_version is not None else user_version

                if min_version > 0 and effective < min_version:
                    results.append(Check(
                        name=f"schema:{db_name}",
                        status=WARN,
                        detail=(
                            f"schema version {effective} < minimum {min_version} "
                            f"(PRAGMA user_version={user_version})"
                        ),
                    ))
                else:
                    results.append(Check(
                        name=f"schema:{db_name}",
                        status=PASS,
                        detail=f"schema version {effective} (PRAGMA user_version={user_version})",
                    ))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            results.append(Check(
                name=f"schema:{db_name}",
                status=FAIL,
                detail=f"cannot open {db_name}: {exc}",
            ))

    return results


_BUILTIN_ROLES = frozenset({
    "backend-developer", "frontend-developer", "architect", "test-engineer",
    "security-engineer", "data-analyst", "devops-engineer", "fullstack-developer",
    "refactoring-expert", "python-expert", "intelligence-engineer", "database-engineer",
    "quality-engineer", "performance-engineer",
})


def _skill_resolvable(skill_ref: str, skill_dirs: list[Path]) -> bool:
    """Return True if skill_ref resolves in a known skill directory or is a builtin role."""
    if skill_ref in _BUILTIN_ROLES:
        return True
    for skill_dir in skill_dirs:
        for candidate in (f"{skill_ref}.md", f"{skill_ref}/SKILL.md", skill_ref):
            if (skill_dir / candidate).exists():
                return True
    return False


def _check_skill_coverage(project_dir: Path) -> Check:
    """Audit skill/role refs in pending dispatches against resolvable skill directories."""
    dispatch_dir = project_dir / ".vnx-data" / "dispatches" / "pending"
    if not dispatch_dir.is_dir():
        return Check(
            name="skills:coverage",
            status=WARN,
            detail="pending dispatch directory not found (skipping skill coverage check)",
        )

    skill_dirs: list[Path] = []
    for candidate in (
        project_dir / ".claude" / "skills",
        project_dir / ".claude" / "vnx-system" / "skills",
        Path.home() / ".vnx-system" / "current" / "skills",
        project_dir / ".vnx-overrides",
    ):
        if candidate.is_dir():
            skill_dirs.append(candidate)

    dispatch_files = list(dispatch_dir.glob("*.md")) + list(dispatch_dir.glob("*.txt"))
    missing: list[str] = []

    for df in dispatch_files:
        try:
            content = df.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            stripped = line.strip()
            for prefix in ("role:", "skill:", "Role:", "Skill:"):
                if stripped.startswith(prefix):
                    ref = stripped[len(prefix):].strip().split()[0] if stripped[len(prefix):].strip() else ""
                    if ref and not _skill_resolvable(ref, skill_dirs):
                        missing.append(f"{df.name}:{ref}")

    if missing:
        return Check(
            name="skills:coverage",
            status=WARN,
            detail=f"unresolvable skill ref(s): {', '.join(missing[:5])}",
        )
    return Check(
        name="skills:coverage",
        status=PASS,
        detail=f"all skill refs resolvable ({len(dispatch_files)} dispatch(es) scanned)",
    )


def _check_overrides(project_dir: Path) -> Check:
    """List contents of .vnx-overrides/ if present."""
    overrides_dir = project_dir / ".vnx-overrides"
    if not overrides_dir.is_dir():
        return Check(
            name="overrides",
            status=PASS,
            detail="no .vnx-overrides/ directory",
        )

    try:
        entries = sorted(overrides_dir.iterdir())
    except OSError as exc:
        return Check(
            name="overrides",
            status=WARN,
            detail=f"cannot list .vnx-overrides/: {exc}",
        )

    if not entries:
        return Check(
            name="overrides",
            status=PASS,
            detail=".vnx-overrides/ exists but is empty",
        )

    names = [e.name for e in entries[:10]]
    suffix = f" (+{len(entries) - 10} more)" if len(entries) > 10 else ""
    return Check(
        name="overrides",
        status=PASS,
        detail=f"{len(entries)} override(s): {', '.join(names)}{suffix}",
    )


def _parse_worktree_porcelain(output: str) -> list[dict]:
    """Parse git worktree list --porcelain into a list of dicts."""
    records: list[dict] = []
    current: dict = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
        elif line.startswith("worktree "):
            current["worktree"] = line[9:].strip()
        elif line.startswith("HEAD "):
            current["HEAD"] = line[5:].strip()
        elif line.startswith("branch "):
            current["branch"] = line[7:].strip()
        elif line == "detached":
            current["detached"] = True
    if current:
        records.append(current)
    return records


def _check_worktree_orphans(project_dir: Path) -> list[Check]:
    """Detect worktrees whose .git or path no longer exists (orphan state)."""
    try:
        output = subprocess.check_output(
            ["git", "-C", str(project_dir), "worktree", "list", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [Check(
            name="worktrees:orphans",
            status=WARN,
            detail="git worktree list failed (not a git repo or git unavailable)",
        )]

    worktrees = _parse_worktree_porcelain(output)
    orphans: list[str] = []

    for wt in worktrees:
        wt_path = Path(wt.get("worktree", ""))
        if not wt_path.exists():
            orphans.append(f"{wt_path.name} (path gone)")

    if orphans:
        return [Check(
            name="worktrees:orphans",
            status=WARN,
            detail=f"{len(orphans)} orphan(s): {', '.join(orphans[:5])} — prune with `git worktree prune`",
        )]
    return [Check(
        name="worktrees:orphans",
        status=PASS,
        detail=f"{len(worktrees)} worktree(s) checked, none orphaned",
    )]


def _check_active_drain(project_dir: Path) -> Check:
    """Count in-flight dispatches in runtime_coordination.db; advise drain if > 0."""
    db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
    if not db_path.exists():
        return Check(
            name="drain:active",
            status=PASS,
            detail="runtime_coordination.db not found (no dispatches to drain)",
        )

    active_states = ("queued", "claimed", "delivering", "accepted", "running")
    placeholders = ", ".join("?" * len(active_states))

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM dispatches WHERE state IN ({placeholders})",
                active_states,
            ).fetchone()
            count = int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check(
            name="drain:active",
            status=WARN,
            detail=f"could not query coordination db: {exc}",
        )

    if count > 0:
        return Check(
            name="drain:active",
            status=WARN,
            detail=(
                f"{count} active dispatch(es) still in flight — "
                "drain before centralization migration"
            ),
        )
    return Check(
        name="drain:active",
        status=PASS,
        detail="no active dispatches",
    )


def vnx_doctor(args) -> int:
    project_dir = Path(args.project_dir).resolve()
    emit_json = getattr(args, "json", False)
    strict = getattr(args, "strict", False)

    checks: list[Check] = []
    checks.extend(_check_tools())
    checks.extend(_check_directories(project_dir))
    checks.append(_check_install_mode(project_dir))
    checks.append(_check_dual_install(project_dir))
    checks.extend(_check_schema_versions(project_dir))
    checks.append(_check_skill_coverage(project_dir))
    checks.append(_check_overrides(project_dir))
    checks.extend(_check_worktree_orphans(project_dir))
    checks.append(_check_active_drain(project_dir))

    passed = sum(1 for c in checks if c.status == PASS)
    warned = sum(1 for c in checks if c.status == WARN)
    failed = sum(1 for c in checks if c.status == FAIL)

    if emit_json:
        output = {
            "project_dir": str(project_dir),
            "strict": strict,
            "summary": {"pass": passed, "warn": warned, "fail": failed},
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail}
                for c in checks
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        for c in checks:
            marker = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[c.status]
            print(f"  {marker}  {c.name:<28}  {c.detail}")
        print()
        print(f"  Summary: {passed} passed, {warned} warned, {failed} failed")
        if strict and (warned > 0 or failed > 0):
            print("  [strict] non-zero warnings/failures → exit 1")

    if strict:
        return 1 if (failed > 0 or warned > 0) else 0
    return 1 if failed > 0 else 0
