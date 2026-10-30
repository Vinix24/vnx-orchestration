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

OI-2461 (round 3) makes the SNAPSHOT a filter and a LIVE READ the thing that
books:

  - every decision that books a TERMINAL status on a snapshot-derived fact
    (evidence rescue, retire on MERGED/CLOSED, retire on a branch gone from
    the listing) confirms that fact with exactly ONE live read right before
    the booking; a live answer that differs, cannot be read, or was skipped by
    the per-tick live-confirm cap (``_LIVE_CONFIRM_CAP_PER_TICK`` = 20) books
    nothing;
  - a lookup the tick's budget SKIPPED (fallback cap, rate-limit stop, live
    confirm cap) is its own outcome — ``DEFERRED`` at the lookup,
    ``{"kind": "deferred"}`` at the decision — never the ``None`` that means
    "could not be determined". A deferred obligation gets no write,
    ``attempts`` does not move, and it can never escalate to
    ``not_executable`` through deferrals. A GENUINE failure (gh missing,
    timed out, non-zero exit without a rate-limit marker) keeps today's
    unresolvable/escalate path exactly.

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
from gate_obligations import obligation_path, register_obligation, update_obligation  # noqa: E402

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
    """N obligations, NONE of which books a terminal status (OI-2461).

    Exactly ONE OPEN PR — so exactly one gate LAUNCH, which legitimately costs
    one LIVE post-run head read — and every other obligation has no PR and a
    live branch, so it simply waits. No retire and no evidence rescue: a
    terminal booking legitimately costs one live confirmation (OI-2461), so
    the constant-cost invariant is measured on the NON-BOOKING path here; the
    booking path's own bound is pinned separately below
    (``_LIVE_CONFIRM_CAP_PER_TICK``).
    """
    snapshot: List[Dict[str, Any]] = []
    open_prs: List[Dict[str, Any]] = []
    pr_view: Dict[str, Any] = {}
    pr_list_head: Dict[str, Any] = {}
    branch_refs: List[str] = []
    for index, dispatch_id in enumerate(_dispatch_ids(n)):
        branch = f"dispatch/{dispatch_id}"
        branch_refs.append(f"refs/heads/{branch}")
        pr_list_head[branch] = []
        if index != 0:
            continue
        number = 1000
        head = "0" * 40
        snapshot.append({
            "number": number, "state": "OPEN", "headRefName": branch, "headRefOid": head,
        })
        open_prs.append({"number": number, "headRefName": branch})
        pr_list_head[branch] = [{"number": number}]
        pr_view[str(number)] = {"state": "OPEN", "headRefName": branch, "headRefOid": head}
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

    # OI-2459 round 2 / OI-2461: the per-tick cost is still independent of the
    # backlog on the NON-BOOKING path. Both fixtures have exactly one gate
    # launch, so the one live head read it needs does not vary with N either —
    # everything else is the fixed snapshot + listing + open list. No
    # obligation here books a terminal status, so no live confirmation is
    # owed; the booking path's own cap is pinned further down.
    assert sum(counts5.values()) == 4, counts5   # snapshot + listing + open list + 1 live head read
    assert sum(counts40.values()) == sum(counts5.values()), (
        f"the tick's gh budget must not grow with the obligation count: {counts5} vs {counts40}"
    )
    # The decision logic is unchanged: an open PR stays pending (temporary
    # refusal), a no-PR obligation with a live branch waits.
    assert summary5["pending_after"] == 5
    assert summary40["pending_after"] == 40


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
    # OI-2461: the snapshot only SELECTS the merged PR; the retire itself is
    # booked on a LIVE state read, so both paths cost exactly one call — and
    # the deferred/single-call path needs no second read (it was never a
    # snapshot fact).
    assert from_snapshot["counts"].get("pr_view_state", 0) == 1, from_snapshot["counts"]
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
    actions = [outcome["action"] for outcome in summary["outcomes"]]
    # The first `cap` lookups were ASKED and failed (the fake gh has no answer
    # for them): a genuine failure keeps today's unresolvable path. Every
    # lookup after the cap was NEVER ASKED (OI-2461): that is a deferral —
    # pending, nothing written, no attempt counted — never "unresolvable".
    assert actions[:cap] == ["unresolvable"] * cap, actions
    assert actions[cap:] == ["pending"] * (40 - cap), actions
    assert all(outcome.get("deferred") for outcome in summary["outcomes"][cap:])
    assert summary["pending_after"] == 40, "a skipped lookup is a wait, never a retirement"
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
    actions = [outcome["action"] for outcome in summary["outcomes"]]
    # The lookup that RECEIVED the rate-limit reply was asked and failed — a
    # genuine failure, today's unresolvable path. The two lookups the stop
    # SKIPPED were never asked: deferred (OI-2461), nothing written.
    assert actions == ["unresolvable", "pending", "pending"], actions
    assert all(outcome.get("deferred") for outcome in summary["outcomes"][1:])
    assert summary["pending_after"] == 3
    skipped_lines = [rec.getMessage() for rec in caplog.records if "lookup(s) skipped" in rec.getMessage()]
    assert len(skipped_lines) == 1, skipped_lines
    assert "rate limit" in skipped_lines[0], skipped_lines[0]


# ---------------------------------------------------------------------------
# OI-2461: the snapshot FILTERS, a LIVE read BOOKS — and a skipped lookup is
# DEFERRED, never an attempt.
# ---------------------------------------------------------------------------


def _seed_decided_evidence(
    state_dir: Path, *, dispatch_id: str, pr_number: int, commit_sha: str,
    gate: str = GATE,
) -> Path:
    """Write a DECIDED, complete-evidence result record for one dispatch."""
    report = state_dir.parent / "unified_reports" / f"{gate}-pr{pr_number}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"{gate} report for {dispatch_id}", encoding="utf-8")
    path = state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"
    path.write_text(json.dumps({
        "gate": gate,
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
        "status": "pass",
        "contract_hash": "sha256:oi-2461-round-3",
        "report_path": str(report),
        "commit_sha": commit_sha,
    }), encoding="utf-8")
    return path


def _evidence_fixtures(
    dispatch_id: str, pr_number: int, *,
    snapshot_head: str, live_head: str,
    snapshot_state: str = "OPEN", live_state: str = "OPEN",
) -> Dict[str, Any]:
    """The snapshot and the live ``pr view`` disagree — the round-3 shape."""
    branch = f"dispatch/{dispatch_id}"
    return {
        "pr_snapshot": [{
            "number": pr_number, "state": snapshot_state,
            "headRefName": branch, "headRefOid": snapshot_head,
        }],
        "pr_list_open": [],
        "pr_list_head": {branch: [{"number": pr_number}]},
        "pr_view": {
            str(pr_number): {
                "state": live_state, "headRefName": branch, "headRefOid": live_head,
            },
        },
        "branch_refs": [f"refs/heads/{branch}"],
        "branches": {},
    }


def _open_evidence_tick(
    root: Path, monkeypatch, fake_gh: _FakeGh, *,
    dispatch_id: str,
    pr_number: Optional[int],
    fixtures: Dict[str, Any],
    evidence_sha: Optional[str] = None,
    attempts: int = 0,
    occupancy_lock: bool = False,
) -> Dict[str, Any]:
    """Register one obligation, run one tick, return everything worth asserting."""
    monkeypatch.setattr(subprocess, "run", _REAL_SUBPROCESS_RUN)
    state_dir = _make_state_dir(root)
    path = register_obligation(
        state_dir, dispatch_id=dispatch_id, gate=GATE, project_id="vnx-dev",
        pr_number=pr_number,
    )
    if evidence_sha is not None:
        _seed_decided_evidence(
            state_dir, dispatch_id=dispatch_id, pr_number=pr_number, commit_sha=evidence_sha,
        )
    if attempts:
        update_obligation(path, attempts=attempts)
    if occupancy_lock:
        lock_path = state_dir / "dispatch_worktree_claims" / f"{dispatch_id}.occupancy"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("", encoding="utf-8")
    fake_gh.fixtures(fixtures)
    fake_gh.reset()
    manager = _FakeReviewGateManager(state_dir)
    _patch_runner(monkeypatch, manager)
    summary = runner.run(state_dir)
    return {
        "summary": summary,
        "record": json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8")),
        "counts": fake_gh.counts(),
        "manager": manager,
        "state_dir": state_dir,
        "path": path,
    }


def test_evidence_current_on_the_snapshot_but_not_live_is_never_booked(
    tmp_path, monkeypatch, fake_gh,
):
    """Red on 9eeeb8f4: the verdict names commit A, the snapshot still says A,
    the PR has advanced to B — ``_fulfilling_result`` judged the evidence
    against the SNAPSHOT and booked ``fulfilled`` with zero live reads."""
    dispatch_id = "20261028-oi2461-head-advanced"
    tick = _open_evidence_tick(
        tmp_path, monkeypatch, fake_gh,
        dispatch_id=dispatch_id, pr_number=62001,
        fixtures=_evidence_fixtures(
            dispatch_id, 62001, snapshot_head=HEAD_A, live_head=HEAD_B,
        ),
        evidence_sha=HEAD_A,
    )

    record, counts = tick["record"], tick["counts"]
    assert tick["manager"].calls == [], "no gate run is owed; the evidence check comes first"
    assert record["status"] == runner.STATUS_PENDING, record
    assert record["status"] != runner.STATUS_FULFILLED, (
        "evidence about A must never be booked once the LIVE head is B, even "
        "while the tick snapshot still says A"
    )
    assert (record.get("attempts") or 0) == 0, "a deferral counts no attempt"
    assert tick["summary"]["outcomes"][0]["action"] == "pending"
    assert tick["summary"]["outcomes"][0]["deferred"] is True
    assert tick["summary"]["pending_after"] == 1
    assert counts.get("pr_view_headRefOid", 0) == 1, (
        f"the booking must rest on exactly one live head read: {counts}"
    )


def test_snapshot_closed_but_live_open_does_not_retire(tmp_path, monkeypatch, fake_gh):
    """Red on 9eeeb8f4: a CLOSED row in the snapshot retired the obligation
    without ever asking GitHub whether the PR was still closed."""
    dispatch_id = "20261028-oi2461-closed-vs-open"
    tick = _open_evidence_tick(
        tmp_path, monkeypatch, fake_gh,
        dispatch_id=dispatch_id, pr_number=62002,
        fixtures=_evidence_fixtures(
            dispatch_id, 62002, snapshot_head=HEAD_A, live_head=HEAD_A,
            snapshot_state="CLOSED", live_state="OPEN",
        ),
    )

    record, counts = tick["record"], tick["counts"]
    assert record["status"] == runner.STATUS_PENDING, record
    assert record["status"] != runner.STATUS_RETIRED, (
        "a snapshot CLOSED that the live read says is OPEN must not retire"
    )
    assert record.get("reason") != runner.REASON_PR_CLOSED
    assert (record.get("attempts") or 0) == 0, "a deferral counts no attempt"
    assert tick["summary"]["outcomes"][0]["action"] == "pending"
    assert tick["summary"]["outcomes"][0]["deferred"] is True
    assert counts.get("pr_view_state", 0) == 1, (
        f"the retire decision must rest on exactly one live state read: {counts}"
    )


def test_branch_gone_in_the_listing_but_present_live_does_not_retire(
    tmp_path, monkeypatch, fake_gh,
):
    """Red on 9eeeb8f4: a branch missing from ``matching-refs`` retired the
    obligation; the live single-branch check says it is still there."""
    dispatch_id = "20261028-oi2461-branch-gone"
    branch = f"dispatch/{dispatch_id}"
    fixtures = {
        # No PR for this dispatch, and the LISTING says its branch is gone.
        "pr_snapshot": [],
        "pr_list_open": [],
        "pr_list_head": {branch: []},
        "pr_view": {},
        "branch_refs": [],
        # ...but the LIVE single-branch check says it exists.
        "branches": {branch: "exists"},
    }
    tick = _open_evidence_tick(
        tmp_path, monkeypatch, fake_gh,
        dispatch_id=dispatch_id, pr_number=None, fixtures=fixtures,
        occupancy_lock=True,  # the lock file exists and is unheld: not live
    )

    record, counts = tick["record"], tick["counts"]
    assert record["status"] == runner.STATUS_PENDING, record
    assert record["status"] != runner.STATUS_RETIRED, (
        "a branch gone from the listing but present on the live check must "
        "not retire the obligation"
    )
    assert tick["summary"]["outcomes"][0]["deferred"] is True
    assert (record.get("attempts") or 0) == 0, "a deferral counts no attempt"
    assert counts.get("branch_single", 0) == 1, (
        f"the retire must rest on exactly one live branch read: {counts}"
    )


def test_an_exhausted_budget_defers_and_never_escalates(tmp_path, monkeypatch, fake_gh):
    """Red on 9eeeb8f4: a lookup SKIPPED by the budget answered ``None``,
    which flowed into ``unresolvable``, advanced ``attempts``, and at 96 booked
    the terminal ``not_executable`` — over a lookup that was never even asked.

    The budget is exhausted by a rate-limit refusal on the FIRST lookup, which
    stops every later one for the rest of the tick (OI-2459 round 2) — repeated
    on every tick. The last obligation is one attempt short of the escalation
    threshold: one MORE inexhausted tick would book it not_executable on
    9eeeb8f4, and must never do so here.
    """
    monkeypatch.setattr(subprocess, "run", _REAL_SUBPROCESS_RUN)
    dispatch_ids = [f"20261028-oi2461-noescalate-{index}" for index in range(3)]
    state_dir = _make_state_dir(tmp_path)
    for index, dispatch_id in enumerate(dispatch_ids):
        path = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate=GATE, project_id="vnx-dev",
            pr_number=62003 + index,
        )
        if index == len(dispatch_ids) - 1:
            update_obligation(path, attempts=95)
    fake_gh.fixtures(_failing_snapshot_fixtures(len(dispatch_ids), rate_limit=True))
    fake_gh.reset()
    manager = _FakeReviewGateManager(state_dir)
    _patch_runner(monkeypatch, manager)

    skipped = dispatch_ids[-1]
    for _ in range(3):
        summary = runner.run(state_dir)
        record = json.loads(obligation_path(state_dir, skipped).read_text(encoding="utf-8"))
        outcome = [
            item for item in summary["outcomes"] if item["dispatch_id"] == skipped
        ][0]
        assert outcome["action"] == "pending", outcome
        assert outcome["deferred"] is True, outcome
        assert record["status"] == runner.STATUS_PENDING, record
        assert record["status"] != runner.STATUS_NOT_EXECUTABLE, (
            "a lookup the budget skipped must never escalate an obligation"
        )
        assert (record.get("attempts") or 0) == 95, (
            "a deferral must not move attempts, so deferrals can never escalate"
        )
    assert manager.calls == []


def test_a_genuine_gh_failure_still_advances_attempts(tmp_path, monkeypatch, fake_gh):
    """A real failure (non-zero exit, no rate-limit marker) is NOT a deferral."""
    dispatch_id = "20261028-oi2461-genuine-failure"
    tick = _open_evidence_tick(
        tmp_path, monkeypatch, fake_gh,
        dispatch_id=dispatch_id, pr_number=62004,
        fixtures=_failing_snapshot_fixtures(1),
    )

    outcome = tick["summary"]["outcomes"][0]
    assert outcome["action"] == "unresolvable", outcome
    assert not outcome.get("deferred"), outcome
    assert (tick["record"].get("attempts") or 0) == 1, tick["record"]
    # Exactly as on origin/main: a genuine failure is recorded as unresolvable
    # and counts its attempt (the escalate branch converts it to
    # not_executable once attempts reach the threshold).
    assert tick["record"]["status"] == runner.STATUS_UNRESOLVABLE, tick["record"]


def test_a_genuine_gh_failure_still_escalates_at_the_threshold(
    tmp_path, monkeypatch, fake_gh,
):
    """The escalate path is unchanged for a real failure (attempts 95 -> 96)."""
    dispatch_id = "20261028-oi2461-genuine-escalation"
    tick = _open_evidence_tick(
        tmp_path, monkeypatch, fake_gh,
        dispatch_id=dispatch_id, pr_number=62005,
        fixtures=_failing_snapshot_fixtures(1),
        attempts=95,
    )

    assert tick["summary"]["outcomes"][0]["action"] == "not_executable", tick["summary"]["outcomes"]
    assert not tick["summary"]["outcomes"][0].get("deferred")
    assert tick["record"]["status"] == runner.STATUS_NOT_EXECUTABLE, tick["record"]
    assert (tick["record"].get("attempts") or 0) == 96, tick["record"]


def _booking_fixtures(n: int) -> Dict[str, Any]:
    """N obligations, every one wanting a terminal booking, all CURRENT."""
    snapshot: List[Dict[str, Any]] = []
    pr_view: Dict[str, Any] = {}
    pr_list_head: Dict[str, Any] = {}
    branch_refs: List[str] = []
    for index, dispatch_id in enumerate(_dispatch_ids(n)):
        branch = f"dispatch/{dispatch_id}"
        number = 2000 + index
        head = f"{index:08x}" * 5
        branch_refs.append(f"refs/heads/{branch}")
        pr_list_head[branch] = [{"number": number}]
        snapshot.append({
            "number": number, "state": "OPEN", "headRefName": branch, "headRefOid": head,
        })
        pr_view[str(number)] = {"state": "OPEN", "headRefName": branch, "headRefOid": head}
    return {
        "pr_snapshot": snapshot,
        "pr_list_open": [],  # the read-only diagnostics stay quiet
        "pr_list_head": pr_list_head,
        "pr_view": pr_view,
        "branch_refs": branch_refs,
        "branches": {},
    }


def test_live_confirmations_stop_at_the_cap_and_the_rest_are_deferred(
    tmp_path, monkeypatch, fake_gh,
):
    """40 bookings want a live confirmation; only the cap is spent."""
    monkeypatch.setattr(subprocess, "run", _REAL_SUBPROCESS_RUN)
    n = 40
    capacity = runner._LIVE_CONFIRM_CAP_PER_TICK
    state_dir = _make_state_dir(tmp_path)
    for index, dispatch_id in enumerate(_dispatch_ids(n)):
        number = 2000 + index
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate=GATE, project_id="vnx-dev",
            pr_number=number,
        )
        _seed_decided_evidence(
            state_dir, dispatch_id=dispatch_id, pr_number=number,
            commit_sha=f"{index:08x}" * 5,
        )
    fake_gh.fixtures(_booking_fixtures(n))
    fake_gh.reset()
    _patch_runner(monkeypatch, _FakeReviewGateManager(state_dir))

    summary = runner.run(state_dir)

    counts = fake_gh.counts()
    assert counts.get("pr_view_headRefOid", 0) == capacity, (
        f"live confirmations must stop at {capacity} per tick: {counts}"
    )
    statuses = {}
    for dispatch_id in _dispatch_ids(n):
        record = json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))
        statuses.setdefault(record["status"], []).append(dispatch_id)
        if record["status"] == runner.STATUS_PENDING:
            assert (record.get("attempts") or 0) == 0, record
    assert len(statuses.get(runner.STATUS_FULFILLED, [])) == capacity, statuses
    assert len(statuses.get(runner.STATUS_PENDING, [])) == n - capacity, statuses
    assert summary["pending_after"] == n - capacity
    deferred = [outcome for outcome in summary["outcomes"] if outcome.get("deferred")]
    assert len(deferred) == n - capacity, deferred
