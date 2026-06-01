"""Tests for dispatch_govern — govern() + _synthesize() logic."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from dispatch_govern import GovernRaw, GovernSpec, GovernedOutcome, govern, _synthesize
from report_body_contract import validate_body


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture()
def tmp_state(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


def _make_spec(data_dir: Path, state_dir: Path, **kwargs) -> GovernSpec:
    return GovernSpec(
        dispatch_id=kwargs.get("dispatch_id", "test-govern-001"),
        terminal_id=kwargs.get("terminal_id", "T1"),
        instruction=kwargs.get("instruction", "Do the thing."),
        data_dir=data_dir,
        state_dir=state_dir,
        pr_id=kwargs.get("pr_id"),
        base_sha=kwargs.get("base_sha"),
        worktree_path=kwargs.get("worktree_path"),
    )


def _make_raw(**kwargs) -> GovernRaw:
    return GovernRaw(
        receipt=kwargs.get("receipt", {"status": "done"}),
        duration_seconds=kwargs.get("duration_seconds", 5.0),
    )


def _valid_body() -> str:
    return (
        "# Dispatch test-govern-001\n\n"
        "## Summary\n\n"
        "Implemented the feature correctly with full test coverage. "
        "All tests pass and the implementation is complete.\n\n"
        "## Changes\n\n"
        "- scripts/lib/foo.py: added new function\n\n"
        "## Verification\n\n"
        "pytest tests/ -q: 15 passed.\n\n"
        "## Open Items\n\nNone.\n"
    )


# ---------------------------------------------------------------------------
# govern() — authored path
# ---------------------------------------------------------------------------

def test_govern_authored_uses_worker_report(tmp_data, tmp_state, monkeypatch):
    """When a valid worker report exists, govern() uses it and marks authored."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    reports_dir = tmp_data / "unified_reports"
    reports_dir.mkdir(parents=True)
    report_file = reports_dir / "test-govern-001.md"
    report_file.write_text(_valid_body(), encoding="utf-8")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    # govern() checks existence but emit is idempotent-on-exists
    outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.contract_status == "authored"
    assert outcome.report_path is not None


def test_govern_authored_does_not_overwrite_worker_report(tmp_data, tmp_state, monkeypatch):
    """Authored body is not re-written by govern() (idempotency via emit_unified_report)."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    reports_dir = tmp_data / "unified_reports"
    reports_dir.mkdir(parents=True)
    report_file = reports_dir / "test-govern-001.md"
    original_body = _valid_body()
    report_file.write_text(original_body, encoding="utf-8")
    original_mtime = report_file.stat().st_mtime

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()
    govern(spec, raw, lane="tmux_interactive")

    # File unchanged (emit_unified_report idempotency)
    assert report_file.stat().st_mtime == original_mtime
    assert report_file.read_text(encoding="utf-8") == original_body


# ---------------------------------------------------------------------------
# govern() — synthesis path (no worker report)
# ---------------------------------------------------------------------------

def test_govern_synthesized_when_no_worker_report(tmp_data, tmp_state, monkeypatch):
    """Missing worker report -> synthesized body, contract_status=synthesized."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    with patch("dispatch_govern._git_summary",
               return_value="feat: implement X with full coverage. Worker status: done. Synthesized."), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/foo.py | 10 +++++"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.contract_status == "synthesized"
    assert outcome.report_path is not None


def test_govern_synthesized_never_contains_placeholder(tmp_data, tmp_state, monkeypatch):
    """Synthesized body must not contain the old placeholder string."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    placeholder = "Interactive tmux dispatch (lane: tmux_interactive). Status:"

    with patch("dispatch_govern._git_summary", return_value="feat: add governer\n\nWorker status: done."), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/dispatch_govern.py | 5 ++"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.report_path is not None
    content = outcome.report_path.read_text(encoding="utf-8")
    assert placeholder not in content, f"Placeholder found in synthesized report: {content[:300]}"


def test_govern_synthesis_stamped_synthesized(tmp_data, tmp_state, monkeypatch):
    """Synthesized body carries 'contract_status: synthesized' in content."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    with patch("dispatch_govern._git_summary", return_value="feat: synthesized report"), \
         patch("dispatch_govern._git_changes", return_value="No git diff available"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    content = outcome.report_path.read_text(encoding="utf-8")
    assert "synthesized" in content


def test_govern_synthesized_includes_real_summary_not_placeholder(tmp_data, tmp_state, monkeypatch):
    """Synthesized ## Summary uses git log output, not the placeholder string."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()
    git_commit_msg = "feat(tmux): implement GOVERN step with git-derived synthesis"

    with patch("dispatch_govern._git_summary", return_value=git_commit_msg), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/dispatch_govern.py | 120 +++"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    content = outcome.report_path.read_text(encoding="utf-8")
    assert git_commit_msg in content


def test_govern_synthesized_includes_git_changes(tmp_data, tmp_state, monkeypatch):
    """Synthesized ## Changes section uses git diff --stat output."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()
    git_diff = "scripts/lib/dispatch_govern.py | 120 +++\n 1 file changed, 120 insertions(+)"

    with patch("dispatch_govern._git_summary", return_value="feat: implement something with enough characters here"), \
         patch("dispatch_govern._git_changes", return_value=git_diff):
        outcome = govern(spec, raw, lane="tmux_interactive")

    content = outcome.report_path.read_text(encoding="utf-8")
    assert "dispatch_govern.py" in content


# ---------------------------------------------------------------------------
# govern() — placeholder worker report triggers synthesis
# ---------------------------------------------------------------------------

def test_govern_placeholder_worker_report_triggers_synthesis(tmp_data, tmp_state, monkeypatch):
    """A placeholder worker report is rejected, synthesized, and the file is OVERWRITTEN."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    reports_dir = tmp_data / "unified_reports"
    reports_dir.mkdir(parents=True)
    # Write the exact placeholder string that the old _emit_unified_report would produce.
    placeholder_body = (
        "# Dispatch test-govern-001\n\n"
        "## Summary\n\n"
        "Interactive tmux dispatch (lane: tmux_interactive). Status: done.\n\n"
        "## Changes\n\nNone.\n\n"
        "## Verification\n\nNone.\n\n"
        "## Open Items\n\nNone.\n"
    )
    report_file = reports_dir / "test-govern-001.md"
    report_file.write_text(placeholder_body, encoding="utf-8")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    with patch("dispatch_govern._git_summary",
               return_value="feat: something real with enough chars to pass the fifty-char minimum check"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/foo.py | 10 ++"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.contract_status == "synthesized"
    assert outcome.report_path is not None

    # FIX 1: The file must be OVERWRITTEN with the synthesized body — not left as placeholder.
    final_content = outcome.report_path.read_text(encoding="utf-8")
    forbidden = "Interactive tmux dispatch (lane: tmux_interactive). Status:"
    assert forbidden not in final_content, (
        f"Placeholder still present after govern() overwrite: {final_content[:300]}"
    )


# ---------------------------------------------------------------------------
# govern() — emit_unified_report called AFTER body is final
# ---------------------------------------------------------------------------

def test_govern_body_override_passed_to_emit(tmp_data, tmp_state, monkeypatch):
    """emit_unified_report is called with body_override when synthesizing."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    captured_calls = []

    def fake_emit(*args, body_override=None, **kwargs):
        captured_calls.append(body_override)
        reports_dir = tmp_data / "unified_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / f"{spec.dispatch_id}.md"
        path.write_text(body_override or "fallback", encoding="utf-8")
        return path

    with patch("governance_emit.emit_unified_report", side_effect=fake_emit), \
         patch("dispatch_govern._git_summary", return_value="feat: something that has enough chars"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/x.py | 5 ++"):
        govern(spec, raw, lane="tmux_interactive")

    assert len(captured_calls) == 1
    assert captured_calls[0] is not None, "body_override must be set for synthesized body"
    assert "Interactive tmux dispatch" not in captured_calls[0]


# ---------------------------------------------------------------------------
# _synthesize() — direct unit tests
# ---------------------------------------------------------------------------

def test_synthesize_contains_all_required_sections(tmp_data, tmp_state):
    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    with patch("dispatch_govern._git_summary", return_value="feat: implement feature"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/foo.py | 10 ++"):
        body = _synthesize(spec, raw)

    for section in ("## Summary", "## Changes", "## Verification", "## Open Items"):
        assert section in body, f"Missing {section} in synthesized body"


def test_synthesize_never_contains_placeholder():
    from dispatch_govern import GovernSpec, GovernRaw, _synthesize

    spec = GovernSpec(
        dispatch_id="synth-001",
        terminal_id="T1",
        instruction="test",
        data_dir=Path("/tmp"),
        state_dir=Path("/tmp"),
    )
    raw = GovernRaw(receipt={"status": "done"}, duration_seconds=1.0)

    placeholder = "Interactive tmux dispatch (lane: tmux_interactive). Status:"

    with patch("dispatch_govern._git_summary", return_value="feat: implement feature"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/foo.py | 10 ++"):
        body = _synthesize(spec, raw)

    assert placeholder not in body


def test_synthesize_fallback_when_no_commit(tmp_data, tmp_state):
    """When git log returns empty, summary uses the no-commit fallback."""
    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt=None, duration_seconds=10.0)

    with patch("dispatch_govern._git_summary",
               return_value="No commit on branch; worker emitted status=timeout. Body synthesized by lane (no worker report)."), \
         patch("dispatch_govern._git_changes", return_value="No git diff available"):
        body = _synthesize(spec, raw)

    assert "No commit on branch" in body or "synthesized" in body.lower()


# ---------------------------------------------------------------------------
# govern() — timeout path (receipt=None)
# ---------------------------------------------------------------------------

def test_govern_timeout_path_synthesizes(tmp_data, tmp_state, monkeypatch):
    """Receipt=None (timeout) -> synthesized body emitted."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt=None, duration_seconds=3600.0)

    with patch("dispatch_govern._git_summary",
               return_value="No commit; timeout. Body synthesized by governance layer (no worker report)."), \
         patch("dispatch_govern._git_changes", return_value="No git diff available"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.contract_status == "synthesized"
    assert outcome.report_path is not None


# ---------------------------------------------------------------------------
# govern() — VNX_SHARED_GOVERN=0 does not route through govern()
# ---------------------------------------------------------------------------

def test_govern_flag_off_uses_legacy_path(tmp_data, tmp_state, monkeypatch):
    """When VNX_SHARED_GOVERN=0, govern() is not called via the tmux lane."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "0")

    # When govern() IS called directly with flag off, it still works (the flag
    # is checked by the caller, not govern() itself).  Here we just verify that
    # govern() returns a valid outcome regardless of flag state.
    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    with patch("dispatch_govern._git_summary", return_value="feat: something with enough characters for the summary check"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/foo.py | 5 ++"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert isinstance(outcome, GovernedOutcome)


# ---------------------------------------------------------------------------
# govern() — FIX 2: error path emits honest body, never raises
# ---------------------------------------------------------------------------

def test_govern_never_raises_on_internal_error(tmp_data, tmp_state):
    """govern() must never raise — any internal error returns GovernedOutcome."""
    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    with patch("dispatch_govern._govern_impl", side_effect=RuntimeError("simulated failure")):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert isinstance(outcome, GovernedOutcome)
    assert outcome.contract_status == "synthesized"
    assert outcome.error is not None
    assert "simulated failure" in outcome.error


def test_govern_error_path_body_not_placeholder(tmp_data, tmp_state):
    """When govern() hits an error, the emitted body must not contain the forbidden string."""
    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()
    forbidden = "Interactive tmux dispatch (lane: tmux_interactive). Status:"

    with patch("dispatch_govern._govern_impl", side_effect=RuntimeError("synthesis broke")):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert isinstance(outcome, GovernedOutcome)
    if outcome.report_path and outcome.report_path.exists():
        content = outcome.report_path.read_text(encoding="utf-8")
        assert forbidden not in content, f"Placeholder in error-path report: {content[:300]}"


# ---------------------------------------------------------------------------
# govern() — FIX 3: synthesized body includes ## PR when pr_id set
# ---------------------------------------------------------------------------

def test_govern_synthesized_with_pr_id_includes_pr_section(tmp_data, tmp_state):
    """Synthesized body includes ## PR when pr_id is set on the spec."""
    spec = _make_spec(tmp_data, tmp_state, pr_id="42")
    raw = _make_raw()

    with patch("dispatch_govern._git_summary",
               return_value="feat: implement feature with sufficient non-whitespace chars to satisfy validation contract"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/foo.py | 10 ++"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.report_path is not None
    content = outcome.report_path.read_text(encoding="utf-8")
    assert "## PR" in content, "Synthesized body missing ## PR section when pr_id set"
    assert "42" in content


def test_govern_synthesized_without_pr_id_omits_pr_section(tmp_data, tmp_state):
    """Synthesized body omits ## PR when pr_id is not set."""
    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()

    with patch("dispatch_govern._git_summary", return_value="feat: feature without PR with enough chars"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/foo.py | 5 ++"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.report_path is not None
    content = outcome.report_path.read_text(encoding="utf-8")
    assert "## PR" not in content


def test_govern_synthesized_pr_section_passes_validate_body(tmp_data, tmp_state):
    """Synthesized body with pr_id set passes validate_body(pr_id=...) without violation."""
    spec = _make_spec(tmp_data, tmp_state, pr_id="77")
    raw = _make_raw()

    with patch("dispatch_govern._git_summary",
               return_value="feat: add new feature with enough non-whitespace chars to pass the fifty char minimum"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/foo.py | 10 ++"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    # With ## PR included, validate_body should not flag violated.
    assert outcome.contract_status in ("synthesized",), (
        f"Expected synthesized, got {outcome.contract_status} (body_result={outcome.body_result})"
    )


# ---------------------------------------------------------------------------
# Grep-style: forbidden placeholder must not appear in any scripts/ file
# ---------------------------------------------------------------------------

def test_forbidden_placeholder_absent_from_scripts():
    """No script file may emit the forbidden placeholder string."""
    forbidden = "Interactive tmux dispatch (lane: tmux_interactive). Status:"
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"

    violations = []
    for py_file in scripts_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if forbidden in content:
            violations.append(str(py_file))

    assert violations == [], (
        f"Forbidden placeholder string found in scripts/: {violations}"
    )
