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

Format parity (the second half of this file): the same reader also has to see a
verdict its gate wrote BARE, without a markdown fence. codex writes its verdict
that way, in an ``agent_message`` of its NDJSON stream, and a reader that demands
a fence never sees it. Reading bare objects must not loosen the rule above, so
the classes below pin both halves: a bare verdict is read, and everything the
fenced reader already refused (echoed template, unknown value, no verdict key)
is still refused when it is bare.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "gate_verdict"
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from codex_parser import extract_verdict_block
from gate_lane_contract import VALID_VERDICTS, VERDICT_CONTRACT
from gate_runner import _REVIEWER_VERDICT_TEMPLATE


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


# ---------------------------------------------------------------------------
# Format parity: a verdict written bare, without a fence
# ---------------------------------------------------------------------------

_FENCED_PASS = '```json\n{"verdict": "pass", "findings": []}\n```'
_BARE_PASS = '{"verdict": "pass", "findings": []}'
_BARE_FAIL = '{"verdict": "fail", "findings": []}'


def _template_body(template: str) -> str:
    """The JSON object a verdict template asks for, without its fence markers.

    Cut out of the REAL template constant, never a hand-copied duplicate: a copy
    drifts from the original and the test would stop covering anything real.
    """
    return template.split("```json\n", 1)[1].split("\n```", 1)[0]


class TestABareVerdictIsReadLikeAFencedOne:
    """codex writes its verdict bare. Before this fix the reader only saw a
    verdict inside a ```json fence, so a valid codex verdict was invisible to it.
    """

    def test_real_codex_stream_with_a_bare_verdict_is_read(self):
        """The real stdout of codex_gate on PR 1869 (captured 2026-09-19): seven
        NDJSON events, no fence anywhere, the verdict bare in the last
        ``agent_message``. Not a fixture built for this test.
        """
        stdout = (FIXTURES_DIR / "pr-1869-codex_gate-bare-verdict.ndjson").read_text(encoding="utf-8")
        assert "```" not in stdout, "fixture drifted: the real codex stream carries no fence at all"
        assert '"type":"agent_message"' in stdout, "fixture drifted: the verdict must sit in an agent_message"

        result = extract_verdict_block(stdout)

        assert result.get("verdict") == "pass", f"bare verdict in the real codex stream was not read, got {result}"
        assert result.get("findings") == []
        assert result.get("residual_risk", "").startswith("Diff-only review found no ADR")

    def test_bare_verdict_in_a_plain_text_report_is_read(self):
        """The plain-text shape glm/kimi/deepseek reports have: prose, then the
        verdict object on its own lines, with its findings intact.
        """
        stdout = (
            "Review klaar. Er is een blokkerend probleem.\n"
            '{"verdict": "fail", "findings": [{"severity": "error", "message": "missing null check", '
            '"file_path": "scripts/lib/x.py", "line": 12}], "residual_risk": null}\n'
        )

        result = extract_verdict_block(stdout)

        assert result.get("verdict") == "fail", f"got {result}"
        assert result["findings"][0]["file_path"] == "scripts/lib/x.py"
        assert result["findings"][0]["line"] == 12

    def test_bare_verdict_on_the_same_line_as_prose_is_read(self):
        stdout = f"Uitkomst van de review: {_BARE_PASS} en verder niets."

        assert extract_verdict_block(stdout).get("verdict") == "pass"

    def test_bare_verdict_in_a_top_level_agent_message_event_is_read(self):
        """The other event shape ``_extract_codex_text`` unwraps: ``type`` and
        ``text`` at the top level instead of inside an ``item``.
        """
        stdout = json.dumps({"type": "agent_message", "text": 'Klaar.\n{"verdict": "blocked", "findings": []}'})

        assert extract_verdict_block(stdout).get("verdict") == "blocked"


class TestFencedAndBareBlocksShareOneOrder:
    """The LAST valid verdict wins, whether or not it was fenced. Two separate
    scans (fenced first, bare as a fallback) would let a fenced block beat a
    later bare one, or the reverse, depending on which scan ran first.
    """

    def test_a_later_bare_verdict_beats_an_earlier_fenced_one(self):
        stdout = f"Eerste indruk:\n```json\n{_BARE_PASS}\n```\nNa nader onderzoek:\n{_BARE_FAIL}\n"

        assert extract_verdict_block(stdout).get("verdict") == "fail"

    def test_a_later_fenced_verdict_beats_an_earlier_bare_one(self):
        stdout = f"Eerste indruk:\n{_BARE_FAIL}\nNa nader onderzoek:\n{_FENCED_PASS}\n"

        assert extract_verdict_block(stdout).get("verdict") == "pass"

    def test_of_two_bare_verdicts_the_last_wins(self):
        stdout = f"{_BARE_PASS}\nHerzien:\n{_BARE_FAIL}\n"

        assert extract_verdict_block(stdout).get("verdict") == "fail"

    def test_of_two_fenced_verdicts_the_last_wins(self):
        """Control: this order rule existed for fenced blocks before the change."""
        stdout = f"```json\n{_BARE_PASS}\n```\nHerzien:\n```json\n{_BARE_FAIL}\n```\n"

        assert extract_verdict_block(stdout).get("verdict") == "fail"


_TEMPLATES = pytest.mark.parametrize(
    "template",
    [VERDICT_CONTRACT, _REVIEWER_VERDICT_TEMPLATE],
    ids=["harness-lane-VERDICT_CONTRACT", "codex-gemini-REVIEWER_TEMPLATE"],
)


@_TEMPLATES
class TestAnEchoedTemplateIsStillRefusedWhenBare:
    """The strictness OI-1767 introduced must survive reading bare objects. A
    worker that echoes its own instructions and dies must not read as a verdict,
    whether the echo kept its fence or lost it. Both contracts are covered: the
    harness-lane one, and the codex/gemini one whose gate this reader is next
    in line to serve.
    """

    def test_the_template_body_is_valid_json_whose_only_defect_is_the_placeholder(self, template):
        """Makes the refusals below non-vacuous: if the body did not parse, they
        would pass for the wrong reason (unparseable, not rejected on value).
        """
        body = json.loads(_template_body(template))

        assert body["verdict"] == "pass|fail|blocked"
        assert body["verdict"] not in VALID_VERDICTS

    def test_a_bare_echoed_template_then_death_is_refused(self, template):
        stdout = (
            "Ik ga de diff reviewen. Mijn opdracht is:\n"
            + _template_body(template)
            + "\n...De bevindingen zijn geen blokkerende problemen. Het oordeel is geslaagd. Laat me het neerschrijven."
        )

        result = extract_verdict_block(stdout)

        assert result == {}, f"a bare echoed template must not read as a verdict, got {result}"

    def test_a_fenced_echoed_template_then_death_is_refused(self, template):
        stdout = "Ik ga de diff reviewen. Mijn opdracht is:\n" + template + "...Laat me het neerschrijven."

        assert extract_verdict_block(stdout) == {}

    def test_a_bare_echoed_template_inside_an_ndjson_agent_message_is_refused(self, template):
        text = "Mijn instructies zijn:\n" + _template_body(template) + "\n...Ik schrijf het nu op."
        stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})

        assert extract_verdict_block(stdout) == {}

    def test_a_bare_echoed_template_followed_by_a_real_bare_verdict_returns_the_real_one(self, template):
        stdout = (
            "Mijn opdracht is:\n"
            + _template_body(template)
            + f"\nNa onderzoek:\n{_BARE_FAIL}\n"
        )

        assert extract_verdict_block(stdout).get("verdict") == "fail"


class TestABareObjectNeedsARealVerdict:
    """The same closed set the fenced reader uses. ``_extract_codex_verdict``
    accepts any object with a ``verdict`` OR a ``findings`` key, value unchecked:
    that is the looseness this reader must not inherit.
    """

    def test_an_unknown_verdict_value_is_refused(self):
        assert extract_verdict_block('{"verdict": "maybe", "findings": []}') == {}

    def test_an_object_with_findings_but_no_verdict_key_is_refused(self):
        assert extract_verdict_block('{"findings": [], "residual_risk": null}') == {}

    def test_the_verdict_value_is_trimmed_and_lowercased_before_the_check(self):
        result = extract_verdict_block('{"verdict": " PASS ", "findings": []}')

        assert result.get("verdict") == " PASS ", f"got {result}"

    def test_an_invalid_trailing_object_does_not_hide_an_earlier_valid_verdict(self):
        """Scanning from the end means the last VALID object, not the last object."""
        stdout = f'{_BARE_PASS}\n{{"verdict": "maybe", "findings": []}}\n'

        assert extract_verdict_block(stdout).get("verdict") == "pass"

    def test_a_verdict_key_nested_in_a_finding_does_not_beat_the_outer_verdict(self):
        """Only top-level objects are candidates. A finding that happens to carry
        its own ``verdict`` key sits LATER in the text than the outer verdict and
        would win a naive "last object with a verdict key" scan.
        """
        stdout = (
            '{"verdict": "pass", "findings": [{"severity": "info", "message": "m", '
            '"verdict": "fail"}], "residual_risk": null}'
        )

        result = extract_verdict_block(stdout)

        assert result.get("verdict") == "pass", f"nested verdict key was read instead of the outer one: {result}"


class TestMalformedInputNeverRaisesAndNeverInventsAVerdict:

    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            "   \n\n",
            "prose with { a brace and } another one",
            "{{{{",
            '{"verdict": "pass", "findings": [',
            '{"verdict": "pass"',
            '{"verdict": pass}',
        ],
        ids=["empty", "whitespace", "braces-in-prose", "open-braces", "truncated-array", "truncated-object", "unquoted-value"],
    )
    def test_returns_an_empty_dict(self, stdout):
        assert extract_verdict_block(stdout) == {}
