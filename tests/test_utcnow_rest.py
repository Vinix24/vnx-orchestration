"""The remaining datetime.utcnow() callers use an aware clock, same output shape."""
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

ISO_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?Z$")


@pytest.fixture(autouse=True)
def _no_deprecation():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        yield


def _agg(tmp_path, monkeypatch):
    import t0_intelligence_aggregator as m
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    return m.T0IntelligenceAggregator(vnx_root=str(tmp_path))


def test_skill_refinement_utcnow_iso_shape():
    import skill_refinement as m
    out = m._utcnow_iso()
    assert len(out) == 20 and out.endswith("Z")
    datetime.strptime(out, "%Y-%m-%dT%H:%M:%SZ")


def test_confidence_reconcile_recency_decay_naive_last_used():
    import confidence_reconcile as m
    last = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(weeks=8)
    assert m._recency_decay(1.0, last) == pytest.approx(0.95 ** 8, rel=0.01)
    parsed = m._parse_last_used("2026-01-01 00:00:00")
    assert parsed.tzinfo is None
    assert m._recency_decay(1.0, parsed) >= 0.1


def test_query_quality_lessons_timestamp_shape(tmp_path):
    import query_quality_intelligence as m
    f = tmp_path / "i.ndjson"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    import json
    f.write_text("".join(json.dumps({
        "event_type": "quality_gate_failure", "terminal": "T1",
        "timestamp": now.isoformat(),
        "failures": [{"category": "x", "severity": "high", "details": "boom"}]}) + "\n"
        for _ in range(4)))
    lessons = m.generate_lessons_learned(30, f)
    assert lessons
    for lesson in lessons:
        assert ISO_Z.match(lesson["timestamp"])


def test_aggregator_normalize_timestamp_shape(tmp_path, monkeypatch):
    a = _agg(tmp_path, monkeypatch)
    ev = a._normalize_event({"event_type": "x", "terminal": "T1"})
    assert ISO_Z.match(ev["timestamp"])


def test_aggregator_snapshot_timestamp_and_summary(tmp_path, monkeypatch):
    a = _agg(tmp_path, monkeypatch)
    ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    a.event_buffer.append({"event_type": "x", "terminal": "T1", "status": "success",
                           "timestamp": ts})
    snap = a._generate_snapshot()
    assert ISO_Z.match(snap["timestamp"])


def test_aggregator_parse_fallback_is_naive_and_comparable(tmp_path, monkeypatch):
    a = _agg(tmp_path, monkeypatch)
    fb = a._parse_timestamp("not-a-timestamp")
    assert fb.tzinfo is None
    assert a._parse_timestamp("2026-01-01T00:00:00Z").tzinfo is None
    assert a._is_recent(datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
    assert not a._is_recent("2020-01-01T00:00:00Z")
    assert not a._is_recent("")


def test_aggregator_blocked_duration_subtracts(tmp_path, monkeypatch):
    a = _agg(tmp_path, monkeypatch)
    ts = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)).isoformat() + "Z"
    a.terminal_activity["T1"] = [{"status": "blocked", "timestamp": ts}]
    assert 29 <= a._get_blocked_duration("T1") <= 40


def test_deliberation_panel_helper_is_aware():
    import deliberation_panel as m
    now = m._utcnow()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
