#!/usr/bin/env python3
"""Tests for routing_preflight.py — PR-3 quality gate (Python layer).

Gate: gate_pr3_routing_preflight_readiness

Covers:
  - Provider readiness checks (ready, misconfigured, unsupported)
  - Model readiness checks (ready, ready_with_switch, unsupported)
  - Pinned assumption verification
  - FEATURE_PLAN requirement extraction
  - Full preflight report generation
  - Routing state classification (unsupported, unavailable, misconfigured)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Add scripts to path
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import routing_preflight as rp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_env():
    """Remove all VNX terminal env vars to reset to defaults."""
    for tid in ("T0", "T1", "T2", "T3"):
        os.environ.pop(f"VNX_{tid}_PROVIDER", None)
        os.environ.pop(f"VNX_{tid}_MODEL", None)


def _write_feature_plan(content: str) -> Path:
    """Write a temporary FEATURE_PLAN.md and return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
    f.write(content)
    f.close()
    return Path(f.name)


# ---------------------------------------------------------------------------
# Section 1: Provider readiness
# ---------------------------------------------------------------------------

def test_provider_no_requirement():
    _clean_env()
    r = rp.check_provider_readiness("T1", "", "advisory")
    assert r.ready, "no provider requirement should be ready"
    assert r.gap == "", "no gap expected"


def test_provider_match():
    _clean_env()
    r = rp.check_provider_readiness("T1", "claude_code", "required")
    assert r.ready, "default claude_code should match"


def test_provider_mismatch_required():
    _clean_env()
    r = rp.check_provider_readiness("T1", "codex_cli", "required")
    assert not r.ready, "required mismatch should not be ready"
    assert r.gap == "misconfigured", f"expected misconfigured, got {r.gap}"


def test_provider_mismatch_advisory():
    _clean_env()
    r = rp.check_provider_readiness("T1", "codex_cli", "advisory")
    assert r.ready, "advisory mismatch should still be ready"


def test_provider_unsupported():
    _clean_env()
    r = rp.check_provider_readiness("T1", "unknown_llm", "required")
    assert not r.ready
    assert r.gap == "unsupported"


def test_provider_env_override():
    _clean_env()
    os.environ["VNX_T1_PROVIDER"] = "codex_cli"
    r = rp.check_provider_readiness("T1", "codex_cli", "required")
    assert r.ready, "env override should satisfy requirement"
    _clean_env()


# ---------------------------------------------------------------------------
# Section 2: Model readiness
# ---------------------------------------------------------------------------

def test_model_no_requirement():
    _clean_env()
    r = rp.check_model_readiness("T1", "", "advisory")
    assert r.ready


def test_model_pinned_match():
    _clean_env()
    r = rp.check_model_readiness("T1", "sonnet", "required")
    assert r.ready, "T1 pinned to sonnet should match"


def test_model_opus_default_equivalence():
    _clean_env()
    r = rp.check_model_readiness("T0", "opus", "required")
    assert r.ready, "opus == default should match for T0"


def test_model_switch_capable():
    _clean_env()
    r = rp.check_model_readiness("T1", "opus", "required")
    assert r.ready, "claude_code supports /model switch"
    assert r.can_switch, "should flag can_switch=True"


def test_model_no_switch_required():
    _clean_env()
    os.environ["VNX_T1_PROVIDER"] = "gemini_cli"
    r = rp.check_model_readiness("T1", "opus", "required")
    assert not r.ready, "gemini cannot switch models"
    assert r.gap == "unsupported"
    _clean_env()


def test_model_no_switch_advisory():
    _clean_env()
    os.environ["VNX_T1_PROVIDER"] = "gemini_cli"
    r = rp.check_model_readiness("T1", "opus", "advisory")
    assert r.ready, "advisory should pass even without switch capability"
    _clean_env()


def test_model_env_override():
    _clean_env()
    os.environ["VNX_T1_MODEL"] = "opus"
    r = rp.check_model_readiness("T1", "opus", "required")
    assert r.ready
    _clean_env()


# ---------------------------------------------------------------------------
# Section 3: Pinned assumptions
# ---------------------------------------------------------------------------

def test_pinned_default_verified():
    _clean_env()
    results = rp.check_pinned_assumptions()
    assert len(results) == 4
    for p in results:
        assert p.provider_ok, f"{p.terminal_id} provider should be ok"
        assert p.model_ok, f"{p.terminal_id} model should be ok"


def test_pinned_provider_drift():
    _clean_env()
    os.environ["VNX_T1_PROVIDER"] = "codex_cli"
    results = rp.check_pinned_assumptions()
    t1 = [p for p in results if p.terminal_id == "T1"][0]
    assert not t1.provider_ok, "T1 provider should show drift"
    _clean_env()


def test_pinned_model_drift():
    _clean_env()
    os.environ["VNX_T2_MODEL"] = "opus"
    results = rp.check_pinned_assumptions()
    t2 = [p for p in results if p.terminal_id == "T2"][0]
    assert not t2.model_ok, "T2 model should show drift"
    _clean_env()


# ---------------------------------------------------------------------------
# Section 4: FEATURE_PLAN extraction
# ---------------------------------------------------------------------------

SAMPLE_FEATURE_PLAN = """\
# Feature: Test

## PR-1: Provider Test
**Track**: B
**Priority**: P1

### Scope
- Requires-Provider: codex_cli required
- Requires-Model: opus

## PR-2: Model Test
**Track**: C
**Priority**: P1

### Scope
- Requires-Model: sonnet required
"""


def test_feature_plan_extraction():
    fp = _write_feature_plan(SAMPLE_FEATURE_PLAN)
    try:
        reqs = rp.extract_requirements_from_feature_plan(fp)
        assert len(reqs) == 3, f"expected 3 requirements, got {len(reqs)}"

        # PR-1 provider
        pr1_prov = [r for r in reqs if r.pr_id == "PR-1" and r.dimension == "provider"]
        assert len(pr1_prov) == 1
        assert pr1_prov[0].value == "codex_cli"
        assert pr1_prov[0].strength == "required"
        assert pr1_prov[0].terminal_id == "T2"  # Track B -> T2

        # PR-1 model
        pr1_model = [r for r in reqs if r.pr_id == "PR-1" and r.dimension == "model"]
        assert len(pr1_model) == 1
        assert pr1_model[0].value == "opus"
        assert pr1_model[0].strength == "advisory"

        # PR-2 model
        pr2_model = [r for r in reqs if r.pr_id == "PR-2" and r.dimension == "model"]
        assert len(pr2_model) == 1
        assert pr2_model[0].value == "sonnet"
        assert pr2_model[0].strength == "required"
        assert pr2_model[0].terminal_id == "T3"  # Track C -> T3
    finally:
        os.unlink(fp)


def test_feature_plan_filter_by_pr():
    fp = _write_feature_plan(SAMPLE_FEATURE_PLAN)
    try:
        reqs = rp.extract_requirements_from_feature_plan(fp, pr_id="PR-2")
        assert len(reqs) == 1
        assert reqs[0].pr_id == "PR-2"
    finally:
        os.unlink(fp)


# ---------------------------------------------------------------------------
# Section 5: Full preflight report
# ---------------------------------------------------------------------------

def test_preflight_all_pass():
    _clean_env()
    reqs = [
        rp.RoutingRequirement("provider", "claude_code", "required", "test", terminal_id="T1"),
        rp.RoutingRequirement("model", "sonnet", "required", "test", terminal_id="T1"),
    ]
    report = rp.run_routing_preflight(reqs, check_pinned=False)
    assert report.ready
    assert len(report.blocking) == 0
    assert len(report.checks) == 2


def test_preflight_provider_blocks():
    _clean_env()
    reqs = [
        rp.RoutingRequirement("provider", "codex_cli", "required", "test", terminal_id="T1"),
    ]
    report = rp.run_routing_preflight(reqs, check_pinned=False)
    assert not report.ready
    assert len(report.blocking) == 1
    assert report.blocking[0].gap == "misconfigured"


def test_preflight_model_blocks_on_gemini():
    _clean_env()
    os.environ["VNX_T2_PROVIDER"] = "gemini_cli"
    reqs = [
        rp.RoutingRequirement("model", "opus", "required", "test", terminal_id="T2"),
    ]
    report = rp.run_routing_preflight(reqs, check_pinned=False)
    assert not report.ready
    assert len(report.blocking) == 1
    assert report.blocking[0].gap == "unsupported"
    _clean_env()


def test_preflight_advisory_warnings():
    _clean_env()
    reqs = [
        rp.RoutingRequirement("provider", "codex_cli", "advisory", "test", terminal_id="T1"),
    ]
    report = rp.run_routing_preflight(reqs, check_pinned=False)
    assert report.ready, "advisory should not block"
    assert len(report.warnings) == 1


def test_preflight_with_pinned():
    _clean_env()
    report = rp.run_routing_preflight([], check_pinned=True)
    assert report.ready
    assert len(report.pinned) == 4


# ---------------------------------------------------------------------------
# Section 6: State classification — T0 can distinguish states
# ---------------------------------------------------------------------------

def test_classify_unsupported():
    _clean_env()
    r = rp.check_provider_readiness("T1", "llama_cli", "required")
    assert r.gap == "unsupported"


def test_classify_misconfigured():
    _clean_env()
    r = rp.check_provider_readiness("T1", "gemini_cli", "required")
    assert r.gap == "misconfigured"


def test_classify_model_unsupported():
    _clean_env()
    os.environ["VNX_T1_PROVIDER"] = "gemini_cli"
    r = rp.check_model_readiness("T1", "opus", "required")
    assert r.gap == "unsupported"
    _clean_env()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    import traceback

    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    passed = 0
    failed = 0

    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL: {t.__name__}")
            traceback.print_exc()
            failed += 1

    print(f"\n=== Results ===")
    print(f"PASS: {passed}")
    print(f"FAIL: {failed}")

    if failed:
        print(f"\nRESULT: FAIL ({failed} test(s) failed)")
        return 1
    else:
        print(f"\nRESULT: PASS — all {passed} routing preflight Python tests passed")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
