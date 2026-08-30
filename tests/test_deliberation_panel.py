#!/usr/bin/env python3
"""Tests for scripts/lib/deliberation_panel.py — the 4-stage multi-provider deliberation.
Uses a FAKE dispatcher (records calls) so no live provider is hit."""

from __future__ import annotations

import json
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


@pytest.fixture(autouse=True)
def _no_ambient_ledger(monkeypatch, tmp_path):
    """Hermeticity (OI-1519): without an explicit ``receipts_path``, run_deliberation
    resolves the machine's REAL receipt ledger — which on a dev machine exists and
    knows none of these tests' random dispatch-ids, flipping every seat to
    'unmeasured'. Pin the default resolution to a path that never exists so the
    pre-ledger tests exercise the legacy fallback exactly as before. Tests of the
    ledger path itself override this per-test."""
    monkeypatch.setattr(
        dp, "_default_receipts_path",
        lambda: tmp_path / "no-ledger-here" / "t0_receipts.ndjson",
        raising=False,  # pre-fix the function does not exist yet; setting it is harmless
    )


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


def _fake_report(dispatch_id, provider, exit_code, body, *, token_output=0, token_input=0, measured=None):
    """Build a realistic unified-report text (frontmatter + body), matching what
    ``_read_report`` hands back to a panel dispatcher on disk (OI-1358). ``exit_code``
    and the token fields are passed in as DATA, never hardcoded per-test, so a test can
    assert on the value it fed in rather than a literal string echoed on both sides."""
    lines = [
        "---",
        "schema_version: 1",
        f"dispatch_id: {dispatch_id}",
        f"provider: {provider}",
        "sub_provider: none",
        "model: sonnet",
        "terminal_id: T0",
        "pool_id: headless",
        "role: deliberation-panelist",
        "task_class: implementation",
        "pr_id: none",
        "duration_seconds: 12.34",
        f"exit_code: {exit_code}",
        "token_usage:",
        f"  input: {token_input}",
        f"  output: {token_output}",
        "  cache_read: 0",
        "cost_usd: 0.0",
    ]
    if measured is not None:
        lines.append(f"token_usage_measured: {'true' if measured else 'false'}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


# The measured shape of a refused seat (panel-sweep-diverge-3-e4bafa.md, OI-1358): the
# lane wrapper's generic instruction-echo body, non-empty, matching none of the three
# sentinel markers _is_error used to rely on.
REFUSAL_BODY = (
    "# Dispatch panel-refused-seat\n\n"
    "## Instruction\n\n"
    "You are one seat on a deliberation panel. Analyse the question through your lens.\n"
    "This dispatch was refused before inference ran; no analysis was produced.\n"
)


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


FIVE_ROSTER = [
    ("codex", "gpt-5.5"),
    ("kimi", "k2"),
    ("claude", "sonnet"),
    ("glm-harness", "glm-5.2"),
    ("deepseek-harness", "deepseek-v4-pro"),
]


class TestSynthesisCoverageGate:
    """OI-1154: below a minimum number of DELIVERED seats the synthesis refuses
    LOUDLY — naming delivered AND expected — instead of silently synthesizing
    over a handful of seats (e.g. 1 of 5). The gate decides on the SAME
    present/total count ``coverage`` reports (OI-1150), never its own tally."""

    def test_synthesis_refuses_below_min_seats_with_both_counts(self):
        def one_of_five(provider, model, prompt, did):
            if provider != "codex":
                return "[dispatch error: down]"
            return "ok"

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=one_of_five, roster=FIVE_ROSTER,
            max_workers=5, min_seats=3,
        )
        assert res.synthesis_refused_reason, "a 1-of-5 synthesis must be refused"
        assert "refusing synthesis" in res.synthesis_refused_reason
        assert "1/5" in res.synthesis_refused_reason  # delivered/expected both named
        assert "minimum 3" in res.synthesis_refused_reason
        # nothing was synthesized, and the downstream stages were skipped
        assert res.synthesis == ""
        assert res.contrarian == "" and res.factcheck == ""

    def test_synthesis_proceeds_at_min_seats(self):
        def three_of_five(provider, model, prompt, did):
            if provider in ("kimi", "deepseek-harness"):
                return "[dispatch error: down]"
            return f"ok-{provider}"

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=three_of_five, roster=FIVE_ROSTER,
            max_workers=5, min_seats=3,
        )
        assert res.synthesis_refused_reason == ""
        assert not res.degraded_synthesis
        assert res.synthesis  # exactly at the floor, the synthesis runs

    def test_allow_degraded_lets_one_of_five_through_visibly(self):
        def one_of_five(provider, model, prompt, did):
            if provider != "codex":
                return "[dispatch error: down]"
            return f"ok-{provider}"

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=one_of_five, roster=FIVE_ROSTER,
            max_workers=5, min_seats=3, allow_degraded=True,
        )
        assert res.synthesis_refused_reason == ""
        assert res.degraded_synthesis is True
        assert res.synthesis  # the escape lets the synthesis run degraded
        report = res.to_report()
        assert "--allow-degraded" in report  # the choice is visible in the output

    def test_refusal_is_rendered_in_report(self):
        def one_of_five(provider, model, prompt, did):
            if provider != "codex":
                return "[dispatch error: down]"
            return "ok"

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=one_of_five, roster=FIVE_ROSTER,
            max_workers=5, min_seats=3,
        )
        report = res.to_report()
        assert "REFUSED" in report
        assert "1/5" in report

    def test_no_gate_when_min_seats_is_none(self):
        """min_seats=None (the default) keeps the pre-gate behaviour: no refusal,
        no degraded flag — existing callers that bound coverage their own way are
        unaffected."""
        def one_of_five(provider, model, prompt, did):
            if provider != "codex":
                return "[dispatch error: down]"
            return f"ok-{provider}"

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=one_of_five, roster=FIVE_ROSTER, max_workers=5,
        )
        assert res.synthesis_refused_reason == ""
        assert not res.degraded_synthesis
        assert res.synthesis


class TestOutcomeBasedCoverage:
    """OI-1358: coverage must be decided from the seat's real OUTCOME (``exit_code``,
    read from its unified-report frontmatter) — not from matching its text against
    three sentinel strings. A seat that fail-closed REFUSES but still emits a non-empty
    report body (the lane wrapper's generic instruction-echo) used to slip through
    ``_is_error`` and get counted as a contributing lens."""

    def test_refused_seat_with_non_empty_body_does_not_count_as_present(self):
        """The exact bug, reproduced with a fake dispatcher: every seat gets a
        frontmatter report whose BODY is non-empty prose matching none of the three
        sentinels. ONE seat's ``exit_code`` is fed in as data (1, the refused seat); the
        rest are 0. Show the OLD sentinel-only tally and the NEW outcome-based tally
        side by side, and that they differ."""
        exit_codes = {"codex": 0, "kimi": 0, "claude": 1}  # claude: refused, non-empty body

        def dispatcher(provider, model, prompt, did):
            code = exit_codes[provider]
            body = REFUSAL_BODY if code != 0 else "real analysis body, cites file:line"
            return _fake_report(did, provider, code, body)

        res = dp.run_deliberation("sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3)

        # OLD tally: the pre-fix _is_error only looked at text shape (empty / sentinel
        # prefix) -- every seat's text here is non-empty prose matching no sentinel, so
        # the old check would have called ALL THREE present.
        def _old_is_error(text):
            t = (text or "").strip()
            return (not t) or t.startswith("[dispatch error") or t == "[empty]"

        old_present = sum(1 for fo in res.fan_out if not _old_is_error(fo["text"]))
        assert old_present == 3, "sanity: the old sentinel-only check misses the refusal"

        # NEW tally: exactly the seats whose real exit_code (fed in as data above) is 0.
        expected_present = sum(1 for code in exit_codes.values() if code == 0)
        assert expected_present == 2
        assert len(res.present_lenses) == expected_present
        assert len(res.failed_seats) == 1
        assert res.failed_seats[0]["provider"] == "claude"
        assert "2/3" in res.coverage and "claude" in res.coverage and "failed" in res.coverage

        # the refused seat's real exit_code is independently readable off its own text
        claude_text = next(fo["text"] for fo in res.fan_out if fo["provider"] == "claude")
        assert dp._seat_exit_code(claude_text) == exit_codes["claude"]
        codex_text = next(fo["text"] for fo in res.fan_out if fo["provider"] == "codex")
        assert dp._seat_exit_code(codex_text) == exit_codes["codex"]

    def test_measurement_gap_seat_still_counts_as_present(self):
        """A seat with ``exit_code=0`` but ``token_usage.output=0`` AND
        ``token_usage_measured=false`` (the measured shape of 14 real failed-panel kimi
        seats, 8 of which carried real content in a ``.partial.md`` sidecar) MUST count
        as present. Gating presence on output-token count instead of exit_code would
        flip this exact seat to failed -- the false-negative mirror of the bug this
        fix repairs."""

        def dispatcher(provider, model, prompt, did):
            if provider == "kimi":
                return _fake_report(
                    did, provider, 0, "real panel analysis text, cites source X",
                    token_output=0, measured=False,
                )
            return _fake_report(did, provider, 0, "ok")

        res = dp.run_deliberation("sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3)

        assert len(res.present_lenses) == 3
        assert res.failed_seats == []
        kimi_text = next(fo["text"] for fo in res.fan_out if fo["provider"] == "kimi")
        assert dp._seat_exit_code(kimi_text) == 0

    def test_no_frontmatter_falls_back_to_sentinel_check(self):
        """A report with NO frontmatter (e.g. a worker-authored report that
        ``emit_unified_report``'s idempotent no-overwrite path left in place) carries no
        readable outcome. The discriminator must fall back to exactly the pre-fix
        sentinel check -- no worse than today, per the dispatch instruction."""

        def dispatcher(provider, model, prompt, did):
            if provider == "kimi":
                return "[dispatch error: boom]"       # no frontmatter -> sentinel path
            if provider == "codex":
                return ""                              # no frontmatter -> sentinel path
            return "plain worker-authored report body, no frontmatter block at all"

        res = dp.run_deliberation("sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3)

        assert dp._seat_exit_code("[dispatch error: boom]") is None
        assert dp._seat_exit_code("plain worker-authored report body, no frontmatter block at all") is None
        assert len(res.present_lenses) == 1
        assert {s["provider"] for s in res.failed_seats} == {"kimi", "codex"}


class TestZeroSeatsDelivered:
    """OI-1358: a caller must be able to detect "0/N seats delivered real content"
    WITHOUT parsing the report -- independent of the min_seats gate, since a run with
    no floor configured (or with --allow-degraded) still lets a 0/N synthesis proceed."""

    def test_true_when_every_seat_is_refused(self):
        def all_refused(provider, model, prompt, did):
            return _fake_report(did, provider, 1, REFUSAL_BODY)

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=all_refused, roster=ROSTER, max_workers=3, allow_degraded=True,
        )
        assert res.present_lenses == []
        assert res.zero_seats_delivered is True

    def test_false_on_a_normal_run(self):
        rec = _Recorder()
        res = dp.run_deliberation("sweep", "q", dispatcher=rec, roster=ROSTER, max_workers=3)
        assert res.zero_seats_delivered is False

    def test_true_even_when_the_min_seats_floor_already_refused_the_synthesis(self):
        """zero_seats_delivered must still read True on the early-return path (the floor
        refusal returns before stages 2-4 run) -- present_lenses is populated before the
        gate check, so this flag is available regardless of which path returned."""

        def all_refused(provider, model, prompt, did):
            return _fake_report(did, provider, 1, REFUSAL_BODY)

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=all_refused, roster=ROSTER, max_workers=3, min_seats=1,
        )
        assert res.synthesis_refused_reason
        assert res.zero_seats_delivered is True


class TestMinSeatsFloorWithOutcomeBasedCounting:
    """The min_seats floor and allow_degraded must keep working the same way after
    switching the discriminator to outcome-based counting -- a stricter tally means the
    floor bites on real failures it used to miss (an intended behavior change per the
    dispatch's scope, not a regression), but the floor mechanics themselves are
    unaffected."""

    def test_floor_now_correctly_catches_refused_seats_with_non_empty_bodies(self):
        exit_codes = {"codex": 0, "kimi": 1, "claude": 1, "glm-harness": 1, "deepseek-harness": 0}

        def dispatcher(provider, model, prompt, did):
            code = exit_codes[provider]
            body = "real content, cites file:line" if code == 0 else REFUSAL_BODY
            return _fake_report(did, provider, code, body)

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=FIVE_ROSTER, max_workers=5, min_seats=3,
        )
        # 2/5 really delivered (codex, deepseek-harness) -- below the floor of 3, and the
        # 3 refused seats all carried non-empty bodies that the OLD check would have
        # counted as present (which would have wrongly cleared this same floor).
        assert len(res.present_lenses) == 2
        assert res.synthesis_refused_reason
        assert "2/5" in res.synthesis_refused_reason
        assert "minimum 3" in res.synthesis_refused_reason

    def test_floor_still_proceeds_at_exactly_the_floor(self):
        exit_codes = {"codex": 0, "kimi": 0, "claude": 0, "glm-harness": 1, "deepseek-harness": 1}

        def dispatcher(provider, model, prompt, did):
            code = exit_codes[provider]
            body = "real content, cites file:line" if code == 0 else REFUSAL_BODY
            return _fake_report(did, provider, code, body)

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=FIVE_ROSTER, max_workers=5, min_seats=3,
        )
        assert len(res.present_lenses) == 3
        assert res.synthesis_refused_reason == ""
        assert not res.degraded_synthesis
        assert res.synthesis


def _append_receipt(ledger_path, record):
    """Append one receipt to a tmp NDJSON ledger — mirrors the governed lane, which
    writes a seat's receipt before the dispatcher returns its report."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


class TestLedgerReconciliation:
    """OI-1519: the panel's coverage tally must be reconciled against the receipt
    LEDGER (t0_receipts.ndjson), not against the ``exit_code`` a seat writes about
    ITSELF in its own report frontmatter. Measured live on dispatch
    ``panel-sweep-diverge-0-a6421f`` (2026-08-30): the ledger says
    ``status=timeout`` / ``verdict.decision=reject`` while the report frontmatter
    says ``exit_code: 0`` — the pre-ledger tally counted that timed-out seat as a
    PRESENT lens (4/5 reported, really 3 + a timeout).

    Three branches, never two: ledger-says-failed → FAILED, ledger-says-success →
    PRESENT, ledger-has-no-decisive-record (or no ledger at all) → UNMEASURED —
    never silently counted as present on the seat's own say-so.
    """

    def test_ledger_timeout_beats_frontmatter_exit_code_zero(self, tmp_path, monkeypatch):
        """The exact measured divergence, rebuilt: seat frontmatter claims
        ``exit_code: 0``; the ledger says ``status=timeout, verdict.decision=reject``.
        The ledger must win the count AND the divergence must be reported."""
        ledger = tmp_path / "state" / "t0_receipts.ndjson"
        # Resolve via the default-path hook (no receipts_path kwarg) so this test
        # fails with a plain ASSERTION on the pre-fix tree, not a TypeError.
        monkeypatch.setattr(dp, "_default_receipts_path", lambda: ledger, raising=False)

        def dispatcher(provider, model, prompt, did):
            if provider == "kimi" and "-diverge-" in did:
                _append_receipt(ledger, {
                    "dispatch_id": did, "status": "timeout",
                    "verdict": {"decision": "reject"},
                    "failure_reason": "deadline exceeded (source: openai-api) [600.1s]",
                })
                # the lie, exactly as measured: exit_code 0 over a wrapper-echo body
                return _fake_report(did, provider, 0, REFUSAL_BODY)
            _append_receipt(ledger, {
                "dispatch_id": did, "status": "success",
                "verdict": {"decision": "accept"},
            })
            return _fake_report(did, provider, 0, "real analysis, cites file:line")

        res = dp.run_deliberation("sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3)

        assert len(res.present_lenses) == 2, (
            "a seat the ledger recorded as timeout/reject must NOT count as present "
            "just because its own frontmatter says exit_code: 0"
        )
        assert len(res.failed_seats) == 1
        assert res.failed_seats[0]["provider"] == "kimi"
        assert "2/3" in res.coverage

        # the divergence itself is the finding — visible on the result object
        assert len(res.ledger_divergences) == 1
        d = res.ledger_divergences[0]
        assert d["provider"] == "kimi"
        assert d["dispatch_id"]
        assert d["frontmatter_outcome"] == "present"
        assert d["ledger_outcome"] == "failed"
        assert d["ledger_status"] == "timeout"
        assert d["ledger_decision"] == "reject"

        # ... and in the rendered report
        report = res.to_report()
        assert "timeout" in report
        assert "reject" in report
        assert "exit_code" in report
        assert "SEAT FAILED" in report  # to_report agrees with the count

    def test_ledger_success_counts_present(self, tmp_path):
        """Branch 2: the ledger knows the dispatch-id and says success → PRESENT."""
        ledger = tmp_path / "t0_receipts.ndjson"

        def dispatcher(provider, model, prompt, did):
            _append_receipt(ledger, {"dispatch_id": did, "status": "success"})
            return _fake_report(did, provider, 0, "real analysis")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        assert res.ledger_available is True
        assert len(res.present_lenses) == 3
        assert res.failed_seats == []
        assert res.unmeasured_seats == []
        assert res.ledger_divergences == []

    def test_ledger_without_record_marks_seat_unmeasured_not_present(self, tmp_path):
        """Branch 3a: the ledger EXISTS and is readable but has NO record of this
        dispatch-id (e.g. receipt-processing lag) → the seat is UNMEASURED: not
        counted as present (fail-closed), not counted as failed (no evidence of
        failure), and visibly its own category."""
        ledger = tmp_path / "t0_receipts.ndjson"
        _append_receipt(ledger, {"dispatch_id": "someone-else", "status": "success"})

        def dispatcher(provider, model, prompt, did):
            return _fake_report(did, provider, 0, "real-looking analysis")  # writes NO receipt

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        assert res.ledger_available is True
        assert res.present_lenses == [], "an unconfirmed seat must not fail open to present"
        assert res.failed_seats == [], "no evidence of failure either — this is its own branch"
        assert len(res.unmeasured_seats) == 3
        assert {s["provider"] for s in res.unmeasured_seats} == {"codex", "kimi", "claude"}
        assert all(s["dispatch_id"] for s in res.unmeasured_seats)
        assert "unmeasured" in res.coverage
        report = res.to_report()
        assert "UNMEASURED" in report
        assert "unmeasured" in report.lower()

    def test_indecisive_receipts_are_unmeasured(self, tmp_path):
        """Branch 3b: the ledger knows the dispatch-id but every receipt is indecisive
        (status=unknown, no reject/accept decision) → UNMEASURED, not present."""
        ledger = tmp_path / "t0_receipts.ndjson"

        def dispatcher(provider, model, prompt, did):
            if "-diverge-" in did:
                _append_receipt(ledger, {
                    "dispatch_id": did, "status": "unknown",
                    "verdict": {"decision": "investigate"},
                })
            else:
                _append_receipt(ledger, {"dispatch_id": did, "status": "success"})
            return _fake_report(did, provider, 0, "real-looking analysis")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        assert res.present_lenses == []
        assert len(res.unmeasured_seats) == 3

    def test_absent_ledger_falls_back_to_legacy_measurement_visibly(self, tmp_path):
        """Point 4: no ledger at all (fresh checkout) must not crash the panel. The
        tally falls back to the pre-OI-1519 frontmatter measurement — but LOUDLY:
        the result and report flag the coverage as unverified, never silently."""
        ledger = tmp_path / "no-such-dir" / "t0_receipts.ndjson"  # never created

        def dispatcher(provider, model, prompt, did):
            if provider == "kimi" and "-diverge-" in did:
                return _fake_report(did, provider, 1, REFUSAL_BODY)
            return _fake_report(did, provider, 0, "real analysis")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        assert res.ledger_available is False
        # legacy measurement still works — the panel keeps functioning
        assert len(res.present_lenses) == 2
        assert len(res.failed_seats) == 1
        assert res.failed_seats[0]["provider"] == "kimi"
        # ... but the missing measurement instrument is visible, not silent
        report = res.to_report()
        assert "ledger" in report.lower()
        assert "unverified" in report.lower()

    def test_unreadable_ledger_path_is_legacy_mode_not_a_crash(self, tmp_path):
        """Negative path: a receipts_path that is a DIRECTORY (not a file) counts as
        'ledger unavailable' → legacy fallback, never an exception."""
        def dispatcher(provider, model, prompt, did):
            return _fake_report(did, provider, 0, "real analysis")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=tmp_path,  # a directory — unreadable as an ndjson file
        )
        assert res.ledger_available is False
        assert len(res.present_lenses) == 3

    def test_multiple_receipts_success_and_unknown_resolve_present(self, tmp_path):
        """Point 6, as measured: ``panel-architecture-diverge-1-094e47`` carries BOTH
        a success and an unknown receipt. No failure record → PRESENT."""
        ledger = tmp_path / "t0_receipts.ndjson"

        def dispatcher(provider, model, prompt, did):
            _append_receipt(ledger, {
                "dispatch_id": did, "status": "success",
                "verdict": {"decision": "investigate"},
            })
            _append_receipt(ledger, {
                "dispatch_id": did, "status": "unknown",
                "verdict": {"decision": "investigate"},
            })
            return _fake_report(did, provider, 0, "real analysis")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        assert len(res.present_lenses) == 3
        assert res.unmeasured_seats == []

    def test_multiple_receipts_failure_beats_success(self, tmp_path):
        """Point 6, fail-closed direction: when one dispatch-id carries BOTH a success
        and a failure record, the failure wins — a success record never launders a
        recorded failure."""
        ledger = tmp_path / "t0_receipts.ndjson"

        def dispatcher(provider, model, prompt, did):
            _append_receipt(ledger, {"dispatch_id": did, "status": "success"})
            if provider == "kimi" and "-diverge-" in did:
                _append_receipt(ledger, {
                    "dispatch_id": did, "status": "timeout",
                    "verdict": {"decision": "reject"},
                })
            return _fake_report(did, provider, 0, "real analysis")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        assert len(res.present_lenses) == 2
        assert [s["provider"] for s in res.failed_seats] == ["kimi"]

    def test_verdict_reject_is_the_secondary_failure_signal(self, tmp_path):
        """Point 5: ``status`` is primary, ``verdict.decision`` secondary — a receipt
        with a NON-decisive status but ``decision=reject`` still fails the seat."""
        ledger = tmp_path / "t0_receipts.ndjson"

        def dispatcher(provider, model, prompt, did):
            if provider == "kimi" and "-diverge-" in did:
                _append_receipt(ledger, {
                    "dispatch_id": did, "status": "unknown",
                    "verdict": {"decision": "reject"},
                })
            else:
                _append_receipt(ledger, {"dispatch_id": did, "status": "success"})
            return _fake_report(did, provider, 0, "real analysis")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        assert len(res.present_lenses) == 2
        assert [s["provider"] for s in res.failed_seats] == ["kimi"]

    def test_dispatch_ids_carried_on_fan_out_entries(self, tmp_path):
        """Reconciliation needs the dispatch-id: every fan-out entry must carry the
        id its seat was dispatched under (pre-fix it was built and thrown away)."""
        ledger = tmp_path / "t0_receipts.ndjson"
        seen = []

        def dispatcher(provider, model, prompt, did):
            seen.append(did)
            _append_receipt(ledger, {"dispatch_id": did, "status": "success"})
            return _fake_report(did, provider, 0, "ok")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        fan_out_ids = {fo.get("dispatch_id") for fo in res.fan_out}
        assert None not in fan_out_ids
        diverge_ids = {d for d in seen if "-diverge-" in d}
        assert fan_out_ids == diverge_ids

    def test_first_ok_reconciles_sequential_stages_against_the_ledger(self, tmp_path):
        """The second place: _first_ok serves the contrarian/verify/synthesis stages
        on the SAME _is_error that trusted the seat's own frontmatter. A stage seat
        whose frontmatter lies (exit_code 0) but whose ledger record says
        timeout/reject must be SKIPPED, and the divergence recorded."""
        ledger = tmp_path / "t0_receipts.ndjson"

        def dispatcher(provider, model, prompt, did):
            if "-contrarian-" in did and provider == "codex":
                _append_receipt(ledger, {
                    "dispatch_id": did, "status": "timeout",
                    "verdict": {"decision": "reject"},
                })
                return _fake_report(did, provider, 0, "FAKE-CONTRARIAN wrapper echo")
            _append_receipt(ledger, {"dispatch_id": did, "status": "success"})
            return _fake_report(did, provider, 0, f"real-{provider}-output")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        # contrarian prefers codex first; codex's ledger record says failed, so the
        # stage output must come from the NEXT seat, not codex's lying frontmatter
        assert "FAKE-CONTRARIAN" not in res.contrarian
        assert "real-" in res.contrarian
        stage_divergences = [d for d in res.ledger_divergences if d["stage"] == "contrarian"]
        assert len(stage_divergences) == 1
        assert stage_divergences[0]["provider"] == "codex"

    def test_unmeasured_stage_seat_falls_back_without_cascading(self, tmp_path):
        """_first_ok with a ledger that knows nothing (lag window): an unmeasured
        seat's real content is kept as the stage result rather than burning the
        whole roster and collapsing to '[empty]' — but the seats are recorded as
        unmeasured so the gap is visible."""
        ledger = tmp_path / "t0_receipts.ndjson"
        _append_receipt(ledger, {"dispatch_id": "someone-else", "status": "success"})

        def dispatcher(provider, model, prompt, did):
            return _fake_report(did, provider, 0, f"real-{provider}-output")

        res = dp.run_deliberation(
            "sweep", "q", dispatcher=dispatcher, roster=ROSTER, max_workers=3,
            receipts_path=ledger,
        )
        assert "real-" in res.contrarian
        assert "real-" in res.synthesis
        stage_unmeasured = [m for m in res.seat_measurements
                            if m["stage"] != "diverge" and m["outcome"] == "unmeasured"]
        assert stage_unmeasured, "unmeasured stage seats must be recorded, not silently accepted"
