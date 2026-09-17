"""OI-1767 fix-forward: extract_verdict_block must not be fooled by an echoed template.

Third fix-forward on PR #1864. ``extract_verdict_block`` (scripts/lib/codex_parser.py)
delegated to ``_extract_codex_verdict``, which takes the FIRST fenced ```json block
and accepts any dict carrying a "verdict" key, value unchecked. ``VERDICT_CONTRACT``
(gate_lane_contract.py) is itself a fenced ```json block with a "verdict" key whose
value is the literal placeholder text ``"pass|fail|blocked"``. A worker that echoes
its own instructions and then dies mid-report hands back exactly that block first —
before this fix, that read as a genuine, blocking-free verdict.

These tests build the echoed-template scenario with the REAL ``VERDICT_CONTRACT``
constant, never a hand-copied duplicate: a copy drifts from the original and the
test would stop covering anything real.

glm_gate._extract_verdict and kimi_gate._extract_verdict already solved this by
scanning fenced blocks from the END and validating the verdict value against a
closed set. ``extract_verdict_block`` now applies the identical rule, sharing the
set (``gate_lane_contract.VALID_VERDICTS``) rather than adding a fourth literal copy.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from codex_parser import extract_verdict_block
from gate_lane_contract import VALID_VERDICTS, VERDICT_CONTRACT


class TestEchoedTemplateIsNeverMistakenForAVerdict:

    def test_echoed_template_then_death_mid_sentence_yields_no_verdict_block(self):
        """Red before the fix: this scenario used to return the template's own
        placeholder dict, ``{"verdict": "pass|fail|blocked", ...}`` — a dict
        with a "verdict" key, so the old first-match/any-value check accepted
        it. It is not a real decision, so this must return ``{}``.
        """
        stdout = (
            "Ik ga de diff reviewen. Mijn opdracht is:\n"
            + VERDICT_CONTRACT
            + "...De bevindingen zijn geen blokkerende problemen. Het oordeel "
            "is geslaagd. Laat me het neerschrijven."
        )

        result = extract_verdict_block(stdout)

        assert result == {}, (
            f"echoed VERDICT_CONTRACT template must not read as a real verdict, got {result}"
        )

    def test_template_echo_followed_by_a_real_verdict_returns_the_real_one(self):
        """Control 1: a worker that echoes the template and THEN writes a real
        verdict must resolve to the real one, not the template placeholder.
        Proves the fix picks the LAST valid block, not merely "no blocks ever".
        """
        stdout = (
            "Ik ga de diff reviewen. Mijn opdracht is:\n"
            + VERDICT_CONTRACT
            + "...Na onderzoek is er geen blokkerend probleem gevonden.\n"
            '```json\n{"verdict": "pass", "findings": [], "residual_risk": null}\n```\n'
        )

        result = extract_verdict_block(stdout)

        assert result.get("verdict") == "pass", f"expected the real trailing verdict, got {result}"
        assert result.get("findings") == []

    def test_only_a_real_verdict_is_unaffected(self):
        """Control 2: a report with only a real verdict block (no echoed
        template ahead of it) must keep working exactly as before.
        """
        stdout = (
            "Review complete. No blocking issues.\n"
            '```json\n{"verdict": "fail", "findings": '
            '[{"severity": "high", "message": "missing null check"}]}\n```\n'
        )

        result = extract_verdict_block(stdout)

        assert result.get("verdict") == "fail"
        assert result.get("findings") == [{"severity": "high", "message": "missing null check"}]

    def test_template_value_itself_is_not_a_valid_verdict(self):
        """VERDICT_CONTRACT's own placeholder text must never satisfy the
        closed set of real verdicts — the whole defect hinges on this.
        """
        assert "pass|fail|blocked" not in VALID_VERDICTS

    def test_no_block_at_all_returns_empty_dict(self):
        """Baseline: plain prose with no fenced json block at all is unchanged."""
        assert extract_verdict_block("Just some prose, no fences here.") == {}

    def test_ndjson_wrapped_echoed_template_is_also_refused(self):
        """extract_verdict_block runs the NDJSON-unwrap (_extract_codex_text)
        before scanning for fences, so the same refusal must hold when the
        echoed template arrives wrapped in a codex ``agent_message`` event,
        not just as a bare string — this is the shape codex's own stream
        actually produces.
        """
        import json

        text = (
            "Reviewing the diff. My instructions are:\n"
            + VERDICT_CONTRACT
            + "...no blocking issues found. Writing it down now."
        )
        stdout = json.dumps({"type": "agent_message", "text": text})

        result = extract_verdict_block(stdout)

        assert result == {}, f"NDJSON-wrapped echoed template must also be refused, got {result}"
