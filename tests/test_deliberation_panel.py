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


class _HugeRecorder:
    """Fake dispatcher: returns a HUGE fixed reply for every call (so digest/contrarian/
    factcheck all balloon like a live cascading-verbatim run would) while still recording
    every (provider, prompt, did) so a test can inspect the ACTUAL prompt built for a
    later stage."""
    def __init__(self, size=50_000):
        self.calls = []
        self.size = size

    def __call__(self, provider, model, prompt, did):
        self.calls.append({"provider": provider, "prompt": prompt, "did": did})
        return "H" * self.size

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
        """A report whose echoed context ALONE exceeds the per-seat distillate budget must
        still surface the analysis that comes after it. An 11.7KB --context-file reproduced
        the bug live: the echo measured ~12.8K chars inside a ~13K report, and a head-first
        12000-char cut kept only the boilerplate and dropped the analysis entirely. The
        per-seat cut keeps HEAD and TAIL, so the conclusion survives even when the echo eats
        the whole budget."""
        analysis_marker = "ANALYSIS_ONLY_MARKER_XYZ_789"
        echoed_context = "context line: lorem ipsum dolor sit amet consectetur adipiscing\n" * 200
        assert len(echoed_context) > dp._SEAT_DISTILLATE_BUDGET, (
            f"echo size {len(echoed_context)} must exceed the per-seat budget "
            f"{dp._SEAT_DISTILLATE_BUDGET}"
        )
        report = (
            "---\ntitle: panel report\nprovider: codex\n---\n"
            "## Instruction\nYou are one seat on a deliberation panel.\n"
            "QUESTION: audit src/\n\n## Shared context\n"
            + echoed_context
            + f"\n\nFindings:\n{analysis_marker}\nreal analysis text follows here."
        )
        assert len(report) > dp._SEAT_DISTILLATE_BUDGET
        fan_out = [{"provider": "codex", "lens": "security", "text": report}]
        digest = dp._digest(fan_out)
        assert analysis_marker in digest, "analysis was cut off — the old truncation bug is back"

    def test_distill_keeps_head_and_tail_and_marks_the_omitted_middle(self):
        """The per-seat cut must keep the HEAD (question/framing) and the TAIL
        (analysis/conclusion) within the budget and mark the dropped middle with a marker
        naming how many chars were omitted — not a head-first cut, which drops the analysis
        when the echoed context alone exceeds the budget."""
        head_marker = "QUESTION_FRAMING_START"
        analysis_marker = "ANALYSIS_ONLY_MARKER_XYZ_789"
        echoed = "context line: lorem ipsum dolor sit amet consectetur adipiscing\n" * 200
        report = (
            head_marker + "\n## Shared context\n" + echoed
            + f"\n\nFindings:\n{analysis_marker}\nreal analysis text follows here."
        )
        assert len(report) > dp._SEAT_DISTILLATE_BUDGET
        fan_out = [{"provider": "codex", "lens": "security", "text": report}]
        digest = dp._digest(fan_out)
        assert head_marker in digest, "head (question/framing) must survive the cut"
        assert analysis_marker in digest, "tail (analysis/conclusion) must survive the cut"
        omitted = len(report) - dp._SEAT_DISTILLATE_BUDGET
        assert f"{omitted:,} middle chars omitted" in digest, (
            "the omission marker must name how many middle chars were dropped"
        )

    def test_seat_over_budget_is_distilled_loudly_per_seat(self, caplog):
        """A single seat report over the per-seat distillate budget is cut at ASSEMBLY time
        (inside _digest, OI-820) — head and tail kept within the seat budget, middle dropped
        with a marker — and the cut is logged loudly with the seat name and the seat-budget
        env var, never a silent drop."""
        report = "frontmatter + echoed context\n" + "c" * 15_000 + "\nTAIL_ANALYSIS_MARKER_789"
        fan_out = [{"provider": "kimi", "lens": "risks", "text": report}]
        with caplog.at_level(logging.WARNING, logger="deliberation_panel"):
            digest = dp._digest(fan_out)
        warnings = [r for r in caplog.records if "distillate" in r.message]
        assert warnings, "a per-seat distill cut must be logged loudly, never silently"
        assert "kimi" in warnings[0].message
        assert "VNX_PANEL_SEAT_DISTILLATE_BUDGET" in warnings[0].message
        body = digest.split("]\n", 1)[1]
        assert len(body) < 12_500  # bounded to the per-seat budget (+ short truncation notice)
        assert "[kimi / risks]" in digest  # the seat still appears, bounded

    def test_every_seat_survives_per_seat_distill(self):
        """OI-820 regression: under the old single-budget whole-digest cut the first seats
        ate all the space and the last seats vanished entirely. With a per-seat budget, EVERY
        seat — including the last — must be present in the digest even when each report is
        far larger than the budget."""
        seats = [
            {"provider": f"p{i}", "lens": f"lens{i}", "text": f"MARKER_{i}\n" + "x" * 15_000}
            for i in range(5)
        ]
        digest = dp._digest(seats)
        for i in range(5):
            assert f"[p{i} / lens{i}]" in digest, f"seat {i} missing from the digest"
            assert f"MARKER_{i}" in digest, (
                f"seat {i}'s content vanished — head-first whole-digest cut is back"
            )
        assert len(digest) > 5 * 10_000, "digest too small for 5 seats at the per-seat budget"

    def test_seat_distillate_budget_default_is_12000(self):
        """The per-seat budget default is 12k chars: at 5 seats ~60k chars carry into the
        synthesis stage, which fits a modern context window (OI-820)."""
        assert dp._SEAT_DISTILLATE_BUDGET == 12_000

    def test_seat_distillate_budget_env_var_override(self):
        lib_dir = str(REPO_ROOT / "scripts" / "lib")
        code = (
            f"import sys; sys.path.insert(0, {lib_dir!r}); import deliberation_panel as dp; "
            "print(dp._SEAT_DISTILLATE_BUDGET)"
        )
        env = {**os.environ, "VNX_PANEL_SEAT_DISTILLATE_BUDGET": "777"}
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "777"


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


class TestStageDistillation:
    """OI-809 (BLOCKER): each downstream stage must carry a BOUNDED distillate of prior
    stages, not the full verbatim material re-embedded at every hop — and the stage
    instruction must survive regardless of how large prior stages were."""

    def test_distill_passes_small_text_whole_and_silent(self, caplog):
        small = "y" * 500
        with caplog.at_level(logging.WARNING, logger="deliberation_panel"):
            out = dp._distill(small, "contrarian", limit=6000)
        assert out == small
        assert not caplog.records

    def test_distill_bounds_and_warns_on_large_text(self, caplog):
        big = "x" * 20_000
        with caplog.at_level(logging.WARNING, logger="deliberation_panel"):
            out = dp._distill(big, "contrarian", limit=6000)
        assert len(out) < 6100  # bounded to the limit (+ short truncation notice)
        warnings = [r for r in caplog.records if "distillate" in r.message]
        assert warnings, "distillation trim must emit a loud warning, never fail silently"
        assert "contrarian" in warnings[0].message
        assert "VNX_PANEL_DISTILLATE_BUDGET" in warnings[0].message

    def test_default_distillate_budget_is_much_smaller_than_report_backstop(self):
        """The distillate budget is a per-hop carry-forward cap, not the generous
        single-report backstop — it must be meaningfully smaller so cascading across
        3 downstream stages does not reproduce the old blow-up."""
        assert dp._DISTILLATE_BUDGET < dp._REPORT_BACKSTOP

    def test_distillate_budget_env_var_override(self):
        lib_dir = str(REPO_ROOT / "scripts" / "lib")
        code = (
            f"import sys; sys.path.insert(0, {lib_dir!r}); import deliberation_panel as dp; "
            "print(dp._DISTILLATE_BUDGET)"
        )
        env = {**os.environ, "VNX_PANEL_DISTILLATE_BUDGET": "999"}
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "999"

    def test_synthesis_prompt_bounded_and_instruction_survives_huge_prior_stages(self):
        """The exact OI-809 failure mode: every stage returns a HUGE report (as a live
        cascading-verbatim run would produce). The synthesis stage's prompt must (a)
        still contain its own instruction/ask, (b) stay bounded PER UNIT — each seat's
        fan-out slice capped at _SEAT_DISTILLATE_BUDGET at assembly (OI-820), each single
        prior-stage text capped at _DISTILLATE_BUDGET — with no raw 50k blob carried
        verbatim, and (c) place the instruction BEFORE the bulky context so a
        tail-truncation downstream can only ever cut context, never the task."""
        rec = _HugeRecorder(size=50_000)
        dp.run_deliberation("sweep", "audit src/", dispatcher=rec, roster=ROSTER, max_workers=3)
        synth_prompts = rec.stage_prompts("synth")
        assert len(synth_prompts) == 1
        synth_prompt = synth_prompts[0]

        # (a) the synthesis instruction/ask is present, not truncated away
        assert "SYNTHESISER" in synth_prompt
        assert "Produce" in synth_prompt and "CONSENSUS" in synth_prompt

        # (b) carried-forward material is DISTILLED (bounded per unit), not the raw huge
        # blobs — the old cascading-verbatim approach would put ~3 x 50_000 = 150_000+
        # chars of digest/contrarian/factcheck into this single prompt verbatim.
        def _section(prompt, label):
            start = prompt.index(f"--- {label} ---") + len(f"--- {label} ---")
            end = prompt.index(f"--- END {label} ---")
            return prompt[start:end]

        divergent = _section(synth_prompt, "Divergent views")
        red_team = _section(synth_prompt, "Red-team")
        verification = _section(synth_prompt, "Verification")
        # fan-out digest: 3 seats x per-seat budget (+ tags / truncation notices)
        assert len(divergent) < 3 * dp._SEAT_DISTILLATE_BUDGET + 2_000
        # single-text hops: bounded by the per-hop budget (+ truncation notice)
        assert len(red_team) < dp._DISTILLATE_BUDGET + 1_000
        assert len(verification) < dp._DISTILLATE_BUDGET + 1_000
        # no raw 50k blob survives verbatim anywhere in the prompt
        assert "H" * 40_000 not in synth_prompt

        # (c) instruction precedes the bulky context
        instr_pos = synth_prompt.index("SYNTHESISER")
        context_pos = synth_prompt.index("H" * 100)
        assert instr_pos < context_pos

    def test_all_seats_reach_the_contrarian_and_synthesis_prompts(self):
        """OI-820 regression at the stage level: the old flow distilled the WHOLE
        concatenated digest head-first at each stage transition, so seat 1 ate the budget
        and the later seats never reached the downstream stages. With per-seat distillation
        at assembly, every seat's tag must appear in the contrarian and synthesis prompts."""
        calls = []

        def big(provider, model, prompt, did):
            calls.append({"provider": provider, "prompt": prompt, "did": did})
            return f"SEAT_{provider}\n" + "y" * 15_000

        dp.run_deliberation("sweep", "audit src/", dispatcher=big, roster=ROSTER, max_workers=3)
        contra = next(c["prompt"] for c in calls if "-contrarian-" in c["did"])
        synth = next(c["prompt"] for c in calls if "-synth-" in c["did"])
        for provider, _ in ROSTER:
            assert f"[{provider} / " in contra, f"{provider} missing from the contrarian prompt"
            assert f"[{provider} / " in synth, f"{provider} missing from the synthesis prompt"

    def test_instruction_survives_a_simulated_downstream_tail_truncation(self):
        """Even if some layer downstream of this module applies its own fixed-length cut
        (the lane, or a future backstop), the now-bounded prompt's instruction — sitting
        at the front — must survive a truncation that would have destroyed it under the
        old (instruction-last) layout."""
        rec = _HugeRecorder(size=50_000)
        dp.run_deliberation("sweep", "audit src/", dispatcher=rec, roster=ROSTER, max_workers=3)
        synth_prompt = rec.stage_prompts("synth")[0]
        simulated_cut = synth_prompt[:40_000]
        assert "SYNTHESISER" in simulated_cut
        assert "Produce" in simulated_cut


class TestCoverageAwareDegradation:
    """OI-810 (warn): a failed/empty seat must yield explicit degraded coverage, never a
    phantom full-coverage render."""

    def test_failed_seat_recorded_as_degraded_coverage(self):
        def flaky(provider, model, prompt, did):
            if provider == "kimi":
                return ""  # the glm-harness class of failure: fast exit, no text
            return f"ok-{provider}"

        res = dp.run_deliberation("sweep", "q", dispatcher=flaky, roster=ROSTER, max_workers=3)
        assert len(res.present_lenses) == 2
        assert len(res.failed_seats) == 1
        assert res.failed_seats[0]["provider"] == "kimi"
        assert "2/3" in res.coverage
        assert "kimi" in res.coverage

    def test_all_seats_present_reports_full_coverage_without_failed_note(self):
        rec = _Recorder()
        res = dp.run_deliberation("sweep", "q", dispatcher=rec, roster=ROSTER, max_workers=3)
        assert res.failed_seats == []
        assert len(res.present_lenses) == 3
        assert res.coverage == "3/3 lenses present"

    def test_report_surfaces_degraded_coverage_not_phantom_full(self):
        def flaky(provider, model, prompt, did):
            if provider == "kimi":
                return ""
            return f"ok-{provider}"

        res = dp.run_deliberation("sweep", "q", dispatcher=flaky, roster=ROSTER, max_workers=3)
        report = res.to_report()
        assert "Coverage" in report
        assert "2/3" in report
        assert "SEAT FAILED" in report  # the dead seat is rendered loudly, not silently

    def test_digest_excludes_failed_seats_from_downstream_stages(self):
        rec_calls = []

        def mixed(provider, model, prompt, did):
            rec_calls.append({"provider": provider, "prompt": prompt, "did": did})
            if provider == "kimi" and "-diverge-" in did:
                raise RuntimeError("kimi down")
            return f"<<{provider}>>"

        dp.run_deliberation("sweep", "q", dispatcher=mixed, roster=ROSTER, max_workers=3)
        contra_prompts = [c["prompt"] for c in rec_calls if "-contrarian-" in c["did"]]
        assert len(contra_prompts) == 1
        prompt = contra_prompts[0]
        # the failed kimi seat's fan-out entry (its raw error text) must not leak into the
        # digest as if it were real analysis — but the coverage note (which names the
        # failed seat so the seat reasons about the true present-lens set) is expected.
        assert "dispatch error" not in prompt
        assert "PANEL COVERAGE" in prompt and "kimi" in prompt

    def test_logs_a_loud_warning_on_degraded_coverage(self, caplog):
        def flaky(provider, model, prompt, did):
            if provider == "kimi":
                return ""
            return f"ok-{provider}"

        with caplog.at_level(logging.WARNING, logger="deliberation_panel"):
            dp.run_deliberation("sweep", "q", dispatcher=flaky, roster=ROSTER, max_workers=3)
        warnings = [r for r in caplog.records if "usable report" in r.message]
        assert warnings, "a failed seat must be logged loudly, never silently"
