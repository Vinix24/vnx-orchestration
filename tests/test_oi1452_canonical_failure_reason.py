"""tests/test_oi1452_canonical_failure_reason.py — kimi_gate.py/glm_gate.py's
OI-1452 lift lands the real outage reason in ``residual_risk``, but never in
``failure_reason`` — the CANONICAL field OI-1415 (#1666,
scripts/lib/phantom_guard.py) established for generic failure readers.

Measured 23-08 over ALL 405 review-gate result records on the central store,
across all six gates and the entire history: 0 of 405 carry a filled
``failure_reason``. 328 carry the reason in ``residual_risk`` instead — the
request-time takeover classifier finds it there, but a generic reader that
only knows to look at the canonical field sees nothing.

This fix stamps ``failure_reason`` with the SAME text placed in
``residual_risk`` — but ONLY the text the OI-1452 lift itself computed (the
lane-log-derived reason), never a placeholder and never a summary of the
frontmatter fields when the lift found nothing. Three ways to be wrong here,
in ascending order of danger: an EMPTY field fails visibly (a reader can tell
the cause is unknown); a PLACEHOLDER passes every presence check and fails
silently; a SUMMARY that merely looks like a cause is worst of all, because
it cannot be told apart from a real one. That is why every test below
asserts EQUALITY against the exact lifted text, never mere non-emptiness — a
``assert record["failure_reason"]`` check would go green the moment someone
"helpfully" fills the field with anything at all.

Covers all four states from the dispatch:
  1. lane-log WITH exhaustion marker -> failure_reason == the lifted text,
     and that same text is a substring of residual_risk
  2. lane-log ABSENT -> failure_reason empty, current behavior unchanged
  3. lane-log present but WITHOUT a marker -> failure_reason empty
  4. a SUCCESSFUL run -> failure_reason empty, no lane log ever read

Plus one verification against the REAL production record/lane-log pair
(``kimi-gate-pr1677-1787477677``, the same outage
``test_oi1452_lane_log_lift_provider_failed_detail.py`` documents) — read
from the live central store (read-only) and round-tripped through the real
gate code on a copy in ``tmp_path``; nothing is ever written back to the
live store. Skips cleanly on a machine without that local store.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
for _p in (str(SCRIPTS_DIR), str(SCRIPTS_DIR / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kimi_gate
import glm_gate
from governance_emit import _classify_lane_log_text

from test_oi1452_lane_log_lift_provider_failed_detail import (
    _FIXTURES,
    _GLM_REAL_SHAPE_REPORT,
    _KIMI_REAL_SHAPE_REPORT,
    _REAL_PASS_REPORT,
    _run_gate_for_real_pr,
)

_KIMI_MARKER_LOG = (_FIXTURES / "kimi_403_quota.log").read_text(encoding="utf-8")
_NO_MARKER_LOG = (_FIXTURES / "content_no_verdict.log").read_text(encoding="utf-8")

# The exact text the lift computes for the marker fixture — derived from the
# SAME classifier the gates call, never a separately hand-typed string that
# could silently drift from what the code actually produces.
_EXPECTED_LIFTED_REASON = _classify_lane_log_text(_KIMI_MARKER_LOG)[1]
assert _EXPECTED_LIFTED_REASON, "fixture must carry a real exhaustion marker"


# ---------------------------------------------------------------------------
# 1. lane-log WITH exhaustion marker -> failure_reason == the lifted text
# ---------------------------------------------------------------------------


def test_kimi_lane_log_with_marker_stamps_failure_reason_equal_to_lifted_text(tmp_path, monkeypatch):
    rc, record, _data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, _KIMI_REAL_SHAPE_REPORT, _KIMI_MARKER_LOG,
    )
    assert rc == 1
    assert record["failure_reason"] == _EXPECTED_LIFTED_REASON
    assert _EXPECTED_LIFTED_REASON in record["residual_risk"]
    # residual_risk keeps its own additional prefix — the two fields are not
    # simply duplicates of each other.
    assert record["residual_risk"] != record["failure_reason"]


def test_glm_lane_log_with_marker_stamps_failure_reason_equal_to_lifted_text(tmp_path, monkeypatch):
    rc, record, _data_dir = _run_gate_for_real_pr(
        glm_gate, tmp_path, monkeypatch, _GLM_REAL_SHAPE_REPORT, _KIMI_MARKER_LOG,
    )
    assert rc == 1
    assert record["failure_reason"] == _EXPECTED_LIFTED_REASON
    assert _EXPECTED_LIFTED_REASON in record["residual_risk"]
    assert record["residual_risk"] != record["failure_reason"]


# ---------------------------------------------------------------------------
# 2. lane-log ABSENT -> failure_reason empty, current behavior unchanged
# ---------------------------------------------------------------------------


def test_kimi_no_lane_log_leaves_failure_reason_empty(tmp_path, monkeypatch):
    rc, record, _data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, _KIMI_REAL_SHAPE_REPORT, lane_log_text=None,
    )
    assert rc == 1
    assert record["failure_reason"] == ""
    assert record["residual_risk"] == (
        "kimi's own report frontmatter stamps this run as failed "
        "(exit_code=1, token_usage.output=0) — provider-side outage, not a "
        "review outcome"
    )


def test_glm_no_lane_log_leaves_failure_reason_empty(tmp_path, monkeypatch):
    rc, record, _data_dir = _run_gate_for_real_pr(
        glm_gate, tmp_path, monkeypatch, _GLM_REAL_SHAPE_REPORT, lane_log_text=None,
    )
    assert rc == 1
    assert record["failure_reason"] == ""
    assert record["residual_risk"] == (
        "glm's own report frontmatter stamps this run as failed "
        "(exit_code=1, token_usage.output=0) — provider-side outage, not a "
        "review outcome"
    )


# ---------------------------------------------------------------------------
# 3. lane-log present but WITHOUT a marker -> failure_reason empty
# ---------------------------------------------------------------------------


def test_kimi_lane_log_without_marker_leaves_failure_reason_empty(tmp_path, monkeypatch):
    rc, record, _data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, _KIMI_REAL_SHAPE_REPORT, _NO_MARKER_LOG,
    )
    assert rc == 1
    assert record["failure_reason"] == ""
    assert "lane log" not in record["residual_risk"]


def test_glm_lane_log_without_marker_leaves_failure_reason_empty(tmp_path, monkeypatch):
    rc, record, _data_dir = _run_gate_for_real_pr(
        glm_gate, tmp_path, monkeypatch, _GLM_REAL_SHAPE_REPORT, _NO_MARKER_LOG,
    )
    assert rc == 1
    assert record["failure_reason"] == ""
    assert "lane log" not in record["residual_risk"]


# ---------------------------------------------------------------------------
# 4. a SUCCESSFUL run -> failure_reason empty, no lane log ever read
# ---------------------------------------------------------------------------


def test_kimi_successful_run_leaves_failure_reason_empty(tmp_path, monkeypatch):
    rc, record, _data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, _REAL_PASS_REPORT, _KIMI_MARKER_LOG,
    )
    assert rc == 0
    assert record["status"] == "pass"
    assert record["failure_reason"] == ""


def test_glm_successful_run_leaves_failure_reason_empty(tmp_path, monkeypatch):
    rc, record, _data_dir = _run_gate_for_real_pr(
        glm_gate, tmp_path, monkeypatch, _REAL_PASS_REPORT, _KIMI_MARKER_LOG,
    )
    assert rc == 0
    assert record["status"] == "pass"
    assert record["failure_reason"] == ""


# ---------------------------------------------------------------------------
# 5. Verification against the REAL production record/lane-log pair — read
#    from the live central store (read-only), round-tripped through the
#    real gate on a COPY in tmp_path. Never writes back to the live store.
# ---------------------------------------------------------------------------


def _real_store_dir() -> Path:
    """The operator's real central store — deliberately NOT read from
    ``VNX_DATA_DIR``: ``tests/conftest.py`` pins that env var to an isolated
    tmp dir fleet-wide (module-level, before any test module is collected)
    so tests never touch production by accident. This lookup wants the
    opposite — a real, read-only look at the live store — so it goes
    straight at the well-known local path instead of the isolated env var.
    """
    return Path.home() / ".vnx-data" / "vnx-dev"


def test_kimi_real_pr1677_record_and_lane_log_stamp_failure_reason_on_copy(tmp_path, monkeypatch):
    store = _real_store_dir()
    real_report = store / "unified_reports" / "kimi-gate-pr1677-1787477677.md"
    real_log = store / "logs" / "conversations" / "kimi-gate-pr1677-1787477677.log"
    if not (real_report.is_file() and real_log.is_file()):
        pytest.skip(
            f"real PR #1677 kimi_gate report/lane-log not present under {store} — "
            "this verification only runs on the operator's own local central store"
        )

    real_report_text = real_report.read_text(encoding="utf-8")
    real_log_text = real_log.read_text(encoding="utf-8")
    expected_reason = _classify_lane_log_text(real_log_text)[1]
    assert expected_reason, "real PR #1677 lane log must classify as lane_exhausted"

    rc, record, data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, real_report_text, real_log_text, pr="1677",
    )

    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["failure_reason"] == expected_reason
    assert expected_reason in record["residual_risk"]
    # only the tmp_path copy was ever touched — the live store's files are
    # unchanged (read-only reads above), and the result record landed under
    # data_dir, not under the live store.
    assert str(data_dir).startswith(str(tmp_path))
    assert real_report.read_text(encoding="utf-8") == real_report_text
    assert real_log.read_text(encoding="utf-8") == real_log_text
