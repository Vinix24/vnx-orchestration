"""Cost provenance reaches the written report (dispatch 20261019-kp-k1-r2).

The codex seat FAIL on head ``845d0d5a`` was the ``_compute_cost`` wrapper
discarding ``price_run``'s ``(cost_usd, cost_source)`` pair: an unpriced lane
resolved to ``cost_usd=None``, which ``_build_frontmatter`` then collapsed to
``0.0`` — recording a billable run as free with no provenance.

These tests drive the real report path — ``_compute_cost`` followed by
``_build_frontmatter`` — and pin the two ends of the contract:

* an unavailable price stays ``None`` and carries an ``unavailable:`` source
  (never a silent 0), and
* a genuinely free run (measured $0, e.g. local inference) keeps ``0.0`` with
  its own non-unavailable source.

Round 3 (dispatch 20261020-kp-k1-r3, OI-2194): that null failed
``schemas/unified_report_v1.json``, which demanded a number. The schema now
accepts ``cost_usd: null`` only next to an ``unavailable:`` source, the
converter carries the pair to the receipt unchanged, and the govern step no
longer turns an unavailable price back into 0.0.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_govern  # noqa: E402
import governance_emit  # noqa: E402
import provider_dispatch  # noqa: E402
import report_to_receipt_converter  # noqa: E402
from unified_report_schema import SchemaViolation, validate_frontmatter  # noqa: E402

_UNPRICED = ("litellm:openrouter", "openrouter/openai/gpt-4o-mini")
_BILLABLE_USAGE = {"input": 1_000_000, "output": 1_000_000, "cache_hit": 0}
_BODY = (
    "## Summary\n\nPriced the run through the registry and wrote the report with "
    "its cost provenance for the receipt converter.\n\n"
    "## Changes\n\n- scripts/lib/provider_dispatch.py: report cost pair\n\n"
    "## Verification\n\npytest tests/test_cost_source_propagation.py: green\n\n"
    "## Open Items\n\nNone\n"
)


def _make_args(dispatch_id: str = "cost-source-test-001") -> argparse.Namespace:
    return argparse.Namespace(
        dispatch_id=dispatch_id,
        terminal_id="T1",
        role="backend-developer",
        pr_id=None,
        task_class=None,
    )


class _BareResult:
    """Result stub with no frontmatter_fields() — matches the hasattr() miss path."""

    returncode = 0


def _frontmatter(provider: str, model: str, token_usage: dict, cost_usd, cost_source: str,
                 dispatch_id: str = "cost-source-test-001") -> dict:
    return provider_dispatch._build_frontmatter(
        _make_args(dispatch_id),
        provider,
        model,
        _BareResult(),
        1.0,
        token_usage,
        cost_usd,
        cost_source,
    )


def _report_text(frontmatter: dict) -> str:
    return f"---\n{yaml.safe_dump(frontmatter, sort_keys=False)}---\n\n{_BODY}"


def _emit(tmp_path: Path, frontmatter: dict) -> Path:
    """Write the report through the real emitter (it validates the frontmatter)."""
    return governance_emit.emit_unified_report(
        dispatch_id=frontmatter["dispatch_id"],
        terminal_id="T1",
        provider=frontmatter["route_decision"]["selected_provider"],
        instruction="price the run",
        response_text="",
        findings=None,
        duration_seconds=1.0,
        data_dir=tmp_path,
        frontmatter=frontmatter,
        body_override=_BODY,
    )


def _convert(report_path: Path, monkeypatch) -> dict:
    # Delivery is checked against GitHub/origin; this test is about the cost pair.
    monkeypatch.setattr(report_to_receipt_converter, "_check_branch_on_origin", lambda _did: True)
    text = report_path.read_text(encoding="utf-8")
    return report_to_receipt_converter.build_receipt_from_report(report_path, text)


# Red run: on the unfixed head 845d0d5a this test fails — _build_frontmatter
# collapsed the unavailable price's None to 0.0 and dropped the source, so a
# billable lane was recorded as free.
def test_unavailable_price_reaches_frontmatter_as_none_not_zero():
    cost, cost_source = provider_dispatch._compute_cost(*_UNPRICED, _BILLABLE_USAGE)
    assert cost is None
    assert cost_source.startswith("unavailable:")

    fm = _frontmatter(*_UNPRICED, _BILLABLE_USAGE, cost, cost_source)
    assert fm["cost_usd"] is None
    assert fm["cost_source"].startswith("unavailable:")


def test_measured_free_run_keeps_zero_with_non_unavailable_source():
    """local-gemma bills 0.0/0.0 per MTok — a measured $0, not an unavailable price."""
    token_usage = {"input": 100, "output": 50, "cache_hit": 0}
    cost, cost_source = provider_dispatch._compute_cost(
        "local-gemma", "gemma-4b-local", token_usage
    )
    assert cost == 0.0
    assert cost_source and not cost_source.startswith("unavailable:")

    fm = _frontmatter("local-gemma", "gemma-4b-local", token_usage, cost, cost_source)
    assert fm["cost_usd"] == 0.0
    assert fm["cost_source"] and not fm["cost_source"].startswith("unavailable:")


# Red run: on head df74d588 this test fails. The emitter raised
# SchemaViolation("invalid type: cost_usd (expected number)") under
# VNX_SCHEMA_STRICT=1, and the converter booked the report as task_failed
# with a frontmatter_v1 violation and no cost on the receipt (OI-2194).
def test_unavailable_price_report_passes_strict_schema_and_reaches_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")
    cost, cost_source = provider_dispatch._compute_cost(*_UNPRICED, _BILLABLE_USAGE)
    fm = _frontmatter(*_UNPRICED, _BILLABLE_USAGE, cost, cost_source,
                      dispatch_id="20261020-kp-k1-r3-unpriced")

    report_path = _emit(tmp_path, fm)
    written = validate_frontmatter(report_path.read_text(encoding="utf-8"))
    assert written["cost_usd"] is None
    assert written["cost_source"] == cost_source

    receipt = _convert(report_path, monkeypatch)
    assert receipt["event_type"] == "task_complete", receipt.get("fail_closed_violations")
    assert "cost_usd" in receipt and receipt["cost_usd"] is None
    assert receipt["cost_source"] == cost_source


def test_null_cost_without_source_fails_schema_and_fail_closed_check(tmp_path, monkeypatch):
    fm = _frontmatter(*_UNPRICED, _BILLABLE_USAGE, 0.5, "", dispatch_id="20261020-kp-k1-r3-nosrc")
    fm["cost_usd"] = None
    fm.pop("cost_source", None)
    text = _report_text(fm)

    with pytest.raises(SchemaViolation, match="missing field: cost_source"):
        validate_frontmatter(text)

    report_path = tmp_path / "20261020-kp-k1-r3-nosrc.md"
    report_path.write_text(text, encoding="utf-8")
    receipt = _convert(report_path, monkeypatch)
    assert receipt["event_type"] == "task_failed"
    assert "frontmatter_v1: missing field: cost_source" in receipt["fail_closed_violations"]


def test_null_cost_with_a_priced_source_fails_schema():
    fm = _frontmatter(*_UNPRICED, _BILLABLE_USAGE, None, "unavailable:no_price:x")
    fm["cost_source"] = "gpt-4o-mini@2026-09-14"
    with pytest.raises(SchemaViolation, match="cost_source"):
        validate_frontmatter(_report_text(fm))


def test_unavailable_source_next_to_a_number_fails_schema():
    """Round 1 at the contract: an unavailable price is never written as 0.0."""
    fm = _frontmatter(*_UNPRICED, _BILLABLE_USAGE, 0.0, "unavailable:no_price:x")
    with pytest.raises(SchemaViolation, match=r"invalid type: cost_usd \(expected null\)"):
        validate_frontmatter(_report_text(fm))


def test_measured_free_run_report_validates_and_receipt_keeps_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")
    token_usage = {"input": 100, "output": 50, "cache_hit": 0}
    cost, cost_source = provider_dispatch._compute_cost("local-gemma", "gemma-4b-local", token_usage)
    fm = _frontmatter("local-gemma", "gemma-4b-local", token_usage, cost, cost_source,
                      dispatch_id="20261020-kp-k1-r3-free")

    receipt = _convert(_emit(tmp_path, fm), monkeypatch)
    assert receipt["event_type"] == "task_complete", receipt.get("fail_closed_violations")
    assert receipt["cost_usd"] == 0.0
    assert receipt["cost_source"] == cost_source
    assert not receipt["cost_source"].startswith("unavailable:")


def test_no_usage_null_cost_is_named_and_validates(tmp_path, monkeypatch):
    """``_compute_cost`` prices nothing without a reason when usage is missing.

    The report still says why: ``unavailable:no_usage``, never a bare null and
    never 0.0.
    """
    monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")
    usage = {"unavailable": True}
    cost, cost_source = provider_dispatch._compute_cost(*_UNPRICED, usage)
    assert (cost, cost_source) == (None, "")

    fm = _frontmatter(*_UNPRICED, {}, cost, cost_source, dispatch_id="20261020-kp-k1-r3-nousage")
    assert fm["cost_usd"] is None
    assert fm["cost_source"] == "unavailable:no_usage"
    written = validate_frontmatter(_emit(tmp_path, fm).read_text(encoding="utf-8"))
    assert written["cost_source"] == "unavailable:no_usage"


def test_pricing_failure_is_named_not_mistaken_for_no_usage(monkeypatch):
    """A crash inside pricing is its own reason, not ``no_usage`` and not 0.0."""
    import provider_costs

    def _broken_price_run(*_args, **_kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(provider_dispatch, "_load_pricing_from_registry", lambda *_a: None)
    monkeypatch.setattr(provider_costs, "price_run", _broken_price_run)
    cost, cost_source = provider_dispatch._compute_cost(*_UNPRICED, _BILLABLE_USAGE)
    assert (cost, cost_source) == (None, "unavailable:price_error")

    fm = _frontmatter(*_UNPRICED, _BILLABLE_USAGE, cost, cost_source)
    assert validate_frontmatter(_report_text(fm))["cost_source"] == "unavailable:price_error"


def test_missing_cost_key_keeps_its_fail_closed_violation(tmp_path, monkeypatch):
    fm = _frontmatter(*_UNPRICED, _BILLABLE_USAGE, 0.5, "", dispatch_id="20261020-kp-k1-r3-nokey")
    fm.pop("cost_usd")
    report_path = tmp_path / "20261020-kp-k1-r3-nokey.md"
    report_path.write_text(_report_text(fm), encoding="utf-8")

    receipt = _convert(report_path, monkeypatch)
    assert receipt["event_type"] == "task_failed"
    assert "frontmatter_v1: missing field: cost_usd" in receipt["fail_closed_violations"]
    assert "cost_usd" not in receipt and "cost_source" not in receipt


# ---------------------------------------------------------------------------
# dispatch_govern: the subprocess lane hands its price to govern() as a
# receipt dict; the govern step builds the report frontmatter from it.
# ---------------------------------------------------------------------------

def _govern_frontmatter(tmp_path: Path, monkeypatch, receipt: dict, dispatch_id: str) -> dict:
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")
    monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    (data_dir / "unified_reports").mkdir(parents=True)
    state_dir.mkdir()
    (data_dir / "unified_reports" / f"{dispatch_id}.md").write_text(_BODY, encoding="utf-8")
    spec = dispatch_govern.GovernSpec(
        dispatch_id=dispatch_id, terminal_id="T1", instruction="price the run",
        data_dir=data_dir, state_dir=state_dir, model="sonnet",
    )
    outcome = dispatch_govern.govern(
        spec, dispatch_govern.GovernRaw(receipt=receipt, duration_seconds=1.0), lane="subprocess",
    )
    return validate_frontmatter(Path(outcome.report_path).read_text(encoding="utf-8"))


# Red run: on head df74d588 this test fails — the govern step wrote
# float(None or 0.0) and dropped the source, booking the unavailable price as 0.0.
def test_govern_keeps_unavailable_price_null_with_its_source(tmp_path, monkeypatch):
    fm = _govern_frontmatter(tmp_path, monkeypatch, {
        "status": "done", "model": "sonnet",
        "cost_usd": None, "cost_source": "unavailable:no_price:sonnet",
    }, "20261020-kp-k1-r3-govern-unpriced")
    assert fm["cost_usd"] is None
    assert fm["cost_source"] == "unavailable:no_price:sonnet"


def test_govern_keeps_a_priced_cost_and_its_source(tmp_path, monkeypatch):
    fm = _govern_frontmatter(tmp_path, monkeypatch, {
        "status": "done", "model": "sonnet",
        "cost_usd": 0.42, "cost_source": "sonnet@2026-09-14",
    }, "20261020-kp-k1-r3-govern-priced")
    assert fm["cost_usd"] == 0.42
    assert fm["cost_source"] == "sonnet@2026-09-14"


def test_govern_without_any_price_keeps_its_zero(tmp_path, monkeypatch):
    fm = _govern_frontmatter(tmp_path, monkeypatch, {"status": "done", "model": "sonnet"},
                             "20261020-kp-k1-r3-govern-none")
    assert fm["cost_usd"] == 0.0
    assert "cost_source" not in fm


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
