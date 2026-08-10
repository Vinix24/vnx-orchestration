#!/usr/bin/env python3
"""OI-1043: the receipt writer must not leak into the real central ledger.

Incident: ``~/.vnx-data/vnx-dev/state/t0_receipts.ndjson`` accumulated 684+
lines carrying pytest tmp paths (25 days, accelerating: 155 on 2026-08-04)
even though tests/conftest.py pins VNX_DATA_DIR_EXPLICIT=1 plus every
subsystem dir resolve_paths() honors, AND the culprit tests pin their own
``receipts_file = tmp_state / "t0_receipts.ndjson"``.

Root cause (measured, see claudedocs/oi1043-ledger-vervuiling.md): the
Phase 6 P3 dual-write mirror in append_receipt_internals.payload resolved
its central target through ``vnx_paths.resolve_central_data_dir()``, which
returns ``Path.home() / ".vnx-data" / project_id`` UNCONDITIONALLY — it
honors neither VNX_STATE_DIR nor VNX_DATA_DIR_EXPLICIT=1 + VNX_DATA_DIR.
Every pinned test append whose receipt carried a project_id (stamped by
_enrich_completion_receipt from the repo's .vnx-project-id) was mirrored
straight into the real production ledger. A receipt writer that cannot be
pinned can write to the wrong ledger in production too — a production
defect, not test hygiene.

These tests use a FAKE HOME (monkeypatch.setenv("HOME", ...)) so the "real
central store" they assert against is a tmp directory — they never touch
the operator's actual ledger. On the pre-fix code the first four tests are
RED (the mirror/primary write lands in the fake real store, or no guard
error is raised); after the fix they are GREEN.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import append_receipt_internals.payload as payload_mod
import vnx_paths

# Aliased: imported into a test module, the class would otherwise be
# collected as a test class (name starts with "Test").
IsolationGuardError = vnx_paths.TestIsolationGuardError

REAL_STORE_REL = Path(".vnx-data") / "vnx-dev" / "state" / "t0_receipts.ndjson"


def _load_append_receipt():
    mod_name = "append_receipt_oi1043_testmodule"
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


def _make_receipt(dispatch_id: str = "oi1043-repro-001") -> Dict[str, Any]:
    """Mirror of the polluted ledger lines: subprocess_completion carrying
    project_id=vnx-dev (as stamped by enrichment from .vnx-project-id)."""
    return {
        "timestamp": "2026-08-05T06:00:00Z",
        "event_type": "subprocess_completion",
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "status": "failed",
        "source": "tmux_interactive_lane_synthesized",
        "project_id": "vnx-dev",
        "model": "sonnet",
    }


def _fake_real_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at a tmp dir with an existing central install; return the
    would-be real central ledger path."""
    home = tmp_path / "home"
    real_ledger = home / REAL_STORE_REL
    real_ledger.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return real_ledger


def _quiet_hooks(ar):
    """Patch the post-append side-effect hooks; the append + mirror under
    test stay fully real."""
    return (
        patch.object(ar, "_enrich_completion_receipt", side_effect=lambda r, repo_root=None: dict(r)),
        patch.object(ar, "_count_quality_violations", return_value=0),
        patch.object(ar, "_register_quality_open_items"),
        patch.object(ar, "_update_confidence_from_receipt"),
        patch.object(ar, "_emit_dispatch_register", return_value=False),
        patch.object(ar, "_maybe_trigger_state_rebuild"),
        patch.object(ar, "_trigger_receipt_classifier"),
    )


class TestMirrorHonorsStorePin:
    """Fix verification: the mirror resolves through the same pin as every
    other write path (VNX_STATE_DIR / VNX_DATA_DIR_EXPLICIT=1+VNX_DATA_DIR)."""

    def test_mirror_confined_to_pinned_state_dir(self, tmp_path, monkeypatch, ar):
        real_ledger = _fake_real_store(tmp_path, monkeypatch)
        pinned_state = tmp_path / "pinned" / "state"
        monkeypatch.setenv("VNX_STATE_DIR", str(pinned_state))
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "pinned"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        primary = tmp_path / "elsewhere" / "state" / "t0_receipts.ndjson"
        patches = _quiet_hooks(ar)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = ar.append_receipt_payload(_make_receipt(), receipts_file=str(primary))

        assert result.status == "appended"
        assert not real_ledger.exists(), (
            "mirror escaped the explicit store pin and wrote into the real "
            f"central store: {real_ledger}"
        )
        # The mirror is not dropped — it is confined to the pinned store.
        pinned_mirror = pinned_state / "t0_receipts.ndjson"
        assert pinned_mirror.exists(), "mirror must resolve through the pinned VNX_STATE_DIR"
        mirrored = [json.loads(l) for l in pinned_mirror.read_text().splitlines() if l.strip()]
        assert [r["dispatch_id"] for r in mirrored] == ["oi1043-repro-001"]

    def test_mirror_confined_to_explicit_data_dir(self, tmp_path, monkeypatch, ar):
        real_ledger = _fake_real_store(tmp_path, monkeypatch)
        monkeypatch.delenv("VNX_STATE_DIR", raising=False)
        pinned_data = tmp_path / "pinned-data"
        monkeypatch.setenv("VNX_DATA_DIR", str(pinned_data))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        primary = tmp_path / "elsewhere" / "state" / "t0_receipts.ndjson"
        patches = _quiet_hooks(ar)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = ar.append_receipt_payload(_make_receipt("oi1043-repro-002"), receipts_file=str(primary))

        assert result.status == "appended"
        assert not real_ledger.exists()
        pinned_mirror = pinned_data / "state" / "t0_receipts.ndjson"
        assert pinned_mirror.exists(), "mirror must resolve through the pinned VNX_DATA_DIR"

    def test_single_write_when_primary_is_pinned_store(self, tmp_path, monkeypatch, ar):
        """Pin pointing at the primary itself: cutover skip, no double write,
        no escape to the real store."""
        real_ledger = _fake_real_store(tmp_path, monkeypatch)
        pinned_state = tmp_path / "pinned" / "state"
        monkeypatch.setenv("VNX_STATE_DIR", str(pinned_state))
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "pinned"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        target = pinned_state / "t0_receipts.ndjson"
        patches = _quiet_hooks(ar)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = ar.append_receipt_payload(_make_receipt("oi1043-repro-003"), receipts_file=str(target))

        assert result.status == "appended"
        lines = [l for l in target.read_text().splitlines() if l.strip()]
        assert len(lines) == 1, "mirror must cutover-skip when central == primary"
        assert not real_ledger.exists()


class TestRealStoreWriteGuard:
    """Guard verification (#1333 follow-up): under pytest a write into the
    real central store FAILS instead of succeeding — on both write surfaces
    (primary append and central mirror)."""

    def test_primary_append_into_real_store_fails_loud(self, tmp_path, monkeypatch, ar):
        real_ledger = _fake_real_store(tmp_path, monkeypatch)

        patches = _quiet_hooks(ar)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            with pytest.raises(IsolationGuardError, match="TEST ISOLATION GUARD"):
                ar.append_receipt_payload(_make_receipt(), receipts_file=str(real_ledger))

        assert not real_ledger.exists(), "guarded write must not reach disk"

    def test_unpinned_mirror_into_real_store_fails_loud(self, tmp_path, monkeypatch, ar):
        """A test that lost its pin entirely: the mirror resolution correctly
        lands on the real store — and the guard refuses the write loudly
        instead of letting it queue as pending mirror debt."""
        real_ledger = _fake_real_store(tmp_path, monkeypatch)
        for key in ("VNX_STATE_DIR", "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT"):
            monkeypatch.delenv(key, raising=False)

        primary = tmp_path / "primary" / "state" / "t0_receipts.ndjson"
        patches = _quiet_hooks(ar)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            with pytest.raises(IsolationGuardError, match="TEST ISOLATION GUARD"):
                ar.append_receipt_payload(_make_receipt(), receipts_file=str(primary))

        assert not real_ledger.exists(), "guarded mirror must not reach disk"
        pending = primary.parent / "pending_mirrors.ndjson"
        assert not pending.exists(), (
            "a test-isolation violation must fail, not queue as retryable mirror debt"
        )

    def test_mirror_function_itself_refuses_real_store(self, tmp_path, monkeypatch):
        """The low-level mirror surface refuses too, even called directly."""
        real_ledger = _fake_real_store(tmp_path, monkeypatch)
        for key in ("VNX_STATE_DIR", "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT"):
            monkeypatch.delenv(key, raising=False)

        primary = tmp_path / "primary" / "t0_receipts.ndjson"
        with pytest.raises(IsolationGuardError, match="TEST ISOLATION GUARD"):
            payload_mod._mirror_receipt_to_central(_make_receipt(), primary)
        assert not real_ledger.exists()


class TestGovernEnsureReceiptIsolation:
    """End-to-end over the exact culprit flow: dispatch_govern.ensure_receipt
    appends a lane-synthesized receipt pinned to the test's own state dir."""

    def test_ensure_receipt_does_not_touch_real_store(self, tmp_path, monkeypatch):
        from dispatch_govern import GovernRaw, GovernSpec, ensure_receipt

        real_ledger = _fake_real_store(tmp_path, monkeypatch)
        data_dir = tmp_path / "data"
        state_dir = tmp_path / "state"
        data_dir.mkdir()
        state_dir.mkdir()

        spec = GovernSpec(
            dispatch_id="oi1043-govern-001",
            terminal_id="T1",
            instruction="Reproduce the leak.",
            data_dir=data_dir,
            state_dir=state_dir,
            model="sonnet",
        )
        raw = GovernRaw(receipt=None, duration_seconds=1.0)
        ensure_receipt(
            spec,
            raw,
            "tmux_interactive",
            report_path=None,
            contract_status="synthesized",
            permission_enforcement="soft",
        )

        pinned_ledger = state_dir / "t0_receipts.ndjson"
        assert pinned_ledger.exists(), "primary write to the pinned state dir must happen"
        assert not real_ledger.exists(), (
            "ensure_receipt's append escaped the pin into the real central store"
        )


class TestGuardExceptionContract:
    """The guard raises a dedicated RuntimeError subclass so best-effort
    wrappers can re-raise it instead of swallowing it as routine I/O debt."""

    def test_guard_raises_dedicated_subclass(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        target = home / ".vnx-data" / "some-project"
        target.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))

        with pytest.raises(IsolationGuardError, match="TEST ISOLATION GUARD"):
            vnx_paths.refuse_real_central_store_write_under_pytest(target)
        # Backwards compatible: still a RuntimeError with the same message.
        assert issubclass(IsolationGuardError, RuntimeError)
