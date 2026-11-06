"""Characterisation of the three steps of ``run_plan_gate_for_track`` (G2-5).

``run_plan_gate_for_track`` is the single implementation behind ``plan-gate
run`` and ``plan-gate batch``: it decides when a plan clears. Track
``functies-onder-200`` requires it below 200 executable lines, so three steps
moved into helpers — the plan-source resolution (``_resolve_gate_plan_source``),
the verdict text (``_append_verdict_text``) and the pass record
(``_record_plan_gate_pass``). This file is the behaviour-preservation net for
that move: every case drives the REAL ``run_plan_gate_for_track`` and asserts
the WHOLE ``PlanGateRunResult`` (``dataclasses.asdict``), so a step that moved
with a changed exit code, a reordered ``stderr_lines`` entry, a dropped
``thin_length`` or a missing ``stdout_json`` fails here.

Only the panel run, the seat-ledger location and the tiebreaker config are
stubbed — the same stubs ``test_plan_gate_one_seat.py``,
``test_plan_gate_deliverables_source.py`` and ``test_plan_gate_batch.py`` use.
Nothing here patches the code under test.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
_SCRIPTS = REPO_ROOT / "scripts"
_MIGRATIONS = REPO_ROOT / "schemas" / "migrations"
for _p in (str(_LIB), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import schema_migration  # noqa: E402
import tracks  # noqa: E402
import planning_cli  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402
import plan_gate_tiebreaker as pgt  # noqa: E402
from ndjson_hash_chain import walk_chain  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402

_THICK_GOAL = "Ship a coherent plan for the widget. " * 10  # 390 chars
_MIN_GOAL_CHARS = 200
_TRACK = "feat-steps"
_PROJECT = "p1"
_DEFAULT_TRACE = "weight=default -> 1 seat(s) by default (codex); opus reads on BLOCK"


# ---------------------------------------------------------------------------
# A pre-migrated store — same bootstrap as test_plan_gate_batch.py
# ---------------------------------------------------------------------------

def _bootstrap(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
        "state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT, "
        "priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT, "
        "attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "expires_after TEXT, metadata_json TEXT DEFAULT '{}', "
        "operator_approved_at TEXT, output_kind TEXT, output_ref TEXT, "
        "UNIQUE(dispatch_id, project_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT, event_type TEXT, entity_type TEXT, entity_id TEXT, from_state TEXT, "
        "to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT)"
    )
    conn.commit()
    # 0022 rebuilds ``dispatches`` (RENAME + CREATE + DROP): the canonical
    # columns must be added AFTER it runs.
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ]:
        schema_migration.apply_script_if_below(
            conn, version, (_MIGRATIONS / filename).read_text(encoding="utf-8"))
        conn.commit()
    ensure_dispatches_columns(conn)
    conn.commit()
    for version, filename in [
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
        (33, "0033_track_decision_ref.sql"),
    ]:
        schema_migration.apply_script_if_below(
            conn, version, (_MIGRATIONS / filename).read_text(encoding="utf-8"))
        conn.commit()
    conn.close()
    return state_dir


@pytest.fixture(autouse=True)
def _no_real_lane(tmp_path, monkeypatch):
    """No test may reach a real seat or tiebreaker lane; the connector is stubbed.

    ``max_rounds=2`` keeps every run below the stop-rule threshold, so no case
    here needs the tiebreaker at all — the patch below turns a run that does
    reach for one into a loud failure instead of a live dispatch.
    """
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    monkeypatch.setattr(
        pgt, "load_tiebreaker_config",
        lambda *a, **k: {"max_rounds": 2, "provider": "deepseek_harness",
                         "model": "deepseek-v4-pro"},
    )

    def _refuse(*_args, **_kwargs):
        raise AssertionError("a test reached the real tiebreaker lane")

    monkeypatch.setattr(pgt, "run_tiebreaker", _refuse)


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch) -> Path:
    """An isolated seat ledger per test.

    Autouse: ``_resolve_seat_ledger_path`` anchors on the repo's ``.git`` marker,
    so without this patch a run would read — and record a round into — the real
    ledger under the checkout.
    """
    path = tmp_path / ".vnx-attest" / "plan-gate-seats.ndjson"
    monkeypatch.setattr(pgp, "_resolve_seat_ledger_path", lambda data_dir: path)
    return path


@pytest.fixture
def state_dir(tmp_path):
    return _bootstrap(tmp_path)


def _make_track(state_dir: Path, track_id: str = _TRACK, *, goal: str = _THICK_GOAL) -> None:
    tracks.create_track(state_dir, track_id, _PROJECT, "t", goal, phase="queued")


def _panelist(label: str, verdict: str, *, findings=(), rationale: str = "",
              blocking: bool = True) -> dict:
    return {
        "label": label, "provider": label, "verdict": verdict, "dispatched": True,
        "parse_error": False, "no_verdict": False, "rationale": rationale,
        "blocking_findings": list(findings) if blocking else [], "error": "",
    }


def _panel_result(decision: str, panelists: list, *, counts: dict, rationale: str,
                  truncation=None) -> dict:
    return {
        "track_id": _TRACK, "project_id": _PROJECT, "decision": decision,
        "summary": {"decision": decision, "rationale": rationale, **counts},
        "panelists": panelists,
        "doc_truncation": truncation or {"truncated": False},
    }


def _stub_panel(monkeypatch, result: dict, captured: list | None = None) -> None:
    """Stub the panel run only (as the sibling gate tests do); record its arguments."""
    def _run_panel(doc_path, **kw):
        if captured is not None:
            captured.append({"doc_path": doc_path, **kw})
        return result

    monkeypatch.setattr(pgp, "run_panel", _run_panel)


def _stub_pass_record(monkeypatch, *, wrote: bool, captured: list | None = None):
    def _emit(**kw):
        if captured is not None:
            captured.append(kw)
        return wrote

    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", _emit)


def _run(state_dir: Path, **kw):
    kw.setdefault("min_goal_chars", _MIN_GOAL_CHARS)
    kw.setdefault("track_id", _TRACK)
    return planning_cli.run_plan_gate_for_track(
        state_dir=state_dir, project_id=_PROJECT, **kw,
    )


def _write_doc(tmp_path: Path, text: str = "## Plan\none version\n") -> Path:
    doc = tmp_path / "plan.md"
    doc.write_text(text, encoding="utf-8")
    return doc


def _seat_round_note(doc_text: str) -> str:
    """The stop-rule line for an unread version: the plan hash, the count, the threshold.

    The rationale names the version by the first 12 hex chars of its sha256.
    """
    return (
        f"plan-gate rounds: plan version {pgt.plan_sha256(doc_text)[:12]} has no read seat "
        "round yet, so its seat reads it first (0 earlier version(s) read without a "
        "PASS, threshold 2)."
    )


def _expect(**overrides) -> dict:
    """The full ``PlanGateRunResult`` with every field at its default."""
    base = {
        "track_id": _TRACK, "project_id": _PROJECT, "exit_code": 0, "outcome": "",
        "decision": "", "seats": 0, "variant": "", "chosen_by": "", "gov_trace": "",
        "plan_source": "", "plan_source_info": {}, "ignored_goal": False,
        "resolved": False, "derived_status": None, "still_blocked": False,
        "thin_length": None, "thin_threshold": None, "error": "", "result": None,
        "stderr_lines": [], "stdout_text": [], "stdout_json": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Step A — the plan source: the two _error exits and the PlanRefusal result
# ---------------------------------------------------------------------------

def test_a_missing_doc_is_an_error_before_anything_else_runs(state_dir, tmp_path, monkeypatch):
    _make_track(state_dir)
    captured: list = []
    _stub_panel(monkeypatch, _panel_result("PASS", [], counts={"pass_count": 0,
                  "revise_count": 0, "block_count": 0}, rationale="unreachable"),
                captured)
    missing = tmp_path / "absent-plan.md"

    outcome = _run(state_dir, doc=str(missing))

    message = f"plan doc not found: {missing}"
    assert asdict(outcome) == _expect(
        exit_code=1, outcome="ERROR", error=message, stderr_lines=[message],
    )
    assert captured == [], "a missing doc must not reach the panel"


def test_an_unreadable_doc_is_an_error_naming_the_os_reason(state_dir, tmp_path, monkeypatch):
    _make_track(state_dir)
    doc = _write_doc(tmp_path)
    real_read_text = Path.read_text

    def _read_text(self, *args, **kwargs):
        if self == doc:
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    captured: list = []
    _stub_panel(monkeypatch, _panel_result("PASS", [], counts={"pass_count": 0,
                  "revise_count": 0, "block_count": 0}, rationale="unreachable"),
                captured)

    outcome = _run(state_dir, doc=str(doc))

    message = f"plan doc unreadable: {doc}: permission denied"
    assert asdict(outcome) == _expect(
        exit_code=1, outcome="ERROR", error=message, stderr_lines=[message],
    )
    assert captured == [], "an unreadable doc must not reach the panel"


def test_a_thin_goal_is_refused_with_its_length_and_the_threshold(state_dir, monkeypatch):
    _make_track(state_dir, "feat-thin", goal="too short")
    captured: list = []
    _stub_panel(monkeypatch, _panel_result("PASS", [], counts={"pass_count": 0,
                  "revise_count": 0, "block_count": 0}, rationale="unreachable"),
                captured)

    outcome = _run(state_dir, track_id="feat-thin")

    refusal = planning_cli.PlanRefusal.thin_goal(len("too short"), _MIN_GOAL_CHARS)
    assert asdict(outcome) == _expect(
        track_id="feat-thin", exit_code=2, outcome="REFUSED_THIN", thin_length=9,
        thin_threshold=_MIN_GOAL_CHARS, error=str(refusal),
        stderr_lines=[str(refusal)],
    )
    assert captured == [], "a refused plan must not burn a seat"


def test_zero_deliverables_is_refused_naming_the_deliverables(state_dir, monkeypatch):
    _make_track(state_dir, "feat-nodlv")
    captured: list = []
    _stub_panel(monkeypatch, _panel_result("PASS", [], counts={"pass_count": 0,
                  "revise_count": 0, "block_count": 0}, rationale="unreachable"),
                captured)

    outcome = _run(state_dir, track_id="feat-nodlv")

    refusal = planning_cli.PlanRefusal.no_deliverables("feat-nodlv")
    assert asdict(outcome) == _expect(
        track_id="feat-nodlv", exit_code=2, outcome="REFUSED_NO_DELIVERABLES",
        error=str(refusal),
        stderr_lines=[str(refusal)],
    )
    assert (outcome.thin_length, outcome.thin_threshold) == (None, None)
    assert captured == [], "a refused plan must not burn a seat"


def test_a_doc_run_ignores_the_tracks_goal_state_and_says_so(state_dir, tmp_path, monkeypatch):
    """The doc wins; the goal on the track is marked ignored in the note and the info dict."""
    _make_track(state_dir)
    doc = _write_doc(tmp_path)
    panel_calls: list = []
    _stub_panel(monkeypatch, _panel_result(
        "REVISE", [_panelist("codex", "revise", findings=["split the PR"],
                             rationale="too coarse")],
        counts={"pass_count": 0, "revise_count": 1, "block_count": 0},
        rationale="one revise"), panel_calls)
    _stub_pass_record(monkeypatch, wrote=True)

    outcome = _run(state_dir, doc=str(doc))

    assert outcome.ignored_goal is True
    assert outcome.plan_source == "doc"
    assert outcome.plan_source_info == {
        "source": "doc", "ignored_goal": True, "doc_path": str(doc),
    }
    assert outcome.stderr_lines[0] == f"plan-gate source: doc {doc} (track goal_state ignored)"
    assert panel_calls[0]["doc_text"] == doc.read_text(encoding="utf-8")
    assert panel_calls[0]["doc_path"] == doc
    assert asdict(outcome) == _expect(
        exit_code=2, outcome="REVISE", decision="REVISE", seats=1, variant="default",
        chosen_by="default", gov_trace=_DEFAULT_TRACE, plan_source="doc",
        plan_source_info={"source": "doc", "ignored_goal": True, "doc_path": str(doc)},
        ignored_goal=True, result=_panel_result(
            "REVISE", [_panelist("codex", "revise", findings=["split the PR"],
                                 rationale="too coarse")],
            counts={"pass_count": 0, "revise_count": 1, "block_count": 0},
            rationale="one revise"),
        stderr_lines=[
            f"plan-gate source: doc {doc} (track goal_state ignored)",
            f"plan-gate seats: {_DEFAULT_TRACE}.",
            _seat_round_note(doc.read_text(encoding="utf-8")),
            "REVISE — plan gate NOT cleared; track stays blocked. Revise the plan and re-run.",
        ],
        stdout_text=[
            "Plan gate: REVISE  (0 pass / 1 revise / 0 block)",
            "  one revise",
            "  - codex            REVISE  too coarse",
            "      · split the PR",
        ],
        stdout_json=json.dumps({**outcome.result, "plan_source": {
            "source": "doc", "ignored_goal": True, "doc_path": str(doc),
        }}, indent=2),
    )


# ---------------------------------------------------------------------------
# Step C — the pass record (inside the PASS branch)
# ---------------------------------------------------------------------------

def _pass_panelists() -> list:
    return [_panelist("codex", "pass", rationale="solid", blocking=False)]


def _pass_result() -> dict:
    return _panel_result("PASS", _pass_panelists(),
                         counts={"pass_count": 1, "revise_count": 0, "block_count": 0},
                         rationale="one pass")


def test_a_pass_that_resolves_the_blocker_writes_the_pass_record(state_dir, tmp_path,
                                                                 monkeypatch):
    _make_track(state_dir)
    doc = _write_doc(tmp_path)
    _stub_panel(monkeypatch, _pass_result())
    monkeypatch.setattr(planning_cli, "_resolve_plan_blocker", lambda *a, **k: True)
    written: list = []
    _stub_pass_record(monkeypatch, wrote=True, captured=written)

    outcome = _run(state_dir, doc=str(doc))

    assert written == [{
        "repo_root": None, "track_id": _TRACK, "project_id": _PROJECT,
        "resolver": "run", "seats": 1, "scope": "default", "reason": _DEFAULT_TRACE,
        "governance_variant": "default", "gov_trace": _DEFAULT_TRACE,
    }]
    assert asdict(outcome) == _expect(
        outcome="PASS", decision="PASS", seats=1, variant="default", chosen_by="default",
        gov_trace=_DEFAULT_TRACE, plan_source="doc",
        plan_source_info={"source": "doc", "ignored_goal": True, "doc_path": str(doc)},
        ignored_goal=True, resolved=True, result=_pass_result(),
        stderr_lines=[
            f"plan-gate source: doc {doc} (track goal_state ignored)",
            f"plan-gate seats: {_DEFAULT_TRACE}.",
            _seat_round_note(doc.read_text(encoding="utf-8")),
        ],
        stdout_text=[
            "Plan gate: PASS  (1 pass / 0 revise / 0 block)",
            "  one pass",
            "  - codex            PASS  solid",
            f"PASS — plan gate cleared. OI-PLAN-{_TRACK} resolved=True; "
            "track derived_status=None.",
        ],
        stdout_json=json.dumps({**_pass_result(), "plan_source": {
            "source": "doc", "ignored_goal": True, "doc_path": str(doc),
        }}, indent=2),
    )


def test_a_pass_without_a_written_record_says_the_evidence_is_missing(state_dir, tmp_path,
                                                                      monkeypatch):
    _make_track(state_dir)
    doc = _write_doc(tmp_path)
    _stub_panel(monkeypatch, _pass_result())
    monkeypatch.setattr(planning_cli, "_resolve_plan_blocker", lambda *a, **k: True)
    written: list = []
    _stub_pass_record(monkeypatch, wrote=False, captured=written)

    outcome = _run(state_dir, doc=str(doc))

    warning = (
        f"WARNING: plan_gate_pass evidence NOT written for track {_TRACK} "
        "(resolver=run, seats=1, scope=default). The plan blocker IS resolved, but the "
        "durable pass record is missing - the merge gate may not recognize this pass."
    )
    assert written == [{
        "repo_root": None, "track_id": _TRACK, "project_id": _PROJECT,
        "resolver": "run", "seats": 1, "scope": "default", "reason": _DEFAULT_TRACE,
        "governance_variant": "default", "gov_trace": _DEFAULT_TRACE,
    }]
    assert asdict(outcome) == _expect(
        outcome="PASS", decision="PASS", seats=1, variant="default", chosen_by="default",
        gov_trace=_DEFAULT_TRACE, plan_source="doc",
        plan_source_info={"source": "doc", "ignored_goal": True, "doc_path": str(doc)},
        ignored_goal=True, resolved=True, result=_pass_result(),
        stderr_lines=[
            f"plan-gate source: doc {doc} (track goal_state ignored)",
            f"plan-gate seats: {_DEFAULT_TRACE}.",
            _seat_round_note(doc.read_text(encoding="utf-8")),
            warning,
        ],
        stdout_text=[
            "Plan gate: PASS  (1 pass / 0 revise / 0 block)",
            "  one pass",
            "  - codex            PASS  solid",
            f"PASS — plan gate cleared. OI-PLAN-{_TRACK} resolved=True; "
            "track derived_status=None.",
        ],
        stdout_json=json.dumps({**_pass_result(), "plan_source": {
            "source": "doc", "ignored_goal": True, "doc_path": str(doc),
        }}, indent=2),
    )


# ---------------------------------------------------------------------------
# Step B — the verdict text
# ---------------------------------------------------------------------------

def test_a_truncated_revise_renders_the_verdict_text_and_json(state_dir, tmp_path,
                                                             monkeypatch, ledger):
    """Two panelists, one of them blocking, on a truncated doc: the whole verdict text
    (truncation line, rationale, per-seat lines with their findings) and the json."""
    _make_track(state_dir)
    doc = _write_doc(tmp_path)
    panelists = [
        _panelist("codex", "revise", findings=["name the rollback", "split the PR"],
                  rationale="needs a rollback"),
        _panelist("opus", "block", findings=["scope is unstated"], rationale="no scope"),
    ]
    result = _panel_result(
        "REVISE", panelists,
        counts={"pass_count": 0, "revise_count": 1, "block_count": 1},
        rationale="1 BLOCK verdict",
        truncation={"truncated": True, "kept_chars": 1000, "original_chars": 4000},
    )
    _stub_panel(monkeypatch, result)

    outcome = _run(state_dir, doc=str(doc), panel_seats="codex,opus")

    stdout_text = [
        "Plan gate: REVISE  (0 pass / 1 revise / 1 block)",
        "  ** PLAN DOC TRUNCATED: gate read 1000 of 4000 chars (25.0%) — this verdict "
        "may be based on an incomplete plan **",
        "  1 BLOCK verdict",
        "  - codex            REVISE  needs a rollback",
        "      · name the rollback",
        "      · split the PR",
        "  - opus             BLOCK  no scope",
        "      · scope is unstated",
    ]
    plan_source_info = {"source": "doc", "ignored_goal": True, "doc_path": str(doc)}
    assert asdict(outcome) == _expect(
        exit_code=2, outcome="REVISE", decision="REVISE", seats=2, variant="default",
        chosen_by="operator",
        gov_trace="weight=default -> 2 seat(s) by operator (opus, codex); "
                  "OVERRIDES the default seat codex",
        plan_source="doc", plan_source_info=plan_source_info, ignored_goal=True,
        result=result, stdout_text=stdout_text,
        stderr_lines=[
            f"plan-gate source: doc {doc} (track goal_state ignored)",
            "plan-gate override: weight=default -> 2 seat(s) by operator (opus, codex); "
            "OVERRIDES the default seat codex.",
            _seat_round_note(doc.read_text(encoding="utf-8")),
            "REVISE — plan gate NOT cleared; track stays blocked. Revise the plan and re-run.",
        ],
        stdout_json=json.dumps({**result, "plan_source": plan_source_info}, indent=2),
    )
    # The verdict json is the panel result plus the plan source — nothing else.
    assert json.loads(outcome.stdout_json) == {**result, "plan_source": plan_source_info}
    # The non-PASS round landed in the seat ledger with this plan's hash and both verdicts.
    (round_record,) = [rec for _ln, rec, _h in walk_chain(ledger)
                       if rec.get("type") == pgt.ROUND_RECORD_TYPE]
    assert round_record["plan_sha256"] == pgt.plan_sha256(doc.read_text(encoding="utf-8"))
    assert round_record["scored_seats"] == 2
    assert [v["seat"] for v in round_record["seat_verdicts"]] == ["codex", "opus"]
