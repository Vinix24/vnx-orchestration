"""R7b: a review seat is passed over for the builder's MODEL, not for its vendor (dispatch dlv-b4de838ac1ec).

Operator decision 2026-10-22 (evening), refining R7: a seat never gets a
reviewer that runs the same model as the builder. A reviewer of the same
vendor on a different model may take the seat, so a ``deepseek-flash`` build can
be read by ``deepseek_gate`` on ``deepseek-v4-pro`` while kimi is out.

The decision code runs for real: specs are files on disk, the reviewer's model
comes from ``gate_lane_contract.gate_model`` (env var, else default), the
comparison from ``model_normalizer``. The one walk test goes through
``request_reviews`` on a real store.
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
from providers import model_normalizer, provider_registry

_FRESH = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
_KIMI_WEEKLY_LIMIT = (
    "governed kimi dispatch failed: Error code: 403 - You've reached your weekly (7-day) usage "
    "limit. Your quota will reset when the current 7-day window ends"
)
_CLAUDE_EXHAUSTED = "claude seat: Error code: 403 - {'error': {'type': 'access_terminated_error'}}"
_MODEL_ENVS = (
    "VNX_DEEPSEEK_GATE_MODEL", "VNX_CLAUDE_GATE_MODEL", "VNX_KIMI_GATE_MODEL", "VNX_GLM_GATE_MODEL",
    "VNX_CODEX_HEADLESS_MODEL", "VNX_CODEX_MODEL",
)


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    for name in _MODEL_ENVS:
        monkeypatch.delenv(name, raising=False)


def _receipt(tmp_path: Path, dispatch_id: str, **fields) -> None:
    ledger = tmp_path / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event_type": "task_complete", "dispatch_id": dispatch_id, **fields}) + "\n")


def _builder(
    tmp_path: Path, provider: str, model: str, dispatch_id: str = "dlv-built", receipt_model: str = "",
) -> review_vendor.BuilderVendor:
    bundle = tmp_path / "dispatches" / "active" / dispatch_id
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "dispatch-spec.json").write_text(json.dumps({"provider": provider, "model": model}), encoding="utf-8")
    if receipt_model:
        _receipt(tmp_path, dispatch_id, provider=provider, model=receipt_model)
    return review_vendor.resolve_builder_vendor(dispatch_id, data_dir=tmp_path)


# ---------------------------------------------------------------------------
# The reviewer's model: one function, no second table
# ---------------------------------------------------------------------------


class TestGateModel:
    @pytest.mark.parametrize("gate,default", [
        ("deepseek_gate", "deepseek-flash"), ("claude_gate", "sonnet"),
        ("kimi_gate", "kimi-k3"), ("glm_gate", "glm-5.2"),
    ])
    def test_the_default_comes_from_model_defaults(self, gate, default):
        assert gate_lane_contract.gate_model(gate) == default == gate_lane_contract.MODEL_DEFAULTS[gate][1]

    def test_the_env_var_wins_at_decision_time(self, monkeypatch):
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", " deepseek-v4-pro ")
        assert gate_lane_contract.gate_model("deepseek_gate") == "deepseek-v4-pro"

    def test_codex_reads_the_override_then_the_registry_default(self, monkeypatch):
        assert gate_lane_contract.gate_model("codex_gate") == provider_registry.resolve_codex_model()
        monkeypatch.setenv("VNX_CODEX_MODEL", "gpt-x")
        assert gate_lane_contract.gate_model("codex_gate") == "gpt-x"

    def test_codex_with_an_unreadable_registry_has_no_model(self):
        with patch.object(
            provider_registry, "resolve_codex_model", side_effect=provider_registry.CodexModelResolutionError("x"),
        ):
            assert gate_lane_contract.gate_model("codex_gate") is None

    @pytest.mark.parametrize("gate", ["ci_gate", "wiring_gate", "claude_github_optional", "not_a_gate"])
    def test_a_gate_that_calls_no_model_has_none(self, gate):
        assert gate_lane_contract.gate_model(gate) is None


class TestComparableModelName:
    def test_the_phased_out_name_is_the_model_that_serves_it(self):
        assert model_normalizer.comparable_model_name("deepseek-v4-flash") == "deepseek-flash"
        assert model_normalizer.comparable_model_name("deepseek-v4-flash-vision-exp") == "deepseek-flash"
        assert model_normalizer.comparable_model_name("deepseek-flash") == "deepseek-flash"

    def test_the_ledger_normalizer_keeps_the_name_that_was_called(self):
        assert model_normalizer.normalize_model_name("deepseek-v4-flash") == "deepseek-v4-flash"

    def test_other_names_compare_as_they_normalize(self):
        assert model_normalizer.comparable_model_name("claude-sonnet-5-5") == model_normalizer.normalize_model_name("claude-sonnet-5-5")
        assert model_normalizer.comparable_model_name("deepseek-v4-pro") != model_normalizer.comparable_model_name("deepseek-flash")
        assert model_normalizer.comparable_model_name(None) == ""


# ---------------------------------------------------------------------------
# The hop
# ---------------------------------------------------------------------------


class TestModelMatchHop:
    def test_same_vendor_other_model_is_not_passed_over(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", "deepseek-v4-pro")
        builder = _builder(tmp_path, "deepseek-harness", "deepseek-flash")
        assert review_vendor.vendor_match_hop("deepseek_gate", builder) is None

    def test_same_vendor_same_model_is_passed_over(self, tmp_path):
        builder = _builder(tmp_path, "deepseek-harness", "deepseek-flash")
        hop = review_vendor.vendor_match_hop("deepseek_gate", builder)
        assert hop is not None
        assert hop["reason"] == "model_matches_builder"
        assert hop["builder_model"] == "deepseek-flash" and hop["reviewer_model"] == "deepseek-flash"

    def test_the_phased_out_name_is_the_same_model(self, tmp_path):
        builder = _builder(tmp_path, "deepseek-harness", "deepseek-v4-flash")
        hop = review_vendor.vendor_match_hop("deepseek_gate", builder)
        assert hop is not None and hop["reason"] == "model_matches_builder"

    def test_a_placeholder_spec_reads_the_model_from_the_receipt(self, tmp_path, monkeypatch):
        builder = _builder(tmp_path, "deepseek-harness", "default", receipt_model="deepseek-flash")
        assert builder.effective_model == "deepseek-flash"
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", "deepseek-flash")
        hop = review_vendor.vendor_match_hop("deepseek_gate", builder)
        assert hop is not None and hop["reason"] == "model_matches_builder"

    def test_a_placeholder_spec_with_a_receipt_on_v4_pro_admits_the_gate_on_flash(self, tmp_path, monkeypatch):
        builder = _builder(tmp_path, "deepseek-harness", "default", receipt_model="deepseek-v4-pro")
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", "deepseek-flash")
        assert review_vendor.vendor_match_hop("deepseek_gate", builder) is None

    def test_the_receipt_not_the_review_environment_decides(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DEEPSEEK_HARNESS_MODEL", "deepseek-flash")
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", "deepseek-flash")
        builder = _builder(tmp_path, "deepseek-harness", "default", receipt_model="deepseek-v4-pro")
        assert builder.effective_model == "deepseek-v4-pro"
        assert review_vendor.vendor_match_hop("deepseek_gate", builder) is None

    def test_model_resolved_wins_over_model_and_the_latest_receipt_wins(self, tmp_path):
        _receipt(tmp_path, "dlv-built", model="deepseek-flash")
        _receipt(tmp_path, "dlv-other", model="deepseek-v4-pro")
        _receipt(tmp_path, "dlv-built", model="sonnet", model_resolved="claude-sonnet-5-5")
        builder = _builder(tmp_path, "claude", "default")
        assert builder.effective_model == "claude-sonnet-5-5"

    def test_a_placeholder_spec_without_a_receipt_is_passed_over_as_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", "deepseek-v4-pro")
        builder = _builder(tmp_path, "deepseek-harness", "default")
        assert builder.effective_model == ""
        hop = review_vendor.vendor_match_hop("deepseek_gate", builder)
        assert hop["reason"] == "model_unknown_for_same_vendor"
        assert "the builder model is unknown" in hop["detail"]

    def test_a_receipt_with_a_sentinel_model_is_unknown(self, tmp_path):
        builder = _builder(tmp_path, "deepseek-harness", "default", receipt_model="unknown")
        assert builder.effective_model == ""

    @pytest.mark.parametrize("placeholder", ["", "auto", "none"])
    def test_every_placeholder_reads_the_receipt(self, tmp_path, placeholder):
        builder = _builder(tmp_path, "deepseek-harness", placeholder, receipt_model="deepseek-flash")
        assert builder.effective_model == "deepseek-flash"

    @pytest.mark.parametrize("sentinel", ["unknown", "null", "n/a", "-"])
    def test_a_sentinel_spec_model_is_unknown_even_with_a_receipt(self, tmp_path, monkeypatch, sentinel):
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", "deepseek-v4-pro")
        builder = _builder(tmp_path, "deepseek-harness", sentinel, receipt_model="deepseek-flash")
        assert builder.effective_model == ""
        assert review_vendor.vendor_match_hop("deepseek_gate", builder)["reason"] == "model_unknown_for_same_vendor"

    @pytest.mark.parametrize("sentinel", ["unknown", "null"])
    def test_a_reviewer_model_that_resolves_to_a_sentinel_is_passed_over(self, tmp_path, monkeypatch, sentinel):
        builder = _builder(tmp_path, "deepseek-harness", "deepseek-flash")
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", sentinel)
        hop = review_vendor.vendor_match_hop("deepseek_gate", builder)
        assert hop["reason"] == "model_unknown_for_same_vendor"
        assert "the reviewer model is unknown" in hop["detail"]

    def test_opus_build_is_read_by_claude_gate_on_sonnet(self, tmp_path):
        builder = _builder(tmp_path, "claude", "opus")
        assert review_vendor.vendor_match_hop("claude_gate", builder) is None

    def test_sonnet_build_is_not_read_by_claude_gate_on_sonnet(self, tmp_path):
        builder = _builder(tmp_path, "claude", "sonnet")
        assert review_vendor.vendor_match_hop("claude_gate", builder)["reason"] == "model_matches_builder"

    def test_an_unknown_builder_model_in_the_same_vendor_fails_closed(self, tmp_path):
        builder = _builder(tmp_path, "claude", "default")
        assert builder.effective_model == ""
        hop = review_vendor.vendor_match_hop("claude_gate", builder)
        assert hop["reason"] == "model_unknown_for_same_vendor"
        assert "the builder model is unknown" in hop["detail"]

    def test_an_unknown_reviewer_model_in_the_same_vendor_fails_closed(self, tmp_path):
        builder = _builder(tmp_path, "codex", "gpt-x")
        with patch.object(
            provider_registry, "resolve_codex_model", side_effect=provider_registry.CodexModelResolutionError("x"),
        ):
            hop = review_vendor.vendor_match_hop("codex_gate", builder)
        assert hop["reason"] == "model_unknown_for_same_vendor"
        assert "the reviewer model is unknown" in hop["detail"]

    @pytest.mark.parametrize("gate", ["codex_gate", "kimi_gate", "claude_gate", "glm_gate", "ci_gate"])
    def test_another_vendor_is_never_passed_over(self, tmp_path, gate):
        builder = _builder(tmp_path, "deepseek-harness", "deepseek-flash")
        assert review_vendor.vendor_match_hop(gate, builder) is None

    def test_an_unknown_builder_vendor_passes_nobody_over(self, tmp_path):
        builder = review_vendor.resolve_builder_vendor("dlv-none", data_dir=tmp_path)
        assert review_vendor.vendor_match_hop("deepseek_gate", builder) is None
        assert review_vendor.vendor_match_hop("deepseek_gate", None) is None

    def test_the_builder_record_carries_the_builder_model(self, tmp_path):
        record = _builder(tmp_path, "deepseek-harness", "default", receipt_model="deepseek-v4-pro").to_record()
        assert record["model"] == "default" and record["builder_model"] == "deepseek-v4-pro"

    def test_same_vendor_record_says_model_differs_only_for_a_same_vendor_reader(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", "deepseek-v4-pro")
        builder = _builder(tmp_path, "deepseek-harness", "deepseek-flash")
        record = builder.to_record()
        stamped = review_vendor.same_vendor_record("deepseek_gate", builder, record)
        assert stamped["rule"] == "model_differs"
        assert stamped["builder_model"] == "deepseek-flash" and stamped["reviewer_model"] == "deepseek-v4-pro"
        assert record["rule"] == "applied" and "reviewer_model" not in record
        assert review_vendor.same_vendor_record("claude_gate", builder, record) is record


# ---------------------------------------------------------------------------
# The takeover walk
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
        "contract_hash": "", "report_path": "", "branch": "fix/r7b", "commit_sha": "a" * 40,
    }), encoding="utf-8")


def _request(pr: int, stack: list, dispatch_id: str) -> dict:
    import review_gate_manager as rgm

    with patch("governance_receipts.emit_governance_receipt"):
        return rgm.ReviewGateManager().request_reviews(
            pr_number=pr, branch="fix/r7b", review_stack=stack, risk_class="medium",
            changed_files=["scripts/lib/gate_request_handler.py"], mode="per_pr",
            dispatch_id=dispatch_id,
        )


class TestModelRuleInTheWalk:
    def test_kimi_seat_lands_on_deepseek_gate_on_another_model_for_a_deepseek_flash_build(self, store, monkeypatch):
        pr = 2301
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_MODEL", "deepseek-v4-pro")
        _builder(store["data_dir"], "deepseek-harness", "deepseek-flash", "dlv-ds-flash")
        _exhausted(store["results"], pr, "kimi_gate", _KIMI_WEEKLY_LIMIT)
        _exhausted(store["results"], pr, "claude_gate", _CLAUDE_EXHAUSTED)

        seat = _request(pr, ["kimi_gate"], "dlv-ds-flash")["requested"][0]

        assert seat["gate"] == "deepseek_gate"
        assert seat["seat"] == "kimi_gate"
        assert seat["builder_vendor"]["vendor"] == "deepseek"
        assert seat["builder_vendor"]["rule"] == "model_differs"
        assert seat["builder_vendor"]["builder_model"] == "deepseek-flash"
        assert seat["builder_vendor"]["reviewer_model"] == "deepseek-v4-pro"
        on_disk = json.loads((store["requests"] / f"pr-{pr}-deepseek_gate.json").read_text(encoding="utf-8"))
        assert on_disk["builder_vendor"]["rule"] == "model_differs"
        assert on_disk["builder_vendor"]["reviewer_model"] == "deepseek-v4-pro"

    def test_the_same_build_with_deepseek_gate_on_its_default_is_passed_over(self, store):
        pr = 2302
        _builder(store["data_dir"], "deepseek-harness", "deepseek-flash", "dlv-ds-flash")
        _exhausted(store["results"], pr, "kimi_gate", _KIMI_WEEKLY_LIMIT)
        _exhausted(store["results"], pr, "claude_gate", _CLAUDE_EXHAUSTED)

        seat = _request(pr, ["kimi_gate"], "dlv-ds-flash")["requested"][0]

        assert seat["status"] == "chain_exhausted"
        assert seat["takeover_path"][-1]["gate"] == "deepseek_gate"
        assert seat["takeover_path"][-1]["reason"] == "model_matches_builder"
        assert not (store["requests"] / f"pr-{pr}-deepseek_gate.json").exists()
