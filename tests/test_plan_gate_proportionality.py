"""Proportionality reaches the reviewer and survives the existing result contract."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
import plan_gate_panel as pgp


SKILL = Path(__file__).resolve().parents[1] / "skills/proportionality/SKILL.md"
PREFIXES = ("missing_risk_class", "unjustified_mechanism", "no_removal_list")
SEAT = {"label": "codex", "provider": "codex", "model_arg": "gpt-6-astra"}


@pytest.fixture
def skill_path(tmp_path, monkeypatch):
    path = tmp_path / "SKILL.md"
    path.write_text(SKILL.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(pgp, "_PROPORTIONALITY_SKILL_PATH", path, raising=False)
    return path


def test_panel_delivers_current_rubric_and_preserves_counted_finding(skill_path, tmp_path):
    finding = "no_removal_list: Deliverable B1 has no explicit removal list."
    seen = []

    def dispatch(provider, model, instruction, dispatch_id):
        seen.append(instruction)
        return "```vnx-plan-verdict\n" + json.dumps({
            "verdict": "revise", "blocking_findings": [finding],
            "rationale": "The plan omits its removal list.",
        }) + "\n```"

    for revision in ("First rubric reading.", "Updated rubric reading."):
        source = SKILL.read_text(encoding="utf-8").replace(
            "### 2. Plan-gate: the plan reviewer\n",
            "### 2. Plan-gate: the plan reviewer\n\n" + revision + "\n",
        )
        skill_path.write_text(source, encoding="utf-8")
        result = pgp.run_panel(
            doc_text="## Deliverables\nB1: implement the user flow.\n",
            track_id="proportionality", project_id="p1", panel=[SEAT],
            dispatcher=dispatch, data_dir=str(tmp_path / "data"),
            seat_ledger_path=tmp_path / "seats.ndjson",
        )
        instruction = seen[-1]
        section = source.split("### 2. Plan-gate: the plan reviewer\n", 1)[1]
        section = section.split("### 3. Code review gate:", 1)[0].strip()
        assert section in instruction
        assert revision in instruction
        for line in source.splitlines():
            if line.startswith(("1. **Which", "2. **Who", "3. **How", "| T", "| O")):
                assert line in instruction
        assert "### 3. Code review gate:" not in instruction
        assert result["decision"] == "REVISE"
        findings = result["panelists"][0]["blocking_findings"]
        assert findings == [finding]
        assert pgp.count_blocking_findings(findings)["no_removal_list"] == 1
        assert "no_removal_list=1" in result["summary"]["rationale"]
        assert "finding_counts" not in result
        assert "finding_counts" not in result["panelists"][0]
        assert pgp.seat_verdicts_of(result["panelists"])[0]["findings"] == [finding]
        records = [json.loads(line) for line in (tmp_path / "seats.ndjson").read_text().splitlines()]
        assert all("finding_counts" not in record for record in records)
    assert len(seen) == 2


def test_fileref_uses_same_rubric(skill_path):
    instruction = pgp.build_plan_review_instruction_fileref("/plan.md", "p1", "/report.md")
    section = skill_path.read_text(encoding="utf-8").split(
        "### 2. Plan-gate: the plan reviewer\n", 1,
    )[1].split("### 3. Code review gate:", 1)[0].strip()
    assert section in instruction
    assert "| T7 — Delivered user outcome" in instruction
    assert "| O6 — Hardening" in instruction


@pytest.mark.parametrize("damage", [
    "missing", "unreadable", "encoding", "empty", "section", "empty_section",
])
def test_bad_skill_blocks_before_dispatch(skill_path, tmp_path, damage):
    if damage == "missing":
        skill_path.rename(tmp_path / "moved.md")
    elif damage == "unreadable":
        skill_path.chmod(0)
    elif damage == "encoding":
        skill_path.write_bytes(b"\xff")
    elif damage == "empty":
        skill_path.write_text("", encoding="utf-8")
    elif damage == "section":
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8").replace(
                "### 2. Plan-gate: the plan reviewer", "### Unknown section",
            ), encoding="utf-8",
        )
    else:
        source = skill_path.read_text(encoding="utf-8")
        start = source.index("## Three questions\n") + len("## Three questions\n")
        end = source.index("## Too thin: risk classes\n")
        skill_path.write_text(source[:start] + source[end:], encoding="utf-8")
    calls = []

    def dispatch(*args):
        calls.append(args)
        return '```vnx-plan-verdict\n{"verdict":"pass","blocking_findings":[]}\n```'

    try:
        result = pgp.run_panel(
            doc_text="## Plan\n", track_id="proportionality", project_id="p1",
            panel=[SEAT], dispatcher=dispatch, data_dir=str(tmp_path / "data"),
            seat_ledger_path=tmp_path / "seats.ndjson",
        )
        assert result["decision"] == "INFRA_FAIL"
        error = "unavailable" if damage in {"missing", "unreadable", "encoding"} else "invalid"
        assert f"proportionality_rubric_{error}:" in result["summary"]["rationale"]
        assert str(skill_path) in result["summary"]["rationale"]
        assert result["panelists"] == []
        assert calls == []
        assert not (tmp_path / "seats.ndjson").exists()
        with pytest.raises(pgp.ProportionalityRubricError, match="proportionality_rubric_"):
            pgp.build_plan_review_instruction_fileref("/plan.md", "p1", "/report.md")
    finally:
        if damage == "unreadable":
            skill_path.chmod(0o600)


@pytest.mark.parametrize("findings, expected", [
    ([], [0, 0, 0, 0]),
    (["missing_risk_class: T7 proof"], [1, 0, 0, 0]),
    (["unjustified_mechanism: no caller"], [0, 1, 0, 0]),
    (["no_removal_list: missing"], [0, 0, 1, 0]),
    (["ordinary issue"], [0, 0, 0, 1]),
    (["unknown: issue", "no_removal_list_extra: issue", "no_removal_list"], [0, 0, 0, 3]),
    (["no_removal_list: first", "no_removal_list: second", "else"], [0, 0, 2, 1]),
])
def test_count_blocking_findings(findings, expected):
    original = list(findings)
    assert pgp.count_blocking_findings(findings) == dict(zip((*PREFIXES, "other"), expected))
    assert findings == original


@pytest.mark.parametrize("findings", [None, "no_removal_list: issue", {}, [None], [42]])
def test_count_rejects_malformed_findings(findings):
    with pytest.raises(ValueError, match="blocking_findings must be a list of strings"):
        pgp.count_blocking_findings(findings)


def test_loader_uses_explicit_path_and_not_working_directory(skill_path, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VNX_HOME", str(tmp_path / "wrong-checkout"))
    monkeypatch.setattr(pgp, "_PROPORTIONALITY_SKILL_PATH", tmp_path / "missing.md")
    assert "missing_risk_class:" in pgp.load_proportionality_rubric(skill_path)
    with pytest.raises(pgp.ProportionalityRubricError, match="proportionality_rubric_unavailable"):
        pgp.load_proportionality_rubric()
