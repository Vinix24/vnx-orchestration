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

OI-2459 round 2 bounds the cost and pins where a head may come from:

  - the head AFTER a gate run is read LIVE (``gh pr view --json headRefOid``),
    never from the pre-run snapshot — the round-1 defect: a push landing
    during a minutes-long run left the result about the old head, the
    snapshot still said old head, and the obligation was booked ``fulfilled``
    over superseded code;
  - the PRE-run baseline of a launch reads the snapshot only while its PR
    data is younger than ``_LAUNCH_HEAD_SNAPSHOT_MAX_AGE_SECONDS`` (60 s);
    older, the launch pays one live call — at most two live head reads per
    launch;
  - a failing snapshot's per-obligation fallback is capped per tick
    (``_FALLBACK_CALL_CAP_PER_TICK`` = 20), and a rate-limit refusal stops the
    rest of the tick; skipped lookups answer unknown (a wait), never a
    retirement or a pass.

Every test runs a fake ``gh`` on ``PATH`` (a script that logs each argv and
answers from a JSON fixture). No test here ever talks to real GitHub.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
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

# Captured at import time, BEFORE tests/conftest.py's autouse
# ``_stub_gate_identity_gh_calls`` replaces ``subprocess.run`` in place. A test
# that needs a LIVE ``gh pr view --json headRefOid`` to reach the fake gh on
# PATH restores this function for its own body — the pattern conftest documents
# (tests/test_pr_merge_review_gate.py::TestRealWriterThroughRealMergeCheck).
_REAL_SUBPROCESS_RUN = subprocess.run

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
    if fixtures.get("rate_limit_pr_view"):
        fail("gh: API rate limit exceeded for user (HTTP 403)")
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
    """N obligations: exactly ONE OPEN PR, the rest MERGED, every fifth without a PR.

    Exactly one gate LAUNCH in both ticks is deliberate (OI-2459 round 2): a
    launch legitimately costs one LIVE post-run head read, so the constant-cost
    invariant is asserted on a fixture whose launch count does not vary with N.
    The per-launch bound itself is pinned separately below.
    """
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
        state = "OPEN" if index == 0 else "MERGED"
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
    # The tick's single gate launch needs its LIVE post-run ``gh pr view
    # --json headRefOid`` to reach the fake gh on PATH rather than the suite's
    # offline identity stub (tests/conftest.py); restore the real call for this
    # body only.
    monkeypatch.setattr(subprocess, "run", _REAL_SUBPROCESS_RUN)
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
    # one snapshot, every branch answer from the one listing, and the only
    # pr-view call is the LIVE head read after the single gate launch below.
    for shape in ("pr_list_head", "branch_single", "pr_view_state", "pr_view_headRefName"):
        assert counts40.get(shape, 0) == 0, f"{shape} must not be called per obligation: {counts40}"
    assert counts40.get("pr_view_headRefOid", 0) == 1, (
        f"one gate launch costs exactly one live post-run head read: {counts40}"
    )

    assert counts5.get("pr_snapshot", 0) == 1, counts5
    assert counts40.get("pr_snapshot", 0) == 1, counts40
    assert counts5.get("branch_listing", 0) == 1, counts5
    assert counts40.get("branch_listing", 0) == 1, counts40
    assert counts5.get("pr_list_open", 0) == 1, counts5
    assert counts40.get("pr_list_open", 0) == 1, counts40

    # OI-2459 round 2: the per-tick cost is still independent of the backlog.
    # Both fixtures have exactly one gate launch, so the one live head read it
    # needs does not vary with N either — everything else is the fixed
    # snapshot + listing + open list.
    assert sum(counts5.values()) == 4, counts5   # snapshot + listing + open list + 1 live head read
    assert sum(counts40.values()) == sum(counts5.values()), (
        f"the tick's gh budget must not grow with the obligation count: {counts5} vs {counts40}"
    )
    # The decision logic is unchanged: an open PR stays pending (temporary
    # refusal), merged PRs retire, a no-PR obligation with a live branch waits.
    assert summary5["pending_after"] == 2
    assert summary40["pending_after"] == 9


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


# ---------------------------------------------------------------------------
# OI-2459 round 2: the head AFTER a gate run is read live, never from the
# tick's snapshot; the pre-run baseline reads the snapshot only while it is
# young enough to still describe "before this run".
# ---------------------------------------------------------------------------

HEAD_A = "a1a1a1a1" * 5
HEAD_B = "b2b2b2b2" * 5
GATE = "codex_gate"


class _ScriptedHeadManager:
    """Writes a decided PASS for ``commit_sha``; can move the live head mid-run.

    ``live_head_after`` models a fix-forward push that lands while the gate
    runs: the fixtures file the fake gh reads is rewritten AFTER the result is
    written, so the post-run live lookup answers the NEW head while the
    result still records the OLD one.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        commit_sha: str,
        fixtures_path: Optional[Path] = None,
        live_head_after: Optional[str] = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.commit_sha = commit_sha
        self.fixtures_path = fixtures_path
        self.live_head_after = live_head_after
        self.calls: List[Dict[str, Any]] = []

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "requests" / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"

    def request_and_execute(self, *, pr_number, branch, review_stack, risk_class,
                            changed_files, mode, dispatch_id="", **extra):
        self.calls.append({"pr_number": pr_number, "branch": branch})
        report = self.state_dir.parent / "unified_reports" / f"{GATE}-pr{pr_number}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("codex_gate report body", encoding="utf-8")
        for gate in review_stack:
            self._request_path(gate, pr_number).write_text(
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "completed"}),
                encoding="utf-8",
            )
            self._result_path(gate, pr_number).write_text(
                json.dumps({
                    "gate": gate,
                    "pr_number": pr_number,
                    "status": "pass",
                    "contract_hash": "sha256:oi-2459-round-2",
                    "report_path": str(report),
                    "commit_sha": self.commit_sha,
                }),
                encoding="utf-8",
            )
        if self.live_head_after and self.fixtures_path is not None:
            fixtures = json.loads(self.fixtures_path.read_text(encoding="utf-8"))
            for entry in (fixtures.get("pr_view") or {}).values():
                if isinstance(entry, dict):
                    entry["headRefOid"] = self.live_head_after
            self.fixtures_path.write_text(json.dumps(fixtures), encoding="utf-8")
        return {"pr_number": pr_number, "branch": branch, "gates": [], "has_required_failure": False}


def _single_pr_fixtures(dispatch_id: str, pr_number: int, head: str) -> Dict[str, Any]:
    branch = f"dispatch/{dispatch_id}"
    return {
        "pr_snapshot": [{
            "number": pr_number, "state": "OPEN", "headRefName": branch, "headRefOid": head,
        }],
        "pr_list_open": [{"number": pr_number, "headRefName": branch}],
        "pr_list_head": {branch: [{"number": pr_number}]},
        "pr_view": {
            str(pr_number): {"state": "OPEN", "headRefName": branch, "headRefOid": head},
        },
        "branch_refs": [f"refs/heads/{branch}"],
        "branches": {},
    }


def test_head_movement_with_the_snapshot_on_is_not_booked(tmp_path, monkeypatch, fake_gh):
    """The round-1 regression test again, with the tick snapshot ENABLED.

    A gate run takes minutes; a fix-forward push lands during it. The snapshot
    still says head A, the result records A, and the LIVE ``gh pr view --json
    headRefOid`` after the run answers B. Booking A would stamp a verdict about
    code that no longer exists. Red on 1b4ca29b, which read the post-run head
    from the pre-run snapshot.
    """
    monkeypatch.setattr(subprocess, "run", _REAL_SUBPROCESS_RUN)
    dispatch_id = "20261028-headmoved-snapshot-on"
    pr_number = 42001
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate=GATE, project_id="vnx-dev", pr_number=pr_number,
    )
    fake_gh.fixtures(_single_pr_fixtures(dispatch_id, pr_number, HEAD_A))
    fake_gh.reset()
    manager = _ScriptedHeadManager(
        state_dir, commit_sha=HEAD_A,
        fixtures_path=fake_gh.fixtures_path, live_head_after=HEAD_B,
    )
    _patch_runner(monkeypatch, manager)

    summary = runner.run(state_dir)

    record = json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))
    assert manager.calls, "the gate run itself is allowed; only the booking is refused"
    assert record["status"] == runner.STATUS_PENDING, (
        "a result recorded on the pre-run head must not be booked once the live "
        "head has moved, even when the tick snapshot still says A"
    )
    assert record["status"] != runner.STATUS_FULFILLED
    assert record["reason"] == "stale_evidence_sha_mismatch"
    assert HEAD_A[:8] in record["reason_detail"]
    assert HEAD_B[:8] in record["reason_detail"]
    assert summary["pending_after"] == 1
    assert summary["outcomes"][0]["action"] == "pending"
    assert fake_gh.counts().get("pr_view_headRefOid", 0) == 1, fake_gh.counts()


def _snapshot_age_tick(
    tmp_path: Path, monkeypatch, fake_gh: _FakeGh, *, age_seconds: float,
) -> Dict[str, Any]:
    """One gate launch whose tick snapshot is ``age_seconds`` old when it starts."""
    monkeypatch.setattr(subprocess, "run", _REAL_SUBPROCESS_RUN)
    dispatch_id = f"20261028-launch-age-{int(age_seconds)}"
    pr_number = 43001
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate=GATE, project_id="vnx-dev", pr_number=pr_number,
    )
    # The snapshot and the live head agree (B): this test measures WHICH source
    # the pre-run baseline read, not booking behaviour.
    fake_gh.fixtures(_single_pr_fixtures(dispatch_id, pr_number, HEAD_B))

    clock = {"now": 1000.0}
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock["now"])
    snapshot = runner._GithubSnapshot(OWNER_REPO)
    snapshot.prs()  # fetched at t=1000; the age is measured from this moment
    clock["now"] += age_seconds
    fake_gh.reset()

    manager = _ScriptedHeadManager(state_dir, commit_sha=HEAD_B)
    _patch_runner(monkeypatch, manager)
    monkeypatch.setattr(runner, "_tick_github_context", lambda sd: (OWNER_REPO, snapshot))
    summary = runner.run(state_dir)
    record = json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))
    return {"summary": summary, "record": record, "counts": fake_gh.counts()}


def test_a_fresh_snapshot_answers_the_pre_run_head_without_a_call(tmp_path, monkeypatch, fake_gh):
    fresh = _snapshot_age_tick(tmp_path / "fresh", monkeypatch, fake_gh, age_seconds=0)
    # Only the post-run LIVE read hits gh; the pre-run baseline came from the
    # snapshot taken in this same launch window.
    assert fresh["counts"].get("pr_view_headRefOid", 0) == 1, fresh["counts"]
    assert fresh["record"]["status"] == runner.STATUS_FULFILLED, fresh["record"]


def test_a_stale_snapshot_uses_the_single_call_for_the_pre_run_head(tmp_path, monkeypatch, fake_gh):
    stale = _snapshot_age_tick(
        tmp_path / "stale", monkeypatch, fake_gh,
        age_seconds=runner._LAUNCH_HEAD_SNAPSHOT_MAX_AGE_SECONDS + 1,
    )
    # Past the constant the launch does NOT trust the snapshot: one live read
    # before the run plus the mandatory live read after it.
    assert stale["counts"].get("pr_view_headRefOid", 0) == 2, stale["counts"]
    assert stale["record"]["status"] == runner.STATUS_FULFILLED, stale["record"]


# ---------------------------------------------------------------------------
# OI-2459 round 2: a failing snapshot's per-obligation fallbacks are capped
# per tick, and a rate-limit refusal stops them for the rest of the tick.
# ---------------------------------------------------------------------------


def _failing_snapshot_fixtures(n: int, *, rate_limit: bool = False) -> Dict[str, Any]:
    """A failed snapshot; the per-PR state fallback fails too (never "open")."""
    return {
        "pr_snapshot": None,
        "pr_list_open": [],
        "pr_list_head": {},
        "pr_view": {},  # the fake gh has no state answer -> the fallback fails
        "rate_limit_pr_view": rate_limit,
        "branch_refs": [],
        "branches": {},
    }


def _register_pr_obligations(state_dir: Path, *, count: int, pr_base: int = 9100) -> List[str]:
    dispatch_ids = [f"20261020-fallback-{index:03d}" for index in range(count)]
    for index, dispatch_id in enumerate(dispatch_ids):
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate=GATE, project_id="vnx-dev",
            pr_number=pr_base + index,
        )
    return dispatch_ids


def test_a_failed_snapshot_fallback_stops_at_the_cap(tmp_path, monkeypatch, fake_gh, caplog):
    state_dir = _make_state_dir(tmp_path)
    _register_pr_obligations(state_dir, count=40)
    fake_gh.fixtures(_failing_snapshot_fixtures(40))
    fake_gh.reset()
    _patch_runner(monkeypatch, _FakeReviewGateManager(state_dir))

    with caplog.at_level(logging.WARNING, logger="gate_obligation_runner"):
        summary = runner.run(state_dir)

    cap = runner._FALLBACK_CALL_CAP_PER_TICK
    counts = fake_gh.counts()
    assert cap == 20
    assert counts.get("pr_view_state", 0) == cap, (
        f"the per-obligation fallback must stop at {cap} calls, not run 40: {counts}"
    )
    assert summary["pending_after"] == 40, "a skipped lookup is a wait, never a retirement"
    assert all(outcome["action"] == "unresolvable" for outcome in summary["outcomes"])
    skipped_lines = [rec.getMessage() for rec in caplog.records if "lookup(s) skipped" in rec.getMessage()]
    assert len(skipped_lines) == 1, skipped_lines
    assert "20 lookup(s) skipped" in skipped_lines[0], skipped_lines[0]


def test_a_rate_limit_stops_the_fallback_for_the_rest_of_the_tick(
    tmp_path, monkeypatch, fake_gh, caplog,
):
    state_dir = _make_state_dir(tmp_path)
    _register_pr_obligations(state_dir, count=3)
    fake_gh.fixtures(_failing_snapshot_fixtures(3, rate_limit=True))
    fake_gh.reset()
    _patch_runner(monkeypatch, _FakeReviewGateManager(state_dir))

    with caplog.at_level(logging.WARNING, logger="gate_obligation_runner"):
        summary = runner.run(state_dir)

    counts = fake_gh.counts()
    assert counts.get("pr_view_state", 0) == 1, (
        f"a rate-limit answer on the first fallback must stop every later one: {counts}"
    )
    assert summary["pending_after"] == 3
    assert all(outcome["action"] == "unresolvable" for outcome in summary["outcomes"])
    skipped_lines = [rec.getMessage() for rec in caplog.records if "lookup(s) skipped" in rec.getMessage()]
    assert len(skipped_lines) == 1, skipped_lines
    assert "rate limit" in skipped_lines[0], skipped_lines[0]
