"""GE-5b: the twelve helpers glm_gate and kimi_gate each carried a copy of now have ONE body in
``governed_lane`` (ADR-046 §4), provider as a parameter. Proof: the goldens were produced by the UNMOVED
gates and must still match field for field, the ``_get_diff`` / ``_make_default_dispatcher`` patches must
still reach what ``main`` calls (counted), and the provider must carry the differences."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    sys.path.insert(0, str(_p))

import glm_gate  # noqa: E402  (after the sys.path pins above)
import governed_lane  # noqa: E402
import kimi_gate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "governed_lane"
PROVIDERS = {"glm": glm_gate, "kimi": kimi_gate}
LANE_PROVIDER = {"glm": "glm-harness", "kimi": "kimi"}  # main's own strings, not a moved helper's

_DIFF = (
    "diff --git a/scripts/lib/example.py b/scripts/lib/example.py\n--- a/scripts/lib/example.py\n"
    "+++ b/scripts/lib/example.py\n@@ -1,3 +1,4 @@\n def answer():\n-    return 1\n"
    "+    # the answer changed\n+    return 2\n"
)
# Named one by one, never "everything that differs": a NEW random field must fail this test.
# ``execution_id`` (K3-2a) is random per run by construction — the goldens were recorded before it
# existed, and two runs of one seat must never share it.
VOLATILE_KEYS = frozenset({
    "recorded_at", "dispatch_id", "duration_seconds", "report_path", "report_path_informational",
    "execution_id",
})


def _drive(provider, data_dir):
    """Run ``main`` end to end with a fixed diff and a dispatcher double that writes its report the
    way ``governance_emit`` does, and count what the two patched seams saw."""
    gate, data_dir = PROVIDERS[provider], Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    diff_file = data_dir / "review.diff"
    diff_file.write_text(_DIFF, encoding="utf-8")
    report = (FIXTURES / f"{provider}-pass-clean.md").read_text(encoding="utf-8")
    calls = {"diff": 0, "factory": 0, "provider": None}

    def _get_diff(_pr, _diff_file):
        calls["diff"] += 1
        return _DIFF

    def _factory(*_a, **_k):
        calls["factory"] += 1

        def _dispatch(lane_provider, _model, _instruction, dispatch_id):
            calls["provider"] = lane_provider
            (data_dir / "unified_reports").mkdir(parents=True, exist_ok=True)
            (data_dir / "unified_reports" / f"{dispatch_id}.md").write_text(report, encoding="utf-8")
            return report

        return _dispatch

    with mock.patch.object(gate, "_get_diff", _get_diff), mock.patch.object(
            gate, "_make_default_dispatcher", _factory), redirect_stdout(io.StringIO()), \
            redirect_stderr(io.StringIO()):
        rc = gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir),
                        "--model", gate.DEFAULT_MODEL])
    record = json.loads((data_dir / "state" / "review_gates" / "results" / f"pr-0-{provider}_gate.json").read_text())
    return rc, record, calls


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_main_still_produces_the_pre_move_golden_record(provider, tmp_path):
    rc, record, _calls = _drive(provider, tmp_path / "data")
    golden = json.loads((FIXTURES / "records" / f"{provider}-golden.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert {k: v for k, v in record.items() if k not in VOLATILE_KEYS} == golden
    assert VOLATILE_KEYS <= set(record)


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_main_reaches_get_diff_and_the_dispatcher_factory_through_the_script_namespace(provider, tmp_path):
    _rc, _record, calls = _drive(provider, tmp_path / "data")

    assert (calls["diff"], calls["factory"]) == (1, 1)
    assert calls["provider"] == LANE_PROVIDER[provider]  # the provider main still owns
    assert PROVIDERS[provider]._get_diff is governed_lane.get_diff


# **Red run** on the pre-GE-5b head (01479cbe): everything below fails there — the helpers did not
# exist in ``governed_lane`` and the script names were still the private per-script copies.
def test_the_shared_helpers_are_public_in_governed_lane():
    for name in ("get_diff", "build_prompt", "review_dispatcher", "frontmatter_run_outcome",
                 "parse_dispatch_window_start", "frontmatter_duration_seconds",
                 "reprocess_original_commit_sha", "verdict_to_status", "unread_run_outcome",
                 "lift_lane_log_reason", "status_summary"):
        assert callable(getattr(governed_lane, name))


def test_the_summary_and_the_residual_name_the_provider():
    assert governed_lane.status_summary("glm", "pass", []) == "glm gate: pass (0 blocking finding(s))"
    assert governed_lane.status_summary("kimi", "fail", [{}, {}]) == "kimi gate: fail (2 blocking finding(s))"
    assert governed_lane.status_summary("kimi", "unavailable", [], "timeout").startswith("kimi gate: UNAVAILABLE (timeout")
    assert "kimi produced no readable verdict" in governed_lane.verdict_to_status("kimi", {}, "")[2]
    assert "glm produced no readable verdict" in governed_lane.verdict_to_status("glm", {}, "")[2]
    assert "governed glm dispatch failed" in governed_lane.unread_run_outcome("glm", "boom", "", 0.0)[2]


def test_the_review_dispatcher_uses_the_providers_gate_name_and_the_injected_factory():
    seen = {}

    def _factory(data_dir, **kwargs):
        seen.update(kwargs)
        return "DISPATCHER"

    coverage = {"diff_chars": 10, "diff_limit": 10, "diff_truncated": False}
    assert governed_lane.review_dispatcher(
        "kimi", Path("/tmp/nowhere"), "feature/x", coverage, None, dispatcher_factory=_factory,
    ) == "DISPATCHER"
    assert seen["base_ref"] == "origin/feature/x"
    assert set(seen) == {"role", "base_ref", "timeout_seconds", "total_deadline_seconds"}
    governed_lane.review_dispatcher("glm", Path("/tmp/nowhere"), "", coverage, 42, dispatcher_factory=_factory)
    assert (seen["total_deadline_seconds"], seen["base_ref"]) == (42, None)  # explicit --timeout, offline
