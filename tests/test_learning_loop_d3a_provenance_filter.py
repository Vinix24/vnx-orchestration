#!/usr/bin/env python3
"""D3a (dlv-ceba1526c5a1): the D3 no-provider filter let real failures through the
crack because it keyed on ``provider`` alone.

Measured 2026-09-28 across 38,489 receipts in 7 project ledgers: of 931
failure-status receipts with a sentinel ``provider`` field, 895 carried usable
governance provenance despite the missing provider (163 a real ``model``
value, 732 a converter-stamped ``report_path``) and were being silently
dropped from pattern detection. Only 36 were true double-sentinel noise
(``model`` also a sentinel/absent AND no ``report_path``).

These tests prove:
  1. A converter-written ``report_contract_invalid`` receipt with provider
     "unknown" (empty model, real report_path + dispatch_id) now reaches
     failure-pattern detection instead of being silently skipped.
  2. A historical unknown:unknown receipt (provider AND model both sentinel,
     no report_path) still stays out.
  3. An ordinary failure with a real provider is unaffected.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402


def _now_iso(offset_hours: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()


def _write_receipts(path: Path, receipts: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in receipts) + "\n", encoding="utf-8"
    )


class _FakePaths:
    def __init__(self, state_dir: Path, vnx_home: Path):
        self._d = {
            "VNX_STATE_DIR": str(state_dir),
            "VNX_HOME": str(vnx_home),
            "VNX_DATA_DIR": str(state_dir.parent),
        }

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


@pytest.fixture
def loop_env(tmp_path):
    """LearningLoop bound to a tmp state dir via patched ensure_env."""
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    vnx_home = tmp_path / "repo"
    vnx_home.mkdir()

    fake = _FakePaths(state_dir, vnx_home)
    with patch.object(ll, "ensure_env", return_value=fake):
        loop = ll.LearningLoop()
        yield loop, state_dir
    try:
        loop.conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# (a) real governed failure with unknown provider must reach pattern detection
# ---------------------------------------------------------------------------

def test_converter_contract_invalid_with_unknown_provider_passes(loop_env, capsys):
    """A report_to_receipt_converter-written contract_invalid receipt with
    provider="unknown" (empty model) carries real governance provenance
    (report_path + dispatch_id) and must count as a failure — this is the
    behaviour that was silently dropped before D3a: the old code skipped ANY
    sentinel-provider failure, model/report_path notwithstanding.
    """
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "event_type": "report_contract_invalid",
                "status": "contract_invalid",
                "dispatch_id": "d-converter-1",
                "report_path": "/Users/x/.vnx-data/vnx-dev/unified_reports/d-converter-1.md",
                "provider": "unknown",
                "model": "",
                "terminal": "unknown",
                "contract_violations": ["missing_content_dispatch_id"],
                "timestamp": _now_iso(-1),
            },
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )

    assert len(failures) == 1, (
        "a converter-written contract_invalid receipt with sentinel provider "
        "but real report_path/dispatch_id must reach pattern detection"
    )

    out, _ = capsys.readouterr()
    assert "no-provider passed on governance provenance" in out


def test_subprocess_completion_with_real_model_passes(loop_env):
    """A subprocess_completion failure with a real model value but no
    provider (the phantom_guard.py / pr_enforcement.py corrective-receipt
    shape, which never stamps 'terminal' or 'provider') must count — the
    model alone proves a real worker ran.
    """
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "event_type": "subprocess_completion",
                "status": "failed",
                "dispatch_id": "d-phantom-1",
                "provider": "unknown",
                "model": "sonnet",
                "phantom_rejected": True,
                "failure_reason": "phantom_guard: no diff evidence",
                "source": "phantom_guard",
                "timestamp": _now_iso(-1),
            },
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )

    assert len(failures) == 1, "a real model value must not be treated as noise"


# ---------------------------------------------------------------------------
# (b) historical unknown:unknown noise stays filtered
# ---------------------------------------------------------------------------

def test_historical_unknown_unknown_still_filtered(loop_env, capsys):
    """Double-sentinel receipt (provider AND model both unknown, no
    report_path) is the historical noise the D3 guard targets — must stay
    excluded.
    """
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "event_type": "task_complete",
                "status": "failed",
                "dispatch_id": "d-noise-1",
                "provider": "unknown",
                "model": "unknown",
                "terminal": "unknown",
                "timestamp": _now_iso(-1),
            },
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )

    assert failures == [], "double-sentinel unknown:unknown noise must stay filtered"

    out, _ = capsys.readouterr()
    assert "no-provider filtered" in out


def test_historical_unknown_unknown_absent_model_still_filtered(loop_env):
    """Sentinel provider with the model field absent entirely (no report_path
    either) is the same historical noise shape and must stay excluded.
    """
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "failed",
                "provider": "none",
                "sub_provider": "none",
                "failure_reason": "some error",
                "terminal": "T2",
                "dispatch_id": "d-noprov1",
                "timestamp": _now_iso(-1),
            },
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )

    assert failures == []


# ---------------------------------------------------------------------------
# (c) ordinary failure with a real provider is unaffected
# ---------------------------------------------------------------------------

def test_real_provider_failure_unaffected(loop_env):
    """A normal failure receipt with a real, non-sentinel provider is
    unaffected by the D3a change and still counts.
    """
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "failed",
                "provider": "claude",
                "model": "sonnet",
                "failure_reason": "Exhausted 3 retries",
                "terminal": "T1",
                "dispatch_id": "d-real",
                "timestamp": _now_iso(-1),
            },
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )

    assert len(failures) == 1
    assert failures[0]["agent"] == "claude"
