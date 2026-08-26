"""Regression guard for OI-1475: does `close --evidence` actually persist?

OI-1475 reported that `scripts/open_items_manager.py` declares `--evidence`
on the `close` subcommand but that the close handler never reads it, so
operator-supplied evidence is silently discarded on a green exit.

Measured against this tree: that does NOT reproduce. `close_item()`
(scripts/open_items_manager.py:306) reads `args.evidence` and writes it to
`item["closed_evidence"]`. The fix already shipped in commit b9210e37
("fix(open-items): let the ledger correct itself (OI-1416)", PR #1665,
merged 2026-08-22 -- four days before this dispatch was filed), with its
own regression test:
tests/test_oi1416_open_items_amend.py::test_close_with_evidence_lands_on_item_and_audit.

This file adds a second, independent regression guard tied specifically to
OI-1475's acceptance criterion (a unique sentinel string must survive a
close round-trip). Runs against a tmp-path state dir -- never the live
ledger.

Dispatch-ID: 20260826-alpha-oi1475-evidence-drop
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

SENTINEL = "OI-1475-SENTINEL-3f9a7c2e-evidence-must-survive-close"


def _write_items(oi_file: Path, items: list) -> None:
    data = {"schema_version": "1.0", "items": items, "next_id": len(items) + 1}
    with open(oi_file, "w") as f:
        json.dump(data, f)


@pytest.fixture
def oi_file(tmp_path, monkeypatch):
    """Point every open_items_manager path constant at a tmp dir -- never
    the real central store -- and stub the decision-log fan-out."""
    state = tmp_path / "state"
    state.mkdir()
    items_file = state / "open_items.json"
    monkeypatch.setattr(oim, "STATE_DIR", state)
    monkeypatch.setattr(oim, "OPEN_ITEMS_FILE", items_file)
    monkeypatch.setattr(oim, "DIGEST_FILE", state / "open_items_digest.json")
    monkeypatch.setattr(oim, "MARKDOWN_FILE", state / "open_items.md")
    monkeypatch.setattr(oim, "AUDIT_LOG", state / "open_items_audit.jsonl")
    monkeypatch.setattr(oim, "_log_oi_close_decision", lambda **kwargs: None)
    return items_file


def test_close_evidence_sentinel_survives_round_trip(oi_file):
    """`close --evidence <sentinel>` must leave the sentinel retrievable on
    the stored item afterwards.

    This must fail on BEHAVIOR (an assertion on missing/wrong data) if the
    regression ever returns, not on a missing attribute or TypeError --
    close_item() reads the flag via getattr(args, 'evidence', None), so a
    crash here would signal an unrelated defect, not OI-1475's.
    """
    now = datetime.now().isoformat()
    _write_items(oi_file, [{
        "id": "OI-TEST-1475",
        "status": "open",
        "severity": "warn",
        "title": "Sentinel item for OI-1475 evidence regression",
        "details": "",
        "origin_dispatch_id": "origin-disp-1475",
        "origin_report_path": "",
        "pr_id": "",
        "created_at": now,
        "updated_at": now,
        "closed_reason": None,
    }])

    args = argparse.Namespace(
        item_id="OI-TEST-1475",
        status="done",
        reason="Closed for OI-1475 evidence regression coverage",
        dispatch_id="resolver-disp-1475",
        evidence=SENTINEL,
    )
    oim.close_item(args)

    with open(oi_file) as f:
        data = json.load(f)
    item = next(i for i in data["items"] if i["id"] == "OI-TEST-1475")

    assert item.get("closed_evidence") == SENTINEL, (
        "--evidence sentinel was not persisted on the closed item; "
        f"closed_evidence={item.get('closed_evidence')!r}"
    )
