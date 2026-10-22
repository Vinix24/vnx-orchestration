"""R7: a review seat never gets a reviewer of the builder's vendor (dispatch dlv-9906a482950d).

Measured on PR #2168 (operator decision 2026-10-22): kimi was at its weekly
limit, ``deepseek_gate`` took the kimi seat, and the head it reviewed was
built by ``deepseek-v4-pro`` (dispatch ``dlv-c089a8e32893-r5``, provider
``deepseek-harness``). Nothing in the takeover walk compared the reviewer's
vendor with the builder's.

The rule under test, through the real ``request_reviews`` walk on a real
store (specs and result records on disk, no patched decision code):

- the builder's vendor is read from the dispatch spec of the dispatch the
  request names;
- a reviewer of that vendor is passed over like an unavailable one, first
  choice or takeover, and the record names it (``takeover_from``,
  ``takeover_reason: model_matches_builder``);
- an unknown builder vendor applies no rule, and every seat says so.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_lane_contract
import review_vendor
from providers import model_normalizer
from review_vendor import (
    RULE_APPLIED,
    RULE_NOT_APPLIED,
    MODEL_MATCH_REASON,
    gate_vendor,
    resolve_builder_vendor,
    vendor_from_spec,
)


@pytest.fixture(autouse=True)
def _isolate_model_normalizer_caches(monkeypatch):
    """The model comparison reads the registry through the normalizer's per-process tables (R7b r3).

    A tmp-registry test that ran earlier in the worker would leave its tables
    behind and turn every claude name into an unresolved one here. Reset for
    each test; the monkeypatch restores what it found at teardown.
    """
    for name in ("_registry_cache", "_alias_cache", "_keys_cache"):
        monkeypatch.setattr(model_normalizer, name, None)


_FRESH = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
_KIMI_WEEKLY_LIMIT = (
    "governed kimi dispatch failed: Error code: 403 - You've reached your weekly (7-day) usage "
    "limit. Your quota will reset when the current 7-day window ends"
)
_CLAUDE_EXHAUSTED = "claude seat: Error code: 403 - {'error': {'type': 'access_terminated_error'}}"


# ---------------------------------------------------------------------------
# Vendor of a reviewer and of a builder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate,vendor", [
    ("codex_gate", "openai"),
    ("kimi_gate", "moonshot"),
    ("claude_gate", "anthropic"),
    ("deepseek_gate", "deepseek"),
    ("glm_gate", "zhipu"),
])
def test_every_model_reviewer_has_the_vendor_the_operator_named(gate, vendor):
    assert gate_vendor(gate) == vendor


@pytest.mark.parametrize("gate", ["ci_gate", "wiring_gate", "claude_github_optional", "not_a_gate"])
def test_a_gate_that_calls_no_model_has_no_vendor(gate):
    assert gate_vendor(gate) is None


@pytest.mark.parametrize("provider,model,vendor", [
    ("claude", "sonnet", "anthropic"),
    ("claude", "opus", "anthropic"),
    ("claude", None, "anthropic"),
    ("deepseek-harness", "deepseek-v4-pro", "deepseek"),
    ("deepseek-harness", "deepseek-v4-flash", "deepseek"),
    ("deepseek-harness", "default", "deepseek"),
    ("kimi", "kimi-k3", "moonshot"),
    ("codex", "gpt-5.5", "openai"),
    ("glm-harness", "glm-5.2", "zhipu"),
    ("litellm:zai", None, "zhipu"),
    ("litellm:deepseek", "deepseek-v4-pro", "deepseek"),
    ("auto", "deepseek-v4-pro", "deepseek"),
])
def test_the_spec_names_the_builder_vendor(provider, model, vendor):
    assert vendor_from_spec(provider, model)[0] == vendor


def test_a_spec_that_names_two_makers_is_not_guessed():
    """Measured in the vnx-dev store: two specs carry provider claude with model kimi."""
    vendor, reason = vendor_from_spec("claude", "kimi")
    assert vendor is None
    assert "contradicts" in reason and "anthropic" in reason and "moonshot" in reason


@pytest.mark.parametrize("provider,model", [("auto", None), ("", ""), (None, None), ("mistral", "large")])
def test_a_spec_that_names_no_maker_has_no_vendor(provider, model):
    vendor, reason = vendor_from_spec(provider, model)
    assert vendor is None
    assert "names a model vendor" in reason


def _write_spec(data_dir: Path, dispatch_id: str, spec: object, status: str = "active") -> Path:
    bundle = data_dir / "dispatches" / status / dispatch_id
    bundle.mkdir(parents=True, exist_ok=True)
    path = bundle / "dispatch-spec.json"
    path.write_text(spec if isinstance(spec, str) else json.dumps(spec), encoding="utf-8")
    return path


class TestResolveBuilderVendor:
    def test_reads_the_spec_of_the_named_dispatch(self, tmp_path):
        path = _write_spec(tmp_path, "dlv-c089a8e32893-r5",
                           {"provider": "deepseek-harness", "model": "deepseek-v4-pro"})
        builder = resolve_builder_vendor("dlv-c089a8e32893-r5", data_dir=tmp_path)
        assert builder.vendor == "deepseek"
        assert builder.source == str(path)
        record = builder.to_record()
        assert record["rule"] == RULE_APPLIED
        assert (record["provider"], record["model"]) == ("deepseek-harness", "deepseek-v4-pro")

    def test_no_dispatch_named_applies_no_rule(self, tmp_path):
        builder = resolve_builder_vendor("", data_dir=tmp_path)
        assert builder.vendor is None
        assert builder.to_record()["rule"] == RULE_NOT_APPLIED
        assert "names no dispatch" in builder.reason

    def test_a_dispatch_without_a_spec_applies_no_rule(self, tmp_path):
        builder = resolve_builder_vendor("dlv-unknown", data_dir=tmp_path)
        assert builder.vendor is None
        assert "no dispatch-spec.json for dlv-unknown" in builder.reason

    def test_an_unreadable_spec_applies_no_rule(self, tmp_path):
        _write_spec(tmp_path, "dlv-broken", "{not json")
        builder = resolve_builder_vendor("dlv-broken", data_dir=tmp_path)
        assert builder.vendor is None
        assert "cannot be read" in builder.reason

    def test_specs_of_one_dispatch_that_disagree_apply_no_rule(self, tmp_path):
        _write_spec(tmp_path, "dlv-twice", {"provider": "claude", "model": "opus"}, status="completed")
        _write_spec(tmp_path, "dlv-twice", {"provider": "kimi", "model": "kimi-k3"}, status="pending")
        builder = resolve_builder_vendor("dlv-twice", data_dir=tmp_path)
        assert builder.vendor is None
        assert "different vendors" in builder.reason

    def test_a_path_like_dispatch_id_is_never_read(self, tmp_path):
        builder = resolve_builder_vendor("../etc", data_dir=tmp_path)
        assert builder.vendor is None
        assert "not a plain name" in builder.reason


# ---------------------------------------------------------------------------
# The rule in the real takeover walk (request_reviews)
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    for d in (state_dir / "review_gates" / "requests", state_dir / "review_gates" / "results",
              data_dir / "unified_reports"):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    for key in ("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(project_root)
    return {"data_dir": data_dir, "requests": state_dir / "review_gates" / "requests",
            "results": state_dir / "review_gates" / "results"}


def _exhausted(results: Path, pr: int, gate: str, text: str) -> None:
    (results / f"pr-{pr}-{gate}.json").write_text(json.dumps({
        "gate": gate, "pr_number": pr, "pr_id": str(pr), "status": "unavailable",
        "reason": "dispatch_error", "residual_risk": text, "recorded_at": _FRESH,
        "contract_hash": "", "report_path": "", "branch": "fix/r7", "commit_sha": "a" * 40,
    }), encoding="utf-8")


def _request(pr: int, stack: list, dispatch_id: str) -> dict:
    import review_gate_manager as rgm

    with patch("governance_receipts.emit_governance_receipt"):
        return rgm.ReviewGateManager().request_reviews(
            pr_number=pr, branch="fix/r7", review_stack=stack, risk_class="medium",
            changed_files=["scripts/lib/gate_request_handler.py"], mode="per_pr",
            dispatch_id=dispatch_id,
        )


def _record(requests: Path, pr: int, gate: str) -> dict:
    return json.loads((requests / f"pr-{pr}-{gate}.json").read_text(encoding="utf-8"))


class TestVendorRuleInTheWalk:
    def test_deepseek_built_head_with_kimi_out_gets_claude_not_deepseek(self, store):
        """The #2168 shape: kimi at its weekly limit, the head built by deepseek-v4-pro."""
        pr = 2168
        _write_spec(store["data_dir"], "dlv-c089a8e32893-r5",
                    {"provider": "deepseek-harness", "model": "deepseek-v4-pro"})
        _exhausted(store["results"], pr, "kimi_gate", _KIMI_WEEKLY_LIMIT)

        seat = _request(pr, ["kimi_gate"], "dlv-c089a8e32893-r5")["requested"][0]

        assert seat["gate"] == "claude_gate"
        assert seat["seat"] == "kimi_gate"
        assert seat["takeover_from"] == "kimi_gate"
        assert seat["builder_vendor"]["vendor"] == "deepseek"
        assert seat["builder_vendor"]["rule"] == RULE_APPLIED
        assert _record(store["requests"], pr, "claude_gate")["takeover_from"] == "kimi_gate"
        assert not (store["requests"] / f"pr-{pr}-deepseek_gate.json").exists()

    def test_claude_built_head_with_kimi_out_gets_deepseek_not_claude(self, store):
        pr = 2201
        _write_spec(store["data_dir"], "dlv-claude-built", {"provider": "claude", "model": "sonnet"})
        _exhausted(store["results"], pr, "kimi_gate", _KIMI_WEEKLY_LIMIT)

        seat = _request(pr, ["kimi_gate"], "dlv-claude-built")["requested"][0]

        assert seat["gate"] == "deepseek_gate"
        assert seat["takeover_from"] == "claude_gate"
        assert seat["takeover_reason"] == MODEL_MATCH_REASON
        assert [hop["gate"] for hop in seat["takeover_path"]] == ["kimi_gate", "claude_gate"]
        assert "a reviewer never runs the model that built the head" in seat["failure_reason"]
        assert not (store["requests"] / f"pr-{pr}-claude_gate.json").exists()
        on_disk = _record(store["requests"], pr, "deepseek_gate")
        assert on_disk["takeover_reason"] == MODEL_MATCH_REASON
        assert on_disk["builder_vendor"]["vendor"] == "anthropic"

    def test_unknown_builder_vendor_applies_no_rule_and_says_so(self, store):
        pr = 2202
        _exhausted(store["results"], pr, "kimi_gate", _KIMI_WEEKLY_LIMIT)

        seat = _request(pr, ["kimi_gate"], "dlv-no-spec-anywhere")["requested"][0]

        assert seat["gate"] == "claude_gate"
        assert seat["takeover_from"] == "kimi_gate"
        assert seat["builder_vendor"]["rule"] == RULE_NOT_APPLIED
        assert seat["builder_vendor"]["vendor"] is None
        assert "no dispatch-spec.json for dlv-no-spec-anywhere" in seat["builder_vendor"]["reason"]
        assert _record(store["requests"], pr, "claude_gate")["builder_vendor"]["rule"] == RULE_NOT_APPLIED

    def test_a_contradictory_spec_applies_no_rule(self, store):
        pr = 2203
        _write_spec(store["data_dir"], "dlv-mixed", {"provider": "claude", "model": "kimi"})
        _exhausted(store["results"], pr, "kimi_gate", _KIMI_WEEKLY_LIMIT)

        seat = _request(pr, ["kimi_gate"], "dlv-mixed")["requested"][0]

        assert seat["gate"] == "claude_gate"
        assert seat["builder_vendor"]["rule"] == RULE_NOT_APPLIED
        assert "contradicts" in seat["builder_vendor"]["reason"]

    def test_first_choice_of_the_builders_vendor_is_passed_over(self, store):
        """A codex-built head: the codex seat's own gate is codex, so kimi reads it."""
        pr = 2204
        _write_spec(store["data_dir"], "dlv-codex-built", {"provider": "codex", "model": gate_lane_contract.gate_model("codex_gate")})

        seat = _request(pr, ["codex_gate"], "dlv-codex-built")["requested"][0]

        assert seat["gate"] == "kimi_gate"
        assert seat["seat"] == "codex_gate"
        assert seat["takeover_from"] == "codex_gate"
        assert seat["takeover_reason"] == MODEL_MATCH_REASON
        assert not (store["requests"] / f"pr-{pr}-codex_gate.json").exists()

    def test_only_same_vendor_reviewers_left_ends_unfilled_never_on_deepseek(self, store):
        pr = 2205
        _write_spec(store["data_dir"], "dlv-ds-built", {"provider": "deepseek-harness", "model": "deepseek-flash"})
        _exhausted(store["results"], pr, "kimi_gate", _KIMI_WEEKLY_LIMIT)
        _exhausted(store["results"], pr, "claude_gate", _CLAUDE_EXHAUSTED)

        seat = _request(pr, ["kimi_gate"], "dlv-ds-built")["requested"][0]

        assert seat["status"] == "chain_exhausted"
        assert [hop["gate"] for hop in seat["takeover_path"]] == ["kimi_gate", "claude_gate", "deepseek_gate"]
        assert seat["takeover_path"][-1]["reason"] == MODEL_MATCH_REASON
        assert not (store["requests"] / f"pr-{pr}-deepseek_gate.json").exists()

    def test_a_same_vendor_seat_with_no_successor_is_unfillable(self, store):
        """deepseek_gate is the chain's tail: as a seat of its own it has nobody to hand over to."""
        pr = 2206
        _write_spec(store["data_dir"], "dlv-ds-tail", {"provider": "deepseek-harness", "model": "deepseek-flash"})

        seat = _request(pr, ["deepseek_gate"], "dlv-ds-tail")["requested"][0]

        assert seat["status"] == "chain_exhausted"
        assert seat["takeover_path"] == [review_vendor.vendor_match_hop(
            "deepseek_gate", resolve_builder_vendor("dlv-ds-tail", data_dir=store["data_dir"]),
        )]
        assert not (store["requests"] / f"pr-{pr}-deepseek_gate.json").exists()

    def test_a_reviewer_of_another_vendor_keeps_its_first_choice(self, store):
        pr = 2207
        _write_spec(store["data_dir"], "dlv-ds-built-2", {"provider": "deepseek-harness", "model": "deepseek-v4-pro"})

        seats = _request(pr, ["codex_gate", "kimi_gate"], "dlv-ds-built-2")["requested"]

        assert [seat["gate"] for seat in seats] == ["codex_gate", "kimi_gate"]
        assert all("takeover" not in seat for seat in seats)
        assert all(seat["builder_vendor"]["vendor"] == "deepseek" for seat in seats)
