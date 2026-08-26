"""glm_gate.py tests (dispatch beta-dlv1-glm-gate-nieuwbouw).

glm_gate.py is pure nieuwbouw: ``scripts/glm_gate.py`` did not exist before
this deliverable. ``gate_request_handler`` recognizing "glm_gate" as a
registered ``Gate`` member and refusing requests for it with
``reason=gate_runner_missing`` exists ONLY on a separate, not-yet-merged
branch (``dispatch/beta-dlv45-poortnamen-en-handlers``, commit ``9e6cc3f3``)
— that wiring (the ``Gate.GLM_GATE`` enum member, the
``gate_request_handler`` dispatch branch, the
``closure_verifier._GATE_HANDLERS`` entry) is NOT present on this worktree's
base and is out of this deliverable's scope
(``gate_request_handler.py``/``dispatch_spec.py``/``closure_verifier.py`` are
off-limits here — changed by other, already-merged-or-pending deliverables).
Requesting "glm_gate" through today's unmodified ``_dispatch_one_review``
therefore still falls through to the generic ``unknown_review_gate`` branch
regardless of whether this file exists, so that surface cannot supply the
before/after behavioral contrast the dispatch instruction describes.

The CLI entrypoint every other gate script exposes (``python3 scripts/
kimi_gate.py --pr N ...``) is the real substitute: it needs no wiring from
another deliverable, and its ``--help`` invocation genuinely differs before
vs. after this file exists (a real interpreter-level "file not found" vs. a
real usage dump) — see ``test_glm_gate_cli_surface_did_not_exist_before_this_deliverable``
below.

Every other test that needs the ``glm_gate`` module imports it LAZILY via the
``glm_gate`` fixture, not at module scope — a bare top-level ``import
glm_gate`` would fail collection for every test in this file with one
``ModuleNotFoundError``, masking the distinction between "fails because a
symbol is missing" (expected for these, before this file existed) and "fails
on a real returned value" (the CLI-surface test, which needs no glm_gate
import at all).

``gate_status``/``gate_artifacts``/``closure_verifier`` are pre-existing,
unmodified modules — importing them at module scope does not gate collection
on ``glm_gate.py``'s existence.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier
import gate_artifacts
from gate_status import has_complete_evidence, is_terminal


@pytest.fixture
def glm_gate():
    """Lazy import — fails per-test (at call time), not at module collection."""
    import glm_gate as _glm_gate
    return _glm_gate


_REAL_PASS_REPORT = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

_REAL_FAIL_REPORT = (
    "Reviewed the diff; found a real problem.\n\n"
    "```json\n"
    '{"verdict": "fail", "findings": [{"severity": "error", "message": "sql injection"}],'
    ' "residual_risk": "unsanitized input"}\n'
    "```\n"
)

_FAKE_DIFF = "diff --git a/x b/x\n+ok\n"


def _write_unified_report(data_dir: Path, dispatch_id: str, text: str) -> Path:
    """Mimic ``governance_emit.emit_unified_report``'s write path — the real
    governed dispatch always writes the report before the dispatcher call
    returns the text; glm_gate.py never writes this file itself, only reads
    it back."""
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{dispatch_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_dispatcher_factory(data_dir: Path, text: str):
    """Build a ``_make_default_dispatcher``-shaped double that writes the
    report to disk exactly like the real governed lane, then returns it."""
    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            _write_unified_report(data_dir, dispatch_id, text)
            return text
        return _dispatch
    return _make


def _run_glm_gate_for_real_pr(glm_gate, tmp_path, monkeypatch, report_text, *, pr="4242"):
    """Run glm_gate.main() against a non-offline PR: no --diff-file (so
    test_run stays False), a stubbed diff source (no network), a dispatcher
    double that writes the unified report like the real lane does, and
    stubbed gh-identity lookups (no real `gh` subprocess calls)."""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(glm_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher", _fake_dispatcher_factory(data_dir, report_text),
    )
    monkeypatch.setattr(glm_gate, "get_pr_head_branch", lambda pr_number: "feature/dlv1-test")
    monkeypatch.setattr(glm_gate, "get_pr_head_sha", lambda pr_number: "cafedeadbeef")

    rc = glm_gate.main(["--pr", pr, "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-{pr}-glm_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    return rc, record, data_dir


def _run_gate_offline(glm_gate, tmp_path, monkeypatch, dispatcher, *, pr="0"):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setattr(glm_gate, "_make_default_dispatcher", lambda *a, **k: dispatcher)
    rc = glm_gate.main(["--pr", pr, "--diff-file", str(diff_file), "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-{pr}-glm_gate.json"
    record = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return rc, record


# ---------------------------------------------------------------------------
# 0. STRONG red: a real call against the EXISTING, unmodified CLI-script
#    surface every gate runner uses (kimi_gate.py's own entrypoint
#    convention). Needs no glm_gate import, so it collects and runs today —
#    and it genuinely fails on a returned value, not an import error: python3
#    itself refuses to start with a real "can't open file ... No such file or
#    directory" and a nonzero exit code before this deliverable ships.
# ---------------------------------------------------------------------------


def test_glm_gate_cli_surface_did_not_exist_before_this_deliverable():
    """Runs `python3 scripts/glm_gate.py --help` exactly as an operator or CI
    step would invoke any gate script. Before this deliverable,
    scripts/glm_gate.py does not exist: python3 exits non-zero with a
    literal, non-pytest error on stderr and prints nothing usable on stdout.
    That is the real value this test's first assertion is checked against —
    it is EXPECTED to fail on unmodified main. Once scripts/glm_gate.py
    exists, the identical command succeeds (rc=0) and prints real usage
    text — a different real value, not a resolved import error.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "glm_gate.py"), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        "glm_gate.py --help must succeed once the CLI script exists on disk "
        f"(rc={result.returncode}, stderr={result.stderr!r})"
    )
    assert "--pr" in result.stdout
    assert "--diff-file" in result.stdout


# ---------------------------------------------------------------------------
# 1. A real verdict carries complete evidence
# ---------------------------------------------------------------------------


def test_pass_verdict_carries_complete_evidence(glm_gate, tmp_path, monkeypatch):
    rc, record, _data_dir = _run_glm_gate_for_real_pr(glm_gate, tmp_path, monkeypatch, _REAL_PASS_REPORT)

    assert rc == 0
    assert record["status"] == "pass"
    assert record["gate"] == "glm_gate"
    assert record["provider"] == "glm-harness"
    assert record["model"] == "glm-5.2"
    assert record["contract_hash"], "contract_hash must be non-empty on a terminal verdict"
    assert record["report_path"], "report_path must be non-empty on a terminal verdict"
    assert Path(record["report_path"]).is_file(), (
        "report_path must point at a file that already exists on disk when the record is written"
    )
    # The real function, not a reimplementation of its logic.
    assert has_complete_evidence(record) is True


# ---------------------------------------------------------------------------
# 2. That record clears the merge door's evidence check
# ---------------------------------------------------------------------------


def test_pass_verdict_clears_closure_verifier_merge_check(glm_gate, tmp_path, monkeypatch):
    rc, record, data_dir = _run_glm_gate_for_real_pr(
        glm_gate, tmp_path, monkeypatch, _REAL_PASS_REPORT, pr="4242",
    )
    assert rc == 0

    results_dir = data_dir / "state" / "review_gates" / "results"
    gate = closure_verifier.check_review_gate_for_merge("4242", "glm_gate", results_dir)

    assert gate["verdict"] == "GO", gate["message"]
    assert "mist contract_hash" not in gate["message"]


# ---------------------------------------------------------------------------
# 3. unavailable never carries complete evidence, and is never terminal
#    (OI-1435: has_complete_evidence only checks non-emptiness, not whether a
#    verdict exists — the separation holds because is_terminal() is checked
#    BEFORE contract_hash/report_path are ever consulted, and is_terminal is
#    False on an unavailable record regardless of what those two fields
#    contain. OI-1477: report_path is now populated even here — see below —
#    contract_hash staying empty was always the field that mattered.)
# ---------------------------------------------------------------------------


def test_unavailable_result_never_has_complete_evidence_and_is_not_terminal(glm_gate, tmp_path, monkeypatch):
    # A provider outage: empty report, no readable verdict, no exception.
    rc, record = _run_gate_offline(glm_gate, tmp_path, monkeypatch, lambda *a, **k: "")

    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["contract_hash"] == "", "contract_hash is the field that must stay empty on an outage"
    # OI-1477: report_path is populated on an unavailable result too, so a
    # takeover-time reader can find the failure text without re-deriving the
    # path itself — it points at the same place report_path_informational
    # always has (the file may not exist here since this test's fake
    # dispatcher never writes one, but the path itself is real).
    assert record["report_path"] == record["report_path_informational"]
    assert is_terminal(record) is False, "an outage is retryable, not a decided pass/fail outcome"
    assert has_complete_evidence(record) is False, "an outage must never look like complete evidence"


def test_unavailable_from_dispatcher_exception_still_writes_a_record(glm_gate, tmp_path, monkeypatch):
    """A quota/auth outage surfacing as a raised exception (the lane died
    before any report came back at all) must still leave an unavailable
    record, not vanish — and must never be booked as a review fail."""
    def _boom(*a, **k):
        raise RuntimeError("glm-harness dispatch exploded: 403 access_terminated_error")

    rc, record = _run_gate_offline(glm_gate, tmp_path, monkeypatch, _boom)
    assert rc == 1
    assert record is not None, "an outage must leave an unavailable record, not vanish"
    assert record["status"] == "unavailable"
    assert record["status"] != "fail"
    assert record["reason"] == "dispatch_error"
    assert "access_terminated_error" in record["residual_risk"]
    assert is_terminal(record) is False
    assert has_complete_evidence(record) is False


# ---------------------------------------------------------------------------
# 4. Content without a readable verdict -> unavailable, never fail
# ---------------------------------------------------------------------------


def test_prose_report_without_verdict_is_unavailable_not_fail(glm_gate, tmp_path, monkeypatch):
    prose_report = "## Review notes\n\nLooks fine, no structured verdict emitted.\n"
    rc, record = _run_gate_offline(glm_gate, tmp_path, monkeypatch, lambda *a, **k: prose_report)

    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["status"] != "fail"
    assert "UNAVAILABLE" in record["summary"]


# ---------------------------------------------------------------------------
# 5. Offline (--diff-file) runs never count as evidence for a real PR, even
#    though the record now carries complete evidence fields.
# ---------------------------------------------------------------------------


def test_offline_run_is_test_run_and_rejected_by_merge_check_despite_complete_evidence(
    glm_gate, tmp_path, monkeypatch,
):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher", _fake_dispatcher_factory(data_dir, _REAL_PASS_REPORT),
    )

    rc = glm_gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / "pr-0-glm_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 0
    assert record["status"] == "pass"
    assert record["test_run"] is True
    assert has_complete_evidence(record) is True

    results_dir = data_dir / "state" / "review_gates" / "results"
    gate = closure_verifier.check_review_gate_for_merge("0", "glm_gate", results_dir)
    assert gate["verdict"] == "NO-GO"
    assert "geen review-gate resultaat" in gate["message"]


# ---------------------------------------------------------------------------
# 6. contract_hash: the SAME canonical hasher as every other gate, byte-equal
#    for the same contract, and DIFFERENT for a different one — an
#    always-the-same hasher would also clear a same-contract-only check.
# ---------------------------------------------------------------------------


def test_glm_gate_reuses_the_canonical_hasher_not_a_second_implementation(glm_gate):
    """Identity check, not behavioral: proves there is exactly ONE hashing
    implementation in the codebase, not two that happen to agree today and
    can silently diverge tomorrow."""
    assert glm_gate._compute_contract_hash is gate_artifacts._compute_contract_hash


def test_contract_hash_byte_equal_for_same_contract_and_different_for_another(glm_gate, tmp_path, monkeypatch):
    pr = "777"
    rc, record, _data_dir = _run_glm_gate_for_real_pr(glm_gate, tmp_path, monkeypatch, _REAL_PASS_REPORT, pr=pr)
    assert rc == 0

    # Reconstruct the exact prompt glm_gate built for this run, then hash it
    # via the SAME function the existing (codex_gate) route calls — gate name
    # only affects the fallback branch (no "prompt" key), so a different gate
    # name here still proves it is the same hash for the same contract.
    prompt = glm_gate._build_prompt(_FAKE_DIFF, pr)
    existing_route_hash = gate_artifacts._compute_contract_hash({"prompt": prompt}, "codex_gate")
    assert record["contract_hash"] != ""
    assert record["contract_hash"] == existing_route_hash

    # A DIFFERENT contract must hash differently — a hasher that always
    # returns the same value would also satisfy the equality assertion
    # above, proving nothing on its own.
    different_prompt = glm_gate._build_prompt("diff --git a/y b/y\n+different\n", pr)
    different_hash = gate_artifacts._compute_contract_hash({"prompt": different_prompt}, "codex_gate")
    assert different_hash != existing_route_hash


# ---------------------------------------------------------------------------
# 7. GLM model allowlist: a blocked version is refused LOUDLY, never
#    silently accepted, and never even reaches the dispatcher.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked_model", ["glm-4.5", "glm-4.6", "glm-5", "glm-5.1", "GLM-5.1", "glm-5.3"],
)
def test_blocked_glm_model_is_refused_loudly_not_silently_accepted(
    glm_gate, tmp_path, monkeypatch, capsys, blocked_model,
):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    data_dir = tmp_path / "data"

    dispatched = []

    def _spy_dispatcher_factory(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            dispatched.append((provider, model_arg))
            return _REAL_PASS_REPORT
        return _dispatch

    monkeypatch.setattr(glm_gate, "_make_default_dispatcher", _spy_dispatcher_factory)

    rc = glm_gate.main([
        "--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir),
        "--model", blocked_model,
    ])
    captured = capsys.readouterr()

    assert rc == 1, "a blocked model must be an infra-level refusal, never exit 0 or exit 2"
    assert dispatched == [], "a blocked model must never reach the dispatcher"
    assert "REFUSING" in captured.err
    assert blocked_model in captured.err
    out = data_dir / "state" / "review_gates" / "results" / "pr-0-glm_gate.json"
    assert not out.exists(), "a blocked-model refusal must not fabricate a review-gate result record"


def test_default_and_case_insensitive_glm_5_2_is_accepted(glm_gate, tmp_path, monkeypatch):
    dispatched = []

    def _spy_dispatcher_factory(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            dispatched.append((provider, model_arg))
            return _REAL_PASS_REPORT
        return _dispatch

    monkeypatch.setattr(glm_gate, "_make_default_dispatcher", _spy_dispatcher_factory)
    monkeypatch.delenv("VNX_GLM_GATE_MODEL", raising=False)
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    data_dir = tmp_path / "data"

    # No --model: the default must resolve to the canonical glm-5.2.
    rc = glm_gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir)])
    assert rc == 0
    assert dispatched == [("glm-harness", "glm-5.2")]

    # Case-insensitive input still resolves to the canonical lowercase model.
    dispatched.clear()
    rc = glm_gate.main([
        "--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir), "--model", "GLM-5.2",
    ])
    assert rc == 0
    assert dispatched == [("glm-harness", "glm-5.2")]


# ---------------------------------------------------------------------------
# 8. The gate must never write to the working tree it is reviewing.
# ---------------------------------------------------------------------------


def test_run_does_not_touch_the_working_tree(glm_gate, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        glm_gate, "_make_default_dispatcher", _fake_dispatcher_factory(data_dir, _REAL_PASS_REPORT),
    )
    monkeypatch.chdir(repo)

    before = subprocess.run(
        ["git", "status", "--short"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout

    rc = glm_gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir)])

    after = subprocess.run(
        ["git", "status", "--short"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout

    assert rc == 0
    assert before == after == "", "glm_gate must never modify the working tree it is reviewing"


# ---------------------------------------------------------------------------
# 9. Real fail verdict + gate_status sanity (unchanged behaviour, ported
#    from kimi_gate's own coverage so glm_gate's happy/sad paths both hold).
# ---------------------------------------------------------------------------


def test_real_fail_verdict_still_fails_with_exit_2(glm_gate, tmp_path, monkeypatch):
    rc, record = _run_gate_offline(glm_gate, tmp_path, monkeypatch, lambda *a, **k: _REAL_FAIL_REPORT)
    assert rc == 2
    assert record["status"] == "fail"
    assert record["reason"] == "verdict"
    assert len(record["blocking_findings"]) == 1
