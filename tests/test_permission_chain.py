"""permission_chain (L6): scan, pair, build, persist in that order, the 30-minute pairing, the index view, the digest."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts" / "lib")]

import build_t0_state as bts
import cli_invocations
import permission_chain as chain
import permission_events as pe
import permission_events_scan as scan
import permission_proposals as pp
import send_digest_email as digest

NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
CMD = "bin/vnx deliverable promote dlv-4856e810fb14"


def iso(delta_min):
    return (NOW - timedelta(minutes=delta_min)).strftime("%Y-%m-%dT%H:%M:%SZ")


def asked(minutes_ago, session="s1", command=CMD):
    return pe.build_event(project_id="vnx-dev", session_id=session, ts=iso(minutes_ago), account="vnx", origin="t0",
                          bron="B4a", besluit="asked", command=command)


def invocation(minutes_ago, vorm, actor="operator", iid="i1"):
    return {"schema": "cli_invocation/1", "ts": iso(minutes_ago), "invocation_id": iid, "project_id": "vnx-dev",
            "vorm": vorm, "actor_class": actor, "exit_code": 0, "duration_ms": 5}


@pytest.fixture
def state(tmp_path):
    return tmp_path / ".vnx-data" / "vnx-dev" / "state"


def test_chain_runs_scan_pair_build_persist_in_that_order(state, monkeypatch):
    calls = []
    monkeypatch.setattr(scan, "run_scan", lambda *a, **k: calls.append("scan") or 0)
    real_pair = chain.pair
    monkeypatch.setattr(chain, "pair", lambda *a: calls.append("pair") or real_pair(*a))
    monkeypatch.setattr(pp, "build", lambda *a: calls.append("build") or ([], [], {}))
    monkeypatch.setattr(pp, "persist", lambda *a, **k: calls.append("persist") or {"added": 0})
    report = chain.run_permission_chain(state, "vnx-dev", state.parent / "projects", NOW)
    assert calls == ["scan", "pair", "build", "persist"] and report["status"] == "ok"
    assert json.loads((state / chain.REPORT_FILE).read_text())["persist"] == {"added": 0}


@pytest.mark.parametrize("step", ["scan", "pair", "build", "persist"])
def test_a_failing_step_is_status_error_and_never_raises(state, monkeypatch, step):
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret prompt text"))  # noqa: E731
    monkeypatch.setattr(scan, "run_scan", lambda *a, **k: 0)
    target = {"scan": (scan, "run_scan"), "pair": (chain, "pair"), "build": (pp, "build"), "persist": (pp, "persist")}[step]
    monkeypatch.setattr(*target, boom)
    report = chain.run_permission_chain(state, "vnx-dev", state.parent / "projects", NOW)
    assert report["status"] == "error" and report["reden"] == f"{step}: RuntimeError"
    assert json.loads((state / chain.REPORT_FILE).read_text())["status"] == "error"


@pytest.mark.parametrize("gap, pairs", [(29, 1), (31, 0)])
def test_operator_invocation_pairs_only_within_30_minutes(gap, pairs):
    a = asked(gap + 1)
    out, counts = chain.pair([a], [invocation(1, a["vorm"])])
    assert counts["pairs"] == (1 if gap == 29 else 0) and counts["unpaired"] == 1 - counts["pairs"]
    assert ("paired_invocation_id" in out[0]) == bool(pairs)


@pytest.mark.parametrize("actor", ["worker", "t0", "unknown", None])
def test_other_actor_classes_never_pair(actor):
    a = asked(5)
    assert chain.pair([a], [invocation(1, a["vorm"], actor=actor)])[1]["pairs"] == 0


def test_an_invocation_before_the_ask_or_of_another_vorm_does_not_pair():
    a = asked(5)
    assert chain.pair([a], [invocation(10, a["vorm"])])[1]["pairs"] == 0
    assert chain.pair([a], [invocation(1, "vnx other")])[1]["pairs"] == 0


REAL = cli_invocations.build_vorm(["deliverable", "promote", "dlv-abc123"])


@pytest.mark.parametrize("command, inv_vorm, pairs", [
    ("bin/vnx deliverable promote dlv-abc123", REAL, 1),
    ("/opt/homebrew/bin/vnx deliverable promote dlv-abc123", REAL, 1),
    ("bin/vnx deliverable promote dlv-abc123", cli_invocations.build_vorm(["deliverable", "list"]), 0),
    ("gh pr merge 12", "gh pr merge <pr>", 0),
])
def test_pairing_uses_one_command_key_on_both_sides(command, inv_vorm, pairs):
    a = asked(10, command=command)
    assert chain.pair([a], [invocation(1, inv_vorm)])[1]["pairs"] == pairs


def test_two_asks_before_one_invocation_are_ambiguous_and_pair_none():
    a, b = asked(20, "s1"), asked(10, "s2")
    out, counts = chain.pair([a, b], [invocation(1, a["vorm"])])
    assert counts["ambiguous"] == 1 and counts["pairs"] == 0 and counts["unpaired"] == 0
    assert not any("paired_invocation_id" in e for e in out)


def test_one_invocation_pairs_with_at_most_one_ask_and_a_missing_ledger_pairs_nothing():
    a, b = asked(40, "s1"), asked(10, "s2")
    out, counts = chain.pair([a, b], [invocation(1, a["vorm"])])
    assert counts["pairs"] == 1 and "paired_invocation_id" in out[1] and counts["unpaired"] == 1
    assert chain.pair([a], None)[1]["unpaired"] == 1


def test_missing_ledger_makes_b4b_unknown_in_the_chain_coverage(state, monkeypatch):
    monkeypatch.setattr(scan, "run_scan", lambda *a, **k: 0)
    report = chain.run_permission_chain(state, "vnx-dev", state.parent / "projects", NOW)
    assert report["invocations_readable"] is False and report["bronnen"]["B4b"] == "unknown"


def test_read_invocations_counts_a_damaged_line_and_a_foreign_project(state):
    state.mkdir(parents=True)
    foreign = {**invocation(1, "vnx x"), "project_id": "other"}
    (state / "cli_invocations.ndjson").write_text(
        json.dumps(invocation(1, "vnx x")) + "\nnot json\n" + json.dumps(foreign) + "\n")
    lines, bad = chain.read_invocations("vnx-dev", state)
    assert len(lines) == 1 and bad == 2


def _item(n, kind="permissie", text=None):
    return {"oorzaak": f"permission_form:{kind}:vnx:project:{n}", "evidence": [], "doelpad": ".claude/settings.local.json",
            "artefact_soort": kind, "account": "vnx", "bereik": "project", "handelingsklasse": "K6", "voorstel": text or f"Add `Bash(cmd{n}:*)` via /permissions"}


def _persist(state, *items):
    state.mkdir(parents=True, exist_ok=True)
    return pp.persist(state, "vnx-dev", ([i for i in items if i["artefact_soort"] != "alleen_melding"],
                                         [i for i in items if i["artefact_soort"] == "alleen_melding"], {}), now=NOW.isoformat())


def test_index_carries_permission_proposals_capped_at_five_with_more(state):
    _persist(state, *[_item(n) for n in range(7)])
    view = bts._permission_open_view(state, "vnx-dev")
    assert view["available"] and view["total"] == 7 and view["more"] == 2 and len(view["items"]) == 5
    assert view["items"][0]["rule"].startswith("Bash(cmd") and view["items"][0]["kind"] == "permissie"
    index = bts._build_t0_index({"permission_proposals": view})
    assert index["schema"] == "t0_index/1.6" and index["permission_proposals"] == view


def test_index_marks_an_unreadable_store_and_a_missing_section(state):
    state.mkdir(parents=True)
    (state / "steering_proposal_decisions.ndjson").write_text("{not json\n")
    view = bts._permission_open_view(state, "vnx-dev")
    assert view["available"] is False and "unreadable" in view["reason"] and "items" not in view
    assert bts._build_t0_index({})["permission_proposals"]["available"] is False


def _report(state, status="ok", **extra):
    state.mkdir(parents=True, exist_ok=True)
    (state / chain.REPORT_FILE).write_text(json.dumps({"status": status, "bronnen": {"B1": "ok"}, **extra}))


def test_digest_section_shows_a_proposal_with_its_rule_and_a_notification_with_its_reason(state):
    _persist(state, _item(1, text="Add `Bash(make test)` via /permissions"),
             _item(2, "alleen_melding", "secrets: never an allow rule"))
    _report(state)
    text = chain.digest_section(state, "vnx-dev")
    assert "Permissievoorstellen" in text and "```\n  Bash(make test)\n  ```" in text
    assert "melding" in text and "secrets: never an allow rule" in text and "onbekend" not in text


def test_digest_section_says_geen_for_an_empty_store_and_onbekend_for_a_source_that_is_not_ok(state):
    state.mkdir(parents=True)
    _report(state)
    assert chain.digest_section(state, "vnx-dev").endswith("geen\n")
    _report(state, bronnen={"B4b": "unknown"})
    assert "onbekend: bron B4b unknown" in chain.digest_section(state, "vnx-dev")
    _report(state, status="error", reden="scan: ValueError")
    assert "onbekend: chain error: scan: ValueError" in chain.digest_section(state, "vnx-dev")
    (state / chain.REPORT_FILE).unlink()
    assert "onbekend: no readable chain report" in chain.digest_section(state, "vnx-dev")
    assert "no project id" in chain.digest_section(state, None)


def test_build_digest_includes_the_section(state, monkeypatch):
    monkeypatch.setattr(digest, "STATE_DIR", state)
    monkeypatch.setattr(digest, "project_id_from_state_dir", lambda s: "vnx-dev")
    state.mkdir(parents=True)
    _report(state)
    assert "─── Permissievoorstellen ───" in digest.build_digest()[1]


def test_open_view_counts_only_permission_proposals_and_notifications_not_skill_proposals(state):
    _persist(state, *[_item(n, "skill", f"Add skill `s{n}`") for n in range(5)], _item(9))
    view = chain.open_view(state, "vnx-dev")
    assert view["available"] and view["total"] == 1 and view["more"] == 0
    assert [i["kind"] for i in view["items"]] == ["permissie"]
    text = chain.digest_section(state, "vnx-dev")
    assert "Bash(cmd9:*)" in text and "skill" not in text and "en nog" not in text


def test_permission_open_view_returns_an_error_block_when_open_view_raises(state, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store exploded")
    monkeypatch.setattr(chain, "open_view", boom)
    view = bts._permission_open_view(state, "vnx-dev")
    assert view["available"] is False and view["reason"] == "RuntimeError: store exploded"
    assert bts._build_t0_index({"permission_proposals": view})["permission_proposals"]["available"] is False


def test_permission_open_view_returns_an_error_block_when_the_import_fails(state, monkeypatch):
    monkeypatch.setitem(sys.modules, "permission_chain", None)
    view = bts._permission_open_view(state, "vnx-dev")
    assert view["available"] is False and view["reason"].startswith("ModuleNotFoundError: ")


def test_permission_open_view_reason_is_cut_at_200_characters(state, monkeypatch):
    def long(*a, **k):
        raise ValueError("x" * 500)
    monkeypatch.setattr(chain, "open_view", long)
    assert len(bts._permission_open_view(state, "vnx-dev")["reason"]) == 200
