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


def _registry_spellings():
    """``(spelling, target key)`` for every name the shipped registry gives a model.

    Per entry: its key, its ``litellm_name`` and ``cli_model_arg`` (as written
    and without the routing prefix before the last ``/``) and every name in its
    ``aliases``. The target is the entry's ``served_by`` when it has one, else
    its own key. Read from the registry file, so a model added there is covered
    without touching this test.
    """
    pairs = []
    for section, cfg in provider_registry.load().items():
        for key, entry in cfg.models.items():
            target = entry.served_by or key
            names = {key, *entry.aliases}
            for value in (entry.litellm_name, entry.cli_model_arg):
                if value:
                    names.update({value, value.rsplit("/", 1)[-1]})
            pairs.extend((name, target, f"{section}/{key}") for name in sorted(names))
    return pairs


def _keys_without_a_serving_model():
    """Registry keys no entry of which says what answers it: no litellm_name, cli_model_arg or served_by."""
    has_one = {}
    for cfg in provider_registry.load().values():
        for key, entry in cfg.models.items():
            has_one[key] = has_one.get(key, False) or bool(entry.served_by or entry.litellm_name or entry.cli_model_arg)
    return sorted(key for key, known in has_one.items() if not known)


@pytest.fixture(autouse=True)
def _isolate_model_normalizer_caches(monkeypatch):
    """Build the normalizer's tables from the shipped registry for every test here.

    The normalizer keeps them per process, from whichever registry
    ``provider_registry`` pointed at on the first call; a tmp-registry test that
    ran earlier in the worker would otherwise decide these comparisons. The
    monkeypatch restores the values it found at teardown, so nothing built here
    is left behind either.
    """
    for name in ("_registry_cache", "_alias_cache", "_keys_cache"):
        monkeypatch.setattr(model_normalizer, name, None)


class TestComparableModelName:
    def test_the_phased_out_name_is_the_model_that_serves_it(self):
        assert model_normalizer.comparable_model_name("deepseek-v4-flash") == "deepseek-flash"
        assert model_normalizer.comparable_model_name("deepseek/deepseek-v4-flash") == "deepseek-flash"
        assert model_normalizer.comparable_model_name("deepseek-flash") == "deepseek-flash"

    def test_the_ledger_normalizer_keeps_the_name_that_was_called(self):
        assert model_normalizer.normalize_model_name("deepseek-v4-flash") == "deepseek-v4-flash"

    @pytest.mark.parametrize("alias,target", [
        ("sonnet", "claude-sonnet-5-5"), ("sonnet", "sonnet-5-5"), ("sonnet", "anthropic/claude-sonnet-5-5"),
        ("opus", "claude-opus-4-8"), ("opus", "opus-4-8"),
        ("haiku", "haiku-4-5"), ("haiku", "claude-haiku-4-5"), ("haiku", "claude-haiku-4-5-20251001"),
        ("deepseek-v4-pro", "deepseek-v4-pro-default"), ("deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
        ("kimi-k3", "kimi-code/k3"), ("kimi-k2-7", "kimi-code/kimi-for-coding"), ("kimi-k2-7", "kimi-for-coding"),
        ("kimi-k2-6", "moonshot/kimi-k2.6"),
        ("glm-5.2", "openrouter/z-ai/glm-5.2"), ("glm-5.2", "z-ai/glm-5.2"),
        ("gpt-6-astra", "openai/gpt-6-astra"),
    ])
    def test_the_alias_families_compare_equal(self, alias, target):
        """The codex finding on #2182: `sonnet` and `claude-sonnet-5-5` were `sonnet` and `sonnet-5-5`."""
        served = model_normalizer.comparable_model_name(alias)
        assert served and served == model_normalizer.comparable_model_name(target)

    @pytest.mark.parametrize("one,other", [
        ("sonnet", "sonnet-5"), ("opus", "opus-5-5"), ("opus-5", "opus-5-5"), ("sonnet", "opus"),
        ("deepseek-flash", "deepseek-v4-pro"), ("kimi-k3", "kimi-k2-7"), ("glm-5.3", "glm-5.3-flash"),
    ])
    def test_different_serving_models_stay_apart(self, one, other):
        assert model_normalizer.comparable_model_name(one) != model_normalizer.comparable_model_name(other)

    def test_every_registry_alias_compares_equal_to_its_target(self):
        spellings = _registry_spellings()
        assert len(spellings) > 40
        unresolved = set(_keys_without_a_serving_model())
        mismatched = [
            (where, name, target)
            for name, target, where in spellings
            if target not in unresolved and (
                not model_normalizer.comparable_model_name(target)
                or model_normalizer.comparable_model_name(name) != model_normalizer.comparable_model_name(target)
            )
        ]
        assert mismatched == []

    def test_a_key_the_registry_names_no_serving_model_for_is_unresolved(self):
        unresolved = _keys_without_a_serving_model()
        assert "kimi-default" in unresolved
        assert all(model_normalizer.comparable_model_name(key) == "" for key in unresolved)

    @pytest.mark.parametrize("name", [
        None, "", "unknown", "default", "kimi", "deepseek", "claude-sonnet-9", "claude-sonnet-4-6",
        "deepseek-v4-flash-vision-exp", "claude-sonnet-5-5-20261001", "gpt-x",
    ])
    def test_a_name_the_registry_does_not_resolve_is_empty(self, name):
        assert model_normalizer.comparable_model_name(name) == ""

    def test_the_normalizer_reads_the_registry_aliases(self):
        assert model_normalizer.normalize_model_name("haiku-4-5") == "haiku"


class TestServedBy:
    def _write(self, tmp_path: Path, served_by: str) -> Path:
        path = tmp_path / "registry.yaml"
        path.write_text(
            "providers:\n"
            "  deepseek_harness:\n"
            "    enabled: true\n"
            "    api_key_env: X\n"
            "    models:\n"
            "      deepseek-flash: {litellm_name: '', cli_model_arg: deepseek-flash, cost_input_per_mtok: 1,"
            " cost_output_per_mtok: 1, max_tokens: 1, supports_streaming: true, supports_tool_calls: true}\n"
            f"      deepseek-v4-flash: {{litellm_name: '', cli_model_arg: deepseek-v4-flash, served_by: {served_by!r},"
            " cost_input_per_mtok: 1, cost_output_per_mtok: 1, max_tokens: 1, supports_streaming: true,"
            " supports_tool_calls: true}\n",
            encoding="utf-8",
        )
        return path

    def test_the_shipped_registry_says_who_serves_the_phased_out_flash(self):
        registry = provider_registry.load()
        assert registry["deepseek_harness"].models["deepseek-v4-flash"].served_by == "deepseek-flash"
        assert registry["deepseek"].models["deepseek-v4-flash"].served_by == "deepseek-flash"
        assert registry["deepseek_harness"].models["deepseek-flash"].served_by is None

    def test_a_served_by_that_names_a_registry_key_loads(self, tmp_path):
        registry = provider_registry.load(self._write(tmp_path, "deepseek-flash"))
        assert registry["deepseek_harness"].models["deepseek-v4-flash"].served_by == "deepseek-flash"

    @pytest.mark.parametrize("served_by", ["deepseek-v4.1-flash", "deepseek-v4-flash", ""])
    def test_a_served_by_that_names_no_other_key_fails_the_load(self, tmp_path, served_by):
        with pytest.raises(ValueError, match="served_by"):
            provider_registry.load(self._write(tmp_path, served_by))


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

    def test_a_claude_sonnet_5_5_build_is_not_read_by_claude_gate_on_sonnet(self, tmp_path):
        """Codex on #2182: the registry serves `sonnet` with claude-sonnet-5-5, so this is self-review."""
        builder = _builder(tmp_path, "claude", "claude-sonnet-5-5")
        hop = review_vendor.vendor_match_hop("claude_gate", builder)
        assert hop is not None
        assert hop["reason"] == "model_matches_builder"
        assert hop["builder_model"] == "claude-sonnet-5-5" and hop["reviewer_model"] == "sonnet"
        assert "sonnet-5-5" in hop["detail"]

    def test_a_placeholder_build_whose_receipt_resolved_sonnet_5_5_is_not_read_on_sonnet(self, tmp_path):
        _receipt(tmp_path, "dlv-built", model="sonnet", model_resolved="sonnet-5-5")
        builder = _builder(tmp_path, "claude", "default")
        assert builder.effective_model == "sonnet-5-5"
        assert review_vendor.vendor_match_hop("claude_gate", builder)["reason"] == "model_matches_builder"

    @pytest.mark.parametrize("provider,model,gate,env,reviewer", [
        ("claude", "opus", "claude_gate", "VNX_CLAUDE_GATE_MODEL", "claude-opus-4-8"),
        ("claude", "haiku-4-5", "claude_gate", "VNX_CLAUDE_GATE_MODEL", "haiku"),
        ("deepseek-harness", "deepseek-v4-pro-default", "deepseek_gate", "VNX_DEEPSEEK_GATE_MODEL", "deepseek-v4-pro"),
        ("deepseek-harness", "deepseek/deepseek-v4-flash", "deepseek_gate", "VNX_DEEPSEEK_GATE_MODEL", "deepseek-flash"),
        ("kimi", "kimi-code/k3", "kimi_gate", "VNX_KIMI_GATE_MODEL", "kimi-k3"),
        ("glm", "openrouter/z-ai/glm-5.2", "glm_gate", "VNX_GLM_GATE_MODEL", "glm-5.2"),
        ("codex", "openai/gpt-6-astra", "codex_gate", "VNX_CODEX_MODEL", "gpt-6-astra"),
    ])
    def test_an_alias_of_the_reviewers_model_is_passed_over_in_every_family(
        self, tmp_path, monkeypatch, provider, model, gate, env, reviewer,
    ):
        monkeypatch.setenv(env, reviewer)
        hop = review_vendor.vendor_match_hop(gate, _builder(tmp_path, provider, model))
        assert hop is not None and hop["reason"] == "model_matches_builder"

    def test_a_builder_model_the_registry_does_not_resolve_fails_closed(self, tmp_path):
        builder = _builder(tmp_path, "claude", "claude-sonnet-9")
        hop = review_vendor.vendor_match_hop("claude_gate", builder)
        assert hop["reason"] == "model_unknown_for_same_vendor"
        assert "the builder model 'claude-sonnet-9' does not resolve" in hop["detail"]

    def test_a_reviewer_model_the_registry_does_not_resolve_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_CLAUDE_GATE_MODEL", "claude-sonnet-9")
        hop = review_vendor.vendor_match_hop("claude_gate", _builder(tmp_path, "claude", "opus"))
        assert hop["reason"] == "model_unknown_for_same_vendor"
        assert "the reviewer model 'claude-sonnet-9' does not resolve" in hop["detail"]

    def test_a_kimi_default_build_is_not_read_by_kimi_gate(self, tmp_path):
        """The registry names no model behind `kimi-default`, so it cannot be shown to differ from kimi-k3."""
        hop = review_vendor.vendor_match_hop("kimi_gate", _builder(tmp_path, "kimi", "kimi-default"))
        assert hop["reason"] == "model_unknown_for_same_vendor"
        assert "'kimi-default'" in hop["detail"]

    def test_an_unresolved_name_of_another_vendor_passes_nobody_over(self, tmp_path):
        builder = _builder(tmp_path, "claude", "claude-sonnet-9")
        for gate in ("codex_gate", "kimi_gate", "deepseek_gate", "glm_gate"):
            assert review_vendor.vendor_match_hop(gate, builder) is None

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
