"""kimi_gate.py write-without---data-dir tests (dispatch beta2-h-poort-schrijft-stil-niets).

Same defect, same fix, as ``test_glm_gate_data_dir_guard.py`` — see that file's
module docstring for the full measured reproduction (PR #1672, 23-08).
kimi_gate.py carried the identical bug: the write guard checked the raw
``data_dir`` CLI value instead of the resolved ``base_data_dir``, so an
invocation with no explicit ``--data-dir`` and no ``VNX_DATA_DIR`` in the
environment silently skipped the entire result-record write after a fully
billed dispatch.

``_resolve_data_dir`` is monkeypatched on the kimi_gate module namespace (not
on plan_gate_panel, its source module) to keep these tests hermetic — they
never fall through to the real ``~/.vnx-data/<project_id>`` central store.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import kimi_gate
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
    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            if seen is not None and requests_dir is not None:
                seen.append((requests_dir / f"pr-{pr}-kimi_gate.json").exists())
            _write_unified_report(data_dir, dispatch_id, text)
            return text
        return _dispatch
    return _make


def _force_resolved_base(monkeypatch, base_data_dir: Path) -> None:
    monkeypatch.setattr(kimi_gate, "_resolve_data_dir", lambda _data_dir: base_data_dir)


# ---------------------------------------------------------------------------
# Defect 1 (RED on unfixed main): omitting --data-dir must not skip the write.
# ---------------------------------------------------------------------------


def test_no_data_dir_flag_and_no_env_still_writes_result_record(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

    base_data_dir = tmp_path / "central-store"
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        kimi_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, _REAL_PASS_REPORT),
    )
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    rc = kimi_gate.main(["--pr", "9001"])  # NO --data-dir

    out = base_data_dir / "state" / "review_gates" / "results" / "pr-9001-kimi_gate.json"
    assert out.exists(), "kimi_gate must write its result record even without --data-dir"
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["gate"] == "kimi_gate"
    assert record["pr_id"] == "9001"
    assert rc == 0


# ---------------------------------------------------------------------------
# Control case 1: WITH --data-dir, behaviour is unchanged.
# ---------------------------------------------------------------------------


def test_with_explicit_data_dir_flag_lands_at_exactly_that_path(tmp_path, monkeypatch):
    data_dir = tmp_path / "explicit"
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        kimi_gate, "_make_default_dispatcher", _fake_dispatcher_factory(data_dir, _REAL_PASS_REPORT),
    )
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    rc = kimi_gate.main(["--pr", "9002", "--data-dir", str(data_dir)])

    out = data_dir / "state" / "review_gates" / "results" / "pr-9002-kimi_gate.json"
    assert rc == 0
    assert out.exists()
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "pass"


# ---------------------------------------------------------------------------
# Control case 2: an unavailable record still keeps empty contract_hash and
# report_path.
# ---------------------------------------------------------------------------


def test_unavailable_still_keeps_evidence_fields_empty(tmp_path, monkeypatch):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", lambda *a, **k: (lambda *a2, **k2: ""))

    rc = kimi_gate.main(["--pr", "0", "--diff-file", str(diff_file)])  # NO --data-dir

    out = base_data_dir / "state" / "review_gates" / "results" / "pr-0-kimi_gate.json"
    assert out.exists()
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "unavailable"
    assert record["contract_hash"] == ""
    assert record["report_path"] == ""
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
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        kimi_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, _REAL_PASS_REPORT),
    )
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    rc = kimi_gate.main(["--pr", "9003"])  # NO --data-dir

    out = base_data_dir / "state" / "review_gates" / "results" / "pr-9003-kimi_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 0
    report_path = Path(record["report_path"])
    assert report_path.is_file()
    assert str(base_data_dir) in str(report_path)
    assert str(base_data_dir) in str(out)


# ---------------------------------------------------------------------------
# Defect 3: an informational report-path field points a human at the real
# report even on ``unavailable``, without ever counting as evidence.
# ---------------------------------------------------------------------------


def test_unavailable_carries_informational_report_path_but_not_as_evidence(tmp_path, monkeypatch):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    prose_report = "## Review notes\n\nLooks fine, no structured verdict emitted.\n"
    monkeypatch.setattr(
        kimi_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, prose_report),
    )

    rc = kimi_gate.main(["--pr", "0", "--diff-file", str(diff_file)])  # NO --data-dir

    out = base_data_dir / "state" / "review_gates" / "results" / "pr-0-kimi_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["report_path"] == ""
    assert record["report_path_informational"], "informational field must point at the real report"
    assert Path(record["report_path_informational"]).is_file()
    assert has_complete_evidence(record) is False
    assert is_terminal(record) is False


# ---------------------------------------------------------------------------
# Defect 4: a request record must exist BEFORE the dispatch is ever attempted.
# ---------------------------------------------------------------------------


def test_request_record_exists_before_dispatch_is_attempted(tmp_path, monkeypatch):
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    requests_dir = base_data_dir / "state" / "review_gates" / "requests"
    seen_at_dispatch_time = []
    monkeypatch.setattr(
        kimi_gate, "_make_default_dispatcher",
        _fake_dispatcher_factory(
            base_data_dir, _REAL_PASS_REPORT,
            seen=seen_at_dispatch_time, requests_dir=requests_dir, pr="9004",
        ),
    )

    rc = kimi_gate.main(["--pr", "9004"])  # NO --data-dir

    assert rc == 0
    assert seen_at_dispatch_time == [True], (
        "the request record must already be on disk by the time the dispatcher is invoked"
    )
    req_out = requests_dir / "pr-9004-kimi_gate.json"
    assert req_out.exists()
    request_record = json.loads(req_out.read_text(encoding="utf-8"))
    assert request_record["gate"] == "kimi_gate"
    assert request_record["pr_number"] == 9004
    assert request_record["dispatch_id"]
    assert request_record["branch"] == "feature/x"
    assert request_record["commit_sha"] == "deadbeef"


def test_request_record_written_even_when_dispatch_raises(tmp_path, monkeypatch):
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    def _boom(*a, **k):
        raise RuntimeError("kimi dispatch exploded")

    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", lambda *a, **k: _boom)

    rc = kimi_gate.main(["--pr", "9005"])  # NO --data-dir

    assert rc == 1
    req_out = base_data_dir / "state" / "review_gates" / "requests" / "pr-9005-kimi_gate.json"
    assert req_out.exists()


def test_request_record_field_shape_matches_ci_gate_convention(tmp_path, monkeypatch):
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")
    monkeypatch.setattr(
        kimi_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, _REAL_PASS_REPORT),
    )

    rc = kimi_gate.main(["--pr", "9006"])  # NO --data-dir
    assert rc == 0

    req_out = base_data_dir / "state" / "review_gates" / "requests" / "pr-9006-kimi_gate.json"
    request_record = json.loads(req_out.read_text(encoding="utf-8"))

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
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        kimi_gate, "_make_default_dispatcher", _fake_dispatcher_factory(base_data_dir, _REAL_PASS_REPORT),
    )
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    def _raise_disk_full(**_kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(kimi_gate, "record_terminal_result", _raise_disk_full)

    rc = kimi_gate.main(["--pr", "9007"])  # NO --data-dir
    captured = capsys.readouterr()

    assert rc == 1, "a poort that cannot persist its own evidence must never exit 0"
    assert "FAILED" in captured.err
    assert "disk full" in captured.err


def test_request_write_failure_exits_nonzero_before_any_dispatch(tmp_path, monkeypatch, capsys):
    base_data_dir = tmp_path / "central-store"
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    _force_resolved_base(monkeypatch, base_data_dir)
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: "deadbeef")

    dispatched = []

    def _spy_dispatcher_factory(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            dispatched.append(True)
            return _REAL_PASS_REPORT
        return _dispatch

    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", _spy_dispatcher_factory)

    def _raise_disk_full(*_a, **_k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(kimi_gate, "persist_request", _raise_disk_full)

    rc = kimi_gate.main(["--pr", "9008"])  # NO --data-dir
    captured = capsys.readouterr()

    assert rc == 1
    assert "FAILED" in captured.err
    assert dispatched == [], "a request-record write failure must not still burn a dispatch"


# ---------------------------------------------------------------------------
# Identity check: never a second hasher, sanity re-check after the refactor.
# ---------------------------------------------------------------------------


def test_kimi_gate_still_reuses_the_canonical_hasher():
    assert kimi_gate._compute_contract_hash is gate_artifacts._compute_contract_hash
