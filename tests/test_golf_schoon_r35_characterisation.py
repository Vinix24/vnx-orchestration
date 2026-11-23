"""Golf schoon R35: pins the receipt step of ``envelope_govern._govern`` (fields per outcome,
side-effect order archive/ledger/receipt/clear, proof-chain gap on a failed write) before the cut."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from dispatch_identity import _IDENTITY_UNRESOLVED
from envelope_govern import _govern
from envelope_types import EnvelopeSpec, _AdapterResult

_VALID = ("## Summary\n\nImplemented the feature with full test coverage, every test passes and it "
          "is complete.\n\n## Changes\n\n- X\n\n## Verification\n\n- pytest 5/5\n\n## Open Items\n\nNone\n")


@pytest.fixture()
def spec(tmp_path):
    for name in ("data", "state", "consumer"):
        (tmp_path / name).mkdir()
    return EnvelopeSpec(dispatch_id="r35-char-001", terminal_id="T1", provider="claude", model="sonnet",
                        instruction="do the thing", role="backend-developer", pr_id=None,
                        state_dir=tmp_path / "state", data_dir=tmp_path / "data")


def _run(spec, result, *, emit=None, role=None, classify=None, valid=False):
    calls: list = []
    receipts = spec.state_dir / "t0_receipts.ndjson"
    receipts.write_text("")
    if valid:
        (spec.data_dir / "unified_reports").mkdir()
        (spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md").write_text(_VALID)

    def _emit(**kwargs):
        calls.append(("receipt", kwargs))
        return emit(**kwargs) if emit else receipts

    patches = [
        patch("envelope_govern._archive_dispatch_events", side_effect=lambda *a: calls.append(("archive", a)) or ("/ev", True)),
        patch("envelope_govern._clear_dispatch_events", side_effect=lambda *a, **k: calls.append(("clear", a))),
        patch("envelope_govern._book_ledger_run", side_effect=lambda *a: calls.append(("ledger", a[-1])) or (None, None)),
        patch("envelope_govern._receipt_exists_for_dispatch", return_value=False),
        patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=spec.data_dir.parent / "consumer"),
        patch("dispatch_identity.resolve_effective_role", side_effect=role or (lambda *a, **k: "backend-developer")),
        patch("governance_emit.emit_dispatch_receipt", side_effect=_emit),
    ]
    if classify is not None:
        patches.append(patch("failure_classification.classify_failure_safe", side_effect=classify))
    for p in patches:
        p.start()
    try:
        out = _govern(spec, result, datetime(2026, 11, 15, 12, 0, 0), datetime(2026, 11, 15, 12, 1, 30))
    finally:
        for p in reversed(patches):
            p.stop()
    receipt_kwargs = [c[1] for c in calls if c[0] == "receipt"]
    assert len(receipt_kwargs) == 1 and [c[0] for c in calls] == ["archive", "ledger", "receipt", "clear"]
    trail = spec.state_dir / "receipt_emit_failures.ndjson"
    gaps = [json.loads(x) for x in trail.read_text().splitlines()] if trail.exists() else []
    return out, receipt_kwargs[0], gaps


def test_success_receipt_fields(spec):
    result = _AdapterResult(returncode=0, completion_text="ok", status="success", session_id="s-1")
    (report_path, receipt_path), kw, gaps = _run(spec, result, valid=True)
    assert (kw["status"], kw["completion_pct"], kw["warnings"], kw["lifecycle_v3"]) == ("success", 100, None, False)
    assert (kw["failure_reason"], kw["failure_class"], kw["role"]) == (None, None, "backend-developer")
    assert (kw["duration_seconds"], kw["events_path"], kw["model"], kw["session_id"]) == (90.0, "/ev", "sonnet", "s-1")
    assert (kw["report_path"], kw["state_dir"], kw["receipt_kind"]) == (str(report_path), spec.state_dir, "dispatch")
    assert kw["tool_call_count"] is None and kw["final_prompt_path"] is None
    assert receipt_path == spec.state_dir / "t0_receipts.ndjson" and gaps == []


@pytest.mark.parametrize("raises", [False, True])
def test_failure_receipt_carries_the_classification(spec, raises):
    seen = {}

    def _classify(**kwargs):
        seen.update(kwargs)
        if raises:
            raise RuntimeError("gone")
        return {"failure_class": "provider_crash", "failure_reason": "boom"}

    result = _AdapterResult(returncode=1, completion_text="part", status="failure", error="crashed")
    _, kw, _ = _run(spec, result, classify=_classify)
    assert (kw["status"], kw["completion_pct"]) == ("failure", 0)
    assert seen == {"status": "failure", "error": "crashed", "completion_text": "part", "timed_out": False,
                    "provider": "claude", "duration_seconds": 90.0, "returncode": 1, "dispatch_id": "r35-char-001"}
    expected = ("unknown", "failure_classification unavailable: RuntimeError('gone') (dispatch=r35-char-001)") \
        if raises else ("provider_crash", "boom")
    assert (kw["failure_class"], kw["failure_reason"]) == expected


def test_contract_invalid_receipt_and_role_fallback(spec):
    def _no_role(*args, **kwargs):
        raise LookupError("no role")

    result = _AdapterResult(returncode=0, completion_text="no headings", status="success")
    _, kw, _ = _run(spec, result, role=_no_role)
    assert (kw["status"], kw["completion_pct"], kw["role"]) == ("contract_invalid", 0, _IDENTITY_UNRESOLVED)
    assert [w["code"] for w in kw["warnings"]] == ["report_contract_violated"]
    assert (result.governed_status, result.status) == ("contract_invalid", "success")


@pytest.mark.parametrize("emit, reason", [
    (lambda **k: (_ for _ in ()).throw(OSError("disk full")), "disk full"),
    (lambda **k: None, "receipt_path is None after emit"),
    (lambda **k: Path("/nonexistent/r35/r.ndjson"), "receipt file absent on disk after emit path=/nonexistent/r35/r.ndjson"),
])
def test_failed_receipt_write_records_the_gap_and_does_not_raise(spec, emit, reason):
    result = _AdapterResult(returncode=0, completion_text="ok", status="success")
    (report_path, receipt_path), _, gaps = _run(spec, result, emit=emit, valid=True)
    assert receipt_path is None and report_path is not None
    assert [(g["reason"], g["work_status"]) for g in gaps] == [(reason, "success")]
