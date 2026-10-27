"""GE-5b: ``glm_gate.py`` and ``kimi_gate.py`` are shims, and their CLI is unchanged.

Phases 1-2 (arguments, request record, diff/report acquisition) moved into
``governed_lane.run_gate_head``. What a caller sees must not move: the CLI text, the
exit codes, and the seam call counts. The CLI goldens under
``tests/fixtures/governed_lane/cli/`` were produced by the UNMOVED scripts on the branch
base (d0139cd3) with ``COLUMNS=80``, before any code moved — the ``--help`` text and the
exit code of a missing ``--pr`` must still match them byte for byte.

The seam-count test is the other half of the move: a seam patched on the script module
must be reached through that module's binding, exactly as often as before.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
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

SCRIPTS = ROOT / "scripts"
CLI_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "governed_lane" / "cli"
PROVIDERS = {"glm": glm_gate, "kimi": kimi_gate}
SHIM_LINE_LIMIT = 80

_DIFF = (
    "diff --git a/scripts/lib/example.py b/scripts/lib/example.py\n--- a/scripts/lib/example.py\n"
    "+++ b/scripts/lib/example.py\n@@ -1,3 +1,4 @@\n def answer():\n-    return 1\n"
    "+    # the answer changed\n+    return 2\n"
)
_PASS_REPORT = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)


def _run_script(provider: str, *argv: str) -> subprocess.CompletedProcess:
    """Run the script exactly as an operator or CI step does, with a fixed help width."""
    env = dict(os.environ, COLUMNS="80")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / f"{provider}_gate.py"), *argv],
        capture_output=True, text=True, timeout=60, env=env,
    )


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_help_output_is_the_golden_recorded_on_the_old_head(provider):
    golden = (CLI_FIXTURES / f"{provider}-help.txt").read_text(encoding="utf-8")

    result = _run_script(provider, "--help")

    assert result.returncode == 0
    assert result.stdout == golden


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_a_missing_pr_exits_with_the_golden_code(provider):
    golden = json.loads((CLI_FIXTURES / f"{provider}-missing-pr.json").read_text(encoding="utf-8"))

    result = _run_script(provider)

    assert result.returncode == golden["exit_code"]
    assert "--pr" in result.stderr
    assert result.stdout == ""


# **Red run** on the pre-GE-5b head (d0139cd3): both scripts were 400+ lines, carried the
# argument parser and the request record in their own ``main``, and ``run_gate_head`` did
# not exist in ``governed_lane`` — every assertion below fails there.
@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_each_gate_script_is_a_shim_of_at_most_eighty_lines(provider):
    source = (SCRIPTS / f"{provider}_gate.py").read_text(encoding="utf-8")

    assert len(source.splitlines()) <= SHIM_LINE_LIMIT
    assert "run_gate_head(" in source  # the body is the adapter's, not the script's
    assert "argparse" not in source  # the CLI itself is defined in governed_lane
    assert "import re" not in source  # kimi advisory on #2253: unused since the move
    assert callable(governed_lane.run_gate_head)


def test_the_head_body_lives_in_governed_lane_and_reads_the_register_row():
    source = (ROOT / "scripts" / "lib" / "governed_lane.py").read_text(encoding="utf-8")

    assert "def run_gate_head(" in source
    assert "\n    if provider ==" not in source  # a provider difference is a row edit, never a branch
    assert governed_lane.provider_row("glm").cli_description == "GOVERNED glm-harness review gate."
    assert governed_lane.provider_row("kimi").cli_description == "Temporary GOVERNED kimi review gate."
    assert governed_lane.provider_row("glm").model_pin is not None
    assert governed_lane.provider_row("kimi").model_pin is None


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_a_live_run_calls_every_phase_one_and_two_seam_exactly_once(provider, tmp_path):
    """The count is the contract: the move must not call a seam twice (OI-1874 r3) or zero times."""
    gate = PROVIDERS[provider]
    data_dir = tmp_path / provider
    calls: dict = {}

    def _count(name, result):
        def _seam(*_a, **_k):
            calls[name] = calls.get(name, 0) + 1
            return result
        return _seam

    def _factory(*_a, **_k):
        calls["make_default_dispatcher"] = calls.get("make_default_dispatcher", 0) + 1

        def _dispatch(_lane_provider, _model, _instruction, dispatch_id):
            (data_dir / "unified_reports").mkdir(parents=True, exist_ok=True)
            (data_dir / "unified_reports" / f"{dispatch_id}.md").write_text(_PASS_REPORT, encoding="utf-8")
            return _PASS_REPORT

        return _dispatch

    saved = {
        "_resolve_data_dir": gate._resolve_data_dir, "_get_diff": gate._get_diff,
        "_make_default_dispatcher": gate._make_default_dispatcher,
        "persist_request": gate.persist_request, "max_diff_chars": gate.max_diff_chars,
        "get_pr_head_branch": gate.get_pr_head_branch, "get_pr_head_sha": gate.get_pr_head_sha,
        "record_terminal_result": gate.record_terminal_result,
    }
    gate._resolve_data_dir = _count("resolve_data_dir", data_dir)
    gate._get_diff = _count("get_diff", _DIFF)
    gate._make_default_dispatcher = _factory
    gate.persist_request = _count("persist_request", None)
    gate.max_diff_chars = _count("max_diff_chars", 50_000)
    gate.get_pr_head_branch = _count("get_pr_head_branch", "feature/ge5b")
    gate.get_pr_head_sha = _count("get_pr_head_sha", "sha-ge5b")
    gate.record_terminal_result = _count("record_terminal_result", None)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = gate.main(["--pr", "5150", "--data-dir", str(data_dir), "--model", gate.DEFAULT_MODEL])
    finally:
        for name, value in saved.items():
            setattr(gate, name, value)

    assert rc == 0, f"{provider} live run must pass"
    assert calls == {
        "resolve_data_dir": 1, "get_diff": 1, "make_default_dispatcher": 1,
        "persist_request": 1, "max_diff_chars": 1, "get_pr_head_branch": 1,
        "get_pr_head_sha": 1, "record_terminal_result": 1,
    }
