"""tests/test_review_stack_default_composition.py — dispatch
20260914-poorten-punt3-kimi: the default review-stack must not name a gate
that can never deliver a verdict, and deepseek_gate's harness-lane
registration (the #1714 repair) must be pinned so it cannot silently
regress.

Measured by T0 on 14-09 against the live store: the old default
``gemini_review,codex_gate,claude_github_optional``
(scripts/lib/config_registry.py) named three gates of which two could never
produce an oordeel — gemini_review (binary not on PATH, one record in
fourteen days) and claude_github_optional (never configured: no
``.github/workflows/claude*.yml`` exists and VNX_CLAUDE_GITHUB_REVIEW_ENABLED
resolves to None outside tests, so all 23 of its records are
``claude_github_not_configured``) — while the gates that DO deliver verdicts
(glm_gate: 86 pass on 94 records; kimi_gate; deepseek_gate) were not in the
default stack at all. The registry default is now ``codex_gate,glm_gate``.

Both tests fail on a VALUE, never on a missing symbol:

- Test A goes red when the registry default is set back to the old value
  (``claude_github_optional`` reappears in the stack). It is deliberately
  NOT hung on PATH: whether ``codex`` or ``gemini`` happens to be installed
  is a property of the machine, and CI has neither.
- Test B goes red when gate_recorder.GATE_PROVIDERS["deepseek_gate"] is set
  back to the script-runner form ("script_runner", "scripts/deepseek_gate.py")
  — exactly the registration #1714 repaired (the file has never existed, so
  availability flips back to False and the kind is no longer harness_lane),
  a regression nothing else guarded.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    """Isolate config_registry's process-global DB-resolver/project state
    from whatever another test module left wired -- same idiom
    tests/test_beta3_e1_review_gate_chain.py's ``_clean_config_registry``
    uses. Without this, a resolver left wired by an earlier test in the same
    pytest session could leak an unrelated project's
    VNX_DEFAULT_REVIEW_STACK override into Test A.
    """
    import config_registry as cr
    import config_runtime as crt
    for k in list(cr.CONFIG_REGISTRY):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(k)}", raising=False)
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


def test_default_review_stack_names_no_never_configured_gate():
    """Test A: the default review stack contains no gate that was never
    configured, is not empty, and names only registered gates.

    Measures the REGISTRY DEFAULT (the isolation fixture above clears any
    env/DB override), so reverting the registry default to the old
    ``gemini_review,codex_gate,claude_github_optional`` value turns the
    claude_github_optional assertion red -- a value failure, not a missing
    symbol.
    """
    import gate_recorder
    import review_gate_manager as rgm

    stack = rgm._build_default_review_stack()
    assert stack, "the default review stack must not be empty"
    assert "claude_github_optional" not in stack, (
        "claude_github_optional was never configured (no claude*.yml workflow, "
        "VNX_CLAUDE_GITHUB_REVIEW_ENABLED resolves to None outside tests) -- it "
        "does not belong in the DEFAULT stack; it stays in "
        "gate_recorder.GATE_PROVIDERS so historical requests keep their "
        "deliberate-design label instead of rebooking as unsupported_gate_type"
    )
    unknown = [name for name in stack if name not in gate_recorder.GATE_PROVIDERS]
    assert not unknown, (
        f"every default-stack gate must be a registered provider, got {unknown} "
        f"in {stack}"
    )


def test_deepseek_gate_is_available_as_harness_lane():
    """Test B: deepseek_gate is available by REGISTRATION alone, as a
    harness-lane gate -- the exact shape #1714 shipped and #1838 relies on.

    Fails twice over on the pre-#1714 registration ("script_runner",
    "scripts/deepseek_gate.py"): the kind assertion, and
    gate_is_available() itself, because that file has never existed on disk.
    """
    import gate_recorder

    assert gate_recorder.GATE_PROVIDERS["deepseek_gate"][0] == (
        gate_recorder.GATE_PROVIDER_HARNESS_LANE
    ), (
        "deepseek_gate must stay registered as a harness-lane gate -- the "
        "script-runner form pointed at scripts/deepseek_gate.py, which has "
        "never existed, and booked gate_runner_missing for the one provider "
        "that worked (OI-1714)"
    )
    assert gate_recorder.gate_is_available("deepseek_gate") is True
