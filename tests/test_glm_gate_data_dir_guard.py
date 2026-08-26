"""glm_gate.py write-without---data-dir tests (dispatch beta2-h-poort-schrijft-stil-niets).

Measured 23-08, live, on PR #1672: ``python3 scripts/glm_gate.py --pr 1672
--json`` ran the full governed review (six minutes, a real 2476-char report on
disk), but NOTHING landed in ``state/review_gates/results/`` — the merge door,
closure verifier, and obligation runner could not see the run at all.

Root cause: ``glm_gate.py``/``kimi_gate.py`` both resolve a correct,
always-populated ``base_data_dir`` via ``_resolve_data_dir(data_dir)``, but the
write guard checked the RAW ``data_dir`` (``args.data_dir or None``) instead —
so an invocation with no explicit ``--data-dir`` and no ``VNX_DATA_DIR`` in the
environment skipped the entire write block, silently, on exit 0, after a fully
billed dispatch. The report path (built from ``base_data_dir``) was correct
all along; only the result-record write guarded on the wrong variable.

These tests never touch the real central store: ``_resolve_data_dir`` is
monkeypatched on the glm_gate module namespace (not on plan_gate_panel, its
source module — ``from X import Y`` binds at import time) to return a
tmp_path-rooted directory regardless of what raw ``data_dir`` the CLI parsed,
so the "no --data-dir, no VNX_DATA_DIR" scenario can be reproduced hermetically
without ever falling through to ``~/.vnx-data/<project_id>``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import glm_gate
import gate_artifacts
from gate_status import has_complete_evidence, is_terminal

_REAL_PASS_REPORT = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

_FAKE_DIFF = "diff --git a/x b/x\n+ok\n"


def _write_unified_report(data_dir: Path, dispatch_id: str, text: str) -> Path:
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{dispatch_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_dispatcher_factory(data_dir: Path, text: str, *, seen: "list | None" = None, requests_dir: "Path | None" = None, pr: str = ""):
    """Same shape as the other gate test files' dispatcher double, plus an
    optional ``seen`` list that records whether the request record already
    existed on disk at dispatch time — used to prove ordering (defect 4)."""
    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            if seen is not None and requests_dir is not None:
                seen.append((requests_dir / f"pr-{pr}-glm_gate.json").exists())
            _write_unified_report(data_dir, dispatch_id, text)
            return text
        return _dispatch
    return _make


def _force_resolved_base(monkeypatch, base_data_dir: Path) -> None:
    """Force glm_gate's resolved base to a tmp_path-rooted dir, independent of
    whatever raw ``data_dir`` argparse produced — this is what
    ``_resolve_data_dir(None)`` does for real (falls through to the central
    store for the active project); redirecting it here keeps the test
    hermetic while reproducing the exact "guard checks the wrong variable"
    defect."""
    monkeypatch.setattr(glm_gate, "_resolve_data_dir", lambda _data_dir: base_data_dir)


# ---------------------------------------------------------------------------
# Defect 1 (RED on unfixed main): omitting --data-dir must not skip the write.
# ---------------------------------------------------------------------------


def test_no_data_dir_flag_and_no_env_still_writes_result_record(tmp_path, monkeypatch):
    """The reproduction from the dispatch: no --data-dir, VNX_DATA_DIR absent
    from the environment entirely (not just empty — genuinely unset), a real
    (non-offline) PR run. On unfixed main this assertion fails on the MEASURED
    filesystem state (the file does not exist), not on a missing symbol."""
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

    base_data_dir = tmp_path / "central-store"
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(glm_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, _REAL_PASS_REPORT),
    )
    monkeypatch.setattr(glm_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(glm_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    rc = glm_gate.main(["--pr", "9001"])  # NO --data-dir

    out = base_data_dir / "state" / "review_gates" / "results" / "pr-9001-glm_gate.json"
    # Split asserts, as the dispatch requires: existence, then readability,
    # then field correctness — each is its own measured fact.
    assert out.exists(), "glm_gate must write its result record even without --data-dir"
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["gate"] == "glm_gate"
    assert record["pr_id"] == "9001"
    assert rc == 0


# ---------------------------------------------------------------------------
# Control case 1: WITH --data-dir, behaviour is unchanged — lands exactly at
# that path.
# ---------------------------------------------------------------------------


def test_with_explicit_data_dir_flag_lands_at_exactly_that_path(tmp_path, monkeypatch):
    data_dir = tmp_path / "explicit"
    monkeypatch.setattr(glm_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher", _fake_dispatcher_factory(data_dir, _REAL_PASS_REPORT),
    )
    monkeypatch.setattr(glm_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(glm_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    rc = glm_gate.main(["--pr", "9002", "--data-dir", str(data_dir)])

    out = data_dir / "state" / "review_gates" / "results" / "pr-9002-glm_gate.json"
    assert rc == 0
    assert out.exists()
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "pass"


# ---------------------------------------------------------------------------
# Control case 2: an unavailable record still keeps contract_hash empty (the
# OI-1435 separation is untouched by this fix). OI-1477: report_path is now
# populated even here — it always pointed at the same real report location
# report_path_informational does, so the two are the same value below.
# ---------------------------------------------------------------------------


def test_unavailable_still_keeps_contract_hash_empty(tmp_path, monkeypatch):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(glm_gate, "_make_default_dispatcher", lambda *a, **k: (lambda *a2, **k2: ""))

    rc = glm_gate.main(["--pr", "0", "--diff-file", str(diff_file)])  # NO --data-dir

    out = base_data_dir / "state" / "review_gates" / "results" / "pr-0-glm_gate.json"
    assert out.exists()
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "unavailable"
    assert record["contract_hash"] == ""
    assert record["report_path"] == record["report_path_informational"], (
        "OI-1477: report_path is populated on an unavailable result too"
    )
    assert has_complete_evidence(record) is False
    assert is_terminal(record) is False
    assert rc == 1


# ---------------------------------------------------------------------------
# Control case 3: report path and result path resolve under the same store.
# ---------------------------------------------------------------------------


def test_report_path_and_result_path_share_the_same_resolved_base(tmp_path, monkeypatch):
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(glm_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, _REAL_PASS_REPORT),
    )
    monkeypatch.setattr(glm_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(glm_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    rc = glm_gate.main(["--pr", "9003"])  # NO --data-dir

    out = base_data_dir / "state" / "review_gates" / "results" / "pr-9003-glm_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 0
    report_path = Path(record["report_path"])
    assert report_path.is_file()
    assert str(base_data_dir) in str(report_path)
    assert str(base_data_dir) in str(out)


# ---------------------------------------------------------------------------
# Defect 3: an informational report-path field points a human at the real
# report even on ``unavailable``, without ever counting as evidence.
#
# OI-1477: the ORIGINAL bug this test documented (report_path stayed empty
# while a full report existed on disk with report_path_informational as the
# only field pointing at it) is now fixed at the source — glm_gate.py stamps
# report_path with the same value on the failure path too, so
# gate_request_handler's takeover-time report-fallback scan can find it
# directly off the result record. This test still proves report_path is
# never treated AS EVIDENCE on an unavailable result (has_complete_evidence
# gates on is_terminal() first, unaffected by which fields are populated).
# ---------------------------------------------------------------------------


def test_unavailable_report_path_points_at_the_real_report_but_is_not_evidence(tmp_path, monkeypatch):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    prose_report = "## Review notes\n\nLooks fine, no structured verdict emitted.\n"
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, prose_report),
    )

    rc = glm_gate.main(["--pr", "0", "--diff-file", str(diff_file)])  # NO --data-dir

    out = base_data_dir / "state" / "review_gates" / "results" / "pr-0-glm_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 1
    assert record["status"] == "unavailable"
    # OI-1477: report_path now points at the real, on-disk report (same
    # value as report_path_informational) instead of staying empty.
    assert record["report_path"] == record["report_path_informational"]
    assert record["report_path"], "informational field must point at the real report"
    assert Path(record["report_path"]).is_file()
    # Never counted as evidence — has_complete_evidence gates on is_terminal()
    # BEFORE it ever looks at contract_hash/report_path, and "unavailable" is
    # never terminal, regardless of which of those two fields are populated.
    assert has_complete_evidence(record) is False
    assert is_terminal(record) is False


# ---------------------------------------------------------------------------
# Defect 4: a request record must exist BEFORE the dispatch is ever attempted.
# ---------------------------------------------------------------------------


def test_request_record_exists_before_dispatch_is_attempted(tmp_path, monkeypatch):
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(glm_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(glm_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(glm_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    requests_dir = base_data_dir / "state" / "review_gates" / "requests"
    seen_at_dispatch_time = []
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher",
        _fake_dispatcher_factory(
            base_data_dir, _REAL_PASS_REPORT,
            seen=seen_at_dispatch_time, requests_dir=requests_dir, pr="9004",
        ),
    )

    rc = glm_gate.main(["--pr", "9004"])  # NO --data-dir

    assert rc == 0
    assert seen_at_dispatch_time == [True], (
        "the request record must already be on disk by the time the dispatcher is invoked"
    )
    req_out = requests_dir / "pr-9004-glm_gate.json"
    assert req_out.exists()
    request_record = json.loads(req_out.read_text(encoding="utf-8"))
    assert request_record["gate"] == "glm_gate"
    assert request_record["pr_number"] == 9004
    assert request_record["dispatch_id"]
    assert request_record["branch"] == "feature/x"
    assert request_record["commit_sha"] == "deadbeef"


def test_request_record_written_even_when_dispatch_raises(tmp_path, monkeypatch):
    """A request record proves "this was requested" independent of whether
    the dispatch itself later blows up — it must not vanish on failure."""
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(glm_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(glm_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(glm_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    def _boom(*a, **k):
        raise RuntimeError("glm-harness dispatch exploded")

    monkeypatch.setattr(glm_gate, "_make_default_dispatcher", lambda *a, **k: _boom)

    rc = glm_gate.main(["--pr", "9005"])  # NO --data-dir

    assert rc == 1
    req_out = base_data_dir / "state" / "review_gates" / "requests" / "pr-9005-glm_gate.json"
    assert req_out.exists()


def test_request_record_field_shape_matches_ci_gate_convention(tmp_path, monkeypatch):
    """A generic reader must see no difference between this request record
    and an existing ci_gate/codex_gate one: same core identity fields
    (gate/status/provider/branch/pr_number/commit_sha/report_path/
    requested_at/dispatch_id), all populated."""
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(glm_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(glm_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(glm_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, _REAL_PASS_REPORT),
    )

    rc = glm_gate.main(["--pr", "9006"])  # NO --data-dir
    assert rc == 0

    req_out = base_data_dir / "state" / "review_gates" / "requests" / "pr-9006-glm_gate.json"
    request_record = json.loads(req_out.read_text(encoding="utf-8"))

    # Same core identity field set an existing ci_gate/codex_gate request
    # record carries (gate_request_handler._request_ci_gate/_request_codex),
    # each non-empty/non-null for a live (non-offline) run.
    existing_route_fields = {
        "gate": "ci_gate", "status": "requested", "provider": "gh_cli",
        "branch": "main", "pr_number": 42, "commit_sha": "abc123",
        "report_path": "/tmp/x.md", "requested_at": "2026-08-23T00:00:00Z",
        "dispatch_id": "ci-gate-pr42-1",
    }
    for field in existing_route_fields:
        assert field in request_record, f"missing field {field!r} present on ci_gate's own request shape"
        value = request_record[field]
        assert value not in (None, ""), f"field {field!r} must be populated, got {value!r}"


# ---------------------------------------------------------------------------
# Loud-failure requirement: a write failure must not exit 0.
# ---------------------------------------------------------------------------


def test_result_write_failure_exits_nonzero_with_explicit_stderr_reason(tmp_path, monkeypatch, capsys):
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(glm_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, _REAL_PASS_REPORT),
    )
    monkeypatch.setattr(glm_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(glm_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    def _raise_disk_full(**_kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(glm_gate, "record_terminal_result", _raise_disk_full)

    rc = glm_gate.main(["--pr", "9007"])  # NO --data-dir
    captured = capsys.readouterr()

    assert rc == 1, "a poort that cannot persist its own evidence must never exit 0"
    assert "FAILED" in captured.err
    assert "disk full" in captured.err


def test_request_write_failure_exits_nonzero_before_any_dispatch(tmp_path, monkeypatch, capsys):
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(glm_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(glm_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(glm_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    dispatched = []

    def _spy_dispatcher_factory(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            dispatched.append(True)
            return _REAL_PASS_REPORT
        return _dispatch

    monkeypatch.setattr(glm_gate, "_make_default_dispatcher", _spy_dispatcher_factory)

    def _raise_disk_full(*_a, **_k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(glm_gate, "persist_request", _raise_disk_full)

    rc = glm_gate.main(["--pr", "9008"])  # NO --data-dir
    captured = capsys.readouterr()

    assert rc == 1
    assert "FAILED" in captured.err
    assert dispatched == [], "a request-record write failure must not still burn a dispatch"


# ---------------------------------------------------------------------------
# Identity check: never a second hasher, sanity re-check after the refactor.
# ---------------------------------------------------------------------------


def test_glm_gate_still_reuses_the_canonical_hasher():
    assert glm_gate._compute_contract_hash is gate_artifacts._compute_contract_hash
