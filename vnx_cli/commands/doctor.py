#!/usr/bin/env python3
"""vnx doctor — validate prerequisites and project structure."""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from vnx_cli import _engine
from vnx_cli._reexec import PIN_FILE_NAME

logger = logging.getLogger(__name__)

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
    for tool in ("python3", "git"):
        found = shutil.which(tool)
        results.append(Check(
            name=f"tool:{tool}",
            status=PASS if found else FAIL,
            detail=found or f"{tool} not found in PATH",
        ))
    # Audit F9: jq is used only by the bash operator surface; the pip CLI is pure Python, so a
    # missing jq must not hard-FAIL `vnx doctor`. WARN instead.
    jq = shutil.which("jq")
    results.append(Check(
        name="tool:jq",
        status=PASS if jq else WARN,
        detail=jq or "jq not found in PATH; only the bash operator surface (./bin/vnx) needs it",
    ))
    shellcheck = shutil.which("shellcheck")
    results.append(Check(
        name="tool:shellcheck",
        status=PASS if shellcheck else WARN,
        detail=shellcheck or "shellcheck not found in PATH; shell lint checks will emit tool_unavailable warnings",
    ))
    # Worker CLIs the dispatch lanes drive as subprocesses (audit high #7). WARN, not FAIL: an
    # operator may use a non-Claude lane, but `vnx dispatch-agent` fails at spawn if NONE is present.
    worker_clis = ("claude", "codex", "gemini", "kimi")
    found_workers = [c for c in worker_clis if shutil.which(c)]
    results.append(Check(
        name="tool:worker-cli",
        status=PASS if found_workers else WARN,
        detail=(
            f"found: {', '.join(found_workers)}" if found_workers
            else "no worker CLI (claude/codex/gemini/kimi) on PATH; `vnx dispatch-agent` will fail at "
                 "spawn. Install + authenticate the lane you use (default: claude)."
        ),
    ))
    return results


def _check_directories(project_dir: Path, data_root: Path) -> list[Check]:
    results = []

    vnx_dir = project_dir / ".vnx"
    results.append(Check(
        name="dir:.vnx",
        status=PASS if vnx_dir.is_dir() else FAIL,
        detail=str(vnx_dir) if vnx_dir.is_dir() else ".vnx/ missing — run `vnx init`",
    ))

    # PR-PIP-2: the runtime data tree lives under the resolved state root
    # (a user-data-dir for pip installs), no longer project-local .vnx-data.
    results.append(Check(
        name="dir:data-root",
        status=PASS if data_root.is_dir() else FAIL,
        detail=str(data_root) if data_root.is_dir()
        else f"runtime data root missing ({data_root}) — run `vnx init`",
    ))

    results.append(_check_agents(project_dir))

    return results


def _check_agents(project_dir: Path) -> Check:
    """Count agents across the FULL resolution chain ``dispatch_agent`` uses.

    A project-local ``agents/`` folder is only one tier of the chain
    ``_resolve_agent_claude_md`` walks (project agents/, project examples/,
    engine agents/, engine examples/). Reading only the project-local dir
    made doctor WARN "agents/ directory not found" for engine-fleet-only
    projects that dispatch perfectly fine — WARN only when the full chain
    yields zero agents.
    """
    try:
        _engine.ensure_engine_on_path()
        from agent_resolver import list_available_agents
        agents = list_available_agents(project_dir, engine_root=_engine.engine_root())
    except Exception as exc:
        logger.warning("doctor: agent enumeration failed: %s", exc)
        return Check(
            name="agents",
            status=WARN,
            detail=f"could not enumerate agents: {exc}",
        )

    if not agents:
        return Check(
            name="agents",
            status=WARN,
            detail="no agents found in project agents/, project examples/, engine agents/, or engine examples/",
        )

    by_source: dict[str, int] = {}
    for agent in agents:
        by_source[agent.source] = by_source.get(agent.source, 0) + 1
    breakdown = ", ".join(f"{count} {source}" for source, count in sorted(by_source.items()))
    return Check(
        name="agents",
        status=PASS,
        detail=f"{len(agents)} agent(s) resolvable ({breakdown})",
    )


def _read_project_pin(project_dir: Path) -> str:
    """Read the project's ``.vnx-version`` pin file (the operator's pin intent).

    This is the SAME file ``vnx_cli._reexec`` honors at startup, so doctor's
    ``pin`` field and ``cat .vnx-version`` agree (OI-914). Returns the raw
    first line when present and non-empty, ``"unset"`` when there is no usable
    pin, or ``"error"`` when a pin file exists but cannot be read.
    """
    pin_file = project_dir / PIN_FILE_NAME
    if not pin_file.is_file():
        return "unset"
    try:
        first = pin_file.read_text(encoding="utf-8").strip().splitlines()[0]
    except OSError as exc:
        logger.warning("doctor: cannot read pin file %s: %s", pin_file, exc)
        return "error"
    if not first:
        return "unset"
    return first


def _resolve_active_version(central_path: Path) -> str:
    """The version dir the ``current`` symlink actually points at.

    This is what loads absent a pin re-exec — NOT the project's ``.vnx-version``
    pin intent. Reported separately from ``pin`` so a pin that is not honored is
    visible instead of the two silently disagreeing under one label.
    """
    try:
        if central_path.is_symlink():
            return central_path.resolve().name
    except OSError as exc:
        logger.warning("doctor: cannot resolve central_path symlink: %s", exc)
        return "unresolved"
    return central_path.name


def _check_central_install_marker(central_path: Path) -> "str | None":
    """Return a detail fragment if the resolved central version dir lacks a
    valid `.vnx-install-mode=central` marker, else None.

    ``central_path`` is the ``~/.vnx-system/current`` symlink; resolving it
    gives the actual version dir (e.g. ``~/.vnx-system/versions/edge``) that
    ``_is_central_install()`` in ``scripts/lib/vnx_paths.py`` inspects. A
    missing/invalid marker there makes that resolver misread the install as a
    standalone dev checkout and collapse PROJECT_ROOT onto the shared code tree.
    """
    try:
        version_dir = central_path.resolve()
    except OSError:
        return "install-mode marker: cannot resolve version dir"
    marker = version_dir / ".vnx-install-mode"
    if not marker.is_file():
        return f"install-mode marker missing at {marker}"
    try:
        content = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"install-mode marker unreadable ({exc})"
    if content != "central":
        return f"install-mode marker invalid (content={content!r}, expected 'central')"
    return None


def _check_install_mode(project_dir: Path) -> Check:
    """Detect embedded vs central VNX install mode and report pin vs active.

    In central mode the check reports two values that OI-914 found conflated
    under one "pin" label:
      * ``pin`` — the project's ``.vnx-version`` file (the pin the startup
        re-exec honors; matches ``cat .vnx-version``);
      * ``active`` — the version dir the ``current`` symlink resolves to
        (what actually runs absent a re-exec).
    Reporting both makes a pin that is not honored visible.
    """
    embedded_path = project_dir / ".claude" / "vnx-system"
    central_path = Path.home() / ".vnx-system" / "current"

    central_active = (central_path / "scripts").is_dir()
    embedded_active = (embedded_path / "scripts").is_dir()

    if central_active:
        pin = _read_project_pin(project_dir)
        active = _resolve_active_version(central_path)
        marker_issue = _check_central_install_marker(central_path)
        if pin == "error":
            return Check(
                name="install:mode",
                status=WARN,
                detail=(
                    f"mode: central, pin: error (cannot read "
                    f"{project_dir / PIN_FILE_NAME} — check permissions), active: {active}"
                ),
            )
        if marker_issue is not None:
            return Check(
                name="install:mode",
                status=WARN,
                detail=f"mode: central, pin: {pin}, active: {active}, {marker_issue}",
            )
        return Check(
            name="install:mode",
            status=PASS,
            detail=f"mode: central, pin: {pin}, active: {active}",
        )
    if embedded_active:
        return Check(
            name="install:mode",
            status=PASS,
            detail=f"mode: embedded, path: {embedded_path}",
        )

    # PR-PIP-2: pip-installed engine — vnx_cli ships scripts/ + schemas/ as
    # site-packages siblings. Detect that layout so a wheel install reports a
    # recognized (healthy) mode instead of "no VNX install detected".
    engine_root = _engine.engine_root()
    engine_has_scripts = (engine_root / "scripts").is_dir()
    if engine_has_scripts and _engine.is_packaged_install(engine_root):
        return Check(
            name="install:mode",
            status=PASS,
            detail=f"mode: packaged (site-packages), engine: {engine_root}",
        )
    if engine_has_scripts and (engine_root / "pyproject.toml").is_file():
        return Check(
            name="install:mode",
            status=PASS,
            detail=f"mode: source (dev checkout), engine: {engine_root}",
        )
    return Check(
        name="install:mode",
        status=WARN,
        detail="no VNX install detected (no embedded, central, packaged, or source scripts/ tree found)",
    )


def _check_state_root_location(data_root: Path) -> Check:
    """WARN if the runtime state root resolves inside the (immutable) package.

    PR-PIP-2 mitigation of the "state in immutable package" risk: a pip install
    must not write runtime state under site-packages or VNX_HOME. If it does,
    point the operator at VNX_DATA_HOME / the central default.
    """
    engine_root = _engine.engine_root()
    candidates = [engine_root]
    env_home = os.environ.get("VNX_HOME")
    if env_home:
        candidates.append(Path(env_home).expanduser())

    def _within(child: Path, parent: Path) -> bool:
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except (ValueError, OSError):
            return False

    root_str = str(data_root)
    in_site_packages = "site-packages" in root_str or "dist-packages" in root_str
    in_engine = any(_within(data_root, c) for c in candidates)

    if in_site_packages or in_engine:
        return Check(
            name="state:location",
            status=WARN,
            detail=(
                f"runtime state root resolves inside the package/VNX_HOME ({data_root}) "
                "— set VNX_DATA_HOME or the default "
                "(~/.vnx-data/<project_id>) to keep state writable and out of the wheel"
            ),
        )
    return Check(
        name="state:location",
        status=PASS,
        detail=f"runtime state root outside the package: {data_root}",
    )


def _check_dual_install(project_dir: Path) -> Check:
    """Fail only on a genuine dual install: two DISTINCT real install trees.

    In a correctly configured central-mode consumer, ``.claude/vnx-system`` is a
    symlink into the central store (``~/.vnx-system/current`` -> a version dir).
    ``Path.is_dir()`` follows the symlink, so a naive "both have scripts/" test
    sees the SAME tree twice and reports a conflict that does not exist — and
    then advises a destructive repair that would delete the very symlink that
    makes central mode work (OI-1075).

    Resolution is the fix: resolve both sides to real paths and compare. A real
    embedded copy (a directory that is NOT a symlink into the central store,
    carrying its own ``scripts/``) still resolves to a distinct directory and
    still FAILs with the existing, correct advice.
    """
    embedded_path = project_dir / ".claude" / "vnx-system"
    central_path = Path.home() / ".vnx-system" / "current"

    embedded_active = (embedded_path / "scripts").is_dir()
    central_active = (central_path / "scripts").is_dir()

    if not embedded_active:
        # No embedded scripts/ tree is reachable. Distinguish a genuinely absent
        # embedded path from a dangling symlink so the operator gets a useful
        # verdict either way; neither is a dual-install conflict.
        if embedded_path.is_symlink() and not embedded_path.exists():
            resolved = embedded_path.resolve(strict=False)
            return Check(
                name="install:dual",
                status=WARN,
                detail=(
                    f"embedded symlink {embedded_path} is dangling "
                    f"(target {resolved} missing) — not a dual install conflict, "
                    "but central mode is broken until the link is repaired"
                ),
            )
        return Check(
            name="install:dual",
            status=PASS,
            detail="no dual install conflict",
        )

    if not central_active:
        return Check(
            name="install:dual",
            status=PASS,
            detail="no dual install conflict",
        )

    # Both sides have a reachable scripts/ tree. The embedded path may be a
    # symlink into the central store (the intended central-mode arrangement) or
    # a real second install. Resolve both to real directories and compare.
    try:
        embedded_resolved = embedded_path.resolve(strict=False)
        central_resolved = central_path.resolve(strict=False)
    except OSError as exc:
        # Pathological resolution failure — surface it rather than silently
        # passing; resolution is the basis of the comparison.
        return Check(
            name="install:dual",
            status=FAIL,
            detail=(
                f"dual install conflict: could not resolve paths to compare ({exc}) "
                f"— embedded at {embedded_path}, central at {central_path}"
            ),
        )

    if embedded_resolved == central_resolved:
        # The embedded path is a symlink (directly or transitively) into the
        # central store — that IS the intended central-mode arrangement, not a
        # conflict. Name the resolved version dir for the operator.
        return Check(
            name="install:dual",
            status=PASS,
            detail=(
                f"embedded symlink into central store: {embedded_resolved.name} "
                f"({embedded_resolved})"
            ),
        )

    # Genuinely distinct installs: a real embedded directory with its own
    # scripts/, or a symlink pointing outside the central store entirely.
    return Check(
        name="install:dual",
        status=FAIL,
        detail=(
            f"dual install conflict: embedded at {embedded_path} "
            f"(resolves to {embedded_resolved}) AND central at {central_path} "
            f"(resolves to {central_resolved}) — "
            "remove embedded install before using central mode"
        ),
    )


def _check_schema_versions(data_root: Path) -> list[Check]:
    """Check PRAGMA user_version and runtime_schema_version on coordination databases."""
    state_dir = data_root / "state"
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
                try:
                    row = conn.execute(
                        "SELECT version FROM runtime_schema_version ORDER BY applied_at DESC LIMIT 1"
                    ).fetchone()
                    legacy_version: int | None = int(row[0]) if row else None
                except sqlite3.OperationalError as e:
                    logger.warning(
                        "doctor: runtime_schema_version query failed: %s — falling back to PRAGMA", e
                    )
                    legacy_version = None

                pragma_version = conn.execute("PRAGMA user_version").fetchone()[0]
                effective = max(pragma_version, legacy_version or 0)

                if min_version > 0 and effective < min_version:
                    results.append(Check(
                        name=f"schema:{db_name}",
                        status=WARN,
                        detail=(
                            f"schema version {effective} < minimum {min_version} "
                            f"(PRAGMA user_version={pragma_version})"
                        ),
                    ))
                else:
                    results.append(Check(
                        name=f"schema:{db_name}",
                        status=PASS,
                        detail=f"schema version {effective} (PRAGMA user_version={pragma_version})",
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


def _check_skill_coverage(project_dir: Path, data_root: Path, strict: bool = False) -> Check:
    """Audit skill/role refs in pending dispatches against resolvable skill directories."""
    dispatch_dir = data_root / "dispatches" / "pending"
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
    unreadable: list[dict] = []

    for df in dispatch_files:
        try:
            content = df.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("doctor: cannot read dispatch %s: %s", df, e)
            unreadable.append({"path": str(df), "error": str(e)})
            continue
        for line in content.splitlines():
            stripped = line.strip()
            for prefix in ("role:", "skill:", "Role:", "Skill:"):
                if stripped.startswith(prefix):
                    ref = stripped[len(prefix):].strip().split()[0] if stripped[len(prefix):].strip() else ""
                    if ref and not _skill_resolvable(ref, skill_dirs):
                        missing.append(f"{df.name}:{ref}")

    if unreadable and strict:
        return Check(
            name="skills:coverage",
            status=FAIL,
            detail=f"cannot audit {len(unreadable)} dispatch(es): {', '.join(u['path'] for u in unreadable[:3])}",
        )

    if unreadable:
        return Check(
            name="skills:coverage",
            status=WARN,
            detail=f"cannot read {len(unreadable)} dispatch file(s) — skill audit incomplete",
        )

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


def _check_active_drain(data_root: Path) -> Check:
    """Count in-flight dispatches in runtime_coordination.db; advise drain if > 0."""
    db_path = data_root / "state" / "runtime_coordination.db"
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


def _check_hook_paths(project_dir: Path) -> Check:
    """WARN for hook commands in .claude/settings.json that reference missing files.

    Delegates to hookpin_check.check_project_hook_pins (OI-1123) instead of a second
    resolver. The prior in-repo version skipped existence-checking any absolute path
    outside project_dir before touching the filesystem — scoped to the embedded-layout
    bug it was built for (#1073), not fabric pins. A fabric hook IS an absolute path
    outside the project by design (``~/.vnx-system/versions/<v>/...``), so that skip
    silently exempted exactly the pins most likely to go dead on a version rotation:
    PASS on two confirmed-dead mission-control pins instead of catching them.
    """
    def _result(status: str, detail: str) -> Check:
        return Check(name="hooks:path-resolution", status=status, detail=detail)

    settings_path = project_dir / ".claude" / "settings.json"
    if not settings_path.is_file():
        return _result(PASS, "no .claude/settings.json found; skipping hook path check")

    try:
        json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _result(WARN, f".claude/settings.json is unparseable ({exc}); hook paths cannot be audited")

    try:
        _engine.ensure_engine_on_path()
        from hookpin_check import check_project_hook_pins, STATUS_MISSING, STATUS_UNRESOLVED
    except Exception as exc:
        return _result(WARN, f"could not load hookpin_check module: {exc}")

    findings = check_project_hook_pins(project_dir.resolve())
    missing = [f for f in findings if f.status == STATUS_MISSING]
    unresolved = [f for f in findings if f.status == STATUS_UNRESOLVED]

    if missing:
        descriptions = "; ".join(f"{f.raw_path} ({f.event}) -> {f.resolved_path}" for f in missing)
        return _result(WARN, f"{len(missing)} referenced hook script(s) missing: {descriptions}")

    if unresolved:
        # A locally-assigned shell var (e.g. `ROOT=$(...)` earlier in the same command,
        # this repo's own settings.json convention) is a real, live pin the resolver
        # cannot verify — not a defect. PASS, not WARN: this check exists to catch
        # confirmed-dead pins, and warning on an unprovable blind spot would make it
        # cry wolf on every project using this idiom instead of surfacing real drift.
        checked_ok = len(findings) - len(unresolved)
        return _result(PASS, (
            f"{checked_ok} of {len(findings)} configured hook pin(s) confirmed resolving; "
            f"{len(unresolved)} unresolved (not checked, not confirmed dead)"
        ))

    if not findings:
        return _result(PASS, "no hooks section in .claude/settings.json")

    return _result(PASS, "all referenced hook script paths resolve")


def _check_ledger_health(data_root: Path) -> Check:
    """WARN from the ledger_health beacon: dispatches without a receipt, a
    stale receipt-pull cursor, or a ledger that is unchained while
    VNX_CHAIN_RECEIPTS is configured on.

    Delegates entirely to two existing modules instead of a third resolver
    or threshold set: ``health_beacon.all_beacons`` (the same staleness/
    corrupt classification every other subsystem beacon uses —
    ``vnx_cli/commands/subsystems.py``) reads the beacon file itself, and
    ``ledger_health.COMPONENT_NAME`` names it — the actual coverage/cursor/
    chain thresholds live only in ``scripts/ledger_health.py``. Mirrors
    ``_check_hook_paths``'s delegation to ``hookpin_check``.

    The beacon is written by a separate, manual/periodic run of
    ``python3 scripts/ledger_health.py`` (wiring an automatic cadence is out
    of scope — see the dispatch this check shipped with) — so a project that
    has never run it gets PASS-with-a-pointer, not a false FAIL.
    """
    def _result(status: str, detail: str) -> Check:
        return Check(name="ledger:health", status=status, detail=detail)

    try:
        _engine.ensure_engine_on_path()
        from health_beacon import all_beacons
        from ledger_health import COMPONENT_NAME
    except Exception as exc:
        return _result(WARN, f"could not load ledger_health/health_beacon module: {exc}")

    beacon = all_beacons(data_root).get(COMPONENT_NAME)
    if beacon is None:
        return _result(
            PASS,
            "no ledger-health beacon yet — run `python3 scripts/ledger_health.py` to populate",
        )

    health = beacon.get("health", "unknown")
    if health == "corrupt":
        return _result(WARN, f"ledger-health beacon is corrupt/unreadable: {beacon.get('error', '?')}")

    findings: list[str] = []
    if health == "stale":
        age = beacon.get("age_seconds")
        age_str = f"{round(age / 3600, 1)}h" if isinstance(age, (int, float)) else "?"
        findings.append(f"beacon is stale ({age_str} old) — rerun ledger_health.py")

    details = beacon.get("details") or {}
    if not isinstance(details, dict):
        details = {}
    checks = details.get("checks") or {}
    if not isinstance(checks, dict):
        checks = {}

    coverage = checks.get("receipt_coverage") or {}
    if coverage.get("status") == "finding":
        findings.append(
            f"{coverage.get('missing_receipt_count', '?')} dispatch(es) in the register "
            "have no matching receipt"
        )
    elif coverage.get("status") == "SKIPPED_UNVERIFIED":
        findings.append(f"receipt coverage unmeasurable: {coverage.get('reason', '?')}")

    cursor = checks.get("pull_cursor") or {}
    if cursor.get("status") == "finding":
        age_seconds = cursor.get("cursor_age_seconds")
        age_h = round(age_seconds / 3600, 1) if isinstance(age_seconds, (int, float)) else "?"
        findings.append(
            f"receipt pull cursor is {age_h}h old "
            f"(backlog {cursor.get('backlog_receipt_count', '?')} receipt(s))"
        )
    elif cursor.get("status") == "SKIPPED_UNVERIFIED":
        findings.append(f"pull-cursor health unmeasurable: {cursor.get('reason', '?')}")

    chain = checks.get("chain_status") or {}
    if chain.get("status") == "finding":
        findings.append(
            f"receipts ledger is {chain.get('chain_state', '?')} while VNX_CHAIN_RECEIPTS "
            "is configured on"
        )
    elif chain.get("status") == "SKIPPED_UNVERIFIED":
        findings.append(f"chain-status unmeasurable: {chain.get('reason', '?')}")

    if health == "fail" and not findings:
        findings.append(
            "ledger-health beacon reports fail, but no findings could be derived from its "
            "details — inspect the beacon directly"
        )

    if findings:
        return _result(WARN, "; ".join(findings))
    return _result(PASS, "receipt coverage, pull-cursor age, and chain status all healthy")


def _check_embedded_path_assumptions() -> Check:
    """WARN on __file__-anchored .vnx-data/ROADMAP.yaml AND repo-root derivations.

    Central-mode-path-correctness (#1023/#1024) plus OI-1145: a bare
    ``Path(__file__)….parent…`` walk that builds a ``.vnx-data``/``ROADMAP.yaml``
    path, OR that resolves the REPO ROOT (``resolve_project_root(__file__)`` /
    a ``.git``-marker finder fed ``__file__``), resolves the KEYSTONE
    (``~/.vnx-system/versions/<v>/``) instead of the project in a central
    install. Delegates to the AST-based
    ``scripts/check_no_file_derived_data_paths.py`` detector — which carries a
    grandfathered allowlist for already-migrated last-resort fallbacks and traces
    module-level marker constants (e.g. ``_DEFAULT_RELATIVE_PATH = Path(".vnx-data/x")``
    joined against a file-anchored root elsewhere) — so this stays advisory-accurate
    with no false positives on doc literals or intentional defensive fallbacks.

    Scans the resolved engine root (VNX_HOME-equivalent: the central symlink
    target, the embedded project copy, or the dev checkout), NOT project_dir —
    ``scripts/lib`` is framework code, not project code, and only exists inside
    the engine tree.
    """
    try:
        engine_root = _engine.ensure_engine_on_path()
        import check_no_file_derived_data_paths as _checker
    except Exception as exc:
        return Check(
            name="paths:embedded-assumptions",
            status=WARN,
            detail=f"could not load central-mode path checker: {exc}",
        )

    try:
        violations = _checker.scan_dir(engine_root)
    except Exception as exc:
        return Check(
            name="paths:embedded-assumptions",
            status=WARN,
            detail=f"central-mode path checker failed: {exc}",
        )

    if violations:
        shown = "; ".join(f"{rel}:{lineno} ({seg})" for rel, lineno, seg in violations[:5])
        more = f" (+{len(violations) - 5} more)" if len(violations) > 5 else ""
        return Check(
            name="paths:embedded-assumptions",
            status=WARN,
            detail=(
                f"{len(violations)} __file__-anchored .vnx-data/ROADMAP.yaml or "
                f"repo-root derivation(s) in scripts/lib — resolves the keystone, not "
                f"the project, in a central install. Route data/state through "
                f"vnx_paths.resolve_paths() and resolve the repo root CWD-first "
                f"(OI-1145): {shown}{more}"
            ),
        )
    return Check(
        name="paths:embedded-assumptions",
        status=PASS,
        detail=(
            "no __file__-anchored .vnx-data/ROADMAP.yaml or repo-root "
            "derivations in scripts/lib"
        ),
    )


def vnx_doctor(args) -> int:
    project_dir = Path(args.project_dir).resolve()
    emit_json = getattr(args, "json", False)
    strict = getattr(args, "strict", False)

    # PR-PIP-2: resolve the runtime data root once (explicit > VNX_DATA_HOME >
    # existing ~/.vnx-data/<id> > existing project-local > fresh default) and
    # thread it through the runtime-tree checks so a clean (state-outside-project)
    # install validates against where state actually lives.
    data_root = _engine.resolve_data_root(project_dir)

    checks: list[Check] = []
    checks.extend(_check_tools())
    checks.extend(_check_directories(project_dir, data_root))
    checks.append(_check_install_mode(project_dir))
    checks.append(_check_state_root_location(data_root))
    checks.append(_check_dual_install(project_dir))
    checks.extend(_check_schema_versions(data_root))
    checks.append(_check_skill_coverage(project_dir, data_root, strict=strict))
    checks.append(_check_overrides(project_dir))
    checks.extend(_check_worktree_orphans(project_dir))
    checks.append(_check_active_drain(data_root))
    checks.append(_check_hook_paths(project_dir))
    checks.append(_check_ledger_health(data_root))
    checks.append(_check_embedded_path_assumptions())

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
