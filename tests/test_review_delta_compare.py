"""Deltareview DR-5: ``vnx review delta-compare`` on a tmp state dir with a fixture ledger, reports and shadow file."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from review_stack_support import BRANCH, PR  # importing it puts scripts/ and scripts/lib on sys.path
import review_delta_compare as rdc
import review_rounds as rr
import review_rounds_cli as cli
from review_findings import id_for_finding, symbolic_context

SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)
SOURCE = "def f():\n    return 1\n"


def source_of(head: str, path: str) -> Optional[str]:
    return SOURCE


def head(n: int) -> str:
    return f"{n:040x}"


def blocker(line: int = 2, msg: str = "broken", severity: str = "blocking") -> Dict[str, Any]:
    return {"file_path": "x.py", "line": line, "severity": severity, "message": msg}


def bid(finding: Dict[str, Any]) -> str:
    return id_for_finding("codex_gate", finding, SOURCE)


class Store:
    def __init__(self, tmp: Path) -> None:
        self.state = tmp / "state"
        self.gates = self.state / "review_gates"
        (self.gates / "shadow").mkdir(parents=True)
        self.project = rr.store_project_id(self.gates)
        self.rows: List[Dict[str, Any]] = [{"kind": "opened", "project_id": self.project, "pr_number": PR, "basis": "new",
                                            "evidence": 0, "recorded_at": "2026-10-01T00:00:00Z", "source": "gate"}]
        self.shadow: List[Dict[str, Any]] = []
        self.tmp = tmp

    def pair(self, n: int, blockers=None, *, pr: int = PR, parent: int = 0, statuses=None, new=None, report: bool = True,
             at: str = "2026-11-05T00:00:00Z", verdict: str = "pass", **extra) -> None:
        blockers = [] if blockers is None else blockers
        report_path = self.tmp / f"r{pr}-{n}.md"
        if report:
            report_path.write_text("```json\n" + json.dumps({"verdict": "fail", "findings": blockers}) + "\n```\n")
        self.rows.append(rr.RoundLine(
            project_id=self.project, pr_number=pr, head_sha=head(n), seat="codex_gate", reviewer="codex_gate", takeover_from="",
            verdict="fail", blocking_count=len(blockers), report_path=str(report_path), dispatch_id=f"d-{n}",
            recorded_at=at, branch=BRANCH, source="gate").to_dict())
        self.shadow.append({"pr_number": pr, "seat": "codex_gate", "head": head(n), "parent_head": head(parent),
                            "delta_verdict": verdict, "statuses": statuses or {}, "new_findings": new or [],
                            "duration_seconds": 2.5, **extra})

    def run(self, planted=None, **kw) -> rdc.Comparison:
        (self.gates / "rounds.ndjson").write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        (self.gates / "shadow" / "delta.ndjson").write_text("".join(json.dumps(r) + "\n" for r in self.shadow))
        return rdc.compare(self.state, since=SINCE, source_of=source_of, planted=planted, **kw)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path)


def clean(store: Store, count: int) -> None:
    for n in range(1, count + 1):
        store.pair(n, [blocker()], new=[blocker(line=3, msg="reworded")], verdict="fail")


def planted_set(store: Store, count: int = 10, found: bool = True) -> List[Dict[str, Any]]:
    cases = []
    for n in range(100, 100 + count):
        store.shadow.append({"pr_number": PR, "head": head(n), "delta_verdict": "fail" if found else "pass",
                             "new_findings": [blocker(line=7)] if found else []})
        cases.append({"pr_number": PR, "head": head(n), "file_path": "x.py", "line": 7})
    return cases


def test_one_missed_blocker_fails(store):
    clean(store, 19)
    store.pair(50, [blocker(msg="seen by full only")], new=[])
    result = store.run()
    assert (result.outcome, result.missed_blockers, result.missed_pairs) == (rdc.FAIL, 1, 1)


def test_twenty_clean_pairs_and_ten_found_planted_cases_pass(store):
    clean(store, 20)
    result = store.run(planted=planted_set(store))
    assert result.outcome == rdc.PASS and len(result.pairs) == 20 and result.missed_blockers == 0


@pytest.mark.parametrize("pairs,planted_count,reason", [(19, 10, "19 pairs"), (20, None, "no planted manifest"), (20, 3, "3 planted cases")])
def test_too_few_pairs_or_planted_cases_are_insufficient(store, pairs, planted_count, reason):
    clean(store, pairs)
    result = store.run(planted=None if planted_count is None else planted_set(store, planted_count))
    assert result.outcome == rdc.INSUFFICIENT and any(reason in r for r in result.reasons)


def test_an_unfound_planted_case_fails(store):
    clean(store, 20)
    assert store.run(planted=planted_set(store, 10, found=False)).outcome == rdc.FAIL


def test_a_planted_case_without_a_shadow_line_is_not_found(store):
    clean(store, 20)
    cases = planted_set(store) + [{"pr_number": PR, "head": head(999), "file_path": "x.py", "line": 1}]
    result = store.run(planted=cases)
    assert result.outcome == rdc.FAIL and [c["found"] for c in result.planted].count(False) == 1


def test_missing_report_is_unmeasurable_not_zero_missed(store):
    store.pair(1, [blocker()], report=False)
    result = store.run()
    assert result.pairs == [] and "unreadable" in result.unmeasurable[0]["reason"]


def test_ledger_count_above_parsed_blockers_is_unmeasurable(store):
    store.pair(1, [blocker()])
    store.rows[-1]["blocking_count"] = 3
    assert store.run().unmeasurable[0]["reason"].startswith("ledger counts 3")


def test_shadow_error_and_full_review_reason_are_listed_not_counted(store):
    store.shadow.append({"pr_number": PR, "head": head(7), "reasons": ["shadow_error"], "error": "boom"})
    store.shadow.append({"pr_number": PR, "head": head(8), "full_review_reason": "scope_changed"})
    result = store.run()
    assert result.pairs == [] and set(result.not_pairs) == {"shadow_error", "scope_changed"}


@pytest.mark.parametrize("answer,missed", [("fixed", 1), ("open", 0), ("regressed", 0), ("garbled", 1), (None, 1)])
def test_sent_id_answer(store, answer, missed):
    sent = blocker()
    store.pair(1, [sent], statuses={bid(sent): answer})
    assert store.run().missed_blockers == missed


@pytest.mark.parametrize("delta_line,missed", [(12, 0), (13, 1), (2, 0)])
def test_new_blocker_matched_within_ten_lines(store, delta_line, missed):
    store.pair(1, [blocker(line=2)], new=[blocker(line=delta_line, msg="other words")])
    assert store.run().missed_blockers == missed


def test_advisory_new_finding_does_not_match_and_other_file_does_not(store):
    store.pair(1, [blocker()], new=[blocker(severity="warning")])
    store.pair(2, [blocker()], new=[{**blocker(), "file_path": "y.py"}])
    assert store.run().missed_blockers == 2


def test_extra_delta_blockers_are_listed_and_do_not_fail(store):
    store.pair(1, [], new=[blocker(msg="delta only")])
    result = store.run()
    assert result.pairs[0]["extra_delta_blockers"] == 1 and result.missed_blockers == 0


def test_depth_two_is_reported_apart(store):
    store.pair(1, [])
    store.pair(2, [], parent=1)
    d = store.run().to_dict()
    assert d["pairs_by_depth"] == {1: 1, 2: 1} and d["pair_count"] == 2


def test_tokens_unknown_when_absent_and_window_excludes_old_rounds(store):
    store.pair(1, [])
    store.pair(2, [], at="2026-10-02T00:00:00Z")
    result = store.run()
    assert len(result.pairs) == 1 and result.pairs[0]["tokens"] == "unknown" and result.pairs[0]["full_duration_seconds"] == "unknown"


def test_interval_at_zero_in_twenty():
    assert rdc.exact_interval(0, 20) == (0.0, 0.168)
    low, high = rdc.exact_interval(1, 20)
    assert (low, high) == (0.001, 0.249)


def test_parse_time():
    now = datetime(2026, 11, 12, tzinfo=timezone.utc)
    assert rdc.parse_time("7d", now=now) == datetime(2026, 11, 5, tzinfo=timezone.utc)
    assert rdc.parse_time("2026-11-01T00:00:00Z") == SINCE
    for bad in ("2026-11-01", "soon", ""):
        with pytest.raises(rdc.CompareError):
            rdc.parse_time(bad)


def test_cli_exit_codes(store, monkeypatch, capsys):
    clean(store, 19)
    store.pair(50, [blocker(msg="only full")], new=[])
    store.run()
    monkeypatch.setattr(rdc, "git_source_of", lambda repo: source_of)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    argv = ["delta-compare", "--since", "2026-11-01T00:00:00Z", "--data-dir", str(store.tmp)]
    assert cli.main(argv + ["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "fail"
    assert cli.main(["delta-compare", "--since", "yesterday-ish", "--data-dir", str(store.tmp)]) == 2
    assert cli.main(argv + ["--planted", str(store.tmp / "none.json")]) == 2
