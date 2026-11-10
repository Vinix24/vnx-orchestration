#!/usr/bin/env python3
"""d3filter D3 (dlv-98bf1198e9e1): the converter-to-proposal chain is pinned
end to end.

The acceptance sentence of the track is that a receipt with an unusable
provider still becomes a proposal with a readable scope. D1 fixed that in the
projection; this file pins the whole chain: the REAL report-to-receipt
converter (``_build_receipt_from_report_core``, never mocked) books a report
written without a provider or model as ``report_contract_invalid`` with
``provider="unknown"`` / ``terminal="unknown"``, and that receipt then travels
``extract_failure_patterns`` -> ``generate_prevention_rules`` ->
``update_terminal_constraints`` into ``pending_rules.json``.

Red run, measured on 94ecddd1f (the base before D1): the proposal carries
``terminal_constraint == "unknown"`` where this file demands ``"any"`` and has
no ``agent_constraint`` at all.
Red run, measured on aa7a5c370 (the base before #1961): ``len(failures) == 0``
where this file demands 2 -- the old provider-only filter dropped the
converter receipt before it could ever reach a proposal.

Every receipt and ledger lives under ``tmp_path`` (``VNX_DATA_DIR`` is pinned
there for the converter's report-path resolver and ``ensure_env`` is patched
for the loop), so no case reads or writes ``~/.vnx-data``.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402
from report_to_receipt_converter import _build_receipt_from_report_core  # noqa: E402

# A report with a content-side Dispatch-ID but deliberately missing
# ``## Summary``, ``## Verification`` and ``## Open Items``. Measured: the
# converter books it report_contract_invalid with provider/model unresolved.
_INVALID_REPORT_BODY = (
    "## Changes\n\n"
    "- the report is contract-invalid on purpose: no Summary, no Verification,\n"
    "  no Open Items, and no provider or model in its identity block.\n"
)


class _FakePaths:
    def __init__(self, state_dir: Path, vnx_home: Path):
        self._d = {
            "VNX_STATE_DIR": str(state_dir),
            "VNX_HOME": str(vnx_home),
            "VNX_DATA_DIR": str(state_dir.parent),
        }

    def __getitem__(self, key):
        return self._d[key]

    def get(self, key, default=None):
        return self._d.get(key, default)


def _new_loop(fake_paths: _FakePaths):
    """A persisting loop against the tmp state dir.

    The ``persist`` keyword was introduced after the sha the red run is
    measured on (aa7a5c370), whose zero-arg constructor always persisted.
    Try the current signature first so the same file can run on both bases.
    """
    with patch.object(ll, "ensure_env", return_value=fake_paths):
        try:
            return ll.LearningLoop(persist=True)
        except TypeError:
            loop = ll.LearningLoop()
            loop.persist = True
            return loop


@pytest.fixture
def store(tmp_path, monkeypatch):
    data = tmp_path / "vnx-data"
    state = data / "state"
    reports = data / "unified_reports"
    state.mkdir(parents=True)
    reports.mkdir(parents=True)
    home = tmp_path / "repo"
    home.mkdir()
    # The converter's report-path resolver reads VNX_DATA_DIR from the
    # environment; pin it to the tmp store so nothing under ~/.vnx-data is read.
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    fake = _FakePaths(state, home)
    loop = _new_loop(fake)
    yield SimpleNamespace(data=data, state=state, reports=reports,
                          reports_dir=reports, home=home, fake=fake, loop=loop)
    loop.conn.close()


def _converter_receipt(store, dispatch_id: str, model: str | None = None) -> dict:
    """Book a report through the real converter and return its receipt."""
    report = store.reports / f"{dispatch_id}.md"
    identity = f"Dispatch-ID: {dispatch_id}\n"
    if model:
        identity += f"**Model**: {model}\n"
    report.write_text(identity + "\n" + _INVALID_REPORT_BODY, encoding="utf-8")
    receipt = _build_receipt_from_report_core(
        report, report.read_text(encoding="utf-8"), state_dir=store.state,
    )
    assert receipt is not None, "the converter must book the report (dispatch id present)"
    return receipt


def _hand_receipt(dispatch_id: str, **fields) -> dict:
    receipt = {
        "dispatch_id": dispatch_id,
        "event_type": "task_failed",
        "status": "failed",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    receipt.update(fields)
    return receipt


def _write_ledger(store, receipts: list[dict]) -> None:
    lines = [json.dumps(receipt) for receipt in receipts]
    (store.state / "t0_receipts.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _extract(store) -> list[dict]:
    loop = store.loop
    loop.receipts_path = store.state / "t0_receipts.ndjson"
    loop.cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    return loop.extract_failure_patterns(datetime.now(timezone.utc) - timedelta(hours=2))


def _queue(store, rules: list[dict]) -> list[dict]:
    with patch.object(ll, "ensure_env", return_value=store.fake):
        store.loop.update_terminal_constraints(rules)
    path = store.state / "pending_rules.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["pending_rules"]


def _proposals(store) -> list[dict]:
    rules = store.loop.generate_prevention_rules(_extract(store))
    return _queue(store, rules)


# --- the acceptance sentence, end to end ------------------------------------

def test_unknown_provider_converter_receipt_becomes_a_proposal_with_a_readable_scope(store):
    receipts = [_converter_receipt(store, "dlv-d3-receipt-aaaa"),
                _converter_receipt(store, "dlv-d3-receipt-bbbb")]

    # Fixture sanity: this is the converter's own measured output, not a
    # hand-shaped dict (plan D3, "Wat elke PR precies doet").
    assert [receipt["event_type"] for receipt in receipts] == ["report_contract_invalid"] * 2
    assert {receipt["provider"] for receipt in receipts} == {"unknown"}
    assert {receipt["model"] for receipt in receipts} == {""}
    assert {receipt["terminal"] for receipt in receipts} == {"unknown"}
    assert all(receipt["report_path"] for receipt in receipts)
    assert all(receipt["timestamp"] for receipt in receipts)

    _write_ledger(store, receipts)
    failures = _extract(store)
    assert len(failures) == 2

    rules = store.loop.generate_prevention_rules(failures)
    assert len(rules) == 1
    pending = _queue(store, rules)

    assert (store.state / "pending_rules.json").exists()
    assert len(pending) == 1
    proposal = pending[0]
    assert proposal["status"] == "pending"
    assert proposal["source"] == "learning_loop"
    assert proposal["occurrence_count"] == 2
    assert proposal["terminal_constraint"] == "any"
    assert proposal["agent_constraint"] is None
    assert proposal["id"] == ll._rule_id(proposal)
    assert "Contract violations" in proposal["pattern"]


# --- four negative / boundary cases -----------------------------------------

def test_one_converter_receipt_stays_below_the_threshold(store):
    _write_ledger(store, [_converter_receipt(store, "dlv-d3-receipt-single")])
    failures = _extract(store)
    assert len(failures) == 1
    assert store.loop.generate_prevention_rules(failures) == []
    assert _queue(store, []) == []
    assert not (store.state / "pending_rules.json").exists()


def test_double_sentinel_noise_without_report_path_reaches_no_proposal(store):
    noise = [
        _hand_receipt("dlv-d3-noise-aaaa", provider="unknown", model="", terminal="T1"),
        _hand_receipt("dlv-d3-noise-bbbb", provider="unknown", model="", terminal="T1"),
    ]
    assert all("report_path" not in receipt for receipt in noise)
    _write_ledger(store, noise)
    assert _extract(store) == []
    assert _proposals(store) == []


def test_real_provider_and_terminal_pass_through_unchanged(store):
    _write_ledger(store, [
        _hand_receipt("dlv-d3-real-aaaa", provider="claude", terminal="T1"),
        _hand_receipt("dlv-d3-real-bbbb", provider="claude", terminal="T1"),
    ])
    pending = _proposals(store)
    assert len(pending) == 1
    assert (pending[0]["terminal_constraint"], pending[0]["agent_constraint"]) == ("T1", "claude")


def test_a_real_model_keeps_its_own_group_and_id(store):
    # The threshold is two occurrences (B7), so each group is fed twice: the
    # converter receipts without a model and those with one must stay two
    # groups with two ids, never collapse into one (C1, end to end).
    _write_ledger(store, [
        _converter_receipt(store, "dlv-d3-group-aaaa"),
        _converter_receipt(store, "dlv-d3-group-bbbb"),
        _converter_receipt(store, "dlv-d3-group-cccc", model="sonnet"),
        _converter_receipt(store, "dlv-d3-group-dddd", model="sonnet"),
    ])
    failures = _extract(store)
    assert len(failures) == 4
    assert {failure["agent"] for failure in failures} == {"", "sonnet"}

    pending = _proposals(store)
    assert len(pending) == 2
    assert {proposal["agent_constraint"] for proposal in pending} == {None, "sonnet"}
    assert {proposal["terminal_constraint"] for proposal in pending} == {"any"}
    assert len({proposal["id"] for proposal in pending}) == 2
    for proposal in pending:
        assert proposal["id"] == ll._rule_id(proposal)
