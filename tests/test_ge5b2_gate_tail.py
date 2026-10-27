"""GE-5b-2: phases 3-6 of glm_gate and kimi_gate are ONE body in ``governed_lane``.

The goldens under ``tests/fixtures/governed_lane/records/`` were produced by the UNMOVED gates on
the branch base, before any code moved: a FAIL verdict, an ``unavailable`` run (the dispatcher
raised) and a ``--reprocess`` whose PR head has moved — one per provider. The shared tail must
reproduce each of them field for field, with the same ``VOLATILE_KEYS`` as GE-5b-1 and nothing
added to it. The structural test below is the one that is red on the pre-move head: the tail did
not exist there, and each ``main`` ended in its own copy of these phases.
"""

from __future__ import annotations

import inspect
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    sys.path.insert(0, str(_p))

import glm_gate  # noqa: E402  (after the sys.path pins above)
import governed_lane  # noqa: E402
import kimi_gate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "governed_lane"
PROVIDERS = {"glm": glm_gate, "kimi": kimi_gate}
SCENARIOS = ("fail", "unavailable", "reprocess-stale")
# Same set as GE-5b-1, deliberately: a new random field must still fail the comparison.
VOLATILE_KEYS = frozenset({"recorded_at", "dispatch_id", "duration_seconds", "report_path", "report_path_informational"})

_DIFF = (
    "diff --git a/scripts/lib/example.py b/scripts/lib/example.py\n--- a/scripts/lib/example.py\n"
    "+++ b/scripts/lib/example.py\n@@ -1,3 +1,4 @@\n def answer():\n-    return 1\n"
    "+    # the answer changed\n+    return 2\n"
)


def _factory(data_dir, report=None, error=None):
    """A dispatcher double that writes its report the way ``governance_emit`` does."""
    def _make(*_a, **_k):
        def _dispatch(_provider, _model, _instruction, dispatch_id):
            if error is not None:
                raise error
            (data_dir / "unified_reports").mkdir(parents=True, exist_ok=True)
            (data_dir / "unified_reports" / f"{dispatch_id}.md").write_text(report, encoding="utf-8")
            return report
        return _dispatch
    return _make


def _record(data_dir, pr, gate):
    return json.loads((data_dir / "state" / "review_gates" / "results" / f"pr-{pr}-{gate}.json").read_text())


def _run(gate, argv):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return gate.main(argv)


def _run_offline(provider, data_dir, *, report=None, error=None):
    gate = PROVIDERS[provider]
    data_dir.mkdir(parents=True)
    diff_file = data_dir / "review.diff"
    diff_file.write_text(_DIFF, encoding="utf-8")
    saved = (gate._get_diff, gate._make_default_dispatcher)
    gate._get_diff = lambda _pr, _df: _DIFF
    gate._make_default_dispatcher = _factory(data_dir, report=report, error=error)
    try:
        return _run(gate, ["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir),
                           "--model", gate.DEFAULT_MODEL])
    finally:
        gate._get_diff, gate._make_default_dispatcher = saved


def _drive_offline(provider, data_dir, *, report=None, error=None):
    rc = _run_offline(provider, data_dir, report=report, error=error)
    return rc, _record(data_dir, 0, f"{provider}_gate")


def _run_live(provider, data_dir, *, report=None, error=None, sha="sha-original"):
    """A non-offline run: the identity lookups are doubles, no real ``gh`` call is made."""
    gate = PROVIDERS[provider]
    data_dir.mkdir(parents=True, exist_ok=True)
    saved = (gate._get_diff, gate._make_default_dispatcher, gate.get_pr_head_branch, gate.get_pr_head_sha)
    gate._get_diff = lambda _pr, _df: _DIFF
    gate._make_default_dispatcher = _factory(data_dir, report=report, error=error)
    gate.get_pr_head_branch = lambda _pr: "feature/ge5b2"
    gate.get_pr_head_sha = lambda _pr: sha
    try:
        return _run(gate, ["--pr", "4242", "--data-dir", str(data_dir), "--model", gate.DEFAULT_MODEL])
    finally:
        gate._get_diff, gate._make_default_dispatcher, gate.get_pr_head_branch, gate.get_pr_head_sha = saved


def _drive_reprocess_stale(provider, data_dir):
    """A live run, then ``--reprocess`` of its dispatch_id on a head that has since moved."""
    gate = PROVIDERS[provider]
    report = (FIXTURES / f"{provider}-pass-clean.md").read_text(encoding="utf-8")
    assert _run_live(provider, data_dir, report=report) == 0
    dispatch_id = _record(data_dir, 4242, f"{provider}_gate")["dispatch_id"]
    saved = (gate.get_pr_head_branch, gate.get_pr_head_sha)
    gate.get_pr_head_branch = lambda _pr: "feature/ge5b2"
    gate.get_pr_head_sha = lambda _pr: "sha-moved"
    try:
        rc = _run(gate, ["--pr", "4242", "--reprocess", dispatch_id, "--data-dir", str(data_dir),
                         "--model", gate.DEFAULT_MODEL])
    finally:
        gate.get_pr_head_branch, gate.get_pr_head_sha = saved
    return rc, _record(data_dir, 4242, f"{provider}_gate")


_DRIVERS = {
    "fail": lambda provider, data_dir: _drive_offline(
        provider, data_dir, report=(FIXTURES / f"{provider}-fail-blocking.md").read_text(encoding="utf-8")),
    "unavailable": lambda provider, data_dir: _drive_offline(
        provider, data_dir, error=RuntimeError("harness exploded")),
    "reprocess-stale": _drive_reprocess_stale,
}


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_shared_tail_reproduces_the_new_golden_record(provider, scenario, tmp_path):
    rc, record = _DRIVERS[scenario](provider, tmp_path / f"{provider}-{scenario}")
    golden = json.loads((FIXTURES / "records" / f"{provider}-{scenario}.json").read_text(encoding="utf-8"))

    assert rc == {"fail": 2, "unavailable": 1, "reprocess-stale": 1}[scenario]
    assert {k: v for k, v in record.items() if k not in VOLATILE_KEYS} == golden
    assert VOLATILE_KEYS <= set(record)


# **Red run** on the pre-GE-5b-2 head (b33378be): the tail did not exist and both ``main``s ended
# in their own copy of phases 3-6, so every assertion below fails there.
def test_both_mains_hand_the_run_to_the_one_shared_tail():
    assert callable(governed_lane.run_gate_tail)
    assert governed_lane.run_gate_tail.__module__ == "governed_lane"
    for provider, module in PROVIDERS.items():
        assert "run_gate_tail(state)" in inspect.getsource(module.main), provider


def test_the_provider_differences_live_on_the_register_row_not_in_a_branch():
    glm_row = governed_lane.provider_row("glm")
    kimi_row = governed_lane.provider_row("kimi")

    assert (glm_row.gate, glm_row.lane_provider, glm_row.dispatch_id_prefix) == ("glm_gate", "glm-harness", "glm-gate-pr")
    assert (kimi_row.gate, kimi_row.lane_provider, kimi_row.dispatch_id_prefix) == ("kimi_gate", "kimi", "kimi-gate-pr")
    assert glm_row.report_failure_lift is governed_lane.lift_report_failure_reason
    assert kimi_row.report_failure_lift is None
    with pytest.raises(ValueError):
        governed_lane.provider_row("no-such-provider")


def test_the_tail_reads_the_script_modules_patched_seams(tmp_path):
    """Each seam the movement touched is reached through the SCRIPT module's binding, not a
    snapshot: patch it on the gate module and the shared tail must see the double."""
    gate = PROVIDERS["kimi"]
    seen = []
    saved = (gate.record_terminal_result, gate.result_file_path, gate.get_pr_head_sha)
    gate.record_terminal_result = lambda **kwargs: seen.append(kwargs["payload"])
    gate.result_file_path = lambda *a, **k: None
    gate.get_pr_head_sha = lambda _pr: "patched-sha"
    try:
        rc = _run_live("kimi", tmp_path / "data", error=RuntimeError("boom"), sha="patched-sha")
    finally:
        gate.record_terminal_result, gate.result_file_path, gate.get_pr_head_sha = saved
    assert rc == 1 and len(seen) == 1
    assert seen[0]["gate"] == "kimi_gate"  # the tail called THIS module's patched recorder
    assert seen[0]["commit_sha"] == "patched-sha"  # ... and THIS module's patched head lookup
