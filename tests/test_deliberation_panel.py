#!/usr/bin/env python3
"""Tests for scripts/lib/deliberation_panel.py — the 4-stage multi-provider deliberation.
Uses a FAKE dispatcher (records calls) so no live provider is hit."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import deliberation_panel as dp  # noqa: E402


class _Recorder:
    """Fake dispatcher: returns a tagged stub and records every (provider, prompt, did)."""
    def __init__(self):
        self.calls = []

    def __call__(self, provider, model, prompt, did):
        self.calls.append({"provider": provider, "prompt": prompt, "did": did})
        return f"<<{provider}:{did.split('-')[2] if did.count('-') >= 2 else did}>>"

    def stage_prompts(self, stage: str):
        return [c["prompt"] for c in self.calls if f"-{stage}-" in c["did"]]


ROSTER = [("codex", "gpt-5.5"), ("kimi", "k2"), ("claude", "sonnet")]


class TestFourStageFlow:
    def test_runs_all_four_stages(self):
        rec = _Recorder()
        res = dp.run_deliberation("sweep", "audit src/", dispatcher=rec, roster=ROSTER, max_workers=3)
        # stage 1: one fan-out per roster seat
        assert len(res.fan_out) == 3
        assert {fo["provider"] for fo in res.fan_out} == {"codex", "kimi", "claude"}
        # stages 2-4 produced text
        assert res.contrarian and res.factcheck and res.synthesis
        # exactly 3 + 1 + 1 + 1 dispatches
        assert len(rec.calls) == 6

    def test_stage_prompts_carry_lens_and_prior_context(self):
        rec = _Recorder()
        dp.run_deliberation("sweep", "audit src/", dispatcher=rec, roster=ROSTER, max_workers=3)
        # fan-out prompts mention the lens keyword "lens"
        assert all("LENS" in p for p in rec.stage_prompts("diverge"))
        # contrarian prompt embeds the fan-out digest (provider tags appear)
        contra = rec.stage_prompts("contrarian")[0]
        assert "The panel said" in contra
        # verify prompt embeds the contrarian output
        verify = rec.stage_prompts("verify")[0]
        assert "Red-team" in verify
        # synthesis embeds verification
        synth = rec.stage_prompts("synth")[0]
        assert "Verification" in synth and "Divergent views" in synth

    def test_context_injected_into_every_stage(self):
        rec = _Recorder()
        dp.run_deliberation("architecture", "design X", dispatcher=rec, roster=ROSTER,
                            context="MARKER-CTX-123", max_workers=3)
        assert all("MARKER-CTX-123" in c["prompt"] for c in rec.calls)


class TestDegradation:
    def test_one_dead_provider_does_not_kill_panel(self):
        def flaky(provider, model, prompt, did):
            if provider == "kimi":
                raise RuntimeError("kimi down")
            return "ok"
        res = dp.run_deliberation("sweep", "q", dispatcher=flaky, roster=ROSTER, max_workers=3)
        kimi = next(fo for fo in res.fan_out if fo["provider"] == "kimi")
        assert "dispatch error" in kimi["text"]
        # the other seats + later stages still ran
        assert res.synthesis == "ok"

    def test_synthesis_falls_back_when_first_seat_errors(self):
        # synthesis prefers claude; make claude error and claude's error must NOT be the
        # final synthesis — a later seat produces the real report (no unconsolidated report).
        def flaky(provider, model, prompt, did):
            if provider == "claude":
                return "[dispatch error claude: boom]"
            return f"ok-{provider}"
        res = dp.run_deliberation("architecture", "q", dispatcher=flaky, roster=ROSTER, max_workers=3)
        assert not res.synthesis.startswith("[dispatch error")
        assert res.synthesis.startswith("ok-")  # a fallback seat produced it


class TestModesAndReport:
    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            dp.run_deliberation("nonsense", "q", dispatcher=_Recorder(), roster=ROSTER)

    def test_all_modes_have_specs(self):
        for m in ("sweep", "research", "architecture", "strategy"):
            spec = dp.MODES[m]
            assert spec.lenses and spec.contrarian_focus and spec.verify_target and spec.synth_goal

    def test_report_has_all_sections(self):
        rec = _Recorder()
        res = dp.run_deliberation("strategy", "should we?", dispatcher=rec, roster=ROSTER, max_workers=3)
        report = res.to_report()
        for section in ("Synthesis", "Contrarian", "Verification", "Divergent views"):
            assert section in report


class TestPick:
    def test_prefers_present_provider(self):
        assert dp._pick(ROSTER, prefer=("deepseek-harness", "claude"))[0] == "claude"

    def test_falls_back_to_first_seat(self):
        assert dp._pick(ROSTER, prefer=("nope",))[0] == "codex"


class TestStripEcho:
    def test_whitespace_only_context_keeps_report(self):
        """A context that is only whitespace is not a real echo source; stripping must not
        empty a substantive report (HIGH edge-case #1)."""
        report = "Real findings:\n- bug in scripts/lib/deliberation_panel.py:42\n- edge case"
        assert dp._strip_echo(report, context="\n") == report

    def test_short_context_tail_in_analysis_not_stripped(self):
        """A short shared context cited by a panelist in real analysis must not be treated as
        an echoed prefix (HIGH edge-case #2)."""
        context = "scripts/lib/deliberation_panel.py"
        analysis = (
            "## Answer\n"
            "I reviewed the file and found an issue at "
            "scripts/lib/deliberation_panel.py:42.\n"
            "Conclusion: fix the off-by-one."
        )
        assert dp._strip_echo(analysis, context=context) == analysis

    def test_full_context_echo_is_stripped(self):
        """The original bug: reports that echo frontmatter + instruction + shared context
        must have that boilerplate removed, leaving the actual analysis."""
        long_context = "\n".join(f"context line {i:02d}: lorem ipsum dolor sit amet" for i in range(10))
        assert len(long_context) >= 40
        tail_marker = long_context[-50:]
        report = (
            "---\ntitle: panel report\nprovider: codex\n---\n"
            "You are one seat on a deliberation panel.\n"
            f"QUESTION: audit src/\n\n## Shared context\n{long_context}\n\n"
            "YOUR LENS: security vulnerabilities.\n\n"
            "Actual analysis: found an XSS in dashboard/login.php:23."
        )
        stripped = dp._strip_echo(report, context=long_context)
        assert tail_marker not in stripped
        assert "XSS in dashboard/login.php:23" in stripped
        # The leading boilerplate (frontmatter + shared context echo) must be gone.
        assert not stripped.startswith("---")
        assert "Shared context" not in stripped
