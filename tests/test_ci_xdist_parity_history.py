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


def verdict(*, exit_code=0, profile="heavy", fp=FP, failures=(), wall=600, attempt="1", run_id="1000",
            differences=None) -> dict:
    return {
        "schema": "vnx-xdist-parity/1", "profile": profile, "exit": exit_code, "selection_fingerprint": fp,
        "verdict": {0: "parity", 1: "differences", 2: "invalid_evidence"}.get(exit_code, "not_applicable"),
        "step_failures": list(failures), "run_id": run_id, "run_attempt": attempt, "shadow": {"suite_wall_s": wall},
        "differences": differences or {}, "invalid_evidence": [],
    }


def run(n: int, *, sha=None, day=1, hour=None, branch="dispatch/x", event="pull_request", status="completed",
        attempt=1, verdict_attempt=None, **verdict_args) -> "hist.Run":
    """Run 1000+n; its verdict is of its latest attempt unless *verdict_attempt* says otherwise."""
    created = f"2026-11-{day:02d}T{(hour if hour is not None else n % 24):02d}:00:00Z"
    own = verdict(run_id=str(1000 + n), attempt=str(attempt) if verdict_attempt is None else verdict_attempt,
                  **verdict_args)
    return hist.Run(str(1000 + n), attempt, sha or f"{n:040x}", branch, event, created, status, "success",
                    None if status != "completed" else own)


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


def test_an_empty_window_names_every_missing_condition():
    evaluation = evaluate([])
    assert failed(evaluation) == {"distinct_heads", "calendar_days", "latest_on_main", "suite_wall_time"}


# --- missing and stale evidence: only git ancestry may exclude a run without a current verdict -------------


def _without_verdict(run_: "hist.Run") -> "hist.Run":
    run_.verdict = None
    run_.conclusion = "failure"
    return run_


def test_a_completed_run_without_a_verdict_in_the_window_blocks():
    """codex round 1: ten clean runs plus a newer failed run without an artifact held."""
    runs = clean_window() + [_without_verdict(run(30, day=3, exit_code=1))]
    evaluation = evaluate(runs)
    assert not evaluation.holds
    assert failed(evaluation) == {"every_run_has_verdict"}
    assert evaluation.placements[-1].place == "no_verdict"
    [blocking] = [c for c in evaluation.conditions if c.name == "every_run_has_verdict"]
    assert "run 1030" in blocking.detail


def test_a_run_without_a_verdict_names_why_it_has_none():
    bad = _without_verdict(run(30, day=3))
    bad.verdict_problem = "gh run download exit 1: no valid artifacts found to download"
    evaluation = evaluate(clean_window() + [bad])
    assert evaluation.placements[-1].reasons == [f"{bad.verdict_problem} (run conclusion failure)"]


def test_a_run_without_a_verdict_before_the_window_is_ignored():
    evaluation = evaluate(clean_window() + [_without_verdict(run(30, sha=BEFORE, day=1, hour=0))])
    assert evaluation.holds
    assert evaluation.placements[-1].place == "before_window"


def test_a_run_without_a_verdict_whose_head_cannot_be_found_blocks():
    bad = _without_verdict(run(30, day=3))
    evaluation = evaluate(clean_window() + [bad], ancestry={bad.head_sha: None})
    assert failed(evaluation) == {"every_run_placed"}
    assert evaluation.placements[-1].place == "ancestry_unknown"


@pytest.mark.parametrize("status", ["in_progress", "queued", "waiting"])
def test_a_run_still_running_in_the_window_blocks(status):
    evaluation = evaluate(clean_window() + [run(30, day=3, status=status)])
    assert failed(evaluation) == {"every_run_completed"}
    assert evaluation.placements[-1].place == "pending"


def test_a_run_still_running_before_the_window_is_ignored():
    evaluation = evaluate(clean_window() + [run(30, sha=BEFORE, day=1, hour=0, status="in_progress")])
    assert evaluation.holds
    assert evaluation.placements[-1].place == "before_window"


def test_an_earlier_attempts_verdict_is_stale_evidence():
    """codex round 1: attempt 2 carrying an attempt-1 verdict held."""
    runs = clean_window()
    runs[0].attempt = 2
    runs[0].conclusion = "failure"
    evaluation = evaluate(runs)
    assert not evaluation.holds
    assert evaluation.placements[0].place == "stale_verdict"
    assert evaluation.placements[0].reasons == ["verdict of attempt 1, the run's latest attempt is 2"]
    assert "every_verdict_current" in failed(evaluation)


def test_one_stale_verdict_blocks_even_beside_ten_current_runs():
    evaluation = evaluate(clean_window() + [run(20, day=2, hour=5, attempt=2, verdict_attempt="1")])
    assert failed(evaluation) == {"every_verdict_current"}
    assert len(evaluation.window) == 10


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("run_attempt", None, "the verdict names no run_attempt"),
    ("run_attempt", "x", "the verdict names no run_attempt"),
    ("run_id", None, "verdict of run None, not of run 1020"),
    ("run_id", "999", "verdict of run 999, not of run 1020"),
], ids=["no-attempt", "unparseable-attempt", "no-run-id", "other-run"])
def test_a_verdict_that_does_not_prove_its_run_and_attempt_is_stale(field, value, reason):
    bad = run(20, day=2, hour=5)
    bad.verdict[field] = value
    evaluation = evaluate(clean_window() + [bad])
    assert failed(evaluation) == {"every_verdict_current"}
    assert evaluation.placements[-1].reasons == [reason]


def test_an_integer_run_attempt_proves_its_attempt():
    current = run(20, day=2, hour=5, attempt=3)
    current.verdict["run_attempt"] = 3
    assert evaluate(clean_window() + [current]).holds


def test_a_stale_light_verdict_is_not_read_as_light():
    """Profile and fingerprint of a stale verdict are not evidence: only ancestry may exclude it."""
    stale = run(20, day=2, hour=5, attempt=2, verdict_attempt="1", profile="light")
    evaluation = evaluate(clean_window() + [stale])
    assert failed(evaluation) == {"every_verdict_current"}
    assert evaluation.placements[-1].place == "stale_verdict"


def test_a_stale_verdict_before_the_window_is_ignored():
    evaluation = evaluate(clean_window() + [run(20, sha=BEFORE, day=1, hour=0, attempt=2, verdict_attempt="1")])
    assert evaluation.holds
    assert evaluation.placements[-1].place == "before_window"


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
    runs = clean_window() + [run(20, day=2, hour=5, attempt=3, verdict_attempt="1")]
    runs[0].attempt = 2
    runs[0].verdict["run_attempt"] = "2"
    text = hist.render(evaluate(runs), HEADER)
    assert "2 (of 2)" in text and "1 (of 3)" in text
    assert "[x] every_verdict_current: run 1020: verdict of attempt 1, the run's latest attempt is 3" in text
    assert text.rstrip().endswith("cutover rule: DOES NOT HOLD")


def test_the_table_ends_with_holds_when_the_rule_holds():
    assert hist.render(evaluate(clean_window()), HEADER).rstrip().endswith("cutover rule: HOLDS")


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
    if source == "error":
        print("downloading artifact\nHTTP 502: Bad Gateway", file=sys.stderr)
        sys.exit(1)
    target = args[args.index("--dir") + 1]
    os.makedirs(target, exist_ok=True)
    if source != "empty":
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


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """Put the gh stand-in on PATH for direct calls; *serve(run_id, source)* sets what a download yields."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    spec_path = tmp_path / "spec.json"
    spec = {"runs": [], "verdicts": {}, "pr": {}}

    def serve(run_id: str, source) -> None:
        if isinstance(source, dict):
            path = tmp_path / f"verdict-{run_id}.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            source = str(path)
        elif isinstance(source, bytes):
            path = tmp_path / f"verdict-{run_id}.json"
            path.write_bytes(source)
            source = str(path)
        spec["verdicts"][run_id] = source
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_SPEC", str(spec_path))
    return serve


def _completed(run_id: str, attempt: int = 1) -> "hist.Run":
    return hist.Run(run_id, attempt, "a" * 40, "main", "push", "2099-01-01T00:00:00Z", "completed", "success")


@pytest.mark.parametrize(("source", "problem"), [
    (None, "gh run download exit 1: no valid artifacts found to download"),
    ("error", "gh run download exit 1: HTTP 502: Bad Gateway"),
    ("empty", "the xdist-parity-verdict artifact holds no xdist-parity-verdict.json"),
    (b"{not json", "xdist-parity-verdict.json is not a readable JSON object"),
    (b"[1, 2]", "xdist-parity-verdict.json is not a readable JSON object"),
], ids=["no-artifact", "gh-error", "empty-artifact", "unparsable", "not-an-object"])
def test_a_download_without_a_usable_verdict_names_why_and_caches_nothing(fake_gh, tmp_path, source, problem):
    if source is not None:
        fake_gh("7", source)
    cache = tmp_path / "cache"
    assert hist.download_verdict("o/r", _completed("7"), cache) == (None, problem)
    assert not (cache / "7").exists()
    assert [p.name for p in cache.iterdir()] == []


def test_a_downloaded_verdict_of_another_run_is_returned_for_placement_but_not_cached(fake_gh, tmp_path):
    fake_gh("7", verdict(run_id="8", attempt="1"))
    cache = tmp_path / "cache"
    fetched, problem = hist.download_verdict("o/r", _completed("7"), cache)
    assert fetched["run_id"] == "8" and problem == ""
    assert not (cache / "7").exists()
    assert hist.stale_reason(fetched, _completed("7")) == "verdict of run 8, not of run 7"


def test_a_downloaded_verdict_of_the_latest_attempt_is_cached_with_its_stamp(fake_gh, tmp_path):
    fake_gh("7", verdict(run_id="7", attempt="3"))
    cache = tmp_path / "cache"
    fetched, problem = hist.download_verdict("o/r", _completed("7", attempt=3), cache)
    assert fetched["run_attempt"] == "3" and problem == ""
    assert (cache / "7" / ".attempt").read_text(encoding="utf-8") == "3\n"
    assert json.loads((cache / "7" / "xdist-parity-verdict.json").read_text(encoding="utf-8"))["run_attempt"] == "3"
    assert sorted(p.name for p in (cache / "7").iterdir()) == [".attempt", "xdist-parity-verdict.json"]


def _listed(run_id: int, head: str, created: str, **kw) -> dict:
    return {"databaseId": run_id, "headSha": head, "headBranch": kw.get("branch", "main"), "event": kw.get("event", "push"),
            "status": kw.get("status", "completed"), "conclusion": "success", "createdAt": created, "attempt": kw.get("attempt", 1)}


def test_the_cli_reads_the_verdicts_and_prints_why_the_rule_does_not_hold(tmp_path):
    _, head = _real_commits()
    runs = [_listed(2, head, "2099-01-02T00:00:00Z"), _listed(1, head, "2099-01-01T00:00:00Z", attempt=2),
            _listed(3, head, "2099-01-03T00:00:00Z", status="in_progress")]
    verdicts = {"1": verdict(run_id="1", attempt="2"), "2": verdict(run_id="2", exit_code=1)}
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
    verdicts = {"1": verdict(run_id="1"), "2": verdict(run_id="2")}
    proc = _cli(tmp_path, runs, verdicts, "--since-commit", _real_commits()[0],
                "--fingerprint", FP, "--no-watch-list", "--limit", "2")
    assert "[x] run_list_complete" in proc.stdout


def test_the_cli_exits_2_on_an_unmerged_pr(tmp_path):
    proc = _cli(tmp_path, [], {}, "--since-pr", "7", "--fingerprint", FP, "--no-watch-list")
    assert proc.returncode == 2
    assert "PR #7 is not merged (state OPEN)" in proc.stderr


# --- the CLI on a window that holds, then the two codex round-1 cases end to end ------------------------------


def _first_parent_window() -> tuple:
    """(base, ten heads newest first): eleven first-parent commits of this checkout, each head a descendant of base."""
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-list", "--first-parent", "--max-count=11", "HEAD"],
                         capture_output=True, text=True, check=True)
    chain = out.stdout.split()
    assert len(chain) == 11, "the CLI window tests need eleven first-parent commits (fetch-depth 0)"
    return chain[10], chain[:10]


def _holding_runs(heads: list) -> tuple:
    """Ten listed runs on ten heads over two days, the latest a push on main, each with its own current verdict."""
    runs = [_listed(k, heads[10 - k], f"2099-01-0{1 if k <= 5 else 2}T{k:02d}:00:00Z",
                    branch="main" if k == 10 else "dispatch/x", event="push" if k == 10 else "pull_request")
            for k in range(1, 11)]
    return runs, {str(k): verdict(run_id=str(k)) for k in range(1, 11)}


def _cli_window(tmp_path: Path, runs: list, verdicts: dict, base: str, cache: Path) -> tuple:
    proc = _cli(tmp_path, runs, verdicts, "--since-commit", base, "--fingerprint", FP, "--no-watch-list",
                "--cache-dir", str(cache), "--json")
    assert proc.returncode in (0, 1), proc.stderr
    document = json.loads(proc.stdout)
    places = {r["run_id"]: r for r in document["runs"]}
    return proc.returncode, document, places


def _failed_names(document: dict) -> set:
    return {c["name"] for c in document["conditions"] if not c["holds"]}


def test_the_cli_holds_on_ten_clean_runs_and_caches_each_verdict_with_its_attempt(tmp_path):
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    cache = tmp_path / "cache"
    code, document, _ = _cli_window(tmp_path, runs, verdicts, base, cache)
    assert code == 0 and document["holds"] is True, document["conditions"]
    assert (cache / "3" / ".attempt").read_text(encoding="utf-8").strip() == "1"


def test_the_cli_blocks_on_a_completed_run_whose_artifact_is_missing(tmp_path):
    """codex round 1, end to end: a newer failed run without an artifact must not leave the window."""
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    runs.append({**_listed(11, heads[0], "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request"),
                 "conclusion": "failure"})
    code, document, places = _cli_window(tmp_path, runs, verdicts, base, tmp_path / "cache")
    assert code == 1
    assert _failed_names(document) == {"every_run_has_verdict"}
    assert places["11"]["place"] == "no_verdict"
    assert "no valid artifacts found to download" in places["11"]["reasons"][0]


def test_the_cli_does_not_count_or_cache_an_earlier_attempts_verdict(tmp_path):
    """codex round 1, end to end: a re-run whose upload never happened serves the attempt-1 verdict."""
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    runs.append(_listed(11, heads[0], "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request", attempt=2))
    verdicts["11"] = verdict(run_id="11", attempt="1")
    cache = tmp_path / "cache"
    code, document, places = _cli_window(tmp_path, runs, verdicts, base, cache)
    assert code == 1
    assert _failed_names(document) == {"every_verdict_current"}
    assert places["11"]["place"] == "stale_verdict" and places["11"]["verdict_run_attempt"] == "1"
    assert not (cache / "11").exists()


def test_a_cache_entry_whose_stamp_and_verdict_attempt_disagree_is_downloaded_again(tmp_path):
    """The entry round 1 could write (stamp 2 over an attempt-1 verdict) is not trusted: attempt 2 is fetched."""
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    runs.append(_listed(11, heads[0], "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request", attempt=2))
    verdicts["11"] = verdict(run_id="11", attempt="2", exit_code=1)
    cache = tmp_path / "cache"
    (cache / "11").mkdir(parents=True)
    (cache / "11" / "xdist-parity-verdict.json").write_text(json.dumps(verdict(run_id="11", attempt="1")),
                                                           encoding="utf-8")
    (cache / "11" / ".attempt").write_text("2\n", encoding="utf-8")
    code, document, places = _cli_window(tmp_path, runs, verdicts, base, cache)
    assert code == 1
    assert _failed_names(document) == {"every_run_clean"}
    assert places["11"]["place"] == "in_window" and places["11"]["verdict_run_attempt"] == "2"
    cached = json.loads((cache / "11" / "xdist-parity-verdict.json").read_text(encoding="utf-8"))
    assert cached["run_attempt"] == "2"


def test_a_cache_entry_that_proves_the_latest_attempt_is_used_without_a_download(tmp_path):
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    runs.append(_listed(11, heads[0], "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request", attempt=2))
    cache = tmp_path / "cache"
    (cache / "11").mkdir(parents=True)
    (cache / "11" / "xdist-parity-verdict.json").write_text(json.dumps(verdict(run_id="11", attempt="2")),
                                                           encoding="utf-8")
    (cache / "11" / ".attempt").write_text("2\n", encoding="utf-8")
    code, document, places = _cli_window(tmp_path, runs, verdicts, base, cache)
    assert code == 0, document["conditions"]
    assert places["11"]["place"] == "in_window"
