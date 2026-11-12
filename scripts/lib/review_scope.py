"""The profile key ``VNX_REVIEW_SCOPE`` (ADR-064 "De schakelaar", ADR-060 amendment of 2026-11-10).

``full`` is the default and the only answer a reader gives when the key cannot be trusted: unset, empty, unknown,
unreadable, or ``delta`` on a row without an approval id. ``delta_shadow`` runs the shadow delta next to the full
round. ``delta`` may only be written after a passed DR-5 measurement, a merged DR-7 and a green main CI
(:func:`require_activation`); until part 2 it behaves like ``delta_shadow``.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

import config_registry

SCOPE_KEY = "VNX_REVIEW_SCOPE"
FULL, DELTA_SHADOW, DELTA = "full", "delta_shadow", "delta"
CI_WORKFLOW = "VNX CI"
MAIN_REF = "origin/main"


class ReviewScope(NamedTuple):
    value: str
    reason: str

    @property
    def runs_shadow(self) -> bool:
        return self.value in (DELTA_SHADOW, DELTA)


def _approval_id(state_dir: Path, project_id: str) -> str:
    """The approval id of the project's ``delta`` row; ``""`` when there is none or it cannot be read."""
    try:
        conn = sqlite3.connect(f"file:{Path(state_dir) / 'runtime_coordination.db'}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT approval_id FROM project_config WHERE project_id = ? AND config_key = ? "
                               "AND config_value = ?", (project_id, SCOPE_KEY, DELTA)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    return str(row[0] or "").strip() if row else ""


def active_review_scope(state_dir: Path) -> ReviewScope:
    """The scope this store's project runs under, and why. Never raises; every doubt answers ``full``."""
    try:
        import vnx_paths

        project_id = vnx_paths.project_id_from_state_dir(Path(state_dir)) or ""
        value = (config_registry.get_for_project(SCOPE_KEY, project_id, state_dir) or "").strip()
        if value in ("", FULL):
            return ReviewScope(FULL, "unset" if not value else "configured")
        if value == DELTA_SHADOW:
            return ReviewScope(DELTA_SHADOW, "configured")
        if value == DELTA:
            if _approval_id(state_dir, project_id):
                return ReviewScope(DELTA, "configured with an approval id")
            return ReviewScope(FULL, "delta without an approval id on its row")
        return ReviewScope(FULL, f"unknown value {value!r}")
    except Exception as exc:  # fail-closed: an unreadable key is a full review
        return ReviewScope(FULL, f"unreadable: {type(exc).__name__}")


def measurement_outcome(state_dir: Path, manifest: Path, project_root: Path) -> str:
    """The DR-5 outcome over every shadow line of the project, with the planted manifest the writer names."""
    import review_delta_compare as rdc

    result = rdc.compare(Path(state_dir), since=datetime.fromtimestamp(0, timezone.utc),
                         source_of=rdc.git_source_of(project_root), planted=rdc.load_planted(Path(manifest)))
    return result.outcome


def _run(cmd: List[str], cwd: Path) -> str:
    done = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {done.returncode}")
    return done.stdout


def dr7_on_main(project_root: Path) -> bool:
    """DR-7 is merged when a test on main reconstructs the merge from the ``pr_merged`` receipt's merged tree."""
    out = _run(["git", "grep", "-l", "--all-match", "-e", "pr_merged", "-e", "merged_tree", "-e", "reconstruct",
                MAIN_REF, "--", "tests"], project_root)
    return bool(out.strip())


def main_ci_green(project_root: Path) -> bool:
    """True only when the latest ``VNX CI`` run on main concluded exactly ``success``."""
    runs = json.loads(_run(["gh", "run", "list", "--workflow", CI_WORKFLOW, "--branch", "main", "--limit", "1",
                            "--json", "conclusion,status"], project_root))
    return isinstance(runs, list) and len(runs) == 1 and runs[0].get("conclusion") == "success"


def require_activation(state_dir: Path, manifest: Optional[Path], project_root: Path) -> None:
    """Raise ``PermissionError`` with the reason unless ``delta`` may be switched on. A check that cannot be made refuses."""
    if manifest is None:
        raise PermissionError(f"{SCOPE_KEY}=delta refused: no planted manifest named")
    checks: List[tuple] = [
        ("DR-5 measurement", lambda: measurement_outcome(state_dir, manifest, project_root) == "pass"),
        ("DR-7 merged with its reconstruction test on main", lambda: dr7_on_main(project_root)),
        (f"latest {CI_WORKFLOW} run on main concluded success", lambda: main_ci_green(project_root)),
    ]
    for name, check in checks:
        try:
            ok = check()
        except Exception as exc:  # a check that cannot be made refuses
            raise PermissionError(f"{SCOPE_KEY}=delta refused: {name} could not be checked ({type(exc).__name__}: {exc})") from exc
        if ok is not True:
            raise PermissionError(f"{SCOPE_KEY}=delta refused: {name} not satisfied")
