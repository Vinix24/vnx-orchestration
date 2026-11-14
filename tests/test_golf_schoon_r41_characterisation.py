"""Characterisation of ``_convert_one_detailed`` (golf schoon R41, class C).

Pins outcome tags, sinks, log lines, the receipts file, the dead-letter move
and the open-items sync order of ``scripts/lib/report_to_receipt_converter.py``
before an extract-method refactor. Real reports, state dir and receipts file
under ``tmp_path``; only external processes (``git ls-remote``) and, where a
failure shape cannot be produced for real, the append facade are patched.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import report_to_receipt_converter as rtc

_LOG = "report_to_receipt_converter"
_BODY = ("## Summary\n\nImplemented the feature per dispatch specification. All tests pass "
         "and coverage is at target.\n\n## Changes\n\n- scripts/lib/x.py: added X\n\n"
         "## Verification\n\npytest tests/ -x: 42 passed\n\n## Open Items\n\nNone\n")


@pytest.fixture()
def conv(tmp_path, monkeypatch):
    (tmp_path / "data" / "unified_reports").mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: True)  # git ls-remote
    synced = []
    monkeypatch.setattr(rtc, "_sync_report_open_items", lambda r, t: synced.append(r["dispatch_id"]))
    state = tmp_path / "state"
    reports = tmp_path / "reports"
    state.mkdir(), reports.mkdir()
    return reports, state, state / "t0_receipts.ndjson", synced


def _report(reports: Path, did: str, model: bool = True) -> Path:
    fm = f"dispatch_id: {did}\nprovider: claude\n" + ("model: claude-sonnet-5\nstatus: done\n" if model else "")
    p = reports / f"{did}.md"
    p.write_text(f"---\n{fm}---\n\n{_BODY}", encoding="utf-8")
    return p


def _run(path, receipts, **kw):
    return rtc._convert_one_detailed(path, receipts_file=str(receipts), **kw)


def _msgs(caplog):
    return [r.getMessage() for r in caplog.records if r.name == _LOG]


def test_skip_malformed_and_build_crash(conv, monkeypatch, caplog):
    reports, _, receipts, synced = conv
    (reports / "headless").mkdir()
    assert _run(_report(reports / "headless", "20260601-r41-h"), receipts) == (None, "skipped_non_dispatch")
    assert _run(reports / "missing.md", receipts) == (None, "malformed")
    (reports / "unknown.md").write_text("## nothing", encoding="utf-8")
    assert _run(reports / "unknown.md", receipts) == (None, "malformed")
    p = _report(reports, "20260601-r41-crash")
    monkeypatch.setattr(rtc, "build_receipt_from_report", lambda *a, **k: {}["x"])
    sink = []
    with caplog.at_level(logging.ERROR, logger=_LOG):
        assert _run(p, receipts, error_reason_sink=sink) == (None, "error")
    assert sink == ["build_receipt_from_report crashed: KeyError"]
    assert (f"report_to_receipt_converter: build_receipt_from_report crashed for {p.name}: "
            "KeyError: 'x'") in _msgs(caplog)
    assert not receipts.exists() and synced == []


def test_append_then_guard_duplicate(conv, monkeypatch, caplog):
    reports, _, receipts, synced = conv
    did = "20260601-r41-append"
    receipts.write_text("\n  \nnot json\n" + json.dumps({"dispatch_id": did, "receipt_kind": "review_gate"})
                        + "\n", encoding="utf-8")
    seen, real = [], rtc._same_booking
    monkeypatch.setattr(rtc, "_same_booking", lambda rec, r: seen.append(rec) or real(rec, r))
    p = _report(reports, did)
    result, tag = _run(p, receipts)
    assert (tag, result.status) == ("appended", "appended") and synced == [did]
    assert seen == [{"dispatch_id": did, "receipt_kind": "review_gate"}]
    lines = receipts.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["dispatch_id"] == did
    with caplog.at_level(logging.INFO, logger=_LOG):
        result, tag = _run(p, receipts)
    assert tag == "duplicate"
    assert (result.status, result.receipts_file, result.idempotency_key) == ("duplicate", receipts, did)
    assert receipts.read_text(encoding="utf-8").splitlines() == lines and synced == [did, did]
    assert (f"report_to_receipt_converter: skipping dispatch={did} — completion receipt "
            "already exists (hot-path wrote first)") in _msgs(caplog)
    result, tag = _run(p, receipts, dry_run=True)
    assert tag == "duplicate" and synced == [did, did]


def test_dry_run_outcomes(conv, monkeypatch, caplog):
    reports, state, receipts, synced = conv
    import append_receipt
    import append_receipt_internals.validation as validation
    p = _report(reports, "20260601-r41-dry")
    with caplog.at_level(logging.INFO, logger=_LOG):
        receipt, tag = _run(p, receipts, dry_run=True)
    assert tag == "would_append" and receipt["dispatch_id"] == "20260601-r41-dry"
    assert ("report_to_receipt_converter: [dry-run] would book dispatch=20260601-r41-dry "
            f"event_type=task_failed status=failure file={p.name}")  # flat frontmatter: not v1 in _msgs(caplog)
    nm = _report(reports, "20260601-r41-drynm", model=False)
    sink = []
    assert _run(nm, receipts, dry_run=True, rejected_detail_sink=sink) == (None, "rejected")
    assert list(sink[0]) == ["dispatch_id", "file", "reason", "rejected_at"] and len(sink) == 1
    assert (sink[0]["dispatch_id"], sink[0]["file"]) == ("20260601-r41-drynm", nm.name)
    assert (f"report_to_receipt_converter: [dry-run] REJECTED (fail-closed) dispatch=20260601-r41-drynm "
            f"file={nm.name} reason={sink[0]['reason']}") in _msgs(caplog)
    assert nm.exists() and not (state / "receipt_deadletter").exists()
    for exc, reason in ((append_receipt.AppendReceiptError("other_code", 2, "no"),
                         "dry-run append validation failed: other_code"),
                        (ValueError("bad"), "dry-run append validation crashed: ValueError")):
        monkeypatch.setattr(validation, "_validate_receipt", lambda _r, e=exc: (_ for _ in ()).throw(e))
        sink = []
        assert _run(p, receipts, dry_run=True, error_reason_sink=sink) == (None, "error")
        assert sink == [reason]
    assert not receipts.exists() and synced == []


def test_model_refusal_quarantine(conv, caplog):
    reports, state, receipts, synced = conv
    p = _report(reports, "20260601-r41-nm", model=False)
    sink = []
    with caplog.at_level(logging.WARNING, logger=_LOG):
        assert _run(p, receipts, rejected_detail_sink=sink) == (None, "rejected")
    assert not p.exists() and (state / "receipt_deadletter" / p.name).exists()
    assert "missing_model" in (state / "receipt_deadletter" / "INDEX.txt").read_text(encoding="utf-8")
    assert list(sink[0]) == ["dispatch_id", "file", "reason", "rejected_at"] and len(sink) == 1
    assert (f"report_to_receipt_converter: REJECTED (fail-closed) dispatch=20260601-r41-nm "
            f"file={p.name} reason={sink[0]['reason']}") in _msgs(caplog)
    kept = _report(reports, "20260601-r41-nm2", model=False)
    assert rtc._convert_one_detailed(kept) == (None, "rejected") and kept.exists()
    assert synced == []


def test_append_failures_and_statuses(conv, monkeypatch, caplog):
    reports, _, receipts, synced = conv
    import append_receipt
    p = _report(reports, "20260601-r41-fail")
    seen = {}
    cases = [
        (lambda *a, **k: (_ for _ in ()).throw(append_receipt.AppendReceiptError("lock", 3, "busy")),
         "append failed: lock"),
        (lambda *a, **k: (_ for _ in ()).throw(OSError("disk")), "append failed: OSError"),
        (lambda r, **k: seen.update(k), "append_receipt_payload returned no result"),
    ]
    for fake, reason in cases:
        monkeypatch.setattr(append_receipt, "append_receipt_payload", fake)
        sink = []
        with caplog.at_level(logging.WARNING, logger=_LOG):
            assert _run(p, receipts, error_reason_sink=sink, cache_window_seconds=7) == (None, "error")
        assert sink == [reason]
    assert f"report_to_receipt_converter: append failed for {p.name}: busy" in _msgs(caplog)
    assert seen == {"receipts_file": str(receipts), "cache_window_seconds": 7, "skip_enrichment": True}
    res = append_receipt.AppendResult(status="skipped", receipts_file=receipts, idempotency_key="k")
    monkeypatch.setattr(append_receipt, "append_receipt_payload", lambda *a, **k: res)
    assert _run(p, receipts) == (res, "skipped") and synced == []
