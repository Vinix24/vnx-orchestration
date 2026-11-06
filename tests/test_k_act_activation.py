"""ADR-045 audit-spoor-kern K-act: the dispatch lifecycle is on by default.

Dispatch-ID: dlv-775df50eaf02

The activation delivery flips ``VNX_LEDGER_LIFECYCLE`` to an on-by-default
setting (ADR-045 §9/§10): a dispatch that runs through the single-entry door
with no environment override books the three lifecycle lines, and
``VNX_LEDGER_LIFECYCLE=0`` is the emergency brake that restores the legacy
``task_complete`` shape untouched.

Both cases run the REAL door (``dispatch_cli.run_dispatch``) against a staged
spec in a throwaway git repo with a real store under ``tmp_path``. The only
production process replaced is the worker child: ``spawn_claude`` is stubbed to
run a small executable script that writes a valid report and exits 0, the same
seam ``tests/test_k5_finalizer_every_exit.py`` uses. Everything after the
worker — the hand-over, the start line, the lane's end line, the govern step
and the receipt-processor conversion of the report — is the real code path.

The conversion step is part of the contract, not a shortcut: the door books
``dispatch_started``/``dispatch_finished``, and the receipt processor books
``report_ingested`` when it ingests the worker's report. Together they are the
pipeline the acceptance case names.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import coordination_db
import dispatch_cli
import project_id_migration
import report_to_receipt_converter as converter
from append_receipt_internals import payload as payload_module
from dispatch_plan import ModelPin, RuntimeSnapshot
from provider_spawns.claude_spawn import ClaudeSpawnResult

PROJECT = "vnx-dev"
DISPATCH = "20261104-kact-activation"
REPORT_TEMPLATE = """---
schema_version: 1
dispatch_id: {dispatch_id}
provider: claude
sub_provider: anthropic
model: claude-sonnet-4-6
terminal_id: T1
pool_id: headless
role: backend-developer
task_class: implementation
pr_id: none
duration_seconds: 12.5
exit_code: 0
token_usage: {{input: 10, output: 5, cache_read: 0}}
cost_usd: 0.0
route_decision: {{strategy: default, selected_provider: claude, selected_model: claude-sonnet-4-6, reason: primary route}}
status: success
terminal: T1
timestamp: 2026-11-04T09:00:00Z
project_id: {project}
---

## Summary

Implemented the feature per the dispatch specification. All tests pass and coverage is at target.

## Changes

- scripts/lib/example.py: added X

## Verification

pytest tests/ -x: 42 passed

## Open Items

None
"""
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "kact",
    "GIT_AUTHOR_EMAIL": "kact@example.invalid",
    "GIT_COMMITTER_NAME": "kact",
    "GIT_COMMITTER_EMAIL": "kact@example.invalid",
}


def _clean_snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        constraint_verdicts=(),
        staging_promoted=True,
        target_health={"ephemeral": "healthy", "T1": "healthy"},
        target_capable={"ephemeral": True, "T1": True},
        model_pins={slot: ModelPin(model="sonnet", semantics="floor")
                    for slot in ("T0", "T1", "T2", "T3")},
    )


def _make_bundle(tmp_path: Path) -> Path:
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / f"staging-{DISPATCH}"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": 1, "project_id": PROJECT, "dispatch_id": DISPATCH,
        "staging_id": f"staging-{DISPATCH}", "instruction_file": str(instruction),
        "role": "backend-developer", "target_slot": "T1", "gate": "codex_gate",
        "dispatch_paths": [], "provider": "claude", "deadline_seconds": 3600,
        "isolation": "worktree", "base_ref": "origin/main",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return spec_file


def _make_repo(tmp_path: Path) -> Path:
    """A throwaway repo whose ``origin/main`` the door can resolve as its base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_GIT_ENV)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"],
                   cwd=repo, check=True, env=_GIT_ENV)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True, env=_GIT_ENV).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head],
                   cwd=repo, check=True, env=_GIT_ENV)
    return repo


def _make_store(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    coordination_db.init_schema(state_dir)
    project_id_migration.run_runtime_coordination_migration(
        coordination_db.db_path_from_state_dir(state_dir), default_project_id=PROJECT)


def _worker_script(tmp_path: Path, report_path: Path) -> Path:
    script = tmp_path / "worker.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"mkdir -p '{report_path.parent}'\n"
        f"cat > '{report_path}' <<'REPORT_EOF'\n"
        + REPORT_TEMPLATE.format(dispatch_id=DISPATCH, project=PROJECT)
        + "REPORT_EOF\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _spawn_running(script: Path):
    """The child seam: the real spawn contract, backed by the worker script."""
    def _spawn(**kwargs):
        on_spawn = kwargs.get("on_spawn")
        child = subprocess.Popen([str(script)])
        if on_spawn is not None:
            on_spawn(child.pid)
        returncode = child.wait()
        return ClaudeSpawnResult(
            returncode=returncode, completion={}, events_written=1,
            session_id="s-kact", timed_out=False, completion_text="done",
            model_resolved="claude-sonnet",
        )
    return _spawn


@contextlib.contextmanager
def _lane_slot(*args, **kwargs):
    yield


def _ledger(state_dir: Path) -> "list[dict]":
    path = state_dir / "t0_receipts.ndjson"
    if not path.is_file():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _run_door_and_ingest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> "list[dict]":
    data_dir = tmp_path / "vnx-data"
    state_dir = data_dir / "state"
    spec_file = _make_bundle(tmp_path)
    _make_store(state_dir)
    repo = _make_repo(tmp_path)
    report_path = data_dir / "unified_reports" / f"{DISPATCH}.md"
    script = _worker_script(tmp_path, report_path)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)

    append = payload_module.append_receipt_payload

    def _hermetic_append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return append(receipt, **kwargs)

    with patch.object(payload_module, "append_receipt_payload", _hermetic_append), \
         patch.object(payload_module.facade, "_emit_dispatch_register", lambda receipt: False), \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli.serialize_lane", _lane_slot), \
         patch("provider_spawns.claude_spawn.spawn_claude", side_effect=_spawn_running(script)), \
         patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=repo):
        assert dispatch_cli.run_dispatch(spec_file) == 0

    assert report_path.is_file(), "the worker script must have left a report behind"
    # The receipt processor's ingest step: no other production code is replaced
    # between the door finishing and the report being booked.
    converter.convert_report_to_receipt(report_path, receipts_file=str(state_dir / "t0_receipts.ndjson"))
    return _ledger(state_dir)


def _of_type(records: "list[dict]", event_type: str) -> "list[dict]":
    return [record for record in records if record.get("event_type") == event_type]


def test_the_lifecycle_is_the_default_of_a_real_door_run(tmp_path, monkeypatch):
    """No environment override: one start, one end with push evidence, one report."""
    monkeypatch.delenv("VNX_LEDGER_LIFECYCLE", raising=False)
    records = _run_door_and_ingest(tmp_path, monkeypatch)

    starts = _of_type(records, "dispatch_started")
    ends = _of_type(records, "dispatch_finished")
    reports = _of_type(records, "report_ingested")
    assert len(starts) == 1, records
    assert len(ends) == 1, records
    assert len(reports) == 1, records
    assert starts[0]["dispatch_id"] == DISPATCH
    assert ends[0]["dispatch_id"] == DISPATCH
    assert ends[0]["attempt"] == starts[0]["attempt"] == 1
    head_sha = ends[0].get("head_sha")
    no_push = ends[0].get("no_push")
    assert (isinstance(head_sha, str) and len(head_sha) == 40) \
        or (isinstance(no_push, str) and no_push.startswith("no_push:")), ends[0]
    assert reports[0]["dispatch_id"] == DISPATCH


def test_the_brake_restores_the_legacy_shape(tmp_path, monkeypatch):
    """``VNX_LEDGER_LIFECYCLE=0``: no lifecycle line at all, the legacy receipt as before."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")
    records = _run_door_and_ingest(tmp_path, monkeypatch)

    for event_type in ("dispatch_started", "dispatch_finished", "report_ingested"):
        assert _of_type(records, event_type) == [], records
    assert len(_of_type(records, "task_complete")) == 1, records
