#!/usr/bin/env python3
"""Tests for dispatch instruction template validator (W0 PR-6).

Covers all 8 validation rules:
  D-1: Dispatch-ID format
  D-2: Description / Instruction block presence
  D-3: Scope item count thresholds
  D-4: Unbounded-task language detection
  D-5: Gate header requires Quality Gate section
  D-6: File path directory breadth
  D-7: Instruction body character size
  D-8: Gate header requires Success Criteria section
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from dispatch_instruction_validator import (
    DISPATCH_ID_RE,
    SCOPE_WARN_THRESHOLD,
    SCOPE_BLOCK_THRESHOLD,
    INSTRUCTION_SIZE_WARN,
    DIR_BREADTH_WARN,
    DispatchFinding,
    DispatchValidationResult,
    validate_dispatch_instruction,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Minimal valid dispatch — satisfies all rules
MINIMAL_VALID = """
[[TARGET:A]]
Track: A
Role: backend-developer
Gate: my-gate
Dispatch-ID: 20260422-182004-my-task-A

Instruction:
### Description
Implement the feature as described.

### Scope
- add unit tests
- update lib/foo.py
- update lib/bar.py

### Success Criteria
- All tests pass
- No regressions

### Quality Gate
`my-gate`:
- [ ] Tests pass
- [ ] Code reviewed

[[DONE]]
"""

# No Dispatch-ID present
NO_DISPATCH_ID = """
Track: A
Gate: some-gate

### Description
Do the thing.
"""

# Malformed Dispatch-ID
BAD_DISPATCH_ID = """
Dispatch-ID: not-a-valid-id

### Description
Do the thing.
"""

# No description and no Instruction: block
NO_DESCRIPTION = """
Dispatch-ID: 20260422-182004-no-desc-A
Track: A
"""

# Scope section with exactly SCOPE_WARN_THRESHOLD items
def _make_scope_dispatch(n_items: int) -> str:
    items = "\n".join(f"- scope item {i}" for i in range(n_items))
    return f"""
Dispatch-ID: 20260422-182004-scope-test-A
Track: A

### Description
Some task.

### Scope
{items}
"""

# Unbounded language dispatch
UNBOUNDED_LANGUAGE = """
Dispatch-ID: 20260422-182004-unbounded-A
Track: A

### Description
Fix all the bugs while you're at it.

### Scope
- fix all the things
"""

# Gate without Quality Gate section
GATE_NO_QUALITY = """
Dispatch-ID: 20260422-182004-gate-test-A
Track: A
Gate: missing-gate

### Description
Some task.

### Scope
- do task

### Success Criteria
- Passes
"""

# Gate without Success Criteria
GATE_NO_CRITERIA = """
Dispatch-ID: 20260422-182004-gate-crit-A
Track: A
Gate: some-gate

### Description
Some task.

### Scope
- do something

### Quality Gate
`some-gate`:
- [ ] Passes
"""

# File paths in many top-level directories
MULTI_DIR_PATHS = """
Dispatch-ID: 20260422-182004-multi-dir-A
Track: A

### Description
Update files across many directories.

### Scope
- update `scripts/lib/foo.py`
- update `tests/test_foo.py`
- update `dashboard/components/bar.py`
- update `docs/guide.py`
"""

# Over-large instruction body (>4000 chars)
LARGE_INSTRUCTION = """
Dispatch-ID: 20260422-182004-large-A
Track: A

### Description
""" + ("This is a very long dispatch instruction. " * 120)


# ---------------------------------------------------------------------------
# D-1: Dispatch-ID format
# ---------------------------------------------------------------------------

class TestDispatchIdFormat:

    def test_valid_id_no_d1_finding(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        d1 = [f for f in result.findings if f.rule == "D-1"]
        assert len(d1) == 0

    def test_missing_id_is_blocker(self) -> None:
        result = validate_dispatch_instruction(NO_DISPATCH_ID)
        d1 = [f for f in result.findings if f.rule == "D-1"]
        assert len(d1) == 1
        assert d1[0].severity == "blocker"

    def test_bad_format_id_is_blocker(self) -> None:
        result = validate_dispatch_instruction(BAD_DISPATCH_ID)
        d1 = [f for f in result.findings if f.rule == "D-1"]
        assert len(d1) == 1
        assert d1[0].severity == "blocker"

    def test_id_regex_accepts_all_tracks(self) -> None:
        for track in ("A", "B", "C"):
            dispatch_id = f"20260422-182004-task-slug-{track}"
            assert DISPATCH_ID_RE.match(dispatch_id), f"Track {track} rejected"

    def test_id_regex_rejects_bad_date(self) -> None:
        assert not DISPATCH_ID_RE.match("2026042-182004-task-A")  # short date

    def test_id_regex_rejects_lowercase_track(self) -> None:
        assert not DISPATCH_ID_RE.match("20260422-182004-task-a")

    def test_dispatch_id_returned_in_result(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        assert result.dispatch_id == "20260422-182004-my-task-A"

    def test_unknown_id_when_absent(self) -> None:
        result = validate_dispatch_instruction(NO_DISPATCH_ID)
        assert result.dispatch_id == "(unknown)"


# ---------------------------------------------------------------------------
# D-2: Description / Instruction block
# ---------------------------------------------------------------------------

class TestDescriptionRequired:

    def test_has_description_passes(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        d2 = [f for f in result.findings if f.rule == "D-2"]
        assert len(d2) == 0

    def test_missing_description_is_blocker(self) -> None:
        result = validate_dispatch_instruction(NO_DESCRIPTION)
        d2 = [f for f in result.findings if f.rule == "D-2"]
        assert len(d2) == 1
        assert d2[0].severity == "blocker"

    def test_instruction_block_satisfies_d2(self) -> None:
        content = """
Dispatch-ID: 20260422-182004-inst-A
Instruction:
Do the thing.
"""
        result = validate_dispatch_instruction(content)
        d2 = [f for f in result.findings if f.rule == "D-2"]
        assert len(d2) == 0


# ---------------------------------------------------------------------------
# D-3: Scope item count
# ---------------------------------------------------------------------------

class TestScopeItemCount:

    def test_small_scope_no_d3(self) -> None:
        result = validate_dispatch_instruction(_make_scope_dispatch(3))
        d3 = [f for f in result.findings if f.rule == "D-3"]
        assert len(d3) == 0

    def test_warn_threshold_triggers_warn(self) -> None:
        result = validate_dispatch_instruction(_make_scope_dispatch(SCOPE_WARN_THRESHOLD))
        d3 = [f for f in result.findings if f.rule == "D-3"]
        assert len(d3) == 1
        assert d3[0].severity == "warn"

    def test_block_threshold_triggers_blocker(self) -> None:
        result = validate_dispatch_instruction(_make_scope_dispatch(SCOPE_BLOCK_THRESHOLD))
        d3 = [f for f in result.findings if f.rule == "D-3"]
        assert len(d3) == 1
        assert d3[0].severity == "blocker"

    def test_below_warn_passes(self) -> None:
        result = validate_dispatch_instruction(_make_scope_dispatch(SCOPE_WARN_THRESHOLD - 1))
        d3 = [f for f in result.findings if f.rule == "D-3"]
        assert len(d3) == 0


# ---------------------------------------------------------------------------
# D-4: Unbounded language
# ---------------------------------------------------------------------------

class TestUnboundedLanguage:

    def test_clean_dispatch_no_d4(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        d4 = [f for f in result.findings if f.rule == "D-4"]
        assert len(d4) == 0

    def test_fix_all_triggers_warn(self) -> None:
        result = validate_dispatch_instruction(UNBOUNDED_LANGUAGE)
        d4 = [f for f in result.findings if f.rule == "D-4"]
        assert len(d4) >= 1
        assert all(f.severity == "warn" for f in d4)

    def test_while_youre_at_it_triggers_warn(self) -> None:
        content = """
Dispatch-ID: 20260422-182004-unbounded2-A

### Description
Fix the bug, and while you're at it clean the file.
"""
        result = validate_dispatch_instruction(content)
        d4 = [f for f in result.findings if f.rule == "D-4"]
        assert len(d4) >= 1

    def test_refactor_everything_triggers_warn(self) -> None:
        content = """
Dispatch-ID: 20260422-182004-unbounded3-A

### Description
Refactor everything in this module.
"""
        result = validate_dispatch_instruction(content)
        d4 = [f for f in result.findings if f.rule == "D-4"]
        assert len(d4) >= 1

    def test_sweep_through_triggers_warn(self) -> None:
        content = """
Dispatch-ID: 20260422-182004-sweep-A

### Description
Sweep through all the tests and update them.
"""
        result = validate_dispatch_instruction(content)
        d4 = [f for f in result.findings if f.rule == "D-4"]
        assert len(d4) >= 1


# ---------------------------------------------------------------------------
# D-5: Gate requires Quality Gate section
# ---------------------------------------------------------------------------

class TestGateRequiresQualitySection:

    def test_gate_with_quality_section_no_d5(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        d5 = [f for f in result.findings if f.rule == "D-5"]
        assert len(d5) == 0

    def test_gate_without_quality_section_is_blocker(self) -> None:
        result = validate_dispatch_instruction(GATE_NO_QUALITY)
        d5 = [f for f in result.findings if f.rule == "D-5"]
        assert len(d5) == 1
        assert d5[0].severity == "blocker"

    def test_no_gate_no_d5(self) -> None:
        content = """
Dispatch-ID: 20260422-182004-nogate-A

### Description
Do something with no gate.
"""
        result = validate_dispatch_instruction(content)
        d5 = [f for f in result.findings if f.rule == "D-5"]
        assert len(d5) == 0


# ---------------------------------------------------------------------------
# D-6: File directory breadth
# ---------------------------------------------------------------------------

class TestDirectoryBreadth:

    def test_few_dirs_no_d6(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        d6 = [f for f in result.findings if f.rule == "D-6"]
        assert len(d6) == 0

    def test_many_dirs_triggers_warn(self) -> None:
        result = validate_dispatch_instruction(MULTI_DIR_PATHS)
        d6 = [f for f in result.findings if f.rule == "D-6"]
        assert len(d6) == 1
        assert d6[0].severity == "warn"

    def test_single_dir_no_d6(self) -> None:
        content = """
Dispatch-ID: 20260422-182004-singledir-A

### Description
Update files.

### Scope
- update `scripts/lib/foo.py`
- update `scripts/lib/bar.py`
"""
        result = validate_dispatch_instruction(content)
        d6 = [f for f in result.findings if f.rule == "D-6"]
        assert len(d6) == 0


# ---------------------------------------------------------------------------
# D-7: Instruction body size
# ---------------------------------------------------------------------------

class TestInstructionBodySize:

    def test_normal_size_no_d7(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        d7 = [f for f in result.findings if f.rule == "D-7"]
        assert len(d7) == 0

    def test_large_instruction_triggers_warn(self) -> None:
        result = validate_dispatch_instruction(LARGE_INSTRUCTION)
        d7 = [f for f in result.findings if f.rule == "D-7"]
        assert len(d7) == 1
        assert d7[0].severity == "warn"


# ---------------------------------------------------------------------------
# D-8: Gate requires Success Criteria
# ---------------------------------------------------------------------------

class TestGateRequiresSuccessCriteria:

    def test_gate_with_criteria_no_d8(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        d8 = [f for f in result.findings if f.rule == "D-8"]
        assert len(d8) == 0

    def test_gate_without_criteria_is_warn(self) -> None:
        result = validate_dispatch_instruction(GATE_NO_CRITERIA)
        d8 = [f for f in result.findings if f.rule == "D-8"]
        assert len(d8) == 1
        assert d8[0].severity == "warn"

    def test_no_gate_no_d8(self) -> None:
        content = """
Dispatch-ID: 20260422-182004-nogate2-A

### Description
Simple task without gate.
"""
        result = validate_dispatch_instruction(content)
        d8 = [f for f in result.findings if f.rule == "D-8"]
        assert len(d8) == 0


# ---------------------------------------------------------------------------
# Integration: full valid dispatch produces no blockers
# ---------------------------------------------------------------------------

class TestIntegration:

    def test_minimal_valid_passes(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        assert result.passed is True
        assert result.blocker_count == 0

    def test_result_properties_are_int_and_bool(self) -> None:
        result = validate_dispatch_instruction(MINIMAL_VALID)
        assert isinstance(result.passed, bool)
        assert isinstance(result.blocker_count, int)
        assert isinstance(result.warn_count, int)

    def test_real_dispatch_example(self) -> None:
        """Validate the completed PR-1 dispatch file format (no blockers expected)."""
        real_dispatch = """
[[TARGET:B]]
Manager Block

Role: backend-developer
Track: B
Terminal: T2
Gate: gate_pr1_input_mode_recovery
Priority: P1
Cognition: normal
Risk-Class: high
Merge-Policy: human
Review-Stack: gemini_review,codex_gate,claude_github_optional
Dispatch-ID: 20260401-094055-dispatcher-input-mode-detectio-B
PR-ID: PR-1
Parent-Dispatch: none
On-Success: review
On-Failure: investigation
Reason: Dispatcher Input-Mode Detection And Recovery from PR queue
Status: pending-approval

Context: [[@FEATURE_PLAN.md]]

Instruction:
Dispatcher Input-Mode Detection And Recovery
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0]

### Description
Teach the dispatcher to detect `pane_in_mode`, attempt safe recovery, and fail closed.

### Scope
- add a tmux readiness probe for `pane_in_mode`
- explicitly detect copy/search mode before dispatch
- attempt safe recovery via tmux mode cancel
- re-check readiness after recovery
- block dispatch if readiness cannot be proven
- emit explicit audit reasons
- add tests for slash-prefixed dispatch delivery

### Success Criteria
- dispatcher never blindly sends slash-prefixed prompts into `pane_in_mode = 1`
- safe recovery is deterministic and auditable
- recovery failure blocks dispatch rather than risking corrupted delivery

### Quality Gate
`gate_pr1_input_mode_recovery`:
- [ ] All input-mode guard tests pass
- [ ] Dispatcher detects `pane_in_mode = 1` before dispatch
- [ ] Recovery path is attempted only when contract allows it

[[DONE]]
"""
        result = validate_dispatch_instruction(real_dispatch)
        assert result.blocker_count == 0, f"Unexpected blockers: {result.findings}"

    def test_constants_sane(self) -> None:
        assert SCOPE_WARN_THRESHOLD < SCOPE_BLOCK_THRESHOLD
        assert INSTRUCTION_SIZE_WARN > 0
        assert DIR_BREADTH_WARN >= 2
