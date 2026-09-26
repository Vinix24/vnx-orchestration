"""Shared setup for the two review-stack test files.

``test_review_stack_subscription_first.py`` pins the order (subscription
reviewers first) and ``test_review_stack_execution.py`` pins how that order
runs (kimi through ``vnx gate``, the merge door, codex at its limit, one request
per gate). Both build on the same throwaway project store and the same registry
isolation, so the setup lives here once and the two files cannot drift apart.

Plain helpers, not fixtures: each test file wraps them in a three-line fixture,
the way ``tests/merge_target_helpers.py`` is used.
"""
from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config_registry as cr  # noqa: E402
import gate_recorder  # noqa: E402

PR = 1926
BRANCH = "dispatch/20260926-review-stack-subscription-first"
HEAD_SHA = "9f3c2a1b7e4d5c6a8b0f1e2d3c4b5a6978879605"


def isolate_config_registry(monkeypatch):
    """Same isolation tests/test_beta3_e1_review_gate_chain.py uses: no resolver
    or override left wired by an earlier test may reach ``config_runtime.get``.
    A generator, for ``yield from`` in an autouse fixture."""
    import config_runtime as crt
    for key in list(cr.CONFIG_REGISTRY):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(key)}", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    yield
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


def build_project_store(tmp_path, monkeypatch):
    """A throwaway project store, with the PR head and every provider edge fixed."""
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.chdir(project_root)

    import gate_request_handler
    import gate_result_parser
    # A recorded verdict is normally copied to the forge as a check-run. Not here: the
    # record on disk is what these tests read, and nothing may leave the machine.
    import forge_gate_publisher
    monkeypatch.setattr(forge_gate_publisher, "publish_for_record", lambda *a, **kw: None)
    monkeypatch.setattr(forge_gate_publisher, "publish_review_summary", lambda *a, **kw: None)
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", lambda _pr: HEAD_SHA)
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda _pr: HEAD_SHA)
    monkeypatch.setattr(gate_result_parser, "get_pr_head_sha", lambda _pr: HEAD_SHA)

    return {
        "project_root": project_root,
        "data_dir": data_dir,
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }
