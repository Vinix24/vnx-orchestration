"""Tests for dispatch_govern — govern() + _synthesize() logic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "lib"))
sys.path.insert(0, str(_SCRIPTS))

from dispatch_govern import (
    GovernRaw,
    GovernSpec,
    GovernedOutcome,
    dedup_completion_receipts,
    ensure_receipt,
    govern,
    _synthesize,
)
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


def test_govern_authored_stamps_frontmatter_on_worker_report(tmp_data, tmp_state, monkeypatch):
    """After govern(), a frontmatter-less worker report has a YAML frontmatter block stamped."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    reports_dir = tmp_data / "unified_reports"
    reports_dir.mkdir(parents=True)
    report_file = reports_dir / "test-govern-001.md"
    report_file.write_text(_valid_body(), encoding="utf-8")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()
    outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.contract_status == "authored"
    content = report_file.read_text(encoding="utf-8")
    assert content.startswith("---\n"), "Authored report must begin with YAML frontmatter after govern()"
    assert "schema_version: 1" in content
    assert "contract_status: authored" in content
    assert "provider: claude" in content
    assert "terminal_id: T1" in content
    assert "lane: tmux_interactive" in content


def test_govern_authored_preserves_worker_body(tmp_data, tmp_state, monkeypatch):
    """Worker body is preserved below the stamped frontmatter."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    reports_dir = tmp_data / "unified_reports"
    reports_dir.mkdir(parents=True)
    report_file = reports_dir / "test-govern-001.md"
    worker_body = _valid_body()
    report_file.write_text(worker_body, encoding="utf-8")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()
    govern(spec, raw, lane="tmux_interactive")

    content = report_file.read_text(encoding="utf-8")
    # Body sections from the worker report must survive below the frontmatter.
    assert "## Summary" in content
    assert "## Changes" in content
    assert "## Verification" in content
    assert "## Open Items" in content
    # Worker's specific text content must be preserved.
    assert "Implemented the feature correctly" in content


def test_govern_authored_not_double_stamped(tmp_data, tmp_state, monkeypatch):
    """Defensive: a worker report that already has frontmatter is not double-stamped."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")

    reports_dir = tmp_data / "unified_reports"
    reports_dir.mkdir(parents=True)
    report_file = reports_dir / "test-govern-001.md"
    # Pre-stamp a frontmatter block simulating a future worker that authors its own.
    existing_fm_body = (
        "---\n"
        "schema_version: 1\n"
        "dispatch_id: test-govern-001\n"
        "some_extra_field: worker-value\n"
        "---\n\n"
        + _valid_body()
    )
    report_file.write_text(existing_fm_body, encoding="utf-8")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()
    govern(spec, raw, lane="tmux_interactive")

    content = report_file.read_text(encoding="utf-8")
    # Exactly one opening --- fence at the start.
    assert content.count("---\n") >= 2, "Frontmatter fences missing"
    # No double frontmatter: the content after the first closing --- should be body text.
    fm_end = content.find("\n---\n", 4)  # find closing ---
    assert fm_end != -1, "No closing --- found"
    body_part = content[fm_end + 5:]
    # Second --- must not immediately follow (no double-stamp).
    assert not body_part.lstrip("\n").startswith("---\n"), (
        "Double-stamp detected: body begins with another frontmatter block"
    )
    # Required fields must be present.
    assert "schema_version: 1" in content
    assert "contract_status: authored" in content


def test_govern_authored_schema_strict_valid(tmp_data, tmp_state, monkeypatch):
    """VNX_SCHEMA_STRICT=1: authored report frontmatter validates against the schema."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")
    monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")

    reports_dir = tmp_data / "unified_reports"
    reports_dir.mkdir(parents=True)
    report_file = reports_dir / "test-govern-001.md"
    report_file.write_text(_valid_body(), encoding="utf-8")

    spec = _make_spec(tmp_data, tmp_state)
    raw = _make_raw()
    outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.contract_status == "authored"
    assert outcome.error is None, f"govern() returned error under strict mode: {outcome.error}"
    assert outcome.report_path is not None and outcome.report_path.exists()

    content = outcome.report_path.read_text(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
    from unified_report_schema import UnifiedReportValidator
    validator = UnifiedReportValidator()
    result = validator.validate(content)
    assert result.valid, (
        f"Authored report fails schema validation under strict mode: {result.errors}"
    )


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
# govern() — VNX_SCHEMA_STRICT=1: overwrite must succeed + frontmatter valid
# ---------------------------------------------------------------------------

def test_govern_strict_mode_overwrites_stale_placeholder(tmp_data, tmp_state, monkeypatch):
    """VNX_SCHEMA_STRICT=1: stale placeholder is replaced with schema-valid synthesized report.

    Before the fix, govern() emitted a 5-field frontmatter that failed strict-mode
    schema validation (missing schema_version + 9 other required fields), causing
    SchemaViolation to abort the atomic write and leaving the stale placeholder on disk.
    """
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")
    monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")

    reports_dir = tmp_data / "unified_reports"
    reports_dir.mkdir(parents=True)
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
               return_value="feat: implement something real with enough chars to pass validation"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/foo.py | 10 ++"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.contract_status == "synthesized"
    assert outcome.error is None, f"govern() returned error: {outcome.error}"
    assert outcome.report_path is not None
    assert outcome.report_path.exists()

    content = outcome.report_path.read_text(encoding="utf-8")

    # Placeholder must be gone
    forbidden = "Interactive tmux dispatch (lane: tmux_interactive). Status:"
    assert forbidden not in content, f"Placeholder still present: {content[:400]}"

    # schema_version must appear in the written frontmatter
    assert "schema_version: 1" in content, f"schema_version missing: {content[:400]}"

    # Full schema validation must pass
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
    from unified_report_schema import UnifiedReportValidator
    validator = UnifiedReportValidator()
    result = validator.validate(content)
    assert result.valid, f"Report fails schema validation under strict mode: {result.errors}"


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


# ---------------------------------------------------------------------------
# dedup_completion_receipts — unit tests
# ---------------------------------------------------------------------------

def test_dedup_empty_returns_none():
    assert dedup_completion_receipts([]) is None


def test_dedup_single_returns_it():
    r = {"dispatch_id": "x", "synthesized": True, "timestamp": "2026-06-01T10:00:00Z"}
    assert dedup_completion_receipts([r]) is r


def test_dedup_prefers_authored_over_synthesized():
    """Non-synthesized receipt wins regardless of timestamp order."""
    synthesized = {"dispatch_id": "x", "synthesized": True, "timestamp": "2026-06-01T11:00:00Z"}
    authored = {"dispatch_id": "x", "synthesized": False, "timestamp": "2026-06-01T10:00:00Z"}
    result = dedup_completion_receipts([synthesized, authored])
    assert result is authored, "authored must win over synthesized even when synthesized is newer"


def test_dedup_authored_no_synthesized_field_wins():
    """A receipt without the synthesized field is treated as authored."""
    synthesized = {"dispatch_id": "x", "synthesized": True, "timestamp": "2026-06-01T12:00:00Z"}
    authored = {"dispatch_id": "x", "timestamp": "2026-06-01T10:00:00Z"}
    result = dedup_completion_receipts([synthesized, authored])
    assert result is authored


def test_dedup_late_authored_wins_over_earlier_synthesized():
    """A worker receipt arriving AFTER a synthesized one must win."""
    synthesized = {"dispatch_id": "x", "synthesized": True, "timestamp": "2026-06-01T10:00:00Z"}
    authored = {"dispatch_id": "x", "timestamp": "2026-06-01T11:00:00Z"}
    result = dedup_completion_receipts([synthesized, authored])
    assert result is authored


def test_dedup_no_double_count():
    """With two receipts, dedup returns exactly one."""
    r1 = {"dispatch_id": "x", "synthesized": True, "timestamp": "2026-06-01T10:00:00Z"}
    r2 = {"dispatch_id": "x", "timestamp": "2026-06-01T11:00:00Z"}
    result = dedup_completion_receipts([r1, r2])
    assert isinstance(result, dict)


def test_dedup_all_synthesized_picks_newest():
    """When all receipts are synthesized, picks the one with the latest timestamp."""
    r1 = {"dispatch_id": "x", "synthesized": True, "timestamp": "2026-06-01T10:00:00Z"}
    r2 = {"dispatch_id": "x", "synthesized": True, "timestamp": "2026-06-01T11:00:00Z"}
    result = dedup_completion_receipts([r1, r2])
    assert result is r2


def test_dedup_authored_newest_wins():
    """When multiple authored receipts, picks newest timestamp."""
    r1 = {"dispatch_id": "x", "timestamp": "2026-06-01T09:00:00Z"}
    r2 = {"dispatch_id": "x", "timestamp": "2026-06-01T10:00:00Z"}
    result = dedup_completion_receipts([r1, r2])
    assert result is r2


# ---------------------------------------------------------------------------
# dedup_completion_receipts — authoritative>unknown ranking (uniform receipts)
# ---------------------------------------------------------------------------

def test_dedup_authoritative_done_wins_over_unknown():
    """done receipt outranks status=unknown regardless of timestamp or authored tier."""
    unknown = {"dispatch_id": "x", "status": "unknown", "lane": None, "timestamp": "2026-06-01T11:00:00Z"}
    done = {"dispatch_id": "x", "status": "done", "timestamp": "2026-06-01T10:00:00Z"}
    result = dedup_completion_receipts([unknown, done])
    assert result is done, "done must win over unknown even when unknown is newer"


def test_dedup_authoritative_failed_wins_over_unknown():
    """failed receipt outranks status=unknown."""
    unknown = {"dispatch_id": "x", "status": "unknown", "timestamp": "2026-06-01T12:00:00Z"}
    failed = {"dispatch_id": "x", "status": "failed", "timestamp": "2026-06-01T09:00:00Z"}
    result = dedup_completion_receipts([unknown, failed])
    assert result is failed, "failed must win over unknown"


def test_dedup_unknown_alone_returns_unknown():
    """When only an unknown receipt exists, it is returned (no authoritative exists)."""
    unknown = {"dispatch_id": "x", "status": "unknown", "lane": None, "timestamp": "2026-06-01T10:00:00Z"}
    result = dedup_completion_receipts([unknown])
    assert result is unknown


def test_dedup_done_authored_wins_over_done_synthesized_and_unknown():
    """done(authored) beats done(synthesized) beats unknown — full three-tier stack."""
    unknown = {"dispatch_id": "x", "status": "unknown", "timestamp": "2026-06-01T13:00:00Z"}
    done_synth = {"dispatch_id": "x", "status": "done", "synthesized": True, "timestamp": "2026-06-01T12:00:00Z"}
    done_authored = {"dispatch_id": "x", "status": "done", "timestamp": "2026-06-01T10:00:00Z"}
    result = dedup_completion_receipts([unknown, done_synth, done_authored])
    assert result is done_authored, "done(authored) must win over done(synthesized) and unknown"


def test_dedup_multiple_done_authored_picks_newest():
    """When two authoritative authored receipts exist, newest timestamp wins."""
    r1 = {"dispatch_id": "x", "status": "done", "timestamp": "2026-06-01T09:00:00Z"}
    r2 = {"dispatch_id": "x", "status": "done", "timestamp": "2026-06-01T11:00:00Z"}
    result = dedup_completion_receipts([r1, r2])
    assert result is r2


# ---------------------------------------------------------------------------
# ensure_receipt — uniform stamp: provider/sub_provider/model/lane
# ---------------------------------------------------------------------------

def test_ensure_receipt_carries_provider_model_lane(tmp_data, tmp_state):
    """Lane-synthesized receipt must carry provider, sub_provider, model, and lane fields."""
    spec = _make_spec(tmp_data, tmp_state)
    spec.model = "sonnet"
    raw = GovernRaw(receipt=None, duration_seconds=60.0)

    ensure_receipt(spec, raw, lane="tmux_interactive", report_path=None,
                   contract_status="synthesized", permission_enforcement="soft")

    receipts_file = tmp_state / "t0_receipts.ndjson"
    receipt = json.loads(receipts_file.read_text().splitlines()[0])
    assert receipt.get("provider") == "claude", f"provider missing or wrong: {receipt}"
    assert receipt.get("sub_provider") == "anthropic", f"sub_provider missing or wrong: {receipt}"
    assert receipt.get("model") == "sonnet", f"model missing or wrong: {receipt}"
    assert receipt.get("lane") == "tmux_interactive", f"lane missing or wrong: {receipt}"


def test_ensure_receipt_carries_terminal_id(tmp_data, tmp_state):
    """FIX 2: lane-synthesized receipt must carry terminal_id AND keep terminal as alias."""
    spec = _make_spec(tmp_data, tmp_state, terminal_id="T1")
    raw = GovernRaw(receipt=None, duration_seconds=60.0)

    ensure_receipt(spec, raw, lane="tmux_interactive", report_path=None,
                   contract_status="synthesized", permission_enforcement="soft")

    receipts_file = tmp_state / "t0_receipts.ndjson"
    receipt = json.loads(receipts_file.read_text().splitlines()[0])
    assert receipt.get("terminal_id") == "T1", f"terminal_id missing or wrong: {receipt}"
    assert receipt.get("terminal") == "T1", f"terminal alias missing or wrong: {receipt}"


def test_ensure_receipt_model_unknown_when_not_set(tmp_data, tmp_state):
    """Lane-synthesized receipt uses 'unknown' for model when spec.model is not set."""
    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt=None, duration_seconds=60.0)

    ensure_receipt(spec, raw, lane="tmux_interactive", report_path=None,
                   contract_status="synthesized", permission_enforcement="soft")

    receipts_file = tmp_state / "t0_receipts.ndjson"
    receipt = json.loads(receipts_file.read_text().splitlines()[0])
    assert receipt.get("provider") == "claude"
    assert receipt.get("model") == "unknown"


# ---------------------------------------------------------------------------
# ensure_receipt — unit tests
# ---------------------------------------------------------------------------

def test_ensure_receipt_appended_when_no_worker_receipt(tmp_data, tmp_state):
    """ensure_receipt fires when raw.receipt is None, appending exactly one synthesized receipt."""
    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt=None, duration_seconds=60.0)
    receipts_file = tmp_state / "t0_receipts.ndjson"

    ensure_receipt(spec, raw, lane="tmux_interactive", report_path=None,
                   contract_status="synthesized", permission_enforcement="soft")

    assert receipts_file.exists(), "receipts_file must be created by ensure_receipt"
    lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 1, f"Expected exactly 1 receipt line, got {len(lines)}"
    receipt = json.loads(lines[0])
    assert receipt["source"] == "tmux_interactive_lane_synthesized"
    assert receipt["synthesized"] is True
    assert receipt["dispatch_id"] == spec.dispatch_id
    assert receipt["failure_reason"] == "tmux_receipt_deadline_exceeded"


def test_ensure_receipt_includes_report_path_when_provided(tmp_data, tmp_state):
    """Lane-synthesized receipt includes report_path when a report was emitted."""
    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt=None, duration_seconds=60.0)
    fake_report = tmp_data / "unified_reports" / f"{spec.dispatch_id}.md"

    ensure_receipt(spec, raw, lane="tmux_interactive", report_path=fake_report,
                   contract_status="synthesized", permission_enforcement="soft")

    receipts_file = tmp_state / "t0_receipts.ndjson"
    receipt = json.loads(receipts_file.read_text().splitlines()[0])
    assert receipt.get("report_path") == str(fake_report)


def test_ensure_receipt_not_fired_when_worker_receipt_exists(tmp_data, tmp_state):
    """When raw.receipt is set (worker emitted), ensure_receipt does not fire."""
    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt={"status": "done", "source": "tmux_interactive"}, duration_seconds=5.0)
    receipts_file = tmp_state / "t0_receipts.ndjson"

    ensure_receipt(spec, raw, lane="tmux_interactive", report_path=None,
                   contract_status="authored", permission_enforcement="soft")

    assert not receipts_file.exists() or receipts_file.read_text().strip() == ""


def test_ensure_receipt_disabled_by_flag(tmp_data, tmp_state, monkeypatch):
    """VNX_RECEIPT_FALLBACK=0 disables ensure_receipt entirely."""
    monkeypatch.setenv("VNX_RECEIPT_FALLBACK", "0")
    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt=None, duration_seconds=60.0)
    receipts_file = tmp_state / "t0_receipts.ndjson"

    ensure_receipt(spec, raw, lane="tmux_interactive", report_path=None,
                   contract_status="synthesized", permission_enforcement="soft")

    assert not receipts_file.exists() or receipts_file.read_text().strip() == ""


def test_ensure_receipt_idempotent(tmp_data, tmp_state):
    """Calling ensure_receipt twice for the same dispatch_id appends at most one unique receipt."""
    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt=None, duration_seconds=60.0)

    ensure_receipt(spec, raw, lane="tmux_interactive", report_path=None,
                   contract_status="synthesized", permission_enforcement="soft")
    ensure_receipt(spec, raw, lane="tmux_interactive", report_path=None,
                   contract_status="synthesized", permission_enforcement="soft")

    receipts_file = tmp_state / "t0_receipts.ndjson"
    lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 1, f"Idempotency: expected 1 receipt, got {len(lines)}"


# ---------------------------------------------------------------------------
# govern() + ensure_receipt integration — timeout path appends synthesized receipt
# ---------------------------------------------------------------------------

def test_govern_timeout_appends_synthesized_receipt(tmp_data, tmp_state, monkeypatch):
    """govern() with receipt=None must append a lane-synthesized receipt to t0_receipts.ndjson."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")
    monkeypatch.setenv("VNX_RECEIPT_FALLBACK", "1")

    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt=None, duration_seconds=3600.0)
    receipts_file = tmp_state / "t0_receipts.ndjson"

    with patch("dispatch_govern._git_summary",
               return_value="No commit; timeout. Body synthesized."), \
         patch("dispatch_govern._git_changes", return_value="No git diff available"):
        govern(spec, raw, lane="tmux_interactive")

    assert receipts_file.exists(), "ensure_receipt must create t0_receipts.ndjson"
    lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    synthesized = [json.loads(l) for l in lines
                   if json.loads(l).get("source") == "tmux_interactive_lane_synthesized"]
    assert len(synthesized) == 1, f"Expected exactly 1 lane-synthesized receipt, got {synthesized}"
    assert synthesized[0]["synthesized"] is True
    assert synthesized[0]["dispatch_id"] == spec.dispatch_id


def test_govern_normal_path_no_synthesized_receipt(tmp_data, tmp_state, monkeypatch):
    """govern() with a worker receipt must NOT append a synthesized one."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")
    monkeypatch.setenv("VNX_RECEIPT_FALLBACK", "1")

    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt={"status": "done", "source": "tmux_interactive"}, duration_seconds=5.0)
    receipts_file = tmp_state / "t0_receipts.ndjson"

    with patch("dispatch_govern._git_summary",
               return_value="feat: real worker output with enough chars"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/x.py | 5 ++"):
        govern(spec, raw, lane="tmux_interactive")

    if receipts_file.exists():
        lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
        synthesized = [json.loads(l) for l in lines
                       if json.loads(l).get("source") == "tmux_interactive_lane_synthesized"]
        assert synthesized == [], "No synthesized receipt expected when worker emitted its own"


def test_govern_fallback_disabled_no_receipt_appended(tmp_data, tmp_state, monkeypatch):
    """VNX_RECEIPT_FALLBACK=0: timeout path must NOT append a synthesized receipt."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")
    monkeypatch.setenv("VNX_RECEIPT_FALLBACK", "0")

    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(receipt=None, duration_seconds=3600.0)
    receipts_file = tmp_state / "t0_receipts.ndjson"

    with patch("dispatch_govern._git_summary", return_value="No commit; timeout."), \
         patch("dispatch_govern._git_changes", return_value="No git diff available"):
        govern(spec, raw, lane="tmux_interactive")

    if receipts_file.exists():
        lines = [l for l in receipts_file.read_text().splitlines() if l.strip()]
        synthesized = [json.loads(l) for l in lines
                       if json.loads(l).get("source") == "tmux_interactive_lane_synthesized"]
        assert synthesized == [], "VNX_RECEIPT_FALLBACK=0 must suppress synthesized receipt"


# ---------------------------------------------------------------------------
# Runtime-path regression: ensure_receipt importable under PYTHONPATH=scripts/lib only
# ---------------------------------------------------------------------------

def test_ensure_receipt_runtime_import_path(tmp_path):
    """ensure_receipt() must write a synthesized receipt even when scripts/ is NOT on sys.path.

    Replicates the real tmux runtime: dispatch.sh sets PYTHONPATH=scripts/lib only.
    Before the fix, 'from append_receipt import append_receipt_payload' raised
    ModuleNotFoundError at runtime (silently caught), so NO receipt was written.
    The fix adds _SCRIPTS_DIR to sys.path inside dispatch_govern before the import.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = state_dir / "t0_receipts.ndjson"

    scripts_lib = str(_SCRIPTS / "lib")

    # Inline script that only has scripts/lib on sys.path — no scripts/
    inline = f"""
import sys, json
from pathlib import Path

state_dir = Path({str(state_dir)!r})
receipts_file = state_dir / "t0_receipts.ndjson"

from dispatch_govern import GovernSpec, GovernRaw, ensure_receipt

spec = GovernSpec(
    dispatch_id="runtime-path-test-001",
    terminal_id="T1",
    instruction="test",
    data_dir=state_dir,
    state_dir=state_dir,
)
raw = GovernRaw(receipt=None, duration_seconds=60.0)

ensure_receipt(spec, raw, lane="tmux_interactive", report_path=None,
               contract_status="synthesized", permission_enforcement="soft")

if not receipts_file.exists():
    print("FAIL:no_receipts_file")
    sys.exit(1)
lines = [l.strip() for l in receipts_file.read_text().splitlines() if l.strip()]
if not lines:
    print("FAIL:empty_receipts_file")
    sys.exit(1)
r = json.loads(lines[-1])
if r.get("source") != "tmux_interactive_lane_synthesized":
    print(f"FAIL:wrong_source:{{r.get('source')!r}}")
    sys.exit(1)
if r.get("dispatch_id") != "runtime-path-test-001":
    print(f"FAIL:wrong_dispatch_id:{{r.get('dispatch_id')!r}}")
    sys.exit(1)
print("OK")
"""

    env = {**os.environ, "PYTHONPATH": scripts_lib}
    # Remove any scripts/ that might be inherited via PYTHONPATH
    result = subprocess.run(
        [sys.executable, "-c", inline],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"ensure_receipt failed under PYTHONPATH=scripts/lib only.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "OK" in result.stdout, f"Expected OK, got: {result.stdout!r}"
