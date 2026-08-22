"""Tests for OI-1416: the ledger can now correct itself.

Covers three defects in scripts/open_items_manager.py:

  (a) close_item logged the ORIGIN dispatch (the one that discovered the
      item) instead of the RESOLVING dispatch (the one that fixed it) on
      both the audit-log entry and the t0_decision_log fan-out. Falls back
      to origin_dispatch_id only when no --dispatch-id was given.
  (b) there was no command to correct a closed item's text. `amend` fixes
      that, reading replacement text from a file so shell metacharacters
      (backticks, quotes) never get tokenized -- the exact failure mode
      that mangled OI-1291's closure text.
  (c) attach-evidence required --pr, so evidence not shaped like a PR
      (a measurement, a ledger line, a manual check) had nowhere to attach.
      --pr is now optional as long as --item is given instead.

All tests run against a temporary, monkeypatched state dir -- never the
real central store.

Dispatch-ID: beta-1416-oi-correctie
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import open_items_manager as oim  # noqa: E402


def _make_item(item_id, title, *, severity="warn", status="open",
                origin_dispatch_id="origin-disp", pr_id="", closed_reason=None):
    now = datetime.now().isoformat()
    return {
        "id": item_id,
        "status": status,
        "severity": severity,
        "title": title,
        "details": "",
        "origin_dispatch_id": origin_dispatch_id,
        "origin_report_path": "",
        "pr_id": pr_id,
        "created_at": now,
        "updated_at": now,
        "closed_reason": closed_reason,
    }


def _write_items(oi_file, items):
    data = {"schema_version": "1.0", "items": items, "next_id": len(items) + 1}
    with open(oi_file, "w") as f:
        json.dump(data, f)
    return data


def _read_items(oi_file):
    with open(oi_file) as f:
        return json.load(f)


def _read_audit_lines(audit_file):
    if not audit_file.exists():
        return []
    return [json.loads(l) for l in audit_file.read_text().splitlines() if l.strip()]


@pytest.fixture
def oi_state(tmp_path, monkeypatch):
    """Point every open_items_manager path constant at a tmp dir and stub
    the decision-log fan-out so nothing touches the real central store."""
    state = tmp_path / "state"
    state.mkdir()
    oi_file = state / "open_items.json"
    monkeypatch.setattr(oim, "STATE_DIR", state)
    monkeypatch.setattr(oim, "OPEN_ITEMS_FILE", oi_file)
    monkeypatch.setattr(oim, "DIGEST_FILE", state / "open_items_digest.json")
    monkeypatch.setattr(oim, "MARKDOWN_FILE", state / "open_items.md")
    monkeypatch.setattr(oim, "AUDIT_LOG", state / "open_items_audit.jsonl")

    decision_calls = []

    def _fake_log_oi_close_decision(**kwargs):
        decision_calls.append(kwargs)

    monkeypatch.setattr(oim, "_log_oi_close_decision", _fake_log_oi_close_decision)

    return argparse.Namespace(state=state, oi_file=oi_file, decision_calls=decision_calls)


# ---------------------------------------------------------------------------
# 1. close with explicit --dispatch-id: audit carries THAT id, not origin
# ---------------------------------------------------------------------------

def test_close_with_explicit_dispatch_id_overrides_origin(oi_state):
    items = [_make_item("OI-001", "Something is broken",
                         origin_dispatch_id="origin-disp-A")]
    _write_items(oi_state.oi_file, items)

    args = argparse.Namespace(
        item_id="OI-001", status="done", reason="Fixed in PR #500",
        dispatch_id="resolver-disp-B", evidence=None,
    )
    oim.close_item(args)

    audit = _read_audit_lines(oi_state.state / "open_items_audit.jsonl")
    assert len(audit) == 1
    entry = audit[0]
    assert entry["action"] == "close"
    # The two ids must differ -- otherwise this test measures nothing.
    assert entry["dispatch_id"] != "origin-disp-A"
    assert entry["dispatch_id"] == "resolver-disp-B"

    # The decision-log fan-out gets the same resolving id.
    assert len(oi_state.decision_calls) == 1
    assert oi_state.decision_calls[0]["dispatch_id"] == "resolver-disp-B"

    print(f"audit entry dispatch_id={entry['dispatch_id']!r} "
          f"origin_dispatch_id='origin-disp-A' -> resolved, not origin")


def test_defer_and_wontfix_inherit_the_same_fix(oi_state):
    """defer/wontfix route through close_item in main() -- confirm the fix
    applies there too, since they share the exact same code path."""
    items = [
        _make_item("OI-002", "Deferred thing", origin_dispatch_id="origin-disp-X"),
        _make_item("OI-003", "Wontfix thing", origin_dispatch_id="origin-disp-Y"),
    ]
    _write_items(oi_state.oi_file, items)

    defer_args = argparse.Namespace(
        item_id="OI-002", status="deferred", reason="not now",
        dispatch_id="resolver-disp-DEFER", evidence=None,
    )
    oim.close_item(defer_args)

    wontfix_args = argparse.Namespace(
        item_id="OI-003", status="wontfix", reason="not doing this",
        dispatch_id="resolver-disp-WONTFIX", evidence=None,
    )
    oim.close_item(wontfix_args)

    audit = _read_audit_lines(oi_state.state / "open_items_audit.jsonl")
    assert len(audit) == 2
    by_item = {e["item_id"]: e for e in audit}
    assert by_item["OI-002"]["dispatch_id"] == "resolver-disp-DEFER"
    assert by_item["OI-002"]["dispatch_id"] != "origin-disp-X"
    assert by_item["OI-003"]["dispatch_id"] == "resolver-disp-WONTFIX"
    assert by_item["OI-003"]["dispatch_id"] != "origin-disp-Y"
    print("defer + wontfix both call close_item() -- same fix, confirmed via audit log")


# ---------------------------------------------------------------------------
# 2. close --evidence: text lands on the item AND in the audit line
# ---------------------------------------------------------------------------

def test_close_with_evidence_lands_on_item_and_audit(oi_state):
    items = [_make_item("OI-004", "Needs a measurement")]
    _write_items(oi_state.oi_file, items)

    evidence_text = "Measured: 3195 chars before, 3642 chars after (see /tmp/oi1291-fixed.txt)"
    args = argparse.Namespace(
        item_id="OI-004", status="done", reason="Verified by measurement",
        dispatch_id="resolver-disp-EV", evidence=evidence_text,
    )
    oim.close_item(args)

    data = _read_items(oi_state.oi_file)
    item = next(i for i in data["items"] if i["id"] == "OI-004")
    assert item["closed_evidence"] == evidence_text

    audit = _read_audit_lines(oi_state.state / "open_items_audit.jsonl")
    assert audit[0]["evidence"] == evidence_text
    print(f"item.closed_evidence={item['closed_evidence']!r}")
    print(f"audit[0].evidence={audit[0]['evidence']!r}")


# ---------------------------------------------------------------------------
# 3. amend on a CLOSED item, replacement text read from a file with backticks
# ---------------------------------------------------------------------------

def test_amend_closed_item_from_file_preserves_backticks(oi_state, tmp_path):
    mangled_text = "PR merged. Migration applied"  # truncated, as OI-1291 was
    items = [_make_item(
        "OI-005", "OI-1291 style item", status="done",
        closed_reason=mangled_text,
    )]
    _write_items(oi_state.oi_file, items)

    fixed_text = (
        "PR merged. Migration applied via `alembic upgrade head`, "
        "verified with `SELECT count(*) FROM open_items` returning the "
        "expected row count. Rollback path documented in `docs/rollback.md`. "
        "Closing as done."
    )
    text_file = tmp_path / "oi1291-fixed.txt"
    text_file.write_text(fixed_text, encoding="utf-8")

    before_len = len(mangled_text)

    args = argparse.Namespace(
        item_id="OI-005", title=None, title_file=None,
        closed_reason=None, closed_reason_file=str(text_file),
        reason="restore text lost to shell backtick execution (OI-1291)",
        dispatch_id="resolver-disp-AMEND",
    )
    result = oim.amend_item(args)
    assert result == 0

    data = _read_items(oi_state.oi_file)
    item = next(i for i in data["items"] if i["id"] == "OI-005")
    after_len = len(item["closed_reason"])

    # amend must work on a CLOSED item -- status is untouched, not reopened.
    assert item["status"] == "done"
    assert item["closed_reason"] == fixed_text
    assert "`alembic upgrade head`" in item["closed_reason"]
    assert "`SELECT count(*) FROM open_items`" in item["closed_reason"]
    assert "`docs/rollback.md`" in item["closed_reason"]
    assert before_len != after_len

    audit = _read_audit_lines(oi_state.state / "open_items_audit.jsonl")
    assert len(audit) == 1
    assert audit[0]["action"] == "amend"
    assert audit[0]["changes"]["closed_reason"]["old"] == mangled_text
    assert audit[0]["changes"]["closed_reason"]["new"] == fixed_text

    print(f"closed_reason length before={before_len} after={after_len}")
    print(f"backticks intact: {'`alembic upgrade head`' in item['closed_reason']}")


def test_amend_title_and_mutual_exclusion_guard(oi_state, tmp_path):
    items = [_make_item("OI-006", "Old title", status="open")]
    _write_items(oi_state.oi_file, items)

    args = argparse.Namespace(
        item_id="OI-006", title="New title text", title_file=None,
        closed_reason=None, closed_reason_file=None,
        reason="typo fix", dispatch_id=None,
    )
    result = oim.amend_item(args)
    assert result == 0
    data = _read_items(oi_state.oi_file)
    item = next(i for i in data["items"] if i["id"] == "OI-006")
    assert item["title"] == "New title text"
    # status untouched by amend
    assert item["status"] == "open"

    # Passing both --title and --title-file is a loud, immediate error.
    bad_file = tmp_path / "x.txt"
    bad_file.write_text("irrelevant")
    bad_args = argparse.Namespace(
        item_id="OI-006", title="conflicting value", title_file=str(bad_file),
        closed_reason=None, closed_reason_file=None,
        reason="x", dispatch_id=None,
    )
    with pytest.raises(SystemExit):
        oim.amend_item(bad_args)


def test_amend_unknown_item_fails_loud(oi_state, capsys):
    _write_items(oi_state.oi_file, [])
    args = argparse.Namespace(
        item_id="OI-999", title="x", title_file=None,
        closed_reason=None, closed_reason_file=None,
        reason="x", dispatch_id=None,
    )
    result = oim.amend_item(args)
    assert result == 1
    err = capsys.readouterr().err
    assert "OI-999" in err
    assert "not found" in err


# ---------------------------------------------------------------------------
# 4. attach-evidence: --item without --pr succeeds; neither given fails loud
# ---------------------------------------------------------------------------

def test_attach_evidence_by_item_without_pr_succeeds(oi_state):
    items = [_make_item("OI-007", "Closed thing needing evidence", status="done")]
    _write_items(oi_state.oi_file, items)

    args = argparse.Namespace(
        pr=None, item_id="OI-007", report="claudedocs/measurement.md",
        dispatch="resolver-disp-ATTACH",
    )
    result = oim.attach_evidence(args)
    assert result == 0

    data = _read_items(oi_state.oi_file)
    item = next(i for i in data["items"] if i["id"] == "OI-007")
    assert len(item["evidence"]) == 1
    assert item["evidence"][0]["report_path"] == "claudedocs/measurement.md"
    assert item["evidence"][0]["dispatch_id"] == "resolver-disp-ATTACH"
    print(f"item.evidence={item['evidence']!r}")


def test_attach_evidence_without_pr_or_item_fails_loud(oi_state, capsys):
    args = argparse.Namespace(pr=None, item_id=None, report=None, dispatch=None)
    result = oim.attach_evidence(args)
    assert result == 1
    err = capsys.readouterr().err
    assert "--pr" in err and "--item" in err
    print(f"stderr={err!r}")


def test_attach_evidence_pr_only_still_works(oi_state):
    """Backward compatibility: the legacy --pr-only bulk mode is unchanged."""
    items = [_make_item("OI-008", "PR item", status="open", pr_id="PR-42")]
    _write_items(oi_state.oi_file, items)

    args = argparse.Namespace(
        pr="PR-42", item_id=None, report="report.md", dispatch="disp-legacy",
    )
    result = oim.attach_evidence(args)
    assert result == 0
    data = _read_items(oi_state.oi_file)
    item = next(i for i in data["items"] if i["id"] == "OI-008")
    assert len(item["evidence"]) == 1


# ---------------------------------------------------------------------------
# 5. Fallback: close WITHOUT explicit --dispatch-id still carries origin_id
# ---------------------------------------------------------------------------

def test_close_without_dispatch_id_falls_back_to_origin(oi_state):
    items = [_make_item("OI-009", "Manually created item",
                         origin_dispatch_id="origin-disp-C")]
    _write_items(oi_state.oi_file, items)

    args = argparse.Namespace(
        item_id="OI-009", status="done", reason="Fixed",
        dispatch_id=None, evidence=None,
    )
    oim.close_item(args)

    audit = _read_audit_lines(oi_state.state / "open_items_audit.jsonl")
    assert audit[0]["dispatch_id"] == "origin-disp-C"

    assert oi_state.decision_calls[0]["dispatch_id"] == "origin-disp-C"
    print(f"no --dispatch-id given -> audit dispatch_id={audit[0]['dispatch_id']!r} "
          f"(fell back to origin_dispatch_id)")
