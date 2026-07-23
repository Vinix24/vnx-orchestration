#!/usr/bin/env python3
"""Tests for scripts/lib/deliberation_panel.py — the 4-stage multi-provider deliberation.
Uses a FAKE dispatcher (records calls) so no live provider is hit."""

from __future__ import annotations

import logging
import os
import subprocess
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


class TestDigestBudget:
    def test_realistic_report_fits_in_digest(self):
        """A realistic ~4000 char report (boilerplate + analysis) must be included in full,
        not truncated to the old 1500 char budget that left only boilerplate."""
        analysis_marker = "ACTUAL_ANALYSIS_END"
        report = (
            "---\ntitle: panel report\nprovider: codex\n---\n"
            "You are one seat on a deliberation panel.\n"
            "QUESTION: audit src/\n\n## Shared context\n"
            + "\n".join(f"context line {i:02d}: lorem ipsum dolor sit amet" for i in range(30))
            + "\n\nYOUR LENS: security vulnerabilities.\n\n"
            + "Findings:\n"
            + "\n".join(f"- issue {i}: potential bug in module/{i}.py" for i in range(55))
            + f"\n{analysis_marker}"
        )
        assert 3500 < len(report) < 4500, f"report size {len(report)} outside realistic 3500-4500 band"
        fan_out = [{"provider": "codex", "lens": "security", "text": report}]
        digest = dp._digest(fan_out)
        assert analysis_marker in digest
        assert digest.count("issue ") == 55

    def test_large_context_echo_does_not_cut_analysis(self):
        """A report whose echoed provenance/context ALONE exceeds the old 6000-char budget
        (an 11.7KB --context-file reproduced the bug live) must still surface the analysis
        that comes after it — the exact failure mode that degraded the panel silently."""
        analysis_marker = "ANALYSIS_ONLY_MARKER_XYZ_789"
        echoed_context = "context line: lorem ipsum dolor sit amet consectetur adipiscing\n" * 200
        assert len(echoed_context) > 12_000, f"echo size {len(echoed_context)} must exceed 12KB"
        report = (
            "---\ntitle: panel report\nprovider: codex\n---\n"
            "## Instruction\nYou are one seat on a deliberation panel.\n"
            "QUESTION: audit src/\n\n## Shared context\n"
            + echoed_context
            + f"\n\nFindings:\n{analysis_marker}\nreal analysis text follows here."
        )
        assert len(report) > 12_000
        fan_out = [{"provider": "codex", "lens": "security", "text": report}]
        digest = dp._digest(fan_out)
        assert analysis_marker in digest, "analysis was cut off — the old truncation bug is back"


class TestReportBackstop:
    """The generous per-report backstop replaces the old normal-case 6000-char truncation.
    It must never fire on realistic reports, and when it DOES fire (pathological runaway
    report), the clip must be loud (a warning), never silent."""

    def test_report_under_backstop_passed_whole(self, caplog):
        text = "a" * 1000
        fan_out = [{"provider": "codex", "lens": "security", "text": text}]
        with caplog.at_level(logging.WARNING, logger="deliberation_panel"):
            digest = dp._digest(fan_out, limit=5000)
        assert text in digest
        assert not any("clipped" in r.message for r in caplog.records)

    def test_report_over_backstop_is_clipped_and_warns(self, caplog):
        text = "b" * 6000
        fan_out = [{"provider": "codex", "lens": "security", "text": text}]
        with caplog.at_level(logging.WARNING, logger="deliberation_panel"):
            digest = dp._digest(fan_out, limit=5000)
        body = digest.split("]\n", 1)[1]
        assert len(body) == 5000
        warnings = [r for r in caplog.records if "clipped" in r.message]
        assert warnings, "clipping must emit a loud warning, never fail silently"
        assert "codex" in warnings[0].message
        assert "VNX_PANEL_REPORT_BACKSTOP" in warnings[0].message

    def test_default_backstop_is_generous(self):
        """The default backstop is a pathological-runaway guard, not a normal-case limit —
        it must comfortably exceed any realistic single-seat report."""
        assert dp._REPORT_BACKSTOP >= 40_000

    def test_backstop_env_var_override(self):
        lib_dir = str(REPO_ROOT / "scripts" / "lib")
        code = f"import sys; sys.path.insert(0, {lib_dir!r}); import deliberation_panel as dp; print(dp._REPORT_BACKSTOP)"
        env = {**os.environ, "VNX_PANEL_REPORT_BACKSTOP": "12345"}
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "12345"

    def test_downstream_slices_use_backstop_not_old_limit(self, caplog):
        """result.contrarian / result.factcheck feeding into later stages must go through the
        same generous backstop (_clip), not the removed 6000-char slice — text well past the
        old 6000-char budget but under the default backstop must survive whole."""
        text_past_old_limit = "c" * 20_000
        with caplog.at_level(logging.WARNING, logger="deliberation_panel"):
            clipped = dp._clip(text_past_old_limit, "contrarian", limit=dp._REPORT_BACKSTOP)
        assert len(clipped) == 20_000  # far past the old 6000-char cut, still passed whole
        assert not caplog.records
