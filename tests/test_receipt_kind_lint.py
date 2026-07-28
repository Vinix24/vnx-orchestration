"""test_receipt_kind_lint.py — receipt-quality PR-3: emit-time receipt_kind lint (warn -> raise).

Covers:
- the authoritative closed set (plan §3b) in dispatch_identity.RECEIPT_KINDS
- validate_receipt_kind: accepts closed-set members, raises on missing/unknown
- emit_dispatch_receipt (Path 1, governance_emit): raises ValueError when
  receipt_kind is missing or out-of-vocab; role stays optional (null-stamped)
- emit_governance_receipt (governance_receipts): receipt_kind is a required
  kwarg (TypeError when omitted), ValueError on out-of-vocab, and is stamped
  into the appended receipt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from dispatch_identity import RECEIPT_KINDS, validate_receipt_kind
from governance_emit import emit_dispatch_receipt
from governance_receipts import emit_governance_receipt


# ---------------------------------------------------------------------------
# Closed set (plan §3b — authoritative vocabulary)
# ---------------------------------------------------------------------------

def test_closed_set_is_exactly_the_authoritative_vocabulary():
    assert RECEIPT_KINDS == frozenset({
        "build", "doc", "test", "review_gate", "panel_seat",
        "state_mutation", "sub_dispatch", "dispatch",
    })


def test_validate_receipt_kind_accepts_every_member():
    for kind in sorted(RECEIPT_KINDS):
        assert validate_receipt_kind(kind) == kind


def test_validate_receipt_kind_raises_on_missing():
    with pytest.raises(ValueError, match="receipt_kind"):
        validate_receipt_kind(None)


def test_validate_receipt_kind_raises_on_unknown():
    with pytest.raises(ValueError, match="receipt_kind"):
        validate_receipt_kind("governance")


# ---------------------------------------------------------------------------
# emit_dispatch_receipt (Path 1) — lint raise
# ---------------------------------------------------------------------------

def _emit_kwargs(state_dir):
    return dict(
        dispatch_id="lint-dispatch-001",
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-4-6",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=1.0,
        token_usage={"input": 1, "output": 1},
        cost_usd=None,
        state_dir=state_dir,
    )


def test_emit_dispatch_receipt_without_receipt_kind_raises(tmp_path):
    """PR-3 acceptance: an emit without receipt_kind hard-fails (warn -> raise)."""
    with pytest.raises(ValueError, match="receipt_kind"):
        emit_dispatch_receipt(**_emit_kwargs(tmp_path))
    # Fail-before-write: no receipt line may be appended.
    receipts = tmp_path / "t0_receipts.ndjson"
    assert not receipts.exists() or not receipts.read_text().strip()


def test_emit_dispatch_receipt_with_unknown_receipt_kind_raises(tmp_path):
    kwargs = _emit_kwargs(tmp_path)
    kwargs["receipt_kind"] = "task_class"  # explicitly NOT the vocab (§3b)
    with pytest.raises(ValueError, match="receipt_kind"):
        emit_dispatch_receipt(**kwargs)


def test_emit_dispatch_receipt_role_stays_optional(tmp_path):
    """role=None is still a valid, counted state — only receipt_kind is linted."""
    kwargs = _emit_kwargs(tmp_path)
    kwargs["receipt_kind"] = "dispatch"
    emit_dispatch_receipt(**kwargs)
    data = json.loads((tmp_path / "t0_receipts.ndjson").read_text().strip())
    assert data["role"] is None
    assert data["receipt_kind"] == "dispatch"


# ---------------------------------------------------------------------------
# emit_governance_receipt — required kwarg + lint raise
# ---------------------------------------------------------------------------

def test_emit_governance_receipt_without_receipt_kind_raises(tmp_path):
    """receipt_kind is a required kwarg — Python itself hard-fails the emit."""
    with pytest.raises(TypeError):
        emit_governance_receipt(
            "roadmap_transition",
            status="success",
            receipts_file=str(tmp_path / "t0_receipts.ndjson"),
        )


def test_emit_governance_receipt_with_unknown_receipt_kind_raises(tmp_path):
    with pytest.raises(ValueError, match="receipt_kind"):
        emit_governance_receipt(
            "roadmap_transition",
            receipt_kind="roadmap",  # not in the closed set
            status="success",
            receipts_file=str(tmp_path / "t0_receipts.ndjson"),
        )


def test_emit_governance_receipt_stamps_receipt_kind(tmp_path):
    receipts_file = tmp_path / "t0_receipts.ndjson"
    receipt = emit_governance_receipt(
        "pr_merged",
        receipt_kind="state_mutation",
        status="success",
        source="pr_merge",
        receipts_file=str(receipts_file),
    )
    assert receipt["receipt_kind"] == "state_mutation"
    data = json.loads(receipts_file.read_text().strip().splitlines()[-1])
    assert data["receipt_kind"] == "state_mutation"
    # Non-dispatch kinds are role-exempt: no role field is required/stamped.
    assert "role" not in data


def test_emit_governance_receipt_review_gate_kind_is_role_exempt(tmp_path):
    receipts_file = tmp_path / "t0_receipts.ndjson"
    emit_governance_receipt(
        "review_gate_request",
        receipt_kind="review_gate",
        status="requested",
        gate="ci_gate",
        dispatch_id="lint-dispatch-002",
        receipts_file=str(receipts_file),
    )
    data = json.loads(receipts_file.read_text().strip().splitlines()[-1])
    assert data["receipt_kind"] == "review_gate"
    assert "role" not in data
