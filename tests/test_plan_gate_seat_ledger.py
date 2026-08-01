"""Per-seat verdict persistence for the plan-gate panel (OI-888).

``run_panel`` appends one append-only, hash-chained ``plan_gate_seat`` record
per panelist per run, so the per-seat outcome (panelist id, model, effective
verdict incl. abstain, and whether a report was returned) survives for the
effectiveness probe — previously only the single resolved ``plan_gate_pass``
record was durable. These tests drive the persist layer with a realistic panel
verdict and read the rows back (the panel itself takes an injectable dispatcher,
so no live model is needed).
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import plan_gate_panel as pgp  # noqa: E402
from ndjson_hash_chain import walk_chain  # noqa: E402


def _report(verdict_json: str) -> str:
    return f"# review\n\nsome prose\n\n```{pgp.VERDICT_FENCE}\n{verdict_json}\n```\n"


def _fake_dispatcher(verdict_by_provider):
    def _dispatch(provider, model_arg, instruction, dispatch_id):
        return _report(verdict_by_provider[provider])
    return _dispatch


def _run(tmp_path, *, verdict_by_provider, seat_ledger_path, track="feat-seats"):
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    return pgp.run_panel(
        doc,
        track_id=track,
        project_id="p1",
        dispatcher=_fake_dispatcher(verdict_by_provider),
        seat_ledger_path=seat_ledger_path,
    )


def test_run_panel_persists_one_row_per_seat(tmp_path):
    """Every scoring seat lands a hash-chained row carrying its verdict + model."""
    seat_ledger = tmp_path / "plan-gate-seats.ndjson"
    verdicts = {m["provider"]: '{"verdict": "pass"}' for m in pgp.DEFAULT_PANEL}
    out = _run(tmp_path, verdict_by_provider=verdicts, seat_ledger_path=seat_ledger)

    assert out["decision"] == "PASS"
    rows = list(walk_chain(seat_ledger))
    assert len(rows) == len(pgp.DEFAULT_PANEL)

    by_label = {m["label"]: m for m in pgp.DEFAULT_PANEL}
    for _line_no, rec, _hash in rows:
        assert rec["type"] == "plan_gate_seat"
        assert rec["track_id"] == "feat-seats"
        assert rec["project_id"] == "p1"
        member = by_label[rec["panelist_id"]]
        assert rec["model"] == member["model_arg"]
        assert rec["verdict"] == "pass"
        assert rec["responded"] is True
        assert rec["parse_error"] is False
        assert rec["run_at"]


def test_run_panel_persists_abstain_for_unresponsive_seat(tmp_path):
    """A lane that never returns a report is recorded as abstain, not dropped."""
    seat_ledger = tmp_path / "plan-gate-seats.ndjson"

    def _flaky(provider, model_arg, instruction, dispatch_id):
        if provider == "kimi":
            raise RuntimeError("kimi cli not installed")
        return _report('{"verdict": "pass"}')

    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    out = pgp.run_panel(
        doc, track_id="feat-flaky", project_id="p1",
        dispatcher=_flaky, seat_ledger_path=seat_ledger,
    )

    rows = {rec["panelist_id"]: rec for _ln, rec, _h in walk_chain(seat_ledger)}
    assert out["decision"] == "PASS"  # 2 readable voices meet quorum
    kimi = rows["kimi"]
    assert kimi["verdict"] == "abstain"
    assert kimi["responded"] is False
    assert kimi["parse_error"] is False
    opus = rows["opus"]
    assert opus["verdict"] == "pass"
    assert opus["responded"] is True


def test_run_panel_persists_abstain_for_parse_error_seat(tmp_path):
    """A report whose verdict fence does not parse is a non-scoring abstain row.

    OI-839: the raw lane output must be preserved on the parse-error record so
    a later parser hardening can be built against the REAL failure mode — the
    unparseable text used to vanish with the temporary report file.
    """
    seat_ledger = tmp_path / "plan-gate-seats.ndjson"

    def _garbled(provider, model_arg, instruction, dispatch_id):
        if provider == "glm-harness":
            return "# review\n\nno verdict fence here\n"
        return _report('{"verdict": "pass"}')

    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\n## Approach\n", encoding="utf-8")
    out = pgp.run_panel(
        doc, track_id="feat-garbled", project_id="p1",
        dispatcher=_garbled, seat_ledger_path=seat_ledger,
    )

    rows = {rec["panelist_id"]: rec for _ln, rec, _h in walk_chain(seat_ledger)}
    assert out["decision"] == "PASS"
    assert rows["glm-5.2-harness"]["verdict"] == "abstain"
    assert rows["glm-5.2-harness"]["responded"] is True
    assert rows["glm-5.2-harness"]["parse_error"] is True
    # OI-839: raw output preserved for the parse-error lane, absent for readable ones.
    assert rows["glm-5.2-harness"]["raw_output"] == "# review\n\nno verdict fence here\n"
    for label in ("codex", "deepseek", "opus", "kimi"):
        assert "raw_output" not in rows[label], (
            f"{label} passed cleanly; raw_output must only appear on parse-error records"
        )


def test_run_panel_panelist_rows_carry_model(tmp_path):
    """The panelists in the returned result expose the model they were dispatched with."""
    verdicts = {m["provider"]: '{"verdict": "pass"}' for m in pgp.DEFAULT_PANEL}
    out = _run(tmp_path, verdict_by_provider=verdicts,
               seat_ledger_path=tmp_path / "seats.ndjson")
    by_label = {p["label"]: p for p in out["panelists"]}
    for member in pgp.DEFAULT_PANEL:
        assert by_label[member["label"]]["model"] == member["model_arg"]


def test_emit_seat_records_never_raises_on_bad_path(tmp_path):
    """Best-effort contract: a path that cannot be created yields no exception."""
    bad = tmp_path / "afile"
    bad.write_text("x")
    results = [pgp.PanelistResult(label="opus", provider="claude", model="opus")]
    pgp._emit_seat_records(results, track_id="t", project_id="p1", seat_ledger_path=bad)


def test_resolve_seat_ledger_path_none_without_data_dir():
    """No data_dir -> no seat persistence unless a path is passed explicitly."""
    assert pgp._resolve_seat_ledger_path(None) is None


def test_find_repo_root_walks_to_git_marker(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert pgp._find_repo_root(nested) == tmp_path


def test_find_repo_root_handles_worktree_git_file(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /somewhere/main/.git\n", encoding="utf-8")
    nested = tmp_path / "x"
    nested.mkdir()
    assert pgp._find_repo_root(nested) == tmp_path


def test_find_repo_root_none_without_marker(tmp_path):
    assert pgp._find_repo_root(tmp_path) is None


def test_seat_ledger_constants_are_repo_relative():
    assert pgp.SEAT_LEDGER_RELPATH.endswith("plan-gate-seats.ndjson")
    assert pgp.SEAT_LEDGER_RELPATH.startswith(".vnx-attest/")


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
