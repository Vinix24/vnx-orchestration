"""GE-5a: one GovernedLane adapter — shared parser, report reader, dispatcher wiring.

The golden corpus under ``tests/fixtures/governed_lane/`` holds REAL report
texts (provenance in ``SOURCES.md``) and the output the pre-GE-5a per-module
parser produced for each, committed before any code moved. The adapter's
``extract_verdict(text, provider)`` must reproduce every golden field by field,
and ``plan_gate_panel`` must keep handing out the same objects under its old
names so no caller changes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import dispatch_worktree_isolation as dwi  # noqa: E402  (after the sys.path pins above)
import glm_gate  # noqa: E402
import governed_lane  # noqa: E402
import kimi_gate  # noqa: E402
import plan_gate_panel  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "governed_lane"
GOLDENS = FIXTURES / "goldens"
CORPUS = [
    p for p in sorted(FIXTURES.glob("*.md"))
    if p.name.startswith(("glm-", "kimi-"))
]


def _provider(fixture: Path) -> str:
    return "glm" if fixture.name.startswith("glm-") else "kimi"


def _golden(fixture: Path) -> dict:
    return json.loads((GOLDENS / f"{fixture.stem}.json").read_text(encoding="utf-8"))


def _advisory_only(golden: dict) -> bool:
    findings = golden.get("findings") or []
    return (
        golden.get("verdict") == "pass"
        and bool(findings)
        and all(str(f.get("severity", "")).lower() in {"warning", "info"} for f in findings)
    )


def test_corpus_covers_both_providers_and_every_verdict_shape():
    by_provider = {provider: [f for f in CORPUS if _provider(f) == provider]
                   for provider in ("glm", "kimi")}
    assert 8 <= len(by_provider["kimi"]) <= 12
    assert 4 <= len(by_provider["glm"]) <= 6

    goldens = {f.stem: _golden(f) for f in CORPUS}
    for provider in ("glm", "kimi"):
        ours = {name: g for name, g in goldens.items() if name.startswith(f"{provider}-")}
        assert any(g.get("verdict") == "pass" and not g.get("findings") for g in ours.values())
        assert any(_advisory_only(g) for g in ours.values())
        assert any(
            g.get("verdict") == "fail"
            and any(str(f.get("severity", "")).lower() == "error" for f in g.get("findings") or [])
            for g in ours.values()
        )
        assert ours[f"{provider}-truncated"] == {}


@pytest.mark.parametrize("fixture", CORPUS, ids=lambda f: f.stem)
def test_extract_verdict_reproduces_the_pre_ge5a_golden(fixture):
    text = fixture.read_text(encoding="utf-8")

    assert governed_lane.extract_verdict(text, _provider(fixture)) == _golden(fixture)


def test_glm_and_kimi_module_aliases_read_through_the_register():
    text = (FIXTURES / "kimi-fail-blocking.md").read_text(encoding="utf-8")

    assert glm_gate._extract_verdict(text) == governed_lane.extract_verdict(text, "glm")
    assert kimi_gate._extract_verdict(text) == governed_lane.extract_verdict(text, "kimi")


def test_the_register_names_the_two_standalone_provider_parsers():
    assert set(governed_lane.GOVERNED_LANE_PROVIDERS) == {"glm", "kimi"}


@pytest.mark.parametrize("provider", [None, "", "deepseek", "codex", "kimi_gate"])
def test_an_unknown_provider_is_refused_not_answered(provider):
    with pytest.raises(ValueError):
        governed_lane.extract_verdict('```json\n{"verdict": "pass"}\n```', provider)


@pytest.mark.parametrize("text, expected", [
    (None, {}),
    ("", {}),
    ("no fence at all", {}),
    ("```json\n{not json}\n```", {}),
    ("```json\n[1, 2]\n```", {}),
    ('```json\n{"verdict": "maybe"}\n```', {}),
    ('```json\n{"verdict": "pass|fail|blocked"}\n```', {}),
])
def test_missing_or_malformed_text_is_never_a_verdict(text, expected):
    assert governed_lane.extract_verdict(text, "kimi") == expected


def test_the_last_real_fence_wins_over_the_echoed_contract_template():
    text = (
        '```json\n{"verdict": "pass|fail|blocked", "findings": []}\n```\n'
        "review prose\n"
        '```json\n{"verdict": "fail", "findings": [{"severity": "error", "message": "x"}]}\n```\n'
    )

    assert governed_lane.extract_verdict(text, "glm")["verdict"] == "fail"


def test_the_panel_reexports_the_same_dispatcher_and_reader():
    assert plan_gate_panel._make_default_dispatcher is governed_lane._make_default_dispatcher
    assert plan_gate_panel._read_report is governed_lane.read_report


def test_read_report_accepts_only_the_exact_dispatch_id_and_falls_back(tmp_path):
    reports = tmp_path / "unified_reports"
    reports.mkdir()
    (reports / "d1.md").write_text("FALLBACK", encoding="utf-8")
    foreign = tmp_path / "other.md"
    foreign.write_text("FOREIGN", encoding="utf-8")

    assert governed_lane.read_report(tmp_path, "d1", f"Report: {foreign}") == "FALLBACK"
    assert governed_lane.read_report(tmp_path, "d1", "") == "FALLBACK"
    assert governed_lane.read_report(tmp_path, "missing", "") is None
    assert governed_lane.read_report(None, "missing", "") is None


def test_the_moved_dispatcher_still_builds_the_provider_lane_command(monkeypatch, tmp_path):
    seen = {}

    def fake_child(cmd, *, env, timeout_seconds, dispatch_id, data_dir):
        seen.update(cmd=list(cmd), dispatch_id=dispatch_id, data_dir=data_dir)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plan_gate_panel, "_resolve_data_dir", lambda _data_dir: tmp_path)
    monkeypatch.setattr(plan_gate_panel, "_run_provider_lane_child", fake_child)
    monkeypatch.setattr(plan_gate_panel, "_read_report", lambda *a, **k: "REPORT TEXT")

    dispatcher = governed_lane._make_default_dispatcher(
        None, 42, role="review-gate", base_ref="origin/pr-branch")
    assert dispatcher("kimi", "kimi-k3", "INSTRUCTION", "kimi-gate-pr-1") == "REPORT TEXT"

    cmd = seen["cmd"]
    assert cmd[cmd.index("--provider") + 1] == "kimi"
    assert cmd[cmd.index("--model") + 1] == "kimi-k3"
    assert cmd[cmd.index("--role") + 1] == "review-gate"
    assert cmd[cmd.index("--task-class") + 1] == "research_structured"
    assert cmd[cmd.index("--instruction") + 1] == "INSTRUCTION"
    assert cmd[cmd.index("--base-ref") + 1] == "origin/pr-branch"
    assert "--no-auto-commit" in cmd
    assert seen["dispatch_id"] == "kimi-gate-pr-1"
    assert seen["data_dir"] == tmp_path


def test_the_moved_dispatcher_reads_the_report_through_the_panel_seam(monkeypatch, tmp_path):
    """GE-5a round 2: ``_dispatch`` resolves the report reader through ``_panel()``.

    Round 1 called this module's own ``read_report``, so a test that patched
    ``plan_gate_panel._read_report`` — the seam every other transport helper still
    keeps — no longer reached the dispatcher. The child process is patched the way
    ``tests/test_gate_lane_worktree_reap.py`` patches it: replace the panel's
    ``subprocess``, so the real ``_run_provider_lane_child`` wiring still runs.
    """
    monkeypatch.setattr(plan_gate_panel, "_resolve_data_dir", lambda _data_dir: tmp_path)
    monkeypatch.setattr(dwi, "resolve_consumer_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        plan_gate_panel,
        "subprocess",
        SimpleNamespace(
            run=lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, "", ""),
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    monkeypatch.setattr(plan_gate_panel, "_read_report", lambda *a, **k: "PANEL SEAM MARKER")

    dispatcher = plan_gate_panel._make_default_dispatcher(str(tmp_path), 42)

    assert dispatcher("glm", "glm-4.6", "INSTRUCTION", "glm-gate-seam-1") == "PANEL SEAM MARKER"
