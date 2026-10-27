#!/usr/bin/env python3
"""kimi_gate.py — the governed kimi review gate's CLI shim (GE-5b, ADR-046).

The gate's body lives in ``scripts/lib/governed_lane.py``: phases 1-2 in
``run_gate_head`` (CLI, request record, diff/report acquisition) and phases 3-6 in
``run_gate_tail`` (verdict, evidence pair, record, output). This module keeps what a
script must keep — the CLI entry point, the names callers and tests read, and a
:class:`GateSeams` built from THIS module's globals so every monkeypatch on
``kimi_gate._get_diff``, ``kimi_gate._make_default_dispatcher``,
``kimi_gate._resolve_data_dir``, ``kimi_gate.persist_request`` and friends still
reaches the call. Provider differences live on the register row, never in a branch
here; kimi's lane declares no model pin, so ``--model`` is passed through unchanged.

Usage:
    python3 scripts/kimi_gate.py --pr 378 --data-dir ~/.vnx-data/<project>
    python3 scripts/kimi_gate.py --diff-file /tmp/x.diff --pr 0   # offline diff source

Exit codes: 0 = pass, 2 = fail/blocked (a REAL parsed review verdict), 1 = infra
error / provider unavailable. A provider outage is never booked as a review fail
(OI-1142); see ``governed_lane`` for the reasons, the evidence pair (OI-1178) and
the deadline split (OI-2066).
"""
from __future__ import annotations

import sys
from functools import partial
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from plan_gate_panel import _make_default_dispatcher, _resolve_data_dir  # this module's factory seam
from governed_lane import (  # GE-5a/GE-5b: one shared adapter for the harness lanes
    GateSeams, build_prompt, extract_verdict,
    frontmatter_run_outcome as _frontmatter_run_outcome, get_diff as _get_diff,
    run_gate_head, run_gate_tail, verdict_to_status,
)
from gate_recorder import (
    get_pr_head_branch, get_pr_head_sha, persist_request, record_terminal_result,
    result_file_path, stamp_request_identity,
)
from gate_artifacts import _compute_contract_hash  # canonical hasher; the tail uses governed_lane's binding
from gate_lane_contract import MODEL_DEFAULTS, VALID_VERDICTS, VERDICT_CONTRACT, max_diff_chars
from gate_report_recovery import find_recovery_candidate

_VALID_VERDICTS = VALID_VERDICTS  # C6 step 3 + OI-1767: one source, not a third literal copy
_PROVIDER = "kimi"
_MODEL_ENV, DEFAULT_MODEL = MODEL_DEFAULTS["kimi_gate"]
_VERDICT_CONTRACT = VERDICT_CONTRACT

_extract_verdict = partial(extract_verdict, provider=_PROVIDER)  # text is the first argument
_build_prompt = partial(build_prompt, _PROVIDER)
_verdict_to_status = partial(verdict_to_status, _PROVIDER)


def _gate_seams() -> GateSeams:
    """Phase 1-6 callables, resolved off THIS module at call time (tests patch these names here)."""
    return GateSeams(
        resolve_data_dir=_resolve_data_dir, get_diff=_get_diff,
        make_default_dispatcher=_make_default_dispatcher, persist_request=persist_request,
        max_diff_chars=max_diff_chars,
        get_pr_head_sha=get_pr_head_sha, get_pr_head_branch=get_pr_head_branch,
        record_terminal_result=record_terminal_result, result_file_path=result_file_path,
        stamp_request_identity=stamp_request_identity, find_recovery_candidate=find_recovery_candidate,
    )


def main(argv: "list[str] | None" = None) -> int:
    rc, state = run_gate_head(_PROVIDER, argv, seams=_gate_seams())
    if rc is not None:
        return rc
    return run_gate_tail(state)


if __name__ == "__main__":
    raise SystemExit(main())
