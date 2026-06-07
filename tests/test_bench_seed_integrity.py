"""Seed-integrity guard for the field-tests benchmark.

Every task verifier MUST fail against the untouched repository state. If a
verifier passes on the bare seed, the seed contains the solution and every
benchmark score for that task is a false positive (codex-gate PR #831
finding, 2026-06-07 — seeds shipped with their reference solutions and the
scorer verified the repo root).

This guard runs each task's ``verify(workdir=repo_root)`` exactly like the
scorer does and asserts ``pass is False``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = REPO_ROOT / "scripts" / "benchmark" / "field-tests" / "tasks"

TASK_FOLDERS = sorted(
    p.relative_to(TASKS_DIR)
    for tier in TASKS_DIR.iterdir() if tier.is_dir()
    for p in tier.iterdir() if (p / "verify.py").exists()
)


def _load_verify(task_rel: Path):
    vp = TASKS_DIR / task_rel / "verify.py"
    spec = importlib.util.spec_from_file_location(
        f"seed_integrity_{task_rel.name}", vp,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.verify


@pytest.mark.parametrize("task_rel", TASK_FOLDERS, ids=[p.name for p in TASK_FOLDERS])
def test_verifier_fails_on_bare_seed(task_rel):
    verify = _load_verify(task_rel)
    result = verify(REPO_ROOT, {})
    assert result.get("pass") is False, (
        f"verify for {task_rel} PASSES on the untouched seed — the seed "
        f"contains the solution. Evidence: {result.get('evidence')!r}"
    )


def test_all_tasks_discovered():
    # tasks.yaml registers 9 tasks; a silently missing verify.py would
    # shrink the parametrization and mask a gap.
    assert len(TASK_FOLDERS) == 9, f"expected 9 task verifiers, found {len(TASK_FOLDERS)}: {TASK_FOLDERS}"
