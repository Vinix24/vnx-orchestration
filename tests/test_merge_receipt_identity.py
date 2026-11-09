#!/usr/bin/env python3
"""pr_merged carries project_id and delivery_id, the delivery identity of ADR-056 (zelfst PR-3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import governance_receipts  # noqa: E402
import pr_merge  # noqa: E402

# Keys of a pr_merged receipt written before PR-3 (the ones that are not run-specific).
OLD_KEYS = {
    "timestamp", "event_type", "receipt_kind", "status", "terminal", "source",
    "pr_number", "conclusion", "merge_method", "pr_title", "branch", "pr_id",
    "dispatch_id", "preflight_gates", "review_seats",
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A tmp central store whose path layout names project ``vnx-dev``."""
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    state = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_DATA_DIR", str(state.parent))
    return state / "t0_receipts.ndjson"


def _merge(receipts: Path, dispatch_id: str = "dlv-abc123") -> dict:
    pr_merge._emit_receipt(
        pr_number=42, dispatch_id=dispatch_id, merge_method="squash",
        pr_title="feat: x (PR-3)", branch="dispatch/dlv-abc123", pr_id="PR-3",
        receipts_file=str(receipts),
    )
    lines = [json.loads(line) for line in receipts.read_text().splitlines() if line.strip()]
    merged = [r for r in lines if r.get("event_type") == "pr_merged"]
    assert len(merged) == 1
    return merged[0]


def test_receipt_has_project_id_from_the_store(store):
    rec = _merge(store)
    assert rec.get("project_id") == "vnx-dev"
    print(json.dumps(rec, sort_keys=True))


def test_project_id_unknown_when_resolver_fails(store, monkeypatch):
    import project_scope

    def boom(*args, **kwargs):
        raise project_scope.TenantUnresolved("no source")

    monkeypatch.setattr(project_scope, "resolve_stamp_project_id", boom)
    assert _merge(store)["project_id"] == "unknown"


def test_project_id_unknown_when_resolver_returns_nothing(store, monkeypatch):
    import project_scope

    monkeypatch.setattr(project_scope, "resolve_stamp_project_id", lambda *a, **k: "")
    assert _merge(store)["project_id"] == "unknown"


def test_project_id_is_not_defaulted_without_a_source(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    receipts = tmp_path / "loose" / "t0_receipts.ndjson"
    receipts.parent.mkdir()
    assert _merge(receipts)["project_id"] == "unknown"


def test_delivery_id_of_a_base_id(store):
    assert _merge(store, "dlv-abc123")["delivery_id"] == "dlv-abc123"


def test_delivery_id_of_a_round_is_the_base(store):
    rec = _merge(store, "dlv-abc123-r3")
    assert rec["delivery_id"] == "dlv-abc123"
    assert rec["dispatch_id"] == "dlv-abc123-r3"


def test_delivery_id_unknown_without_a_dispatch_id(store):
    rec = _merge(store, "")
    assert rec["delivery_id"] == "unknown"
    assert "dispatch_id" not in rec


@pytest.mark.parametrize("value, expected", [
    ("dlv-abc-r1", "dlv-abc"),
    ("dlv-abc-r12", "dlv-abc"),
    ("dlv-abc-r0", "dlv-abc-r0"),
    ("dlv-abc-r03", "dlv-abc-r03"),
    ("dlv-abc-rX", "dlv-abc-rX"),
    ("dlv-abc-r", "dlv-abc-r"),
    ("dlv-abc-r2-r3", "dlv-abc-r2"),
    ("  dlv-abc-r3  ", "dlv-abc"),
    ("", "unknown"),
    ("   ", "unknown"),
    (None, "unknown"),
    (42, "unknown"),
])
def test_base_delivery_id(value, expected):
    assert governance_receipts.base_delivery_id(value) == expected


def test_base_delivery_id_agrees_with_is_round_of():
    from schema_manifest import is_round_of

    for suffix in ("1", "7", "10", "0", "03", "X", ""):
        round_id = f"dlv-q-r{suffix}"
        base = governance_receipts.base_delivery_id(round_id)
        assert (base == "dlv-q") == is_round_of("dlv-q", round_id)


def test_every_other_key_is_unchanged(store):
    rec = _merge(store)
    added = set(rec) - OLD_KEYS
    assert {"project_id", "delivery_id"} <= added
    assert not (OLD_KEYS - set(rec))
    assert rec["event_type"] == "pr_merged"
    assert rec["pr_number"] == 42
    assert rec["conclusion"] == "merged"
    assert rec["merge_method"] == "squash"
    assert rec["pr_id"] == "PR-3"


def test_refusal_receipt_is_unchanged(store):
    import inspect

    sig = inspect.signature(pr_merge._emit_refusal_receipt)
    kwargs = {n: "x" for n, p in sig.parameters.items()
              if p.default is inspect.Parameter.empty and p.kind is p.KEYWORD_ONLY}
    for name in kwargs:
        if name == "pr_number":
            kwargs[name] = 42
    kwargs["receipts_file"] = str(store)
    pr_merge._emit_refusal_receipt(**kwargs)
    rec = [json.loads(l) for l in store.read_text().splitlines() if l.strip()][-1]
    assert "delivery_id" not in rec
    assert rec.get("event_type") != "pr_merged"
