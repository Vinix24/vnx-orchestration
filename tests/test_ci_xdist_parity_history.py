"""Tests for scripts/ci/xdist_parity_history.py (track ci-profile-a-xdist, D2): the cutover rule.

The rule (plan "The cutover rule"): every run in the window clean, at least
10 distinct head SHAs over at least two UTC calendar days with the latest on
main, the shadow suite at most 15 minutes on each, every watch-list OI
closed. The window: heavy verdicts with the target fingerprint whose head
descends from the merge commit of the last D3 PR.

Each case the plan names keeps the rule from holding: a light run, a re-run
of one SHA, a changed fingerprint, a run older than the last D3 merge, one
exit-1 run in the window. git ancestry runs on a real throwaway repository;
the CLI runs end to end against a gh stand-in on PATH (an external process,
replaced only inside tests/).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "xdist_parity_history.py"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hist = _load("xdist_parity_history", SCRIPT)

FP = "f" * 64
OTHER_FP = "0" * 64
BEFORE = "b" * 40


def verdict(*, exit_code=0, profile="heavy", fp=FP, failures=(), wall=600, attempt="1", differences=None) -> dict:
    return {
        "schema": "vnx-xdist-parity/1", "profile": profile, "exit": exit_code, "selection_fingerprint": fp,
        "verdict": {0: "parity", 1: "differences", 2: "invalid_evidence"}.get(exit_code, "not_applicable"),
        "step_failures": list(failures), "run_attempt": attempt, "shadow": {"suite_wall_s": wall},
        "differences": differences or {}, "invalid_evidence": [],
    }


def run(n: int, *, sha=None, day=1, hour=None, branch="dispatch/x", event="pull_request", status="completed",
        attempt=1, **verdict_args) -> "hist.Run":
    created = f"2026-11-{day:02d}T{(hour if hour is not None else n % 24):02d}:00:00Z"
    return hist.Run(str(1000 + n), attempt, sha or f"{n:040x}", branch, event, created, status, "success",
                    None if status != "completed" else verdict(**verdict_args))


def main_run(n: int, **kw) -> "hist.Run":
    return run(n, branch="main", event="push", **kw)


def clean_window() -> list:
    """Ten clean runs on ten SHAs over two days; the latest is on main."""
    runs = [run(n, day=1 if n < 5 else 2, hour=n) for n in range(9)]
    return runs + [main_run(9, day=2, hour=23)]


def evaluate(runs, *, watch_list=(), oi_status=None, truncated=False, ancestry=None):
    ancestry = ancestry or {}
    return hist.evaluate(
        runs,
        target_fingerprint=FP,
        is_ancestor=lambda head: ancestry.get(head, head != BEFORE),
        watch_list=list(watch_list) if watch_list is not None else None,
        oi_status=oi_status,
        list_truncated=truncated,
    )


def failed(evaluation) -> set:
    return {c.name for c in evaluation.conditions if not c.holds}


def test_ten_clean_runs_over_two_days_ending_on_main_hold():
    evaluation = evaluate(clean_window())
    assert evaluation.holds, [vars(c) for c in evaluation.conditions]
    assert len(evaluation.window) == 10


# --- the five cases of the plan --------------------------------------------------------------


def test_a_light_run_does_not_count():
    runs = clean_window()
    runs[0].verdict["profile"] = "light"
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"distinct_heads"}
    assert [p.place for p in evaluation.placements].count("light") == 1


def test_a_rerun_of_one_sha_counts_once():
    runs = clean_window()
    runs[1] = run(1, sha=runs[0].head_sha, day=1, hour=1)
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"distinct_heads"}
    assert len(evaluation.window) == 10


def test_a_changed_fingerprint_does_not_count():
    runs = clean_window()
    runs[2].verdict["selection_fingerprint"] = OTHER_FP
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"distinct_heads"}
    assert evaluation.placements[2].place == "other_selection"


def test_a_run_older_than_the_last_d3_merge_does_not_count():
    runs = clean_window()
    runs[3] = run(3, sha=BEFORE, day=1, hour=3)
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"distinct_heads"}
    assert evaluation.placements[3].place == "before_window"


def test_one_exit_1_run_in_the_window_blocks():
    runs = clean_window() + [run(20, day=2, hour=5, exit_code=1,
                                  differences={"outcome_class_differs": [{"nodeid": "tests/t.py::x"}]})]
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_clean"}
    [blocking] = [c for c in evaluation.conditions if c.name == "every_run_clean"]
    assert "run 1020: exit 1 (differences) [outcome_class_differs: 1]" in blocking.detail


# --- the other conditions -------------------------------------------------------------------------


@pytest.mark.parametrize("exit_code", [2, None], ids=["exit-2", "exit-null"])
def test_an_unclean_exit_in_the_window_blocks(exit_code):
    runs = clean_window() + [run(20, day=2, hour=6, exit_code=exit_code)]
    assert failed(evaluate(runs)) == {"every_run_clean"}


@pytest.mark.parametrize("failure", ["parity.prelude=failure", "shadow.verdict=shadow_failed", "parity.download_candidate=failure"],
                         ids=["parity-prelude", "shadow-phase", "download"])
def test_a_failed_step_with_exit_0_blocks(failure):
    runs = clean_window()
    runs[4].verdict["step_failures"] = [failure]
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_clean"}
    assert evaluation.placements[4].reasons == [f"step {failure}"]


def test_one_calendar_day_is_not_enough():
    runs = [run(n, day=1, hour=n) for n in range(9)] + [main_run(9, day=1, hour=23)]
    assert failed(evaluate(runs)) == {"calendar_days"}


def test_the_latest_run_must_be_on_main():
    runs = clean_window() + [run(20, day=3, hour=1)]
    assert failed(evaluate(runs)) == {"latest_on_main"}


@pytest.mark.parametrize("wall", [901, None], ids=["over-15-minutes", "no-wall-time"])
def test_a_slow_or_untimed_shadow_blocks(wall):
    runs = clean_window()
    runs[5].verdict["shadow"]["suite_wall_s"] = wall
    assert {"suite_wall_time", "every_run_clean"} == failed(evaluate(runs))


def test_a_shadow_of_exactly_15_minutes_is_within_the_bound():
    runs = clean_window()
    runs[5].verdict["shadow"]["suite_wall_s"] = 900
    assert evaluate(runs).holds


def test_a_run_whose_head_cannot_be_found_blocks():
    runs = clean_window()
    evaluation = evaluate(runs, ancestry={runs[0].head_sha: None})
    assert failed(evaluation) == {"every_run_placed", "distinct_heads"}
    assert evaluation.placements[0].place == "ancestry_unknown"


def test_a_heavy_verdict_without_fingerprint_blocks_inside_the_window():
    runs = clean_window() + [run(20, day=2, hour=7, exit_code=2, fp=None)]
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_placed"}
    assert evaluation.placements[-1].place == "unplaceable"


def test_a_heavy_verdict_without_fingerprint_before_the_window_is_ignored():
    runs = clean_window() + [run(20, sha=BEFORE, day=1, hour=0, exit_code=2, fp=None)]
    assert evaluate(runs).holds


def test_a_truncated_run_list_blocks():
    assert failed(evaluate(clean_window(), truncated=True)) == {"run_list_complete"}


def test_pending_and_verdictless_runs_are_listed_not_counted():
    runs = clean_window() + [run(30, status="in_progress"), run(31)]
    runs[-1].verdict = None
    evaluation = evaluate(runs)
    assert evaluation.holds
    assert [p.place for p in evaluation.placements[-2:]] == ["pending", "no_verdict"]


def test_an_empty_window_names_every_missing_condition():
    evaluation = evaluate([])
    assert failed(evaluation) == {"distinct_heads", "calendar_days", "latest_on_main", "suite_wall_time"}


# --- the watch list -----------------------------------------------------------------------------------


def test_without_a_watch_list_the_rule_does_not_hold():
    assert failed(evaluate(clean_window(), watch_list=None)) == {"watch_list_closed"}


def test_an_open_watch_list_oi_blocks():
    evaluation = evaluate(clean_window(), watch_list=["OI-2128"], oi_status={"OI-2128": "open"})
    assert failed(evaluation) == {"watch_list_closed"}
    assert "OI-2128=open" in [c for c in evaluation.conditions if c.name == "watch_list_closed"][0].detail


@pytest.mark.parametrize(("status", "holds"), [("done", True), ("deferred", False), ("wontfix", False), (None, False)],
                         ids=["done", "deferred", "wontfix", "absent"])
def test_only_a_done_oi_counts_as_closed(status, holds):
    oi_status = {} if status is None else {"OI-9001": status}
    assert evaluate(clean_window(), watch_list=["OI-9001"], oi_status=oi_status).holds is holds


def test_an_unreadable_ledger_blocks():
    assert failed(evaluate(clean_window(), watch_list=["OI-9001"], oi_status=None)) == {"watch_list_closed"}


def test_read_oi_status_reads_the_ledger_shape(tmp_path):
    ledger = tmp_path / "open_items.json"
    ledger.write_text(json.dumps({"schema_version": "1.0", "items": [{"id": "OI-1", "status": "done"},
                                                                     {"id": "OI-2", "status": "open"}]}), encoding="utf-8")
    assert hist.read_oi_status(ledger) == {"OI-1": "done", "OI-2": "open"}
    assert hist.read_oi_status(tmp_path / "missing.json") is None


# --- output -------------------------------------------------------------------------------------------


HEADER = {"repo": "o/r", "workflow": "VNX CI", "window_base": "a" * 40, "window_base_source": "given",
          "fingerprint": FP, "fingerprint_source": "given"}


def test_the_table_names_the_attempt_of_each_verdict_and_the_outcome():
    runs = clean_window()
    runs[0].attempt = 2
    runs[0].verdict["run_attempt"] = "2"
    runs[1].attempt = 3
    text = hist.render(evaluate(runs), HEADER)
    assert "2 (of 2)" in text and "1 (of 3)" in text
    assert text.rstrip().endswith("cutover rule: HOLDS")


def test_the_table_says_why_the_rule_does_not_hold():
    runs = clean_window() + [run(20, day=2, hour=5, exit_code=1)]
    text = hist.render(evaluate(runs, watch_list=None), HEADER)
    assert "[x] every_run_clean: run 1020: exit 1 (differences)" in text
    assert "[x] watch_list_closed: no watch list given" in text
    assert text.rstrip().endswith("cutover rule: DOES NOT HOLD")


def test_the_json_output_carries_every_run_and_condition():
    document = json.loads(hist.as_json(evaluate(clean_window()), HEADER))
    assert document["holds"] is True and len(document["runs"]) == 10
    assert {c["name"] for c in document["conditions"]} >= {"every_run_clean", "distinct_heads", "watch_list_closed"}
    assert document["runs"][0]["verdict_run_attempt"] == "1"


# --- git ancestry on a real repository ---------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def history_repo(tmp_path) -> tuple:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    shas = []
    for n in range(3):
        (repo / "f.txt").write_text(str(n), encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", f"c{n}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return repo, shas


def test_git_ancestry_answers_true_false_and_unknown(history_repo):
    repo, (first, base, head) = history_repo
    assert hist.git_is_ancestor(repo, base, head) is True
    assert hist.git_is_ancestor(repo, base, first) is False
    assert hist.git_is_ancestor(repo, base, "e" * 40) is None


def test_commit_time_is_utc(history_repo):
    repo, shas = history_repo
    assert hist.commit_time(repo, shas[1]).tzinfo is not None
    assert hist.commit_time(repo, "e" * 40) is None


# --- the CLI end to end, with a gh stand-in on PATH ---------------------------------------------------------

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, shutil, sys
spec = json.load(open(os.environ["FAKE_GH_SPEC"]))
args = sys.argv[1:]
if args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": "o/r"}))
elif args[:2] == ["pr", "view"]:
    print(json.dumps(spec["pr"][args[2]]))
elif args[:2] == ["run", "list"]:
    print(json.dumps(spec["runs"]))
elif args[:2] == ["run", "download"]:
    source = spec["verdicts"].get(args[2])
    if source is None:
        print("no valid artifacts found to download", file=sys.stderr)
        sys.exit(1)
    target = args[args.index("--dir") + 1]
    os.makedirs(target, exist_ok=True)
    shutil.copy(source, os.path.join(target, "xdist-parity-verdict.json"))
else:
    print("unexpected gh call: %r" % (args,), file=sys.stderr)
    sys.exit(3)
'''


def _real_commits() -> tuple:
    """Two real commits of this checkout: the parent (window base) and HEAD (in the window)."""
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "log", "-2", "--format=%H"], capture_output=True, text=True, check=True)
    head, base = out.stdout.split()
    return base, head


def _cli(tmp_path: Path, runs: list, verdicts: dict, *args: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    paths = {}
    for run_id, document in verdicts.items():
        paths[run_id] = str(tmp_path / f"verdict-{run_id}.json")
        Path(paths[run_id]).write_text(json.dumps(document), encoding="utf-8")
    spec = tmp_path / "spec.json"
    base, _ = _real_commits()
    spec.write_text(json.dumps({"runs": runs, "verdicts": paths,
                                "pr": {"2082": {"state": "MERGED", "mergeCommit": {"oid": base}},
                                       "7": {"state": "OPEN", "mergeCommit": None}}}), encoding="utf-8")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")},
           "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_SPEC": str(spec)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, cwd=str(REPO_ROOT))


def _listed(run_id: int, head: str, created: str, **kw) -> dict:
    return {"databaseId": run_id, "headSha": head, "headBranch": kw.get("branch", "main"), "event": kw.get("event", "push"),
            "status": kw.get("status", "completed"), "conclusion": "success", "createdAt": created, "attempt": kw.get("attempt", 1)}


def test_the_cli_reads_the_verdicts_and_prints_why_the_rule_does_not_hold(tmp_path):
    _, head = _real_commits()
    runs = [_listed(2, head, "2099-01-02T00:00:00Z"), _listed(1, head, "2099-01-01T00:00:00Z", attempt=2),
            _listed(3, head, "2099-01-03T00:00:00Z", status="in_progress")]
    verdicts = {"1": verdict(attempt="2"), "2": verdict(exit_code=1)}
    proc = _cli(tmp_path, runs, verdicts, "--since-pr", "2082", "--fingerprint", FP, "--no-watch-list")
    assert proc.returncode == 1, proc.stderr
    assert "window base:" in proc.stdout and "(merge commit of PR #2082)" in proc.stdout
    assert "2 (of 2)" in proc.stdout and "pending" in proc.stdout
    assert "[x] every_run_clean: run 2: exit 1" in proc.stdout
    assert "[x] distinct_heads: 1 distinct head SHAs" in proc.stdout
    assert proc.stdout.rstrip().endswith("cutover rule: DOES NOT HOLD")


def test_the_cli_computes_the_fingerprint_of_this_checkout_by_default(tmp_path):
    proc = _cli(tmp_path, [], {}, "--since-commit", _real_commits()[0], "--no-watch-list", "--json")
    assert proc.returncode == 1, proc.stderr
    document = json.loads(proc.stdout)
    assert document["fingerprint_source"] == "emitted by this checkout" and len(document["fingerprint"]) == 64
    assert document["holds"] is False and document["runs"] == []


def test_the_cli_flags_a_list_cut_off_by_the_limit(tmp_path):
    _, head = _real_commits()
    runs = [_listed(n, head, f"2099-01-0{n}T00:00:00Z") for n in (1, 2)]
    proc = _cli(tmp_path, runs, {"1": verdict(), "2": verdict()}, "--since-commit", _real_commits()[0],
                "--fingerprint", FP, "--no-watch-list", "--limit", "2")
    assert "[x] run_list_complete" in proc.stdout


def test_the_cli_exits_2_on_an_unmerged_pr(tmp_path):
    proc = _cli(tmp_path, [], {}, "--since-pr", "7", "--fingerprint", FP, "--no-watch-list")
    assert proc.returncode == 2
    assert "PR #7 is not merged (state OPEN)" in proc.stderr
