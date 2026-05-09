"""Regression test for ghost-gate receipt reroute path (codex round-7 finding 1, BLOCKING).

Pre-fix: append_receipt_payload resolved receipt_path BEFORE calling
_maybe_reroute_to_gate_stream, then never recomputed after the reroute.
The stale receipt_path caused rerouted ghost-gate receipts to be written to
the original receipts file instead of gate_events.ndjson.

Post-fix: receipt_path is recomputed from receipts_file immediately after the
reroute call, so the write-under-lock and cache-path derivation both use the
correct (rerouted) destination.

This test would FAIL against the pre-fix code and PASS with the fix applied.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


# ---------------------------------------------------------------------------
# Fixture: load append_receipt.py as the active facade module
# ---------------------------------------------------------------------------

def _load_append_receipt(tmp_path: Path):
    env_patch = {
        "PROJECT_ROOT": str(REPO_ROOT),
        "VNX_DATA_DIR": str(tmp_path / ".vnx-data"),
        "VNX_STATE_DIR": str(tmp_path / ".vnx-data" / "state"),
        "VNX_HOME": str(REPO_ROOT),
    }
    mod_name = "append_receipt_reroute_testmodule"
    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(
            mod_name, REPO_ROOT / "scripts" / "append_receipt.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


@pytest.fixture(scope="module")
def ar():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        yield _load_append_receipt(Path(td))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGhostGateReroutePathFixed:
    """Verify that rerouted receipts land in the rerouted path, not the original."""

    def test_rerouted_receipt_writes_to_rerouted_path_not_original(self, tmp_path, ar):
        """The canonical regression test for codex finding 1.

        Sets up an original receipts path and a distinct rerouted path.
        Mocks _maybe_reroute_to_gate_stream to always return the rerouted path.
        Asserts the receipt appears in the rerouted file, NOT the original.
        """
        import append_receipt_internals.payload as payload_mod

        original_dir = tmp_path / "primary" / "state"
        original_dir.mkdir(parents=True)
        original_path = original_dir / "t0_receipts.ndjson"

        rerouted_dir = tmp_path / "gate_state"
        rerouted_dir.mkdir(parents=True)
        rerouted_path = rerouted_dir / "gate_events.ndjson"

        receipt = {
            "timestamp": "2026-05-09T10:00:00.000000Z",
            "dispatch_id": "unknown",
            "event_type": "gate_passed",
            "gate": "codex_gate",
            "terminal": "T3",
            "status": "success",
            "pr_id": "432",
        }

        def _fake_reroute(receipt_arg, receipts_file_arg):
            return str(rerouted_path)

        with patch.object(payload_mod, "_maybe_reroute_to_gate_stream", side_effect=_fake_reroute), \
             patch.object(ar, "_enrich_completion_receipt", side_effect=lambda r, repo_root=None: dict(r)), \
             patch.object(ar, "_count_quality_violations", return_value=0), \
             patch.object(ar, "_register_quality_open_items"), \
             patch.object(ar, "_update_confidence_from_receipt"), \
             patch.object(ar, "_emit_dispatch_register", return_value=False), \
             patch.object(ar, "_maybe_trigger_state_rebuild"), \
             patch.object(ar, "_trigger_receipt_classifier"), \
             patch.object(payload_mod, "_mirror_receipt_to_central"):
            result = ar.append_receipt_payload(
                receipt,
                receipts_file=str(original_path),
                skip_enrichment=True,
            )

        assert result.status == "appended", (
            f"Expected status=appended, got {result.status!r}"
        )

        # Post-fix: receipt must be in the rerouted file.
        assert rerouted_path.exists(), (
            "Receipt must be written to the rerouted gate_events path. "
            "If this assertion fails the receipt_path was not recomputed after reroute."
        )
        rerouted_lines = [
            json.loads(line)
            for line in rerouted_path.read_text().splitlines()
            if line.strip()
        ]
        assert any(r.get("dispatch_id") == "unknown" for r in rerouted_lines), (
            f"Rerouted receipt not found in gate_events path. Lines: {rerouted_lines!r}"
        )

        # Post-fix: receipt must NOT be in the original file.
        if original_path.exists():
            original_lines = [line for line in original_path.read_text().splitlines() if line.strip()]
            receipt_ids = []
            for line in original_lines:
                try:
                    receipt_ids.append(json.loads(line).get("dispatch_id"))
                except json.JSONDecodeError:
                    pass
            assert "unknown" not in receipt_ids, (
                "Receipt written to original path — reroute was not applied. "
                "Pre-fix behaviour: stale receipt_path used after reroute. "
                f"Original file contents: {original_lines!r}"
            )

    def test_rerouted_path_is_used_for_idempotency_cache(self, tmp_path, ar):
        """Idempotency dedup must be keyed on the rerouted path, not the original.

        A second call with the same receipt to the same rerouted destination
        must be deduplicated (status=duplicate) even if a different original
        path was supplied in the first call.
        """
        import append_receipt_internals.payload as payload_mod

        original_dir = tmp_path / "primary2" / "state"
        original_dir.mkdir(parents=True)
        original_path = original_dir / "t0_receipts.ndjson"

        rerouted_dir = tmp_path / "gate_state2"
        rerouted_dir.mkdir(parents=True)
        rerouted_path = rerouted_dir / "gate_events.ndjson"

        receipt = {
            "timestamp": "2026-05-09T10:00:01.000000Z",
            "dispatch_id": "unknown",
            "event_type": "gate_failed",
            "gate": "gemini_review",
            "terminal": "T3",
            "status": "failed",
        }

        def _fake_reroute(receipt_arg, receipts_file_arg):
            return str(rerouted_path)

        common_patches = dict(
            _enrich_completion_receipt=lambda r, repo_root=None: dict(r),
            _count_quality_violations=lambda r: 0,
        )

        with patch.object(payload_mod, "_maybe_reroute_to_gate_stream", side_effect=_fake_reroute), \
             patch.object(ar, "_enrich_completion_receipt", side_effect=lambda r, repo_root=None: dict(r)), \
             patch.object(ar, "_count_quality_violations", return_value=0), \
             patch.object(ar, "_register_quality_open_items"), \
             patch.object(ar, "_update_confidence_from_receipt"), \
             patch.object(ar, "_emit_dispatch_register", return_value=False), \
             patch.object(ar, "_maybe_trigger_state_rebuild"), \
             patch.object(ar, "_trigger_receipt_classifier"), \
             patch.object(payload_mod, "_mirror_receipt_to_central"):
            first = ar.append_receipt_payload(
                dict(receipt), receipts_file=str(original_path), skip_enrichment=True,
            )
            second = ar.append_receipt_payload(
                dict(receipt), receipts_file=str(original_path), skip_enrichment=True,
            )

        assert first.status == "appended"
        assert second.status == "duplicate", (
            "Second call with same receipt to same rerouted path must be a duplicate. "
            f"Got {second.status!r}. If 'appended', idempotency is keyed on wrong path."
        )
