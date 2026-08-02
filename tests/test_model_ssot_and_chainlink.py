"""dispatch-20260802-model-ssot-en-ketenlink — model identity SSOT + chain link.

Covers the two connected changes:

1. Model identity on ONE source (wave7_models.yaml):
   - ``normalize_model_name`` maps every documented variant to the canonical
     registry key (deepseek/deepseek-v4-pro, kimi-code/k3,
     moonshot/kimi-k2-0905-preview, claude-sonnet-5, claude-opus-5, ...).
   - receipt writers normalize at write time and fail closed on a dispatch
     receipt without a real model (worker source), while the three model-less
     producers (vnx_governance / vnx_state / context_rotation) are exempt.
2. The chain link:
   - the dispatch spec carries parent_dispatch / task_class / tier_from /
     tier_to; the door passes them onto the plan (dry-run proves it) and the
     receipt writers stamp them.

Dispatch-ID: 20260802-model-ssot-en-ketenlink
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from append_receipt_internals.common import AppendReceiptError
from append_receipt_internals.payload import append_receipt_payload
from providers.model_normalizer import (
    is_unknown_model,
    normalize_model_name,
    tier_for_model,
)

# Importing append_receipt registers the append facade used by
# append_receipt_payload (mirrors the test_append_receipt_dual_write pattern).
import append_receipt  # noqa: E402, F401


# ---------------------------------------------------------------------------
# 1. Canonical name normalization
# ---------------------------------------------------------------------------

class TestNormalizeModelName:
    """Every variant named in the dispatch, plus the 5-series and aliases."""

    @pytest.mark.parametrize(
        "variant, canonical",
        [
            # The receipt variants from the dispatch measurement.
            ("deepseek/deepseek-v4-pro", "deepseek-v4-pro"),
            ("deepseek-v4-pro", "deepseek-v4-pro"),
            ("moonshot/kimi-k2-0905-preview", "kimi-k2-0905-default"),
            ("kimi-code/k3", "kimi-k3"),
            ("kimi-k3", "kimi-k3"),
            # 5-series + Fable (added to the registry by this dispatch).
            ("claude-sonnet-5", "sonnet-5"),
            ("claude-opus-5", "opus-5"),
            ("claude-fable-5", "fable-5"),
            ("fable", "fable-5"),
            # 4-series and dated full ids.
            ("claude-opus-4-8", "opus-4-8"),
            ("claude-sonnet-4-6", "sonnet-4-6"),
            ("claude-opus-4-6", "opus-4-6"),
            ("claude-haiku-4-5-20251001", "haiku"),
            ("claude-haiku-4-5", "haiku"),
            # Already-canonical keys are unchanged.
            ("sonnet", "sonnet"),
            ("opus", "opus"),
            ("haiku", "haiku"),
        ],
    )
    def test_maps_variant_to_canonical(self, variant, canonical):
        assert normalize_model_name(variant) == canonical

    def test_claude_prefix_stripped_for_retired_generations(self):
        """claude-sonnet-4-6 and claude-opus-4-8 normalize to the same
        family-version form — the claude- provider prefix is stripped for both.
        This is the inconsistency the dispatch fixed: claude-opus-4-8 resolved
        through its litellm_name alias, while claude-sonnet-4-6 (a retired id
        with no registry entry) previously passed through with the prefix
        intact, so the ledger counted the same model under two names."""
        assert normalize_model_name("claude-sonnet-4-6") == "sonnet-4-6"
        assert normalize_model_name("claude-opus-4-8") == "opus-4-8"

    def test_unmapped_string_passes_through(self):
        assert normalize_model_name("gpt-4-turbo") == "gpt-4-turbo"

    def test_unknown_sentinel_passes_through(self):
        assert normalize_model_name("unknown") == "unknown"
        assert normalize_model_name("") == ""

    def test_none_returns_empty(self):
        assert normalize_model_name(None) == ""


class TestIsUnknownModel:
    def test_unknown_variants_are_unknown(self):
        for value in ("unknown", "null", "none", "n/a", "na", "unset", "-", "", None):
            assert is_unknown_model(value), f"{value!r} should be unknown"

    def test_real_model_is_not_unknown(self):
        assert not is_unknown_model("opus-5")
        assert not is_unknown_model("deepseek-v4-pro")


class TestTierForModel:
    """Deterministic model -> cost tier reverse map (escalation signal)."""

    def test_tier_high_for_opus_and_fable(self):
        assert tier_for_model("opus-5") == "tier-high"
        assert tier_for_model("fable-5") == "tier-high"
        assert tier_for_model("claude-opus-4-8") == "tier-high"

    def test_tier_mid_for_sonnet(self):
        assert tier_for_model("sonnet-5") == "tier-mid"
        assert tier_for_model("claude-sonnet-5") == "tier-mid"

    def test_tier_low_for_kimi_and_deepseek(self):
        assert tier_for_model("kimi-k3") == "tier-low"
        assert tier_for_model("deepseek-v4-pro") == "tier-low"

    def test_tier_zero_for_local(self):
        assert tier_for_model("gemma-4b-local") == "tier-zero"

    def test_unknown_returns_none(self):
        assert tier_for_model("unknown") is None
        assert tier_for_model(None) is None


# ---------------------------------------------------------------------------
# 2. Fail-closed model on dispatch receipts
# ---------------------------------------------------------------------------

class TestFailClosedModel:
    """No model -> no receipt for a worker dispatch source; exempt producers pass."""

    def _append(self, tmp_path, receipt: dict, name: str = "t0_receipts.ndjson") -> dict:
        rf = tmp_path / name
        append_receipt_payload(
            receipt,
            receipts_file=str(rf),
            skip_enrichment=True,
        )
        return json.loads(rf.read_text().splitlines()[-1])

    def test_worker_dispatch_receipt_without_model_rejected(self, tmp_path):
        receipt = {
            "timestamp": "2026-08-02T00:00:00Z",
            "event_type": "task_complete",
            "dispatch_id": "d-worker-nomodel",
            "terminal": "T1",
            "status": "success",
            "receipt_kind": "dispatch",
        }
        with pytest.raises(AppendReceiptError) as exc_info:
            self._append(tmp_path, receipt)
        assert exc_info.value.code == "missing_model"

    def test_worker_dispatch_receipt_model_unknown_rejected(self, tmp_path):
        receipt = {
            "timestamp": "2026-08-02T00:00:00Z",
            "event_type": "task_complete",
            "dispatch_id": "d-worker-unknown",
            "terminal": "T1",
            "status": "success",
            "receipt_kind": "dispatch",
            "model": "unknown",
        }
        with pytest.raises(AppendReceiptError) as exc_info:
            self._append(tmp_path, receipt)
        assert exc_info.value.code == "missing_model"

    def test_vnx_governance_receipt_without_model_allowed(self, tmp_path):
        line = self._append(tmp_path, {
            "timestamp": "2026-08-02T00:00:00Z",
            "event_type": "task_complete",
            "dispatch_id": "d-governance",
            "terminal": "T0",
            "status": "info",
            "source": "vnx_governance",
            "receipt_kind": "dispatch",
        })
        assert line["dispatch_id"] == "d-governance"
        assert "model" not in line or line.get("model") in ("", None)

    def test_vnx_state_and_context_rotation_allowed(self, tmp_path):
        for idx, source in enumerate(("vnx_state", "context_rotation")):
            line = self._append(
                tmp_path,
                {
                    "timestamp": "2026-08-02T00:00:00Z",
                    "event_type": "state_mutation" if source == "vnx_state" else "context_rotation_continuation",
                    "dispatch_id": f"d-exempt-{idx}",
                    "terminal": "T0",
                    "source": source,
                },
                name=f"exempt-{idx}.ndjson",
            )
            assert line["source"] == source

    def test_non_dispatch_kind_without_model_still_allowed(self, tmp_path):
        # A state_mutation receipt (receipt_kind outside the dispatch set) keeps
        # the old tolerance — the fail-closed gate is scoped to dispatch receipts.
        line = self._append(tmp_path, {
            "timestamp": "2026-08-02T00:00:00Z",
            "event_type": "state_mutation",
            "dispatch_id": "d-state",
            "terminal": "T0",
            "receipt_kind": "state_mutation",
        })
        assert line["receipt_kind"] == "state_mutation"


# ---------------------------------------------------------------------------
# 3. Receipt writers normalize the model at write time
# ---------------------------------------------------------------------------

class TestReceiptModelNormalization:
    def _append(self, tmp_path, receipt: dict) -> dict:
        rf = tmp_path / "t0_receipts.ndjson"
        append_receipt_payload(receipt, receipts_file=str(rf), skip_enrichment=True)
        return json.loads(rf.read_text().splitlines()[-1])

    def test_deepseek_variant_normalized(self, tmp_path):
        line = self._append(tmp_path, {
            "timestamp": "2026-08-02T00:00:00Z",
            "event_type": "task_complete",
            "dispatch_id": "d-deepseek",
            "terminal": "T1",
            "status": "success",
            "receipt_kind": "dispatch",
            "model": "deepseek/deepseek-v4-pro",
        })
        assert line["model"] == "deepseek-v4-pro"

    def test_kimi_cli_arg_normalized(self, tmp_path):
        line = self._append(tmp_path, {
            "timestamp": "2026-08-02T00:00:00Z",
            "event_type": "task_complete",
            "dispatch_id": "d-kimi",
            "terminal": "T1",
            "status": "success",
            "receipt_kind": "dispatch",
            "model": "kimi-code/k3",
        })
        assert line["model"] == "kimi-k3"

    def test_claude_5_series_normalized(self, tmp_path):
        line = self._append(tmp_path, {
            "timestamp": "2026-08-02T00:00:00Z",
            "event_type": "task_complete",
            "dispatch_id": "d-claude5",
            "terminal": "T0",
            "status": "success",
            "receipt_kind": "dispatch",
            "model": "claude-sonnet-5",
        })
        assert line["model"] == "sonnet-5"


# ---------------------------------------------------------------------------
# 4. Chain link: the door passes parent_dispatch / task_class / tier to the receipt
# ---------------------------------------------------------------------------

class TestChainLink:
    def test_chain_link_env_vars_stamped_on_receipt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_PARENT_DISPATCH", "20260101-parent-dispatch")
        monkeypatch.setenv("VNX_TASK_CLASS", "02_code_review")
        monkeypatch.setenv("VNX_TIER_FROM", "tier-mid")
        monkeypatch.setenv("VNX_TIER_TO", "tier-high")
        rf = tmp_path / "t0_receipts.ndjson"
        append_receipt_payload(
            {
                "timestamp": "2026-08-02T00:00:00Z",
                "event_type": "task_complete",
                "dispatch_id": "d-chain",
                "terminal": "T1",
                "status": "success",
                "receipt_kind": "dispatch",
                "model": "sonnet-5",
            },
            receipts_file=str(rf),
            skip_enrichment=True,
        )
        line = json.loads(rf.read_text().splitlines()[-1])
        assert line["parent_dispatch"] == "20260101-parent-dispatch"
        assert line["task_class"] == "02_code_review"
        assert line["tier_from"] == "tier-mid"
        assert line["tier_to"] == "tier-high"

    def test_chain_link_env_stamps_do_not_overwrite_caller_values(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_PARENT_DISPATCH", "20260101-env-parent")
        rf = tmp_path / "t0_receipts.ndjson"
        append_receipt_payload(
            {
                "timestamp": "2026-08-02T00:00:00Z",
                "event_type": "task_complete",
                "dispatch_id": "d-chain-explicit",
                "terminal": "T1",
                "status": "success",
                "receipt_kind": "dispatch",
                "model": "sonnet-5",
                "parent_dispatch": "20260101-explicit-parent",
            },
            receipts_file=str(rf),
            skip_enrichment=True,
        )
        line = json.loads(rf.read_text().splitlines()[-1])
        assert line["parent_dispatch"] == "20260101-explicit-parent"


# ---------------------------------------------------------------------------
# 5. End-to-end: spec carries the chain link; the door puts it on the plan
# ---------------------------------------------------------------------------

class TestDoorChainLink:
    def test_dry_run_parent_dispatch_lands_on_plan(self, tmp_path, monkeypatch, capsys):
        """A staged spec carrying parent_dispatch/task_class/tier lands on the
        plan (visible in the dry-run plan print) — the door's pass-through."""
        from dispatch_bridge import stage_spec_bundle
        from dispatch_cli import run_dispatch

        data_dir = tmp_path / "data"
        (data_dir / "state").mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

        spec_file = stage_spec_bundle(
            instruction_text=(
                "# Chain-link dry run\n\n"
                "Role: backend-developer\n\n"
                "Review the changes and implement the fix.\n"
            ),
            dispatch_id="20260802-chainlink-test",
            role="backend-developer",
            target_slot="T0",
            project_id="vnx-dev",
            provider="claude",
            data_dir=data_dir,
            parent_dispatch="20260101-parent-dispatch",
            task_class="02_code_review",
            tier_from="tier-mid",
            tier_to="tier-high",
        )

        rc = run_dispatch(spec_file, dry_run=True)
        assert rc == 0, capsys.readouterr().err

        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "parent_dispatch: 20260101-parent-dispatch" in out
        assert "task_class:   02_code_review" in out
        assert "tier:         tier-mid -> tier-high" in out

    def test_spec_rejects_bad_parent_dispatch_format(self, tmp_path):
        from dispatch_spec import DispatchSpec, Isolation, PathAccess, Provider, validate
        from dispatch_cli import load_spec
        import json as _json

        ifile = tmp_path / "instruction.md"
        ifile.write_text("# T\n\nRole: backend-developer\n\nDo work.\n", encoding="utf-8")
        spec_dict = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260802-bad-parent",
            "staging_id": "20260802-bad-parent",
            "instruction_file": str(ifile),
            "role": "backend-developer",
            "target_slot": "T1",
            "gate": "",
            "dispatch_paths": [],
            "provider": "claude",
            "deadline_seconds": 3600,
            "parent_dispatch": "not a valid id!",
        }
        sf = tmp_path / "dispatch-spec.json"
        sf.write_text(_json.dumps(spec_dict), encoding="utf-8")
        spec = load_spec(sf)
        result = validate(spec, project_id="vnx-dev", repo_root=Path(__file__).resolve().parent.parent)
        assert result is not None and getattr(result, "code", None) == "bad-parent-dispatch"

    def test_spec_rejects_bad_tier_value(self, tmp_path):
        import json as _json
        from dispatch_cli import load_spec
        from dispatch_spec import validate

        ifile = tmp_path / "instruction.md"
        ifile.write_text("# T\n\nRole: backend-developer\n\nDo work.\n", encoding="utf-8")
        spec_dict = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260802-bad-tier",
            "staging_id": "20260802-bad-tier",
            "instruction_file": str(ifile),
            "role": "backend-developer",
            "target_slot": "T1",
            "gate": "",
            "dispatch_paths": [],
            "provider": "claude",
            "deadline_seconds": 3600,
            "tier_to": "tier-ultra",
        }
        sf = tmp_path / "dispatch-spec.json"
        sf.write_text(_json.dumps(spec_dict), encoding="utf-8")
        spec = load_spec(sf)
        result = validate(spec, project_id="vnx-dev", repo_root=Path(__file__).resolve().parent.parent)
        assert result is not None and getattr(result, "code", None) == "bad-tier-value"
