#!/usr/bin/env python3
"""Tests for scripts/lib/guard_reachability_registry.py.

golf-4's core finding: three of the eight defects were already "explained"
in prose (a "Caveat" in a doc, "an accepted, intentional no-op" in a
docstring) and that is exactly what kept them invisible — a doc-comment is
not consulted by anything. ``ACCEPTED_GAPS`` is the registry's answer: a
machine-read escape hatch that REFUSES to load without a reason. These tests
prove the refusal actually fires, and that the real (in-repo) registry is
itself valid.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = VNX_ROOT / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import guard_reachability_registry as registry  # noqa: E402
from guard_reachability_registry import (  # noqa: E402
    AcceptedGap,
    FieldMapping,
    StoreTarget,
    validate_registry,
)


def test_real_registry_validates_clean():
    validate_registry()  # must not raise


def test_accepted_gap_with_empty_reason_is_rejected(monkeypatch):
    monkeypatch.setattr(
        registry, "ACCEPTED_GAPS",
        (AcceptedGap(field="x", reason="   ", decided_by="vincent", decided_on="2026-09-05"),),
    )
    with pytest.raises(ValueError, match="empty reason"):
        validate_registry()


def test_accepted_gap_missing_decided_by_is_rejected(monkeypatch):
    monkeypatch.setattr(
        registry, "ACCEPTED_GAPS",
        (AcceptedGap(field="x", reason="genuinely optional", decided_by="", decided_on="2026-09-05"),),
    )
    with pytest.raises(ValueError, match="decided_by"):
        validate_registry()


def test_accepted_gap_with_real_reason_passes(monkeypatch):
    monkeypatch.setattr(
        registry, "ACCEPTED_GAPS",
        (AcceptedGap(field="x", reason="genuinely optional field, reviewed", decided_by="vincent", decided_on="2026-09-05"),),
    )
    validate_registry()  # must not raise


def test_duplicate_field_mapping_is_rejected(monkeypatch):
    dup = FieldMapping(
        field="dup",
        targets=(StoreTarget(kind="ndjson", ndjson_relpaths=("x.ndjson",), note="t"),),
        note="n",
    )
    monkeypatch.setattr(registry, "FIELD_STORE_MAP", (dup, dup))
    with pytest.raises(ValueError, match="duplicate"):
        validate_registry()


def test_store_target_requires_kind_specific_fields():
    with pytest.raises(ValueError):
        StoreTarget(kind="sqlite", note="missing table/column")
    with pytest.raises(ValueError):
        StoreTarget(kind="ndjson", note="missing paths")
    with pytest.raises(ValueError):
        StoreTarget(kind="json_dir", note="missing dir")
    with pytest.raises(ValueError):
        StoreTarget(kind="not-a-real-kind", note="bogus")


def test_calibration_field_is_not_in_accepted_gaps():
    """OI-1632's track_id is a CONFIRMED historical bug, never a deliberate
    gap — belt-and-suspenders alongside the calibration self-test's own
    check of this same invariant."""
    accepted_fields = {g.field for g in registry.ACCEPTED_GAPS}
    assert "track_id" not in accepted_fields
