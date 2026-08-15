"""Stop-rule + tiebreaker for the plan-gate (punten 7-9 OPSCHALING-cluster).

``plan_gate_tiebreaker`` implements the stop-rule: after ``max_rounds`` panel
rounds that do not certify a PASS, the gate stops running the full panel and
decides via a SINGLE tiebreaker (binary START/STOP, at most one change, no
findings list, model from the registry). On STOP the last round's findings
become open items; the blocker clears with a ``resolution_reason`` that names
the tiebreaker model + round + outcome.

These tests drive the module with an injectable dispatcher (no live model) and
read the STATE back from disk — the dispatch's rule "meet de state, niet de
returnwaarde". A counter that "must persist" is only truly tested by writing,
discarding the writer, and reading again.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
for p in (str(_LIB), str(_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import schema_migration  # noqa: E402
import tracks  # noqa: E402
import track_reconciler  # noqa: E402
import planning_cli  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402
import plan_gate_tiebreaker as pgt  # noqa: E402
from ndjson_hash_chain import walk_chain  # noqa: E402


# --------------------------------------------------------------------------
# test helpers (mirror the proven _bootstrap in test_plan_gate_panel.py)
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _stub_reconciler(monkeypatch):
    """Stub ``track_reconciler.reconcile_track`` for the cmd-level tests.

    The minimal ``_bootstrap`` fixture (mirrored from test_plan_gate_panel.py)
    does not run the structural-doctor preflight that adds ``output_ref`` /
    ``output_kind`` to the dispatches table, so the reconciler's deliverable
    rollup raises ``no such column: output_ref``. The reconciler is out of
    scope for the stop-rule tests; ``_resolve_plan_blocker`` writes the
    ``resolution_reason`` via ``unlink_open_item`` BEFORE it calls
    ``reconcile_track``, so stubbing the reconcile call keeps the reason-write
    real while avoiding the schema gap. (This is the same gap the existing
    test_plan_gate_panel.py tests hit on this checkout.)
    """
    monkeypatch.setattr(track_reconciler, "reconcile_track", lambda *a, **k: None)


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
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
        (33, "0033_track_decision_ref.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()
    return state_dir


def _tiebreak_report(payload: str) -> str:
    return f"prose\n```{pgt.TIEBREAKER_FENCE}\n{payload}\n```\n"


def _start_report() -> str:
    return _tiebreak_report('{"outcome": "START", "required_change": "", "rationale": "ok"}')


def _stop_report(change: str = "") -> str:
    return _tiebreak_report(
        f'{{"outcome": "STOP", "required_change": "{change}", "rationale": "stop"}}'
    )


def _resolution_reason(state_dir: Path, track_id: str, pid: str) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT resolution_reason FROM track_open_items "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
            (track_id, pid, planning_cli._plan_blocker_oi(track_id)),
        ).fetchone()
        return (row["resolution_reason"] if row else "") or ""
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Punt 7 — round counter persists across restart
# --------------------------------------------------------------------------

def test_round_counter_increments_and_persists_across_restart(tmp_path):
    """The counter increments per round and survives a "restart": write, discard
    the writer object, read the ledger back, believe the file — not the return
    value of the write call."""
    ledger = tmp_path / "plan-gate-seats.ndjson"

    # Round 0: nothing recorded yet.
    assert pgt.read_round_count(ledger, "trk", "p1") == 0

    # A fresh writer records round 1 (simulating a process), then "exits".
    assert pgt.record_round(
        ledger, track_id="trk", project_id="p1", round_number=1, outcome="panel",
    ) is True
    # The writer is gone (no object held); a NEW reader reads the state back.
    assert pgt.read_round_count(ledger, "trk", "p1") == 1

    # A second process records round 2.
    assert pgt.record_round(
        ledger, track_id="trk", project_id="p1", round_number=2, outcome="panel",
    ) is True
    # Third reader, fresh: the counter is 2, read from disk.
    assert pgt.read_round_count(ledger, "trk", "p1") == 2

    # The records are real hash-chained entries in the ledger (not a side file).
    rounds = [
        rec for _ln, rec, _h in walk_chain(ledger)
        if rec.get("type") == pgt.ROUND_RECORD_TYPE
    ]
    assert len(rounds) == 2
    assert {r["round"] for r in rounds} == {1, 2}


def test_round_counter_is_per_track(tmp_path):
    """The counter is per (track, project): a different track reads 0 even with
    another track's rounds recorded."""
    ledger = tmp_path / "plan-gate-seats.ndjson"
    pgt.record_round(ledger, track_id="trk-a", project_id="p1", round_number=1, outcome="panel")
    pgt.record_round(ledger, track_id="trk-a", project_id="p1", round_number=2, outcome="panel")
    assert pgt.read_round_count(ledger, "trk-a", "p1") == 2
    assert pgt.read_round_count(ledger, "trk-b", "p1") == 0


def test_record_round_always_writes_governance_fields_even_when_empty(tmp_path):
    """A round record always carries ``governance_variant`` + ``gov_trace``.

    The write path must not silently drop the pair when the writer derived
    none: an empty string (same-schema "no derivation") is distinct from a
    MISSING field (an older-schema record). A later sweep diffs the two, so the
    fields must be present on every new record. Fails as soon as the fields fall
    out of ``record_round``'s record dict.
    """
    ledger = tmp_path / "plan-gate-seats.ndjson"
    assert pgt.record_round(
        ledger, track_id="trk", project_id="p1", round_number=1, outcome="panel",
    ) is True

    rounds = [
        rec for _ln, rec, _h in walk_chain(ledger)
        if rec.get("type") == pgt.ROUND_RECORD_TYPE
    ]
    assert len(rounds) == 1
    rec = rounds[0]
    assert "governance_variant" in rec, "governance_variant must always be written"
    assert "gov_trace" in rec, "gov_trace must always be written"
    assert rec["governance_variant"] == ""
    assert rec["gov_trace"] == ""


def test_should_run_tiebreaker_threshold(tmp_path):
    """Below the threshold the full panel runs; at/above it the tiebreaker runs.
    Default threshold is 2 (read from the code default when no config overrides)."""
    ledger = tmp_path / "plan-gate-seats.ndjson"
    # 0 rounds -> full panel
    assert pgt.should_run_tiebreaker(ledger, "trk", "p1", max_rounds=2) is False
    pgt.record_round(ledger, track_id="trk", project_id="p1", round_number=1, outcome="panel")
    # 1 round -> full panel
    assert pgt.should_run_tiebreaker(ledger, "trk", "p1", max_rounds=2) is False
    pgt.record_round(ledger, track_id="trk", project_id="p1", round_number=2, outcome="panel")
    # 2 rounds -> tiebreaker (at threshold)
    assert pgt.should_run_tiebreaker(ledger, "trk", "p1", max_rounds=2) is True
    pgt.record_round(ledger, track_id="trk", project_id="p1", round_number=3, outcome="panel")
    # 3 rounds -> still tiebreaker (above threshold)
    assert pgt.should_run_tiebreaker(ledger, "trk", "p1", max_rounds=2) is True


# --------------------------------------------------------------------------
# Punt 8 — strict tiebreaker parsing
# --------------------------------------------------------------------------

def test_parse_tiebreaker_accepts_clean_start():
    r = pgt.parse_tiebreaker(_start_report())
    assert r.outcome == "START"
    assert r.required_change == ""


def test_parse_tiebreaker_findings_list_fails_loud():
    """A tiebreaker that returns a findings list answered as a seat, not a
    tiebreaker. Parsing fails LOUD — we never silently fill in a decision."""
    report = _tiebreak_report(
        '{"outcome": "START", "findings": ["gap one", "gap two"]}'
    )
    with pytest.raises(pgt.TiebreakerParseError, match="findings"):
        pgt.parse_tiebreaker(report)


def test_parse_tiebreaker_blocking_findings_list_fails_loud():
    """The ``blocking_findings`` key (the seat contract's name) is also rejected."""
    report = _tiebreak_report(
        '{"outcome": "STOP", "blocking_findings": ["a"]}'
    )
    with pytest.raises(pgt.TiebreakerParseError, match="blocking_findings"):
        pgt.parse_tiebreaker(report)


def test_parse_tiebreaker_two_changes_fails_loud():
    """More than one required_change fails LOUD — the brief allows at most ONE."""
    report = _tiebreak_report(
        '{"outcome": "STOP", "required_change": ["change one", "change two"]}'
    )
    with pytest.raises(pgt.TiebreakerParseError, match="2 required changes"):
        pgt.parse_tiebreaker(report)


def test_parse_tiebreaker_single_change_list_accepted():
    """A one-element list is the single change (some models wrap a string in a
    list); it is collapsed, not rejected."""
    report = _tiebreak_report(
        '{"outcome": "STOP", "required_change": ["add a rollback section"]}'
    )
    r = pgt.parse_tiebreaker(report)
    assert r.outcome == "STOP"
    assert r.required_change == "add a rollback section"


def test_parse_tiebreaker_no_fence_fails_loud():
    with pytest.raises(pgt.TiebreakerParseError, match="no tiebreaker decision block"):
        pgt.parse_tiebreaker("just prose, no fence at all")


def test_parse_tiebreaker_bad_outcome_fails_loud():
    report = _tiebreak_report('{"outcome": "MAYBE"}')
    with pytest.raises(pgt.TiebreakerParseError, match="unknown outcome"):
        pgt.parse_tiebreaker(report)


def test_parse_tiebreaker_carries_raw_text_on_failure():
    """The raw text is attached to the error so a caller can surface WHAT failed
    to parse, not just that it failed."""
    try:
        pgt.parse_tiebreaker("no fence here")
    except pgt.TiebreakerParseError as exc:
        assert exc.raw == "no fence here"
    else:
        pytest.fail("expected TiebreakerParseError")


# --------------------------------------------------------------------------
# OI-1219 — a synthesized report is a NO-ANSWER, not a parse failure
# --------------------------------------------------------------------------

def _synthesized_report(status: str = "timeout") -> str:
    """A governance-synthesized body exactly as dispatch_govern._synthesize
    emits it: the marker, a Status line, and NO verdict fence."""
    return (
        f"# Dispatch plan-tiebreak-x\n\n"
        f"- Lane: tmux_interactive\n"
        f"- Status: {status}\n"
        f"- contract_status: synthesized\n"
        f"- {pgp.SYNTHESIZED_REPORT_MARKER}\n\n"
        f"## Summary\n\nNo commit on branch; worker emitted status={status}. "
        f"Body synthesized by lane.\n"
    )


def test_run_tiebreaker_synthesized_report_raises_no_answer_not_parse_error():
    """A synthesized report raises TiebreakerNoAnswerError with the lane status,
    NOT TiebreakerParseError. The lane delivered nothing to parse, so the reason
    names the lane state, not the parser. Measured by feeding the report in and
    reading the exception type, not by grepping for a marker string in code."""
    def _synth_dispatcher(provider, model_arg, instruction, dispatch_id):
        return _synthesized_report("timeout")

    with pytest.raises(pgt.TiebreakerNoAnswerError) as excinfo:
        pgt.run_tiebreaker(
            doc_text="## Problem\n## Approach\n",
            track_id="trk-synth", project_id="p1", round_number=3,
            model_arg="deepseek-v4-pro", dispatcher=_synth_dispatcher,
        )
    assert excinfo.value.status == "timeout"
    assert "no answer" in str(excinfo.value)


def test_run_tiebreaker_synthesized_error_status_is_carried():
    """The lane status from the report's Status line is carried verbatim (error,
    not just the timeout default)."""
    def _synth_dispatcher(provider, model_arg, instruction, dispatch_id):
        return _synthesized_report("error")

    with pytest.raises(pgt.TiebreakerNoAnswerError) as excinfo:
        pgt.run_tiebreaker(
            doc_text="## Problem\n", track_id="t", project_id="p1",
            round_number=3, model_arg="deepseek-v4-pro", dispatcher=_synth_dispatcher,
        )
    assert excinfo.value.status == "error"


def test_run_tiebreaker_real_answer_still_parse_failure():
    """A REAL model answer (no marker) that fails the strict contract still
    raises TiebreakerParseError. Parse failure stays reserved for an answer that
    exists but does not satisfy the contract."""
    def _bad_dispatcher(provider, model_arg, instruction, dispatch_id):
        return f"prose\n```{pgt.TIEBREAKER_FENCE}\n{{\"outcome\": \"MAYBE\"}}\n```\n"

    with pytest.raises(pgt.TiebreakerParseError, match="unknown outcome"):
        pgt.run_tiebreaker(
            doc_text="## Problem\n", track_id="t", project_id="p1",
            round_number=3, model_arg="deepseek-v4-pro", dispatcher=_bad_dispatcher,
        )


# --------------------------------------------------------------------------
# Punt 17 — the "geen antwoord" branch: all three silent-lane forms
# --------------------------------------------------------------------------
#
# The tiebreaker has two failure branches: a REAL answer that fails the strict
# contract (TiebreakerParseError), and NO answer at all (TiebreakerNoAnswerError).
# The second branch had only ever been proven for the synthesized-report form
# (which the probe hit by luck); the empty-completion and dropped-process forms
# were never driven. These tests drive all three forms and assert each reads as
# a DISTINCT no-answer, never as a decision and never as a parse failure.

def test_run_tiebreaker_empty_completion_is_no_answer_not_parse_error():
    """An EMPTY completion (zero tokens) is a NO-ANSWER, not a parse failure.

    ``parse_tiebreaker("")`` raises ``TiebreakerParseError("empty tiebreaker
    report")``, which conflates "the model said nothing" with "the model said
    something malformed". ``run_tiebreaker`` must intercept the empty completion
    BEFORE the parser and raise ``TiebreakerNoAnswerError`` instead.
    """
    def _empty_dispatcher(provider, model_arg, instruction, dispatch_id):
        return ""

    with pytest.raises(pgt.TiebreakerNoAnswerError) as excinfo:
        pgt.run_tiebreaker(
            doc_text="## Problem\n## Approach\n",
            track_id="trk-empty", project_id="p1", round_number=3,
            model_arg="deepseek-v4-pro", dispatcher=_empty_dispatcher,
        )
    assert excinfo.value.status == "empty"


def test_run_tiebreaker_whitespace_completion_is_no_answer():
    """Whitespace-only output (a lane that emitted nothing but blanks) is also a
    no-answer, not a parse failure."""
    def _ws_dispatcher(provider, model_arg, instruction, dispatch_id):
        return "   \n\t\n"

    with pytest.raises(pgt.TiebreakerNoAnswerError) as excinfo:
        pgt.run_tiebreaker(
            doc_text="## Problem\n", track_id="t", project_id="p1",
            round_number=3, model_arg="deepseek-v4-pro", dispatcher=_ws_dispatcher,
        )
    assert excinfo.value.status == "empty"


def test_run_tiebreaker_process_drop_no_output_is_no_answer_not_generic():
    """A lane that drops without output surfaces as a RAISED dispatcher (the real
    dispatcher raises ``RuntimeError`` when ``_read_report`` finds no report
    file). That must read as NO-ANSWER (status "no-output"), not as a generic
    exception leaking out of ``run_tiebreaker``."""
    def _drop_dispatcher(provider, model_arg, instruction, dispatch_id):
        raise RuntimeError("no report for plan-tiebreak-x (rc=1): ...")

    with pytest.raises(pgt.TiebreakerNoAnswerError) as excinfo:
        pgt.run_tiebreaker(
            doc_text="## Problem\n", track_id="t", project_id="p1",
            round_number=3, model_arg="deepseek-v4-pro", dispatcher=_drop_dispatcher,
        )
    assert excinfo.value.status == "no-output"


def test_run_tiebreaker_lane_subprocess_timeout_is_no_answer_timeout_status():
    """A hung lane subprocess (``subprocess.TimeoutExpired``) reads as no-answer
    with status "timeout", distinct from the drop-without-output form."""
    import subprocess

    def _hang_dispatcher(provider, model_arg, instruction, dispatch_id):
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=900)

    with pytest.raises(pgt.TiebreakerNoAnswerError) as excinfo:
        pgt.run_tiebreaker(
            doc_text="## Problem\n", track_id="t", project_id="p1",
            round_number=3, model_arg="deepseek-v4-pro", dispatcher=_hang_dispatcher,
        )
    assert excinfo.value.status == "timeout"


def test_empty_answer_is_never_a_decision():
    """An empty completion must never come back as a START/STOP decision: the
    call RAISES, so there is no ``TiebreakerResult`` and no outcome to misread
    as a PASS (START) or REVISE (STOP)."""
    def _empty_dispatcher(provider, model_arg, instruction, dispatch_id):
        return ""

    with pytest.raises(pgt.TiebreakerNoAnswerError):
        pgt.run_tiebreaker(
            doc_text="## Problem\n", track_id="t", project_id="p1",
            round_number=3, model_arg="deepseek-v4-pro", dispatcher=_empty_dispatcher,
        )


def test_no_answer_is_distinct_from_parse_failure_for_all_three_forms():
    """Each silent-lane form raises ``TiebreakerNoAnswerError`` and NONE raises
    ``TiebreakerParseError``: a no-answer is a lane failure, a parse failure is a
    contract failure, and the two must never be conflated."""
    def _empty(provider, model_arg, instruction, dispatch_id):
        return ""

    def _drop(provider, model_arg, instruction, dispatch_id):
        raise RuntimeError("no report")

    def _synth_timeout(provider, model_arg, instruction, dispatch_id):
        return _synthesized_report("timeout")

    for dispatcher, expected_status in [
        (_empty, "empty"),
        (_drop, "no-output"),
        (_synth_timeout, "timeout"),
    ]:
        with pytest.raises(pgt.TiebreakerNoAnswerError) as excinfo:
            pgt.run_tiebreaker(
                doc_text="## Problem\n", track_id="t", project_id="p1",
                round_number=3, model_arg="deepseek-v4-pro", dispatcher=dispatcher,
            )
        assert excinfo.value.status == expected_status
        assert not isinstance(excinfo.value, pgt.TiebreakerParseError)


# --------------------------------------------------------------------------
# Punt 8 — model identity from the registry (ADR-036)
# --------------------------------------------------------------------------

def test_tiebreaker_model_resolved_from_registry():
    """The model comes from the registry, not a Python literal on the dispatch
    path. resolve_tiebreaker_model returns the DISPATCH enum (what the lane
    consumes, e.g. deepseek-harness) + the registry model key + its
    cli_model_arg."""
    provider, model_key, cli_arg = pgt.resolve_tiebreaker_model()
    assert provider == "deepseek-harness"
    assert model_key == "deepseek-v4-pro"
    # The deepseek_harness registry models carry a cli_model_arg -> it is the
    # arg the provider lane takes.
    assert cli_arg == "deepseek-v4-pro"


def test_tiebreaker_unknown_model_fails_loud():
    """An unknown provider/model in the config fails LOUD (RegistryLookupError),
    naming what was missing — the ADR-036 fail-loud contract, never a silent
    None."""
    from providers.provider_registry import RegistryLookupError

    bad_cfg = {"max_rounds": 2, "provider": "anthropic", "model": "nonexistent-model-xyz"}
    with pytest.raises(RegistryLookupError, match="nonexistent-model-xyz"):
        pgt.resolve_tiebreaker_model(bad_cfg)

    bad_cfg2 = {"max_rounds": 2, "provider": "no-such-provider", "model": "fable-5"}
    with pytest.raises(RegistryLookupError, match="no-such-provider"):
        pgt.resolve_tiebreaker_model(bad_cfg2)

    # The shipped provider (deepseek_harness) with an unknown model also fails loud.
    bad_cfg3 = {"max_rounds": 2, "provider": "deepseek_harness", "model": "nonexistent-model-xyz"}
    with pytest.raises(RegistryLookupError, match="nonexistent-model-xyz"):
        pgt.resolve_tiebreaker_model(bad_cfg3)


def test_no_model_literal_on_dispatch_path():
    """ADR-036: zero model names as Python literals on the dispatch path.

    The tiebreaker's model identity is resolved from the registry at call time
    (``resolve_tiebreaker_model``). The ONLY string literals allowed in the
    module are (a) the config-fallback defaults in ``load_tiebreaker_config``
    (the safety net when the YAML is absent — config values, NOT the dispatch
    path), (b) docstrings/comments, and (c) the instruction contract prose.

    This test parses the module AST and fails if a model name appears as a
    string literal that is ASSIGNED to a dispatch-path variable (``model``,
    ``model_arg``, ``provider``, ``resolved_model``) OUTSIDE the
    ``load_tiebreaker_config`` function. That is the precise shape of the
    violation ADR-036 forbids: a hard-coded model string the dispatch path
    reads instead of the registry. Behavioral coverage (the model is resolved
    from the registry and an unknown model fails loud) is in the tests above;
    this test guards the source against a literal sneaking back in.
    """
    import ast

    src = Path(pgt.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    model_names = {"fable-5", "claude-fable-5", "opus-5", "sonnet-5", "kimi-k3",
                   "deepseek-v4-pro", "deepseek-v4-flash"}
    # Functions whose string literals are config/contract, not dispatch path.
    allowed_fn_names = {"load_tiebreaker_config"}

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        in_allowed = node.name in allowed_fn_names
        for sub in ast.walk(node):
            # An assignment whose VALUE is a model-name string literal, to a
            # dispatch-path variable name, inside a non-allowed function.
            if isinstance(sub, ast.Assign) and not in_allowed:
                for target in sub.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if target.id not in {"model", "model_arg", "provider", "resolved_model"}:
                        continue
                    val = sub.value
                    if isinstance(val, ast.Constant) and isinstance(val.value, str) \
                            and val.value in model_names:
                        violations.append(
                            f"{node.name}: {target.id} = {val.value!r} (line {sub.lineno})"
                        )
            # A keyword-argument default that is a model-name string literal on
            # a dispatch-path parameter, inside a non-allowed function.
            if isinstance(sub, ast.FunctionDef) and not in_allowed:
                for arg in sub.args.defaults:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and arg.value in model_names:
                        violations.append(
                            f"{node.name}: default arg {arg.value!r} (line {sub.lineno})"
                        )
    assert not violations, (
        "model literal(s) on the dispatch path (ADR-036 violation): "
        + "; ".join(violations)
    )


# --------------------------------------------------------------------------
# Punt 7+8 — cmd_plan_gate_run routes panel vs tiebreaker by round count
# --------------------------------------------------------------------------

def _isolate_seat_ledger(monkeypatch, tmp_path):
    """Force the seat ledger (and round counter) to a fresh per-test path.

    ``plan_gate_panel._resolve_seat_ledger_path`` walks to the repo-root
    ``.git`` marker, so in a worktree the ledger lands in the shared
    ``<worktree>/.vnx-attest/plan-gate-seats.ndjson`` — cross-test pollution
    that flips the round threshold. Patching the resolver to a tmp_path keeps
    each test's round counter isolated (the "meet de state" discipline: the
    disk must be the test's own, not a shared one).
    """
    ledger = tmp_path / ".vnx-attest" / "plan-gate-seats.ndjson"
    monkeypatch.setattr(pgp, "_resolve_seat_ledger_path", lambda data_dir: ledger)
    return ledger


def _gate_args(state_dir, doc, track_id="feat-tb", panel_seats=None):
    return argparse.Namespace(
        track_id=track_id, project_id="p1", state_dir=str(state_dir),
        doc=str(doc), json=False, panel_seats=panel_seats,
        dispatch_paths="scripts/lib/some_utility.py", task_class=None,
        irreversible=False, seat_timeout=None, repo_root=None,
    )


def test_cmd_plan_gate_run_full_panel_below_threshold(tmp_path, monkeypatch):
    """Under the threshold the full panel runs (run_panel called), NOT the
    tiebreaker. Asserts on which function was called, not a log line."""
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    ledger = _isolate_seat_ledger(monkeypatch, tmp_path)
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-tb", "p1", "t", "shipped", phase="queued")
    planning_cli._seed_plan_blocker(state_dir, "feat-tb", "p1")
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")

    panel_calls = {"n": 0}
    tb_calls = {"n": 0}

    def _fake_run_panel(doc_path, *, track_id, project_id, panel, data_dir, **kw):
        panel_calls["n"] += 1
        return {
            "track_id": track_id, "project_id": project_id, "decision": "PASS",
            "summary": {"decision": "PASS", "pass_count": len(panel),
                        "revise_count": 0, "block_count": 0, "rationale": "ok"},
            "panelists": [], "doc_truncation": {"truncated": False},
        }

    def _no_tiebreaker(*a, **k):
        tb_calls["n"] += 1
        return pgt.TiebreakerResult(outcome="START", model="fable-5", round=1)

    monkeypatch.setattr(pgp, "run_panel", _fake_run_panel)
    monkeypatch.setattr(pgt, "run_tiebreaker", _no_tiebreaker)
    monkeypatch.setattr(planning_cli, "_resolve_plan_blocker", lambda *a, **k: True)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)

    # No rounds recorded yet -> below threshold -> full panel.
    rc = planning_cli.cmd_plan_gate_run(_gate_args(state_dir, doc))
    assert rc == 0
    assert panel_calls["n"] == 1
    assert tb_calls["n"] == 0


def test_cmd_plan_gate_run_tiebreaker_at_threshold(tmp_path, monkeypatch):
    """At/above the threshold the tiebreaker runs (exactly ONE), NOT the full
    panel. Asserts on which function was called and how many times."""
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-tb", "p1", "t", "shipped", phase="queued")
    planning_cli._seed_plan_blocker(state_dir, "feat-tb", "p1")
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")

    # Force two completed panel rounds into the seat ledger so the next run
    # hits the threshold. The ledger path must match what cmd_plan_gate_run
    # resolves from data_dir.
    ledger = _isolate_seat_ledger(monkeypatch, tmp_path)
    assert ledger is not None
    pgt.record_round(ledger, track_id="feat-tb", project_id="p1", round_number=1, outcome="panel")
    pgt.record_round(ledger, track_id="feat-tb", project_id="p1", round_number=2, outcome="panel")

    panel_calls = {"n": 0}
    tb_calls = {"n": 0}

    def _fake_run_panel(doc_path, *, track_id, project_id, panel, data_dir, **kw):
        panel_calls["n"] += 1
        return {"decision": "PASS", "summary": {}, "panelists": [], "doc_truncation": {}}

    def _fake_run_tiebreaker(doc_path, *, doc_text=None, track_id, project_id, round_number,
                             last_round_findings, data_dir, timeout_seconds, config, model_arg=None):
        tb_calls["n"] += 1
        return pgt.TiebreakerResult(
            outcome="START", model="fable-5", round=round_number,
            required_change="", rationale="ok",
        )

    monkeypatch.setattr(pgp, "run_panel", _fake_run_panel)
    monkeypatch.setattr(pgt, "run_tiebreaker", _fake_run_tiebreaker)
    monkeypatch.setattr(planning_cli, "_resolve_plan_blocker", lambda *a, **k: True)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)

    rc = planning_cli.cmd_plan_gate_run(_gate_args(state_dir, doc))
    assert rc == 0
    # Exactly ONE tiebreaker, zero full-panel runs.
    assert tb_calls["n"] == 1
    assert panel_calls["n"] == 0


def test_cmd_plan_gate_run_records_panel_round_on_revise(tmp_path, monkeypatch):
    """A non-PASS panel round is recorded in the seat ledger so the counter
    advances (the stop-rule's input). A PASS does NOT record a further round."""
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-tb", "p1", "t", "shipped", phase="queued")
    planning_cli._seed_plan_blocker(state_dir, "feat-tb", "p1")
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    ledger = _isolate_seat_ledger(monkeypatch, tmp_path)

    def _revise_panel(doc_path, *, track_id, project_id, panel, data_dir, **kw):
        return {
            "decision": "REVISE", "summary": {"decision": "REVISE", "pass_count": 0,
                        "revise_count": 2, "block_count": 0, "rationale": "gaps"},
            "panelists": [], "doc_truncation": {"truncated": False},
        }

    monkeypatch.setattr(pgp, "run_panel", _revise_panel)
    rc = planning_cli.cmd_plan_gate_run(_gate_args(state_dir, doc))
    assert rc == 2  # REVISE -> track stays blocked
    # The round was recorded — read it back from disk (not the return value).
    assert pgt.read_round_count(ledger, "feat-tb", "p1") == 1


# --------------------------------------------------------------------------
# Punt 9 — STOP leaves real open items + names the model in resolution_reason
# --------------------------------------------------------------------------

def test_cmd_plan_gate_run_stop_creates_open_items_and_names_model(tmp_path, monkeypatch):
    """On STOP: the remaining findings become real open items (read back from
    the open-items store, not a mock call count), and the blocker's
    ``resolution_reason`` contains the tiebreaker model name."""
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-stop", "p1", "t", "shipped", phase="queued")
    planning_cli._seed_plan_blocker(state_dir, "feat-stop", "p1")
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    ledger = _isolate_seat_ledger(monkeypatch, tmp_path)

    # Seed a prior round WITH seat findings so the STOP aftermath has something
    # to carry forward. The seat ledger does not store findings, only the
    # effective verdict + rationale; _last_round_findings reads the rationale.
    pgt.record_round(ledger, track_id="feat-stop", project_id="p1", round_number=1, outcome="panel")
    pgt.record_round(ledger, track_id="feat-stop", project_id="p1", round_number=2, outcome="panel")
    # Append a seat record with a rationale so _last_round_findings returns it.
    from ndjson_hash_chain import append_chained_entry
    append_chained_entry(ledger, {
        "type": pgp.SEAT_RECORD_TYPE, "track_id": "feat-stop", "project_id": "p1",
        "panelist_id": "opus", "model": "opus", "verdict": "revise",
        "responded": True, "parse_error": False, "no_verdict": False,
        "rationale": "plan lacks a rollback section", "run_at": "2026-08-15T00:00:00Z",
    })

    def _stop_tiebreaker(doc_path, *, doc_text=None, track_id, project_id, round_number,
                         last_round_findings, data_dir, timeout_seconds, config, model_arg=None):
        return pgt.TiebreakerResult(
            outcome="STOP", model="fable-5", round=round_number,
            required_change="", rationale="not converging",
        )

    monkeypatch.setattr(pgt, "run_tiebreaker", _stop_tiebreaker)
    # _resolve_plan_blocker stays REAL so the resolution_reason lands in the DB.
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)

    rc = planning_cli.cmd_plan_gate_run(_gate_args(state_dir, doc, track_id="feat-stop"))
    assert rc == 0  # STOP clears the gate (exit 0, like a PASS)

    # The resolution_reason names the tiebreaker model + outcome.
    reason = _resolution_reason(state_dir, "feat-stop", "p1")
    assert "fable-5" in reason, f"resolution_reason must name the model; got {reason!r}"
    assert "STOP" in reason
    assert "tiebreaker" in reason

    # The open items exist in the open-items store — read them back (not a mock).
    oi_state_dir = pgt.open_items_state_dir_for(ledger)
    items_file = Path(oi_state_dir) / "open_items.json"
    assert items_file.is_file(), "open items file must exist after STOP"
    items = json.loads(items_file.read_text())
    open_titles = [i["title"] for i in items["items"] if i["status"] == "open"]
    assert any("rollback" in t for t in open_titles), (
        f"the last round's findings must be recorded as open items; got {open_titles}"
    )


def test_cmd_plan_gate_run_start_names_model_in_resolution_reason(tmp_path, monkeypatch):
    """On START the blocker clears with a resolution_reason that names the
    tiebreaker model + outcome (a blocker may not vanish without a reason)."""
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-start", "p1", "t", "shipped", phase="queued")
    planning_cli._seed_plan_blocker(state_dir, "feat-start", "p1")
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    ledger = _isolate_seat_ledger(monkeypatch, tmp_path)
    pgt.record_round(ledger, track_id="feat-start", project_id="p1", round_number=1, outcome="panel")
    pgt.record_round(ledger, track_id="feat-start", project_id="p1", round_number=2, outcome="panel")

    def _start_tiebreaker(doc_path, *, doc_text=None, track_id, project_id, round_number,
                          last_round_findings, data_dir, timeout_seconds, config, model_arg=None):
        return pgt.TiebreakerResult(
            outcome="START", model="fable-5", round=round_number,
            required_change="", rationale="good enough",
        )

    monkeypatch.setattr(pgt, "run_tiebreaker", _start_tiebreaker)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)

    rc = planning_cli.cmd_plan_gate_run(_gate_args(state_dir, doc, track_id="feat-start"))
    assert rc == 0
    reason = _resolution_reason(state_dir, "feat-start", "p1")
    assert "fable-5" in reason
    assert "START" in reason


def test_cmd_plan_gate_run_tiebreaker_parse_failure_stays_blocked(tmp_path, monkeypatch):
    """A tiebreaker answer that fails the strict contract (e.g. a findings list)
    is a LOUD failure: exit 1, track stays blocked, no silent decision."""
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-fail", "p1", "t", "shipped", phase="queued")
    planning_cli._seed_plan_blocker(state_dir, "feat-fail", "p1")
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    ledger = _isolate_seat_ledger(monkeypatch, tmp_path)
    pgt.record_round(ledger, track_id="feat-fail", project_id="p1", round_number=1, outcome="panel")
    pgt.record_round(ledger, track_id="feat-fail", project_id="p1", round_number=2, outcome="panel")

    # The real behavior when the model returns a findings list: run_tiebreaker
    # raises TiebreakerParseError (parse_tiebreaker rejects a findings list).
    # Make run_tiebreaker raise it directly so the cmd handler's loud-failure
    # path is exercised.
    def _raising_tiebreaker(doc_path, *, doc_text=None, track_id, project_id, round_number,
                            last_round_findings, data_dir, timeout_seconds, config, model_arg=None):
        raise pgt.TiebreakerParseError(
            "tiebreaker returned a 'findings' list — a seat returns findings; "
            "the tiebreaker returns a decision (START/STOP). Refusing to fill in a decision.",
            raw="raw model output",
        )

    monkeypatch.setattr(pgt, "run_tiebreaker", _raising_tiebreaker)
    rc = planning_cli.cmd_plan_gate_run(_gate_args(state_dir, doc, track_id="feat-fail"))
    assert rc == 1  # loud failure, track stays blocked
    # The blocker is still unresolved: no resolution_reason was written (the
    # tiebreaker never produced a decision to clear the gate with).
    assert _resolution_reason(state_dir, "feat-fail", "p1") == ""


def test_cmd_plan_gate_run_synthesized_tiebreaker_stays_blocked_no_answer_reason(
    tmp_path, monkeypatch, capsys,
):
    """A synthesized tiebreaker report (lane delivered no answer) keeps the
    track blocked and reports the NO-ANSWER reason naming the lane status, not
    "parse failure". Exercised end-to-end: the REAL run_tiebreaker runs against
    an injected synthesized dispatcher, so the marker detection is measured
    (the report is fed in and the resulting reason read out), not mocked."""
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-synth", "p1", "t", "shipped", phase="queued")
    planning_cli._seed_plan_blocker(state_dir, "feat-synth", "p1")
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    ledger = _isolate_seat_ledger(monkeypatch, tmp_path)
    pgt.record_round(ledger, track_id="feat-synth", project_id="p1", round_number=1, outcome="panel")
    pgt.record_round(ledger, track_id="feat-synth", project_id="p1", round_number=2, outcome="panel")

    def _synth_factory(data_dir, timeout_seconds):
        def _disp(provider, model_arg, instruction, dispatch_id):
            return (
                f"- Lane: tmux_interactive\n"
                f"- Status: timeout\n"
                f"- contract_status: synthesized\n"
                f"- {pgp.SYNTHESIZED_REPORT_MARKER}\n"
                f"No commit on branch; worker emitted status=timeout.\n"
            )
        return _disp

    monkeypatch.setattr(pgt, "_default_tiebreaker_dispatcher", _synth_factory)
    rc = planning_cli.cmd_plan_gate_run(_gate_args(state_dir, doc, track_id="feat-synth"))
    captured = capsys.readouterr()
    assert rc == 1  # loud failure, track stays blocked
    # The reason names the lane state, not the parser.
    assert "no answer" in captured.err
    assert "status=timeout" in captured.err
    assert "parse failure" not in captured.err
    # The blocker is unresolved: no resolution_reason written.
    assert _resolution_reason(state_dir, "feat-synth", "p1") == ""


def test_cmd_plan_gate_run_empty_completion_stays_blocked_not_pass_or_revise(
    tmp_path, monkeypatch, capsys,
):
    """An empty completion keeps the track blocked with the NO-ANSWER reason —
    NOT a PASS (exit 0) and NOT a REVISE (exit 2). End-to-end: the REAL
    run_tiebreaker runs against an injected empty dispatcher, so the
    empty-completion detection is measured, not mocked."""
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-empty", "p1", "t", "shipped", phase="queued")
    planning_cli._seed_plan_blocker(state_dir, "feat-empty", "p1")
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    ledger = _isolate_seat_ledger(monkeypatch, tmp_path)
    pgt.record_round(ledger, track_id="feat-empty", project_id="p1", round_number=1, outcome="panel")
    pgt.record_round(ledger, track_id="feat-empty", project_id="p1", round_number=2, outcome="panel")

    def _empty_factory(data_dir, timeout_seconds):
        def _disp(provider, model_arg, instruction, dispatch_id):
            return ""
        return _disp

    monkeypatch.setattr(pgt, "_default_tiebreaker_dispatcher", _empty_factory)
    rc = planning_cli.cmd_plan_gate_run(_gate_args(state_dir, doc, track_id="feat-empty"))
    captured = capsys.readouterr()
    assert rc == 1  # loud failure — not PASS (0), not REVISE (2)
    assert "no answer" in captured.err
    assert "status=empty" in captured.err
    assert "parse failure" not in captured.err
    # The blocker is unresolved: no resolution_reason written.
    assert _resolution_reason(state_dir, "feat-empty", "p1") == ""


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

def test_load_tiebreaker_config_defaults_when_absent(tmp_path):
    cfg = pgt.load_tiebreaker_config(tmp_path / "absent.yaml")
    assert cfg["max_rounds"] == pgt.DEFAULT_MAX_ROUNDS
    assert cfg["provider"] == "deepseek_harness"
    assert cfg["model"] == "deepseek-v4-pro"


def test_load_tiebreaker_config_rejects_bad_max_rounds(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("tiebreaker:\n  max_rounds: zero\n  provider: anthropic\n  model: fable-5\n")
    with pytest.raises(ValueError, match="max_rounds"):
        pgt.load_tiebreaker_config(cfg_path)


def test_load_tiebreaker_config_rejects_max_rounds_below_one(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("tiebreaker:\n  max_rounds: 0\n  provider: anthropic\n  model: fable-5\n")
    with pytest.raises(ValueError, match=">= 1"):
        pgt.load_tiebreaker_config(cfg_path)


def test_load_tiebreaker_config_reads_real_repo_file():
    """The shipped configs/plan_gate_panel.yaml carries the tiebreaker block
    with max_rounds=2 and the registry-named provider/model (OI-1219: the
    deepseek-harness lane, not the claude/tmux lane)."""
    cfg = pgt.load_tiebreaker_config()
    assert cfg["max_rounds"] == 2
    assert cfg["provider"] == "deepseek_harness"
    assert cfg["model"] == "deepseek-v4-pro"


# --------------------------------------------------------------------------
# resolution_reason helpers
# --------------------------------------------------------------------------

def test_resolution_reasons_name_model_and_outcome():
    r = pgt.TiebreakerResult(outcome="START", model="fable-5", round=3, required_change="add tests")
    start = pgt.start_open_items_reason(r)
    assert "fable-5" in start and "START" in start and "round=3" in start and "add tests" in start
    stop = pgt.stop_open_items_reason(r)
    assert "fable-5" in stop and "STOP" in stop and "round=3" in stop


def test_remaining_findings_records_real_open_items(tmp_path):
    """STOP aftermath records findings as real open items, read back from the
    store (not a mock). Dedup is idempotent: re-recording the same finding does
    not create a second item."""
    state_dir = tmp_path / "state"
    res1 = pgt.remaining_findings_to_open_items(
        track_id="trk", project_id="p1",
        findings=["gap A: missing rollback", "gap B: no edge-case test"],
        dispatch_id="did-1", state_dir=str(state_dir),
    )
    assert len(res1) == 2
    items_file = state_dir / "open_items.json"
    items = json.loads(items_file.read_text())
    assert len([i for i in items["items"] if i["status"] == "open"]) == 2

    # Re-record the SAME findings -> dedup, no new items.
    res2 = pgt.remaining_findings_to_open_items(
        track_id="trk", project_id="p1",
        findings=["gap A: missing rollback"],
        dispatch_id="did-2", state_dir=str(state_dir),
    )
    assert len(res2) == 1
    assert res2[0][1] is False  # created=False: deduplicated
    items = json.loads(items_file.read_text())
    assert len([i for i in items["items"] if i["status"] == "open"]) == 2


def test_remaining_findings_skips_acceptance_criterion_phrasing(tmp_path):
    """A finding phrased as a passing check-off (the open-items guard rejects
    those) is skipped, not aborting the whole STOP aftermath."""
    state_dir = tmp_path / "state"
    res = pgt.remaining_findings_to_open_items(
        track_id="trk", project_id="p1",
        findings=["CI green", "gap: missing rollback"],
        dispatch_id="did", state_dir=str(state_dir),
    )
    # Only the real problem statement was recorded; "CI green" was skipped.
    assert len(res) == 1
    items = json.loads((state_dir / "open_items.json").read_text())
    titles = [i["title"] for i in items["items"]]
    assert "gap: missing rollback" in titles
    assert "CI green" not in titles


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
