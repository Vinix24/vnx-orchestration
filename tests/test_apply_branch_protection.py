#!/usr/bin/env python3
"""Tests for scripts/forge/apply_branch_protection.py (Golf B, B1).

``fetch_live_protection`` and the actual gh write calls (``_put_protection``
etc.) are monkeypatched per test -- the planning logic (diff, weaken-check,
which write buckets fire) runs for real, exercising the same
``forge_protection_drift.compare``/``is_weakening`` used by the merge-door
preflight and ``vnx doctor``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "forge"))

import apply_branch_protection as abp
from forge_protection_drift import load_protection_config, to_normalized_dict


YAML_PATH = VNX_ROOT / "scripts" / "forge" / "branch_protection.yaml"


def _real_config():
    return load_protection_config(YAML_PATH)


def _real_norm() -> Dict[str, Any]:
    return to_normalized_dict(_real_config())


def _ok(returncode: int = 0, stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _load_receipts(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
def receipts_path() -> Path:
    return Path(os.environ["VNX_STATE_DIR"]) / "t0_receipts.ndjson"


class TestIdempotency:
    """(g) apply idempotent: a second run with identical live state reports
    zero changes and performs zero write calls."""

    def test_no_diffs_makes_zero_write_calls(self, monkeypatch, receipts_path):
        live = _real_norm()
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)
        put_calls: List[Any] = []
        monkeypatch.setattr(abp, "_put_protection", lambda *a, **k: put_calls.append(a) or _ok())
        sig_calls: List[Any] = []
        monkeypatch.setattr(abp, "_patch_required_signatures", lambda *a, **k: sig_calls.append(a) or _ok())
        auto_calls: List[Any] = []
        monkeypatch.setattr(abp, "_patch_repo_auto_merge", lambda *a, **k: auto_calls.append(a) or _ok())

        result = abp.run_apply(yaml_path=YAML_PATH, project_root=VNX_ROOT)

        assert result["verdict"] == "OK"
        assert result["applied"] is False
        assert result["diffs"] == []
        assert not put_calls and not sig_calls and not auto_calls

        receipts = _load_receipts(receipts_path)
        applied_receipts = [r for r in receipts if r.get("event_type") == "branch_protection_applied"]
        assert len(applied_receipts) == 1
        assert applied_receipts[0]["applied"] is False


class TestDryRun:
    def test_dry_run_never_calls_gh_writes_and_prints_the_full_payload(self, monkeypatch, capsys):
        live = _real_norm()
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)
        put_calls: List[Any] = []
        monkeypatch.setattr(abp, "_put_protection", lambda *a, **k: put_calls.append(a) or _ok())

        rc = abp.main(["--yaml-path", str(YAML_PATH), "--project-root", str(VNX_ROOT), "--dry-run"])

        assert rc == 0
        assert not put_calls
        out = json.loads(capsys.readouterr().out)
        assert out["required_status_checks"]["strict"] is False
        assert len(out["required_status_checks"]["checks"]) == 14
        assert out["restrictions"] is None

    def test_dry_run_prints_payload_even_when_state_matches_exactly(self, monkeypatch, capsys):
        """--dry-run always prints the built object, drift or not (it never writes)."""
        live = _real_norm()
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)

        result = abp.run_apply(yaml_path=YAML_PATH, project_root=VNX_ROOT, dry_run=True)

        assert result["verdict"] == "DRY-RUN"
        assert result["changed"] is False
        assert result["payload"]["enforce_admins"] is True


class TestWeakenRefusal:
    """(d) apply with a check less, or a weakened boolean, without
    --allow-weaken: refused. With the flag and a reason: goes through, and
    the receipt carries the reason."""

    def _live_with_an_extra_check(self) -> Dict[str, Any]:
        """Live currently requires one MORE check than the YAML declares --
        applying the YAML would DROP that check ("a check minder")."""
        live = _real_norm()
        live["required_status_checks"] = dict(live["required_status_checks"])
        live["required_status_checks"]["checks"] = list(live["required_status_checks"]["checks"]) + [
            {"context": "Extra Check Not In YAML", "app_id": 1},
        ]
        return live

    def test_refused_without_allow_weaken(self, monkeypatch, receipts_path):
        live = self._live_with_an_extra_check()
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)
        put_calls: List[Any] = []
        monkeypatch.setattr(abp, "_put_protection", lambda *a, **k: put_calls.append(a) or _ok())

        result = abp.run_apply(yaml_path=YAML_PATH, project_root=VNX_ROOT)

        assert result["verdict"] == "REFUSED"
        assert not put_calls
        assert any("checks[" in f for f in result["weak_fields"])
        assert _load_receipts(receipts_path) == []

    def test_empty_reason_is_refused(self, monkeypatch, receipts_path):
        live = self._live_with_an_extra_check()
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)
        put_calls: List[Any] = []
        monkeypatch.setattr(abp, "_put_protection", lambda *a, **k: put_calls.append(a) or _ok())

        result = abp.run_apply(yaml_path=YAML_PATH, project_root=VNX_ROOT, allow_weaken_reason="   ")

        assert result["verdict"] == "REFUSED"
        assert not put_calls

    def test_allowed_with_reason_proceeds_and_receipt_carries_it(self, monkeypatch, receipts_path):
        live = self._live_with_an_extra_check()
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)
        put_calls: List[Any] = []
        monkeypatch.setattr(abp, "_put_protection", lambda *a, **k: put_calls.append(a) or _ok())

        result = abp.run_apply(
            yaml_path=YAML_PATH, project_root=VNX_ROOT, allow_weaken_reason="operator-goedgekeurd",
        )

        assert result["verdict"] == "OK"
        assert put_calls, "the protection PUT must fire once the weakening is accepted"

        receipts = _load_receipts(receipts_path)
        applied = [r for r in receipts if r.get("event_type") == "branch_protection_applied"]
        assert len(applied) == 1
        assert applied[0]["weaken_reason"] == "operator-goedgekeurd"
        assert applied[0]["applied"] is True

    def test_a_weakened_boolean_without_a_missing_check_is_also_refused(self, monkeypatch):
        """Live currently has strict=True (stronger); the YAML wants
        strict=False -- applying the YAML would weaken this one field, with
        no check removed at all."""
        live = _real_norm()
        live["required_status_checks"] = dict(live["required_status_checks"])
        live["required_status_checks"]["strict"] = True

        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)
        put_calls: List[Any] = []
        monkeypatch.setattr(abp, "_put_protection", lambda *a, **k: put_calls.append(a) or _ok())

        result = abp.run_apply(yaml_path=YAML_PATH, project_root=VNX_ROOT)

        assert result["verdict"] == "REFUSED"
        assert "required_status_checks.strict" in result["weak_fields"]
        assert not put_calls


class TestPendingChecksIgnored:
    """(h) pending_checks with an entry: apply ignores it entirely."""

    def test_pending_checks_entry_never_reaches_the_put_payload_or_the_diff(self, monkeypatch, tmp_path):
        doc_text = YAML_PATH.read_text(encoding="utf-8")
        assert "pending_checks: []" in doc_text
        patched = doc_text.replace("pending_checks: []", "pending_checks: [\"vnx-gate/review\"]")
        custom_yaml = tmp_path / "branch_protection.yaml"
        custom_yaml.write_text(patched, encoding="utf-8")

        live = _real_norm()
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)

        result = abp.run_apply(yaml_path=custom_yaml, project_root=VNX_ROOT)

        assert result["verdict"] == "OK"
        assert result["diffs"] == []
        assert "vnx-gate/review" not in json.dumps(result)


class TestWriteBucketing:
    """A diff confined to required_signatures or repo.allow_auto_merge fires
    only that endpoint, never the full protection PUT."""

    def test_auto_merge_only_diff_skips_the_protection_put(self, monkeypatch):
        """Live has allow_auto_merge=True (weaker/more-permissive); the YAML
        wants False (stronger) -- a real diff, but NOT a weakening (turning
        auto-merge OFF needs no --allow-weaken), so it applies directly and
        touches only the repo-patch endpoint."""
        live = _real_norm()
        live["repo"] = {"allow_auto_merge": True}
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)
        put_calls: List[Any] = []
        monkeypatch.setattr(abp, "_put_protection", lambda *a, **k: put_calls.append(a) or _ok())
        auto_calls: List[Any] = []
        monkeypatch.setattr(abp, "_patch_repo_auto_merge", lambda *a, **k: auto_calls.append(a) or _ok())

        result = abp.run_apply(yaml_path=YAML_PATH, project_root=VNX_ROOT)

        assert result["verdict"] == "OK"
        assert not put_calls
        assert auto_calls

    def test_write_failure_is_reported_as_error(self, monkeypatch):
        live = _real_norm()
        live["allow_force_pushes"] = True
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: live)
        monkeypatch.setattr(abp, "_put_protection", lambda *a, **k: _ok(1, stderr="422 nope"))

        result = abp.run_apply(yaml_path=YAML_PATH, project_root=VNX_ROOT)

        assert result["verdict"] == "ERROR"
        assert "422" in result["message"]
