"""Tests for append_receipt central-mirror dual-write (Phase 6 P3).

Verifies:
- _mirror_receipt_to_central writes to central path
- Skip when primary == central (P5 cutover guard)
- Skip when no project_id available
- Central write is locked via append_receipt.lock
- Missing central dir is created automatically
"""

from __future__ import annotations

import importlib
import fcntl
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_receipt(dispatch_id: str = "d-001", project_id: str = "test-proj") -> Dict[str, Any]:
    return {
        "timestamp": f"2026-05-01T12:00:00.{dispatch_id[-1] if dispatch_id[-1].isdigit() else 0}00000Z",
        "event_type": "task_complete",
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "status": "success",
        "project_id": project_id,
    }


def _load_append_receipt():
    env_patch = {
        "PROJECT_ROOT": str(REPO_ROOT),
        "VNX_DATA_DIR": str(REPO_ROOT / ".vnx-data"),
        "VNX_STATE_DIR": str(REPO_ROOT / ".vnx-data" / "state"),
        "VNX_HOME": str(REPO_ROOT),
    }
    mod_name = "append_receipt_dual_write_testmodule"
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
    return _load_append_receipt()


# ---------------------------------------------------------------------------
# _mirror_receipt_to_central
# ---------------------------------------------------------------------------

class TestMirrorReceiptToCentral:
    def test_writes_to_central_path(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        import append_receipt_internals.payload as payload_mod
        from append_receipt_internals.payload import _mirror_receipt_to_central

        central_base = tmp_path / "central"
        primary_path = tmp_path / "primary" / "state" / "t0_receipts.ndjson"
        primary_path.parent.mkdir(parents=True)

        def _patched_resolve(pid):
            return central_base / pid

        with patch.object(payload_mod, "resolve_central_data_dir", _patched_resolve):
            receipt = _make_receipt("d-mirror", "test-proj")
            _mirror_receipt_to_central(receipt, primary_path)

        central_receipts = central_base / "test-proj" / "state" / "t0_receipts.ndjson"
        assert central_receipts.exists(), "central receipts file must be created"
        records = [json.loads(l) for l in central_receipts.read_text().splitlines() if l.strip()]
        assert len(records) == 1
        assert records[0]["dispatch_id"] == "d-mirror"

    def test_skips_when_primary_equals_central(self, tmp_path):
        from unittest.mock import patch
        import append_receipt_internals.payload as payload_mod
        from append_receipt_internals.payload import _mirror_receipt_to_central

        shared_state = tmp_path / "shared" / "state"
        shared_state.mkdir(parents=True)
        primary_path = shared_state / "t0_receipts.ndjson"

        def _patched_resolve(pid):
            return tmp_path / "shared"

        with patch.object(payload_mod, "resolve_central_data_dir", _patched_resolve):
            receipt = _make_receipt("d-skip", "test-proj")
            _mirror_receipt_to_central(receipt, primary_path)

        # No write should have happened (central == primary).
        assert not primary_path.exists(), "must not write when primary == central"

    def test_skips_when_no_project_id(self, tmp_path, monkeypatch):
        from append_receipt_internals.payload import _mirror_receipt_to_central

        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.delenv("VNX_OPERATOR_ID", raising=False)

        receipt = {
            "event_type": "task_complete",
            "dispatch_id": "d-no-pid",
            "terminal": "T1",
            "status": "success",
        }
        primary_path = tmp_path / "primary" / "t0_receipts.ndjson"

        # Should not raise and should not create central file.
        _mirror_receipt_to_central(receipt, primary_path)

    def test_central_dir_created_automatically(self, tmp_path):
        from unittest.mock import patch
        import append_receipt_internals.payload as payload_mod
        from append_receipt_internals.payload import _mirror_receipt_to_central

        central_base = tmp_path / "new_central"
        primary_path = tmp_path / "primary" / "t0_receipts.ndjson"
        primary_path.parent.mkdir(parents=True)

        def _patched_resolve(pid):
            return central_base / pid

        with patch.object(payload_mod, "resolve_central_data_dir", _patched_resolve):
            receipt = _make_receipt("d-newdir", "test-proj")
            _mirror_receipt_to_central(receipt, primary_path)

        central_receipts = central_base / "test-proj" / "state" / "t0_receipts.ndjson"
        assert central_receipts.exists()

    def test_central_lock_file_created(self, tmp_path):
        from unittest.mock import patch
        import append_receipt_internals.payload as payload_mod
        from append_receipt_internals.payload import _mirror_receipt_to_central

        central_base = tmp_path / "central"
        primary_path = tmp_path / "primary" / "t0_receipts.ndjson"
        primary_path.parent.mkdir(parents=True)

        def _patched_resolve(pid):
            return central_base / pid

        with patch.object(payload_mod, "resolve_central_data_dir", _patched_resolve):
            receipt = _make_receipt("d-lock", "test-proj")
            _mirror_receipt_to_central(receipt, primary_path)

        lock_path = central_base / "test-proj" / "state" / "append_receipt.lock"
        assert lock_path.exists(), "lock file must be created alongside central receipts"

    def test_multiple_receipts_accumulate(self, tmp_path):
        from unittest.mock import patch
        import append_receipt_internals.payload as payload_mod
        from append_receipt_internals.payload import _mirror_receipt_to_central

        central_base = tmp_path / "central"
        primary_path = tmp_path / "primary" / "t0_receipts.ndjson"
        primary_path.parent.mkdir(parents=True)

        def _patched_resolve(pid):
            return central_base / pid

        with patch.object(payload_mod, "resolve_central_data_dir", _patched_resolve):
            _mirror_receipt_to_central(_make_receipt("d-a", "test-proj"), primary_path)
            _mirror_receipt_to_central(_make_receipt("d-b", "test-proj"), primary_path)

        central_receipts = central_base / "test-proj" / "state" / "t0_receipts.ndjson"
        records = [json.loads(l) for l in central_receipts.read_text().splitlines() if l.strip()]
        assert len(records) == 2
        ids = {r["dispatch_id"] for r in records}
        assert ids == {"d-a", "d-b"}

    def test_concurrent_mirrors_do_not_corrupt(self, tmp_path):
        """Two threads mirroring to the same central file must not corrupt it."""
        from unittest.mock import patch
        import append_receipt_internals.payload as payload_mod
        from append_receipt_internals.payload import _mirror_receipt_to_central

        central_base = tmp_path / "central"
        primary_path = tmp_path / "primary" / "t0_receipts.ndjson"
        primary_path.parent.mkdir(parents=True)

        def _patched_resolve(pid):
            return central_base / pid

        errors = []

        def mirror_worker(dispatch_id):
            try:
                with patch.object(payload_mod, "resolve_central_data_dir", _patched_resolve):
                    _mirror_receipt_to_central(_make_receipt(dispatch_id, "test-proj"), primary_path)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mirror_worker, args=(f"d-concurrent-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent mirror errors: {errors}"

        central_receipts = central_base / "test-proj" / "state" / "t0_receipts.ndjson"
        if central_receipts.exists():
            lines = [l for l in central_receipts.read_text().splitlines() if l.strip()]
            for line in lines:
                json.loads(line)  # must be valid JSON — no corruption


class TestAppendReceiptMirrorRecovery:
    def test_failed_mirror_is_replayed_on_next_append(self, tmp_path, ar):
        import append_receipt_internals.payload as payload_mod

        receipts_file = tmp_path / "primary" / "state" / "t0_receipts.ndjson"
        central_base = tmp_path / "central"
        original_mirror = payload_mod._mirror_receipt_to_central_or_raise
        call_count = {"count": 0}

        def _patched_resolve(pid):
            return central_base / pid

        def _flaky_mirror(receipt, primary_path):
            call_count["count"] += 1
            if call_count["count"] == 1:
                raise OSError("central down once")
            return original_mirror(receipt, primary_path)

        with patch.object(payload_mod, "resolve_central_data_dir", _patched_resolve), \
             patch.object(ar, "_enrich_completion_receipt", side_effect=lambda r, repo_root=None: dict(r)), \
             patch.object(ar, "_count_quality_violations", return_value=0), \
             patch.object(ar, "_register_quality_open_items"), \
             patch.object(ar, "_update_confidence_from_receipt"), \
             patch.object(ar, "_emit_dispatch_register", return_value=False), \
             patch.object(ar, "_maybe_trigger_state_rebuild"), \
             patch.object(ar, "_trigger_receipt_classifier"), \
             patch.object(payload_mod, "_emit"), \
             patch.object(payload_mod, "_mirror_receipt_to_central_or_raise", side_effect=_flaky_mirror):
            first = ar.append_receipt_payload(_make_receipt("d-101"), receipts_file=str(receipts_file))
            second = ar.append_receipt_payload(_make_receipt("d-102"), receipts_file=str(receipts_file))

        assert first.status == "appended"
        assert second.status == "appended"

        pending_queue = receipts_file.parent / "pending_mirrors.ndjson"
        assert not pending_queue.exists(), "pending queue must drain after the retry succeeds"

        central_receipts = central_base / "test-proj" / "state" / "t0_receipts.ndjson"
        records = [json.loads(line) for line in central_receipts.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert [record["dispatch_id"] for record in records] == ["d-101", "d-102"]

    def test_persistent_mirror_failure_grows_queue_and_warns(self, tmp_path, ar):
        import append_receipt_internals.payload as payload_mod

        receipts_file = tmp_path / "primary" / "state" / "t0_receipts.ndjson"
        central_base = tmp_path / "central"

        def _patched_resolve(pid):
            return central_base / pid

        with patch.object(payload_mod, "resolve_central_data_dir", _patched_resolve), \
             patch.object(ar, "_enrich_completion_receipt", side_effect=lambda r, repo_root=None: dict(r)), \
             patch.object(ar, "_count_quality_violations", return_value=0), \
             patch.object(ar, "_register_quality_open_items"), \
             patch.object(ar, "_update_confidence_from_receipt"), \
             patch.object(ar, "_emit_dispatch_register", return_value=False), \
             patch.object(ar, "_maybe_trigger_state_rebuild"), \
             patch.object(ar, "_trigger_receipt_classifier"), \
             patch.object(payload_mod, "_mirror_receipt_to_central_or_raise", side_effect=OSError("central still down")), \
             patch.object(payload_mod, "_emit") as emit_mock:
            ar.append_receipt_payload(_make_receipt("d-201"), receipts_file=str(receipts_file))
            ar.append_receipt_payload(_make_receipt("d-202"), receipts_file=str(receipts_file))

        pending_queue = receipts_file.parent / "pending_mirrors.ndjson"
        queued = [
            json.loads(line)
            for line in pending_queue.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(queued) == 2, "persistent mirror failures must accumulate in the pending queue"

        warning_calls = [
            call for call in emit_mock.call_args_list
            if call.args[:2] == ("WARN", "central_receipt_mirror_pending")
        ]
        assert warning_calls, "pending mirror debt must emit a warning"
        assert warning_calls[-1].kwargs["pending_count"] == 2
