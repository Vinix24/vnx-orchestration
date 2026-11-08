"""zelfst PR-4 (ADR-056): the override source carries project_id and delivery_id.

The B3-cap override is the first real writer of ``governance_overrides``. The
identity lives in ``metadata_json`` and in the event metadata (no migration); a
missing part is written as ``unknown``, never omitted.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "scripts"), str(ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import actor_class  # noqa: E402
import coordination_db  # noqa: E402
import dispatch_cli  # noqa: E402
import governance_escalation  # noqa: E402
import review_rounds  # noqa: E402
from dispatch_plan import ConstraintVerdict  # noqa: E402


@pytest.fixture()
def state_dir(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    coordination_db.init_schema(state)
    return state


def _override_rows(state_dir):
    with coordination_db.get_connection(state_dir) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM governance_overrides ORDER BY id")]


def _event_metadata(state_dir):
    with coordination_db.get_connection(state_dir) as conn:
        rows = conn.execute(
            "SELECT metadata_json FROM coordination_events WHERE event_type = 'governance_override'",
        ).fetchall()
    return [json.loads(r["metadata_json"]) for r in rows]


def _record(state_dir, **extra):
    with coordination_db.get_connection(state_dir) as conn:
        row = governance_escalation.record_override(
            conn, entity_type="delivery", entity_id="dlv-abc", actor="operator",
            override_type="gate_bypass", justification="operator: one-line fix", **extra,
        )
        conn.commit()
    return row


def _identity(raw):
    """The ADR-056 key of a metadata_json; the derived actor_class (PR-7c) is env-dependent and pinned in test_source_actor_class."""
    meta = json.loads(raw)
    assert meta.pop("actor_class") in actor_class.ACTOR_CLASSES
    return meta


def _round(dispatch_id, at):
    return review_rounds.RoundLine(
        project_id="vnx-dev", pr_number=77, head_sha=f"{at:040d}", seat="codex_gate",
        reviewer="codex_gate", takeover_from="", verdict="fail", blocking_count=3,
        report_path="/r/x.md", dispatch_id=dispatch_id, recorded_at=f"2026-11-08T00:0{at}:00Z",
        branch="dispatch/x",
    )


def _cap(pr, *dispatch_ids):
    rounds = tuple(_round(d, i + 1) for i, d in enumerate(dispatch_ids))
    return review_rounds.CapState(pr_number=pr, rounds=rounds, rule="B3.1", cap_round=len(rounds))


class TestRecordOverrideIdentity:
    def test_override_has_project_and_delivery_in_metadata(self, state_dir):
        pid = "vnx-dev"
        _record(state_dir, project_id=pid, delivery_id="dlv-abc")
        (row,) = _override_rows(state_dir)
        assert json.loads(dict(row)["metadata_json"]).get("project_id") == pid
        assert json.loads(row["metadata_json"]).get("delivery_id") == "dlv-abc"

    def test_override_has_project_in_event_metadata(self, state_dir):
        _record(state_dir, project_id="vnx-dev", delivery_id="dlv-abc")
        (meta,) = _event_metadata(state_dir)
        assert meta["project_id"] == "vnx-dev"
        assert meta["delivery_id"] == "dlv-abc"

    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_missing_project_is_unknown(self, state_dir, missing):
        _record(state_dir, project_id=missing, delivery_id="dlv-abc")
        meta = json.loads(_override_rows(state_dir)[0]["metadata_json"])
        assert meta["project_id"] == "unknown"
        assert meta["delivery_id"] == "dlv-abc"

    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_missing_delivery_is_unknown(self, state_dir, missing):
        _record(state_dir, project_id="vnx-dev", delivery_id=missing)
        meta = json.loads(_override_rows(state_dir)[0]["metadata_json"])
        assert meta["project_id"] == "vnx-dev"
        assert meta["delivery_id"] == "unknown"
        assert _event_metadata(state_dir)[0]["delivery_id"] == "unknown"

    def test_neither_given_is_unknown_not_omitted(self, state_dir):
        _record(state_dir)
        meta = json.loads(_override_rows(state_dir)[0]["metadata_json"])
        assert _identity(json.dumps(meta)) == {"project_id": "unknown", "delivery_id": "unknown"}

    def test_unknown_override_type_is_still_refused(self, state_dir):
        with coordination_db.get_connection(state_dir) as conn, pytest.raises(governance_escalation.GovernanceError):
            governance_escalation.record_override(
                conn, entity_type="delivery", entity_id="x", actor="operator",
                override_type="not_a_type", justification="why",
            )
        assert _override_rows(state_dir) == []


class TestBaseDeliveryId:
    @pytest.mark.parametrize("raw,expected", [
        ("dlv-abc-r3", "dlv-abc"), ("dlv-abc", "dlv-abc"), ("dlv-abc-r0", "dlv-abc-r0"),
        ("dlv-abc-r03", "dlv-abc-r03"), ("dlv-abc-rX", "dlv-abc-rX"), ("", ""), (None, ""),
    ])
    def test_round_suffix_is_stripped_only_for_a_real_round(self, raw, expected):
        assert dispatch_cli._base_delivery_id(raw) == expected


class TestB3OverrideWritesTheRow:
    def _book(self, tmp_path, monkeypatch, caps, *, project_id="vnx-dev"):
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        monkeypatch.setattr(review_rounds, "store_project_id", lambda _d: project_id)
        dispatch_cli._book_b3_override_records(caps, reason="operator: one-line fix", state_dir=state)
        return state

    def test_one_row_per_capped_pr_with_ledger_project_and_base_delivery(self, tmp_path, monkeypatch):
        caps = [_cap(77, "dlv-abc", "dlv-abc-r3"), _cap(78, "dlv-def-r2")]
        state = self._book(tmp_path, monkeypatch, caps)
        rows = _override_rows(state)
        assert len(rows) == 2
        first, second = rows
        assert (first["entity_type"], first["entity_id"]) == ("delivery", "dlv-abc")
        assert (second["entity_type"], second["entity_id"]) == ("delivery", "dlv-def")
        assert first["override_type"] == "b3_cap_override"
        assert first["justification"] == "operator: one-line fix"
        assert first["actor"] == "operator"
        assert first["override_scope"] == dispatch_cli.B3_OVERRIDE_REASON_ENV
        assert _identity(first["metadata_json"]) == {"project_id": "vnx-dev", "delivery_id": "dlv-abc"}
        assert _identity(second["metadata_json"]) == {"project_id": "vnx-dev", "delivery_id": "dlv-def"}
        ledger = review_rounds.ledger_path(state / "review_gates")
        assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2

    def test_empty_ledger_project_is_unknown_not_a_default_project(self, tmp_path, monkeypatch):
        state = self._book(tmp_path, monkeypatch, [_cap(77, "dlv-abc-r1")], project_id="")
        meta = json.loads(_override_rows(state)[0]["metadata_json"])
        assert _identity(json.dumps(meta)) == {"project_id": "unknown", "delivery_id": "dlv-abc"}

    def test_a_pr_without_a_dispatch_id_is_named_by_pr_with_unknown_delivery(self, tmp_path, monkeypatch):
        state = self._book(tmp_path, monkeypatch, [_cap(77, "")])
        (row,) = _override_rows(state)
        assert (row["entity_type"], row["entity_id"]) == ("pr", "77")
        assert json.loads(row["metadata_json"])["delivery_id"] == "unknown"

    def test_an_existing_store_is_not_re_initialised(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        coordination_db.init_schema(state)
        monkeypatch.setattr(coordination_db, "init_schema", lambda *_a, **_k: pytest.fail("re-init"))
        self._book(tmp_path, monkeypatch, [_cap(77, "dlv-abc")])
        assert len(_override_rows(state)) == 1


class TestFailingOverrideWriteRefusesTheFire:
    def _snapshot(self, cap):
        verdict = ConstraintVerdict(
            code="b3-cap-override-applied", severity="warn", message="applied", override_applied=True,
            b3_override_caps=(cap,), b3_override_reason="operator: one-line fix",
        )
        return SimpleNamespace(constraint_verdicts=[verdict])

    def test_a_raising_override_write_gives_the_ledger_error_verdict(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr(review_rounds, "store_project_id", lambda _d: "vnx-dev")

        def boom(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(governance_escalation, "record_override", boom)
        refused = dispatch_cli._book_b3_override_at_fire(self._snapshot(_cap(77, "dlv-abc-r2")), state_dir=state)
        assert refused is not None
        assert refused.code == "b3-override-ledger-error"
        assert refused.severity == "blocking"
        assert "database is locked" in refused.message

    def test_a_governance_error_also_refuses(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr(review_rounds, "store_project_id", lambda _d: "vnx-dev")

        def invalid(*_a, **_k):
            raise governance_escalation.GovernanceError("invalid")

        monkeypatch.setattr(governance_escalation, "record_override", invalid)
        refused = dispatch_cli._book_b3_override_at_fire(self._snapshot(_cap(77, "dlv-abc")), state_dir=state)
        assert refused is not None and refused.code == "b3-override-ledger-error"

    def test_a_good_write_lets_the_fire_through(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr(review_rounds, "store_project_id", lambda _d: "vnx-dev")
        assert dispatch_cli._book_b3_override_at_fire(self._snapshot(_cap(77, "dlv-abc-r2")), state_dir=state) is None
        assert len(_override_rows(state)) == 1
