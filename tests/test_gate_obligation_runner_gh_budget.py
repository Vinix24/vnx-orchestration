"""tests/test_gate_obligation_runner_gh_budget.py — OI-2459 GitHub call budget.

Measured 2026-10-28: a single :func:`gate_obligation_runner.run` tick issued one
``gh`` call per obligation (per-PR ``pr view`` for state/branch/head, one
``pr list`` per dispatch branch, one REST call per branch-existence check), so
the tick's cost grew with the backlog and drained the account's GitHub quota.

OI-2459 replaces that with one PR snapshot and one ``dispatch/*`` branch
listing per tick (the :func:`_list_open_prs` pattern, extended):

  - the snapshot is ``gh pr list --state all --json
    number,state,headRefName,headRefOid --limit <L>``, fetched at most once
    per ``run()`` and read first by every PR lookup;
  - the branch listing is ``gh api --paginate
    repos/<o>/git/matching-refs/heads/dispatch/``, fetched at most once per
    ``run()``;
  - a PR that is in the snapshot is answered from it (no call); a PR that a
    COMPLETE snapshot does not contain is "no PR" for a head-branch lookup and
    a single-call fallback for a lookup by number (an old PR outside ``L``);
  - a FAILED snapshot or listing never answers "absent" — it degrades to
    today's single call, so ``None`` still means "unknown, wait" for the
    branch check (OI-1388) and a failed snapshot is never "no PRs".

Every test runs a fake ``gh`` on ``PATH`` (a script that logs each argv and
answers from a JSON fixture). No test here ever talks to real GitHub.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import types
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import obligation_path, register_obligation  # noqa: E402

OWNER_REPO = "Vinix24/vnx-orchestration"

_FAKE_GH = '''#!/usr/bin/env python3
"""Fake `gh`: append argv to a log, answer from a JSON fixture file."""
import json
import os
import sys

with open(os.environ["FAKE_GH_CALL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")

with open(os.environ["FAKE_GH_FIXTURES"], encoding="utf-8") as fh:
    fixtures = json.load(fh)


def fail(message):
    sys.stderr.write(message + "\\n")
    raise SystemExit(1)


args = list(sys.argv[1:])
if "--repo" in args:
    index = args.index("--repo")
    del args[index:index + 2]

if args[:2] == ["pr", "list"]:
    rest = args[2:]
    if "--head" in rest:
        branch = rest[rest.index("--head") + 1]
        print(json.dumps((fixtures.get("pr_list_head") or {}).get(branch, [])))
        raise SystemExit(0)
    state = rest[rest.index("--state") + 1] if "--state" in rest else "open"
    key = "pr_list_open" if state == "open" else "pr_snapshot"
    data = fixtures.get(key)
    if data is None:
        fail("fake gh: %s failed" % key)
    print(json.dumps(data))
    raise SystemExit(0)

if args[:2] == ["pr", "view"]:
    number = args[2]
    field = args[args.index("--json") + 1]
    entry = (fixtures.get("pr_view") or {}).get(number)
    if not isinstance(entry, dict) or entry.get(field) is None:
        fail("fake gh: no PR %s field %s" % (number, field))
    print(json.dumps({field: entry[field]}))
    raise SystemExit(0)

if args[:1] == ["api"]:
    endpoint = [item for item in args[1:] if not item.startswith("-")][-1]
    if "/git/matching-refs/" in endpoint:
        refs = fixtures.get("branch_refs")
        if refs is None:
            fail("fake gh: branch listing failed")
        print(json.dumps([[{"ref": ref} for ref in refs]]))
        raise SystemExit(0)
    if "/branches/" in endpoint:
        branch = endpoint.split("/branches/", 1)[1]
        answer = (fixtures.get("branches") or {}).get(branch)
        if answer == "missing":
            fail("gh: Not Found (HTTP 404)")
        if answer != "exists":
            fail("gh: HTTP 502")
        print(json.dumps({"name": branch}))
        raise SystemExit(0)

fail("fake gh: unhandled argv: " + " ".join(sys.argv[1:]))
'''


def _classify(argv: List[str]) -> str:
    """The helper a call belongs to, from its argv shape."""
    args = list(argv)
    if "--repo" in args:
        index = args.index("--repo")
        del args[index:index + 2]
    if args[:2] == ["pr", "list"]:
        rest = args[2:]
        if "--head" in rest:
            return "pr_list_head"
        state = rest[rest.index("--state") + 1] if "--state" in rest else "open"
        return "pr_list_open" if state == "open" else "pr_snapshot"
    if args[:2] == ["pr", "view"]:
        return "pr_view_" + args[args.index("--json") + 1]
    if args[:1] == ["api"]:
        endpoint = [item for item in args[1:] if not item.startswith("-")][-1]
        if "/git/matching-refs/" in endpoint:
            return "branch_listing"
        if "/branches/" in endpoint:
            return "branch_single"
    return "other"


class _FakeGh:
    def __init__(self, log_path: Path, fixtures_path: Path) -> None:
        self.log_path = log_path
        self.fixtures_path = fixtures_path

    def fixtures(self, payload: Dict[str, Any]) -> None:
        self.fixtures_path.write_text(json.dumps(payload), encoding="utf-8")

    def reset(self) -> None:
        if self.log_path.exists():
            self.log_path.unlink()

    def calls(self) -> List[List[str]]:
        if not self.log_path.exists():
            return []
        return [
            json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def counts(self) -> Dict[str, int]:
        return dict(Counter(_classify(argv) for argv in self.calls()))

    def total(self) -> int:
        return len(self.calls())


@pytest.fixture
def fake_gh(tmp_path, monkeypatch) -> _FakeGh:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(stat.S_IRWXU)
    log_path = tmp_path / "gh-calls.ndjson"
    fixtures_path = tmp_path / "gh-fixtures.json"
    fixtures_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_GH_CALL_LOG", str(log_path))
    monkeypatch.setenv("FAKE_GH_FIXTURES", str(fixtures_path))
    return _FakeGh(log_path, fixtures_path)


class _FakeReviewGateManager:
    """Writes real request+result files; no gate actually runs, no gh involved."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.calls: List[Dict[str, Any]] = []

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "requests" / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"

    def request_and_execute(self, *, pr_number, branch, review_stack, risk_class,
                            changed_files, mode, dispatch_id="", **extra):
        self.calls.append({"pr_number": pr_number, "branch": branch, "review_stack": list(review_stack)})
        for gate in review_stack:
            self._request_path(gate, pr_number).write_text(
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "completed"}),
                encoding="utf-8",
            )
            self._result_path(gate, pr_number).write_text(
                json.dumps({
                    "gate": gate, "pr_number": pr_number,
                    "status": "not_executable", "reason": "provider_not_installed",
                }),
                encoding="utf-8",
            )
        return {"pr_number": pr_number, "branch": branch, "gates": [], "has_required_failure": False}


def _make_state_dir(root: Path) -> Path:
    state_dir = root / "vnx-data" / "state"
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    return state_dir


def _patch_runner(monkeypatch, manager: _FakeReviewGateManager) -> None:
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: OWNER_REPO)
    fake_rgm = types.ModuleType("review_gate_manager")
    fake_rgm._compute_changed_files = lambda branch: ["scripts/lib/foo.py"]
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake_rgm)


def _dispatch_ids(n: int) -> List[str]:
    return [f"20261020-budget-{index:03d}" for index in range(n)]


def _budget_fixtures(n: int) -> Dict[str, Any]:
    """N obligations: alternating OPEN/MERGED PRs, every fifth one without a PR."""
    snapshot: List[Dict[str, Any]] = []
    open_prs: List[Dict[str, Any]] = []
    pr_view: Dict[str, Any] = {}
    pr_list_head: Dict[str, Any] = {}
    branch_refs: List[str] = []
    for index, dispatch_id in enumerate(_dispatch_ids(n)):
        branch = f"dispatch/{dispatch_id}"
        number = 1000 + index
        branch_refs.append(f"refs/heads/{branch}")
        if index % 5 == 4:
            pr_list_head[branch] = []
            continue
        state = "MERGED" if index % 2 else "OPEN"
        head = f"{index:08x}" * 5
        snapshot.append({
            "number": number, "state": state, "headRefName": branch, "headRefOid": head,
        })
        if state == "OPEN":
            open_prs.append({"number": number, "headRefName": branch})
        pr_list_head[branch] = [{"number": number}]
        pr_view[str(number)] = {"state": state, "headRefName": branch, "headRefOid": head}
    return {
        "pr_snapshot": snapshot,
        "pr_list_open": open_prs,
        "pr_list_head": pr_list_head,
        "pr_view": pr_view,
        "branch_refs": branch_refs,
        "branches": {},
    }


def _run_tick(root: Path, n: int, monkeypatch, fake_gh: _FakeGh) -> Dict[str, Any]:
    state_dir = _make_state_dir(root)
    for dispatch_id in _dispatch_ids(n):
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
    fake_gh.fixtures(_budget_fixtures(n))
    fake_gh.reset()
    manager = _FakeReviewGateManager(state_dir)
    _patch_runner(monkeypatch, manager)
    return runner.run(state_dir)


# ---------------------------------------------------------------------------
# One snapshot + one branch listing per tick: cost does not grow with the backlog
# ---------------------------------------------------------------------------


def test_gh_call_count_is_constant_in_the_obligation_count(tmp_path, monkeypatch, fake_gh):
    summary5 = _run_tick(tmp_path / "n5", 5, monkeypatch, fake_gh)
    counts5 = fake_gh.counts()
    summary40 = _run_tick(tmp_path / "n40", 40, monkeypatch, fake_gh)
    counts40 = fake_gh.counts()

    # No per-obligation call shape survives: every PR answer comes from the
    # one snapshot, every branch answer from the one listing.
    for shape in ("pr_list_head", "branch_single"):
        assert counts40.get(shape, 0) == 0, f"{shape} must not be called per obligation: {counts40}"
    assert not any(shape.startswith("pr_view_") for shape in counts40), counts40

    assert counts5.get("pr_snapshot", 0) == 1, counts5
    assert counts40.get("pr_snapshot", 0) == 1, counts40
    assert counts5.get("branch_listing", 0) == 1, counts5
    assert counts40.get("branch_listing", 0) == 1, counts40
    assert counts5.get("pr_list_open", 0) == 1, counts5
    assert counts40.get("pr_list_open", 0) == 1, counts40

    assert sum(counts5.values()) == 3, counts5
    assert sum(counts40.values()) == sum(counts5.values()), (
        f"the tick's gh budget must not grow with the obligation count: {counts5} vs {counts40}"
    )
    # The decision logic is unchanged: open PRs stay pending (temporary refusal),
    # merged PRs retire, no-PR obligations wait on an existing branch.
    assert summary5["pending_after"] == 3
    assert summary40["pending_after"] == 24


# ---------------------------------------------------------------------------
# A failed snapshot degrades to today's single calls — never to "no PRs"
# ---------------------------------------------------------------------------


def test_failed_snapshot_keeps_per_pr_calls_and_the_pending_wait(tmp_path, monkeypatch, fake_gh):
    state_dir = _make_state_dir(tmp_path)
    for number in (101, 102):
        register_obligation(
            state_dir, dispatch_id=f"20261020-failed-snapshot-{number}", gate="codex_gate",
            project_id="vnx-dev", pr_number=number,
        )
    fake_gh.fixtures({
        "pr_snapshot": None,
        "pr_list_open": [],
        "pr_list_head": {},
        "pr_view": {},
        "branch_refs": [],
        "branches": {},
    })
    fake_gh.reset()
    _patch_runner(monkeypatch, _FakeReviewGateManager(state_dir))

    summary = runner.run(state_dir)
    counts = fake_gh.counts()

    assert counts.get("pr_snapshot", 0) == 1, counts
    assert counts.get("pr_view_state", 0) == 2, (
        f"a failed snapshot must fall back to the per-PR state call: {counts}"
    )
    assert summary["pending_after"] == 2
    assert [outcome["action"] for outcome in summary["outcomes"]] == ["unresolvable", "unresolvable"]


# ---------------------------------------------------------------------------
# A suspect snapshot (exactly L rows) falls back for a PR it does not contain
# ---------------------------------------------------------------------------


def test_suspect_snapshot_falls_back_to_the_single_call(tmp_path, monkeypatch, fake_gh):
    limit = runner._PR_SNAPSHOT_LIMIT
    rows = [
        {
            "number": 100000 + index,
            "state": "OPEN",
            "headRefName": f"dispatch/bulk-{index}",
            "headRefOid": "a" * 40,
        }
        for index in range(limit)
    ]
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20261020-old-pr", gate="codex_gate",
        project_id="vnx-dev", pr_number=999,
    )
    fake_gh.fixtures({
        "pr_snapshot": rows,
        "pr_list_open": [],
        "pr_list_head": {},
        "pr_view": {"999": {"state": "OPEN", "headRefName": "dispatch/old-pr", "headRefOid": "b" * 40}},
        "branch_refs": [],
        "branches": {},
    })
    fake_gh.reset()
    _patch_runner(monkeypatch, _FakeReviewGateManager(state_dir))

    summary = runner.run(state_dir)
    counts = fake_gh.counts()

    assert counts.get("pr_snapshot", 0) == 1, counts
    assert counts.get("pr_view_state", 0) == 1, (
        f"a suspect snapshot cannot answer for a PR it does not contain: {counts}"
    )
    assert counts.get("pr_view_headRefName", 0) == 1, counts
    assert summary["pending_after"] == 1


# ---------------------------------------------------------------------------
# The branch listing: True / False / None, never False on a failed listing
# ---------------------------------------------------------------------------


def test_branch_listing_with_the_branch_is_true_and_costs_no_single_call(fake_gh):
    fake_gh.fixtures({"branch_refs": ["refs/heads/dispatch/present"]})
    fake_gh.reset()
    snapshot = runner._GithubSnapshot(OWNER_REPO)

    assert runner._branch_exists_on_github("present", OWNER_REPO, snapshot=snapshot) is True
    assert fake_gh.counts() == {"branch_listing": 1}, fake_gh.counts()


def test_branch_listing_without_the_branch_is_false_and_costs_no_single_call(fake_gh):
    fake_gh.fixtures({"branch_refs": ["refs/heads/dispatch/other"]})
    fake_gh.reset()
    snapshot = runner._GithubSnapshot(OWNER_REPO)

    assert runner._branch_exists_on_github("gone", OWNER_REPO, snapshot=snapshot) is False
    assert fake_gh.counts() == {"branch_listing": 1}, fake_gh.counts()


def test_failed_branch_listing_is_unknown_never_false(fake_gh):
    fake_gh.fixtures({"branch_refs": None, "branches": {"dispatch/unreachable": "unknown"}})
    fake_gh.reset()
    snapshot = runner._GithubSnapshot(OWNER_REPO)

    assert runner._branch_exists_on_github("unreachable", OWNER_REPO, snapshot=snapshot) is None
    counts = fake_gh.counts()
    assert counts.get("branch_listing", 0) == 1, counts
    assert counts.get("branch_single", 0) == 1, counts


def test_failed_branch_listing_keeps_the_404_versus_other_failure_split(fake_gh):
    fake_gh.fixtures({"branch_refs": None, "branches": {"dispatch/gone": "missing"}})
    fake_gh.reset()
    snapshot = runner._GithubSnapshot(OWNER_REPO)

    assert runner._branch_exists_on_github("gone", OWNER_REPO, snapshot=snapshot) is False


# ---------------------------------------------------------------------------
# The booked outcome for a MERGED PR is the same without a per-PR state call
# ---------------------------------------------------------------------------


def _merged_fixtures() -> Dict[str, Any]:
    dispatch_id = "20261020-merged-tick"
    return {
        "pr_snapshot": [{
            "number": 77,
            "state": "MERGED",
            "headRefName": f"dispatch/{dispatch_id}",
            "headRefOid": "c" * 40,
        }],
        "pr_list_open": [],
        "pr_list_head": {f"dispatch/{dispatch_id}": [{"number": 77}]},
        "pr_view": {"77": {"state": "MERGED", "headRefName": f"dispatch/{dispatch_id}", "headRefOid": "c" * 40}},
        "branch_refs": [f"refs/heads/dispatch/{dispatch_id}"],
        "branches": {},
    }


def _run_merged(root: Path, monkeypatch, fake_gh: _FakeGh, fixtures: Dict[str, Any]) -> Dict[str, Any]:
    dispatch_id = "20261020-merged-tick"
    state_dir = _make_state_dir(root)
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
    )
    fake_gh.fixtures(fixtures)
    fake_gh.reset()
    _patch_runner(monkeypatch, _FakeReviewGateManager(state_dir))
    summary = runner.run(state_dir)
    record = json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))
    return {
        "summary": summary,
        "status": record.get("status"),
        "reason": record.get("reason"),
        "counts": fake_gh.counts(),
    }


def test_a_snapshot_merged_pr_retires_like_the_state_call_did(tmp_path, monkeypatch, fake_gh):
    from_snapshot = _run_merged(tmp_path / "snapshot", monkeypatch, fake_gh, _merged_fixtures())

    degraded = _merged_fixtures()
    degraded["pr_snapshot"] = None  # snapshot fails: today's per-PR path answers
    from_single_call = _run_merged(tmp_path / "single", monkeypatch, fake_gh, degraded)

    assert from_snapshot["status"] == runner.STATUS_RETIRED, from_snapshot
    assert from_snapshot["reason"] == runner.REASON_PR_MERGED, from_snapshot
    assert (from_snapshot["status"], from_snapshot["reason"]) == (
        from_single_call["status"], from_single_call["reason"],
    ), "the booked outcome must not depend on where the facts came from"
    assert from_snapshot["counts"].get("pr_view_state", 0) == 0, from_snapshot["counts"]
    assert from_single_call["counts"].get("pr_view_state", 0) == 1, from_single_call["counts"]
