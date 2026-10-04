"""Tests for pr_queue_state.py (Phase 2.1).

Covers:
  1. Schema validation — output matches pr_queue/1.1 schema
  2. Empty state — no open PRs → empty arrays
  3. Integration — build_t0_state output contains pr_queue key
  4. Gates map — gate_passed/gate_failed register events populate correctly
  5. Atomic write — pr_queue_state.json written atomically
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from pr_queue_state import (
    _build_gates_map,
    build_pr_queue_state,
    write_pr_queue_state,
)
from build_t0_state import build_t0_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dirs(tmp_path: Path):
    state_dir = tmp_path / "state"
    dispatch_dir = tmp_path / "dispatches"
    state_dir.mkdir(parents=True)
    (dispatch_dir / "pending").mkdir(parents=True)
    (dispatch_dir / "active").mkdir(parents=True)
    (dispatch_dir / "conflicts").mkdir(parents=True)
    return state_dir, dispatch_dir


# ---------------------------------------------------------------------------
# 1. Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_schema_field_present(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            result = build_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        assert result["schema"] == "pr_queue/1.1"

    def test_required_keys_present(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            result = build_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        for key in ("schema", "timestamp", "open_prs", "merged_today"):
            assert key in result, f"Missing key: {key}"

    def test_open_prs_is_list(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            result = build_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        assert isinstance(result["open_prs"], list)

    def test_open_pr_fields_complete(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        row = {"number": 42, "title": "Test PR", "branch": "feat/test", "state": "active",
               "head_sha": "a" * 40, "mergeable": "mergeable", "merge_state": "clean"}
        ci = {"workflow": "VNX CI", "state": "success", "conclusion": "success",
              "run_id": 7, "reason": None}
        with patch("pr_queue_state._get_open_prs", return_value=([row], None)), \
             patch("pr_queue_state._measure_ci", return_value={42: ci}), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            result = build_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        assert len(result["open_prs"]) == 1
        pr = result["open_prs"][0]
        for field in ("number", "title", "branch", "state", "head_sha", "mergeable",
                      "merge_state", "ci", "ci_status", "gates_passed", "blocked_on"):
            assert field in pr, f"Missing pr field: {field}"

    def test_is_json_serializable(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            result = build_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        parsed = json.loads(json.dumps(result))
        assert parsed["schema"] == "pr_queue/1.1"


# ---------------------------------------------------------------------------
# 2. Empty state
# ---------------------------------------------------------------------------

class TestEmptyState:
    def test_empty_register_empty_prs(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            result = build_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        assert result["open_prs"] == []
        assert result["merged_today"] == []

    def test_no_register_file_empty_arrays(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        # No dispatch_register.ndjson present, gh also returns nothing
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            result = build_pr_queue_state(state_dir, project_root=tmp_path)
        assert result["open_prs"] == []
        assert result["merged_today"] == []

    def test_gh_failure_produces_empty_prs(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        # _get_open_prs already returns [] on gh failure; verify contract
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            result = build_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        assert result["open_prs"] == []


# ---------------------------------------------------------------------------
# 3. Integration: hooked into build_t0_state output
# ---------------------------------------------------------------------------

class TestBuildT0StateIntegration:
    def test_pr_queue_key_in_state(self, tmp_path):
        state_dir, dispatch_dir = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)
        assert "pr_queue" in state, "pr_queue key missing from build_t0_state output"

    def test_pr_queue_has_correct_schema(self, tmp_path):
        state_dir, dispatch_dir = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)
        pr_queue = state.get("pr_queue", {})
        assert pr_queue.get("schema") == "pr_queue/1.1"

    def test_pr_queue_is_dict(self, tmp_path):
        state_dir, dispatch_dir = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)
        assert isinstance(state.get("pr_queue"), dict)


# ---------------------------------------------------------------------------
# 4. Gates map
# ---------------------------------------------------------------------------

class TestGatesMap:
    def test_gate_passed_recorded(self):
        events = [
            {"event": "gate_passed", "pr_number": 10, "gate": "codex",
             "timestamp": "2026-04-28T00:00:00Z"},
        ]
        result = _build_gates_map(events)
        assert result[10]["gates_passed"] == ["codex"]
        assert result[10]["blocked_on"] == []

    def test_gate_failed_recorded(self):
        events = [
            {"event": "gate_failed", "pr_number": 10, "gate": "gemini",
             "timestamp": "2026-04-28T00:00:00Z"},
        ]
        result = _build_gates_map(events)
        assert result[10]["blocked_on"] == ["gemini"]

    def test_gate_failed_removes_from_passed(self):
        events = [
            {"event": "gate_passed", "pr_number": 10, "gate": "codex",
             "timestamp": "2026-04-28T00:00:00Z"},
            {"event": "gate_failed", "pr_number": 10, "gate": "codex",
             "timestamp": "2026-04-28T00:00:01Z"},
        ]
        result = _build_gates_map(events)
        assert "codex" not in result[10]["gates_passed"]
        assert "codex" in result[10]["blocked_on"]

    def test_multiple_gates_accumulated(self):
        events = [
            {"event": "gate_passed", "pr_number": 5, "gate": "codex",
             "timestamp": "2026-04-28T00:00:00Z"},
            {"event": "gate_passed", "pr_number": 5, "gate": "gemini",
             "timestamp": "2026-04-28T00:00:01Z"},
        ]
        result = _build_gates_map(events)
        assert set(result[5]["gates_passed"]) == {"codex", "gemini"}

    def test_no_pr_number_skipped(self):
        events = [
            {"event": "gate_passed", "gate": "codex",
             "timestamp": "2026-04-28T00:00:00Z"},
        ]
        result = _build_gates_map(events)
        assert result == {}

    def test_empty_events(self):
        assert _build_gates_map([]) == {}


# ---------------------------------------------------------------------------
# 5. Atomic write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_file_written(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            out = write_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["schema"] == "pr_queue/1.1"

    def test_no_tmp_files_left(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            write_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        tmp_files = list(state_dir.glob("*.tmp.*"))
        assert tmp_files == [], f"Temp files left: {tmp_files}"

    def test_output_is_valid_json(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            out = write_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        parsed = json.loads(out.read_text())
        assert isinstance(parsed, dict)

    def test_creates_state_dir_if_missing(self, tmp_path):
        state_dir = tmp_path / "state" / "nested"
        with patch("pr_queue_state._get_open_prs", return_value=([], None)), \
             patch("pr_queue_state._get_merged_today", return_value=([], None)):
            out = write_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        assert out.exists()


# ---------------------------------------------------------------------------
# PR row: head, conflict state, CI on the head (P1, P2, P4, P5-P10)
# ---------------------------------------------------------------------------

import base64
import subprocess
import threading
import time

import pr_queue_state as pqs

_PROTECTION_YAML = (
    (_REPO_ROOT / "scripts" / "forge" / "branch_protection.yaml").read_text(encoding="utf-8")
    + "\nci_workflow: Project CI\n"
)


def _sha(number: int) -> str:
    return f"{number:040x}"


def _pr(number, branch="feat/x", draft=False, mergeable="MERGEABLE", merge_state="CLEAN"):
    return {"number": number, "title": f"PR {number}", "headRefName": branch, "isDraft": draft,
            "headRefOid": _sha(number), "mergeable": mergeable, "mergeStateStatus": merge_state}


def _run(number, conclusion="success", status="completed", run_id=None, created="2026-01-01T00:00:00Z"):
    return {"conclusion": conclusion, "headSha": _sha(number), "status": status,
            "databaseId": run_id or number * 10, "createdAt": created}


class FakeGh:
    """Stands in for ``subprocess.run`` and answers every ``gh`` call the builder and the judge make."""

    def __init__(self, open_prs=(), merged=(), runs=None, open_fail=None, merged_fail=None,
                 run_fail=(), yaml_fail=False, hang=()):
        self.open_prs, self.merged, self.runs = list(open_prs), list(merged), runs or {}
        self.open_fail, self.merged_fail = open_fail, merged_fail
        self.run_fail, self.yaml_fail, self.hang = set(run_fail), yaml_fail, set(hang)
        self.calls = []

    @staticmethod
    def _done(stdout="", rc=0, stderr=""):
        return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("cwd")))
        if argv[1:3] == ["pr", "list"]:
            return self._pr_list(argv[argv.index("--state") + 1])
        if argv[1:3] == ["auth", "status"]:
            return self._done()
        if argv[1:3] == ["run", "list"]:
            return self._run_list(argv[argv.index("--commit") + 1])
        return self._api(argv[2])

    def _pr_list(self, state):
        fail = self.open_fail if state == "open" else self.merged_fail
        if fail:
            return self._done(rc=1, stderr=fail)
        return self._done(json.dumps(self.open_prs if state == "open" else self.merged))

    def _run_list(self, sha):
        if sha in self.hang:
            threading.Event().wait(60)
        if sha in self.run_fail:
            return self._done(rc=1, stderr="HTTP 502 bad gateway")
        return self._done(json.dumps(self.runs.get(sha, [])))

    def _api(self, endpoint):
        if "/commits/" in endpoint:
            return self._done("abc")
        if self.yaml_fail:
            return self._done(rc=1, stderr="HTTP 500 server error")
        if "contents/.vnx/" in endpoint:
            return self._done(rc=1, stderr="gh: Not Found (HTTP 404)")
        content = base64.b64encode(_PROTECTION_YAML.encode()).decode()
        return self._done(json.dumps({"content": content}))


@pytest.fixture
def gh_env(monkeypatch):
    for name in ("VNX_MERGE_OVERRIDE_REASON", "VNX_CI_WORKFLOW_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("merge_preflight_ci_check.shutil.which", lambda _name: "/usr/bin/gh")


def _build(tmp_path, fake):
    state_dir, _ = _make_dirs(tmp_path)
    with patch("subprocess.run", fake):
        return build_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)


def _rows(section):
    return {row["number"]: row for row in section["open_prs"]}


class TestRowHeadAndConflictState:
    def test_p1_row_carries_head_mergeable_and_merge_state(self, tmp_path, gh_env):
        fake = FakeGh(open_prs=[
            _pr(1, mergeable="CONFLICTING", merge_state="DIRTY"),
            _pr(2, mergeable="UNKNOWN", merge_state="UNKNOWN"),
            _pr(3, mergeable="MERGEABLE", merge_state="BLOCKED"),
            _pr(4, mergeable="MERGEABLE", merge_state="HAS_HOOKS"),
        ])
        rows = _rows(_build(tmp_path, fake))
        assert rows[1]["head_sha"] == _sha(1) and len(rows[1]["head_sha"]) == 40
        assert (rows[1]["mergeable"], rows[1]["merge_state"]) == ("conflicting", "dirty")
        assert (rows[2]["mergeable"], rows[2]["merge_state"]) == ("unknown", "unknown")
        assert (rows[3]["mergeable"], rows[3]["merge_state"]) == ("mergeable", "blocked")
        assert rows[4]["merge_state"] == "has_hooks"

    def test_the_open_pr_call_asks_for_the_head_and_not_for_the_roll_up(self, tmp_path, gh_env):
        fake = FakeGh(open_prs=[_pr(1)])
        _build(tmp_path, fake)
        argv = next(a for a, _ in fake.calls if a[1:3] == ["pr", "list"] and "open" in a)
        fields = argv[argv.index("--json") + 1].split(",")
        assert {"headRefOid", "mergeable", "mergeStateStatus"} <= set(fields)
        assert "statusCheckRollup" not in fields


class TestCiOnTheHead:
    def test_p2_ci_state_per_head_from_the_judge(self, tmp_path, gh_env):
        fake = FakeGh(
            open_prs=[_pr(n) for n in range(1, 7)],
            runs={
                _sha(1): [_run(1)],
                _sha(2): [_run(2, conclusion=None, status="in_progress")],
                _sha(3): [_run(3, conclusion="failure")],
                _sha(4): [],
                _sha(5): [_run(5, run_id=51), _run(5, conclusion="failure", run_id=52)],
            },
            run_fail=[_sha(6)],
        )
        rows = _rows(_build(tmp_path, fake))
        seen = {n: (r["ci"]["state"], r["ci_status"]) for n, r in rows.items()}
        assert seen == {
            1: ("success", "pass"), 2: ("running", "pending"), 3: ("failed", "fail"),
            4: ("no_run", "unknown"), 5: ("undetermined", "unknown"), 6: ("unmeasured", "unknown"),
        }
        assert rows[1]["ci"] == {"workflow": "Project CI", "state": "success", "conclusion": "success",
                                 "run_id": 10, "reason": None}
        assert rows[3]["ci"]["conclusion"] == "failure"
        assert rows[6]["ci"]["reason"] == "gh_run_list_failed"

    def test_p4_the_builder_holds_no_ci_rule(self, tmp_path, gh_env):
        sentinel = {"verdict": "GO", "reason": "running", "workflow_name": "Sentinel CI",
                    "ci_conclusion": None, "ci_run_id": 99}
        fake = FakeGh(open_prs=[_pr(1)], runs={_sha(1): [_run(1)]})
        with patch("pr_queue_state.check_ci_run_for_head", return_value=sentinel) as judge:
            rows = _rows(_build(tmp_path, fake))
        assert judge.call_args.kwargs["head_sha"] == _sha(1)
        assert rows[1]["ci"]["state"] == "running" and rows[1]["ci"]["run_id"] == 99
        assert rows[1]["ci_status"] == "pending"

    def test_a_code_the_builder_does_not_know_is_unmeasured_with_that_code(self, tmp_path, gh_env):
        sentinel = {"verdict": "NO-GO", "reason": "gh_unauthenticated", "workflow_name": "Project CI"}
        with patch("pr_queue_state.check_ci_run_for_head", return_value=sentinel):
            rows = _rows(_build(tmp_path, FakeGh(open_prs=[_pr(1)])))
        assert rows[1]["ci"]["state"] == "unmeasured"
        assert rows[1]["ci"]["reason"] == "gh_unauthenticated"

    def test_a_raising_judge_leaves_one_row_unmeasured_and_the_section_available(self, tmp_path, gh_env):
        with patch("pr_queue_state.check_ci_run_for_head", side_effect=RuntimeError("boom")):
            section = _build(tmp_path, FakeGh(open_prs=[_pr(1)]))
        assert section["available"] is True
        assert _rows(section)[1]["ci"]["reason"] == "judge_failed"

    def test_p9_a_failing_workflow_name_read_leaves_every_row_unmeasured(self, tmp_path, gh_env):
        fake = FakeGh(open_prs=[_pr(1), _pr(2)], yaml_fail=True)
        section = _build(tmp_path, fake)
        assert section["available"] is True
        for row in section["open_prs"]:
            assert row["ci"]["state"] == "unmeasured"
            assert row["ci"]["reason"] == "workflow_unreadable"
        assert not any(a[1:3] == ["run", "list"] for a, _ in fake.calls)

    def test_p10_an_override_gives_overridden_never_success(self, tmp_path, gh_env, monkeypatch):
        monkeypatch.setenv("VNX_MERGE_OVERRIDE_REASON", "operator says so")
        fake = FakeGh(open_prs=[_pr(1)], runs={_sha(1): [_run(1)]})
        rows = _rows(_build(tmp_path, fake))
        assert rows[1]["ci"]["state"] == "overridden"
        assert rows[1]["ci_status"] == "unknown"

    def test_the_workflow_name_is_read_once_per_rebuild(self, tmp_path, gh_env):
        fake = FakeGh(open_prs=[_pr(n) for n in range(1, 5)])
        _build(tmp_path, fake)
        contents = [a for a, _ in fake.calls if a[1] == "api" and "/contents/" in a[2]]
        assert len(contents) == 2  # the two search paths of one read, not one per row


class TestMeasureOrderAndCap:
    def test_p6_thirteen_prs_the_order_rule_names_eight(self, tmp_path, gh_env):
        specs = [(n, f"dispatch/d{n}" if n % 3 == 0 else f"feat/{n}", n % 4 == 0) for n in range(1, 14)]
        shuffled = specs[6:] + specs[:6]
        fake = FakeGh(open_prs=[_pr(n, branch=b, draft=d) for n, b, d in shuffled])
        section = _build(tmp_path, fake)
        expected = sorted(specs, key=lambda s: (not s[1].startswith("dispatch/"), s[2], s[0]))[:8]
        rows = _rows(section)
        measured = {n for n, r in rows.items() if r["ci"].get("reason") != "cap"}
        assert measured == {n for n, _, _ in expected}
        capped = [r for r in rows.values() if r["ci"]["reason"] == "cap"]
        assert len(capped) == 5 and all(r["ci"]["state"] == "unmeasured" for r in capped)
        assert len(section["open_prs"]) == 13


class TestDeadline:
    def test_p7_a_hanging_judge_call_costs_one_row_not_the_rebuild(self, tmp_path, gh_env, monkeypatch):
        monkeypatch.setattr(pqs, "CI_STEP_DEADLINE_SECONDS", 1.0)
        fake = FakeGh(open_prs=[_pr(n) for n in range(1, 6)],
                      runs={_sha(n): [_run(n)] for n in range(1, 6)}, hang=[_sha(3)])
        started = time.monotonic()
        section = _build(tmp_path, fake)
        assert time.monotonic() - started < 1.5
        rows = _rows(section)
        assert rows[3]["ci"]["state"] == "unmeasured" and rows[3]["ci"]["reason"] == "budget"
        for n in (1, 2, 4, 5):
            assert rows[n]["ci"]["state"] == "success"

    def test_a_deadline_that_expires_inside_the_workflow_read_gives_budget(self, tmp_path, gh_env, monkeypatch):
        monkeypatch.setattr(pqs, "CI_STEP_DEADLINE_SECONDS", 0.5)
        release = threading.Event()
        monkeypatch.setattr(pqs, "fetch_ci_workflow_from_main", lambda *a, **k: release.wait(60) or (None, ""))
        try:
            section = _build(tmp_path, FakeGh(open_prs=[_pr(1)]))
        finally:
            release.set()
        assert _rows(section)[1]["ci"]["reason"] == "budget"

    def test_at_most_four_judge_calls_run_at_once(self, tmp_path, gh_env):
        active, peak, lock = 0, 0, threading.Lock()

        def judge(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"verdict": "GO", "reason": "go", "ci_conclusion": "success", "ci_run_id": 1}

        with patch("pr_queue_state.check_ci_run_for_head", side_effect=judge):
            _build(tmp_path, FakeGh(open_prs=[_pr(n) for n in range(1, 9)]))
        assert 1 < peak <= 4


class TestEveryGhCallRunsInTheProjectRoot:
    def test_p8_cwd_on_every_gh_call(self, tmp_path, gh_env):
        root = tmp_path / "project"
        root.mkdir()
        fake = FakeGh(open_prs=[_pr(1)], runs={_sha(1): [_run(1)]})
        state_dir, _ = _make_dirs(tmp_path)
        with patch("subprocess.run", fake):
            build_pr_queue_state(state_dir, register_events=[], project_root=root)
        assert {cwd for _, cwd in fake.calls} == {str(root)}
        assert len(fake.calls) >= 6  # open, merged, contents x2, commit check, auth, run list


class TestFailedRead:
    def test_p5_a_failing_open_pr_read_says_so(self, tmp_path, gh_env):
        section = _build(tmp_path, FakeGh(open_fail="HTTP 401: bad credentials " + "x" * 200))
        assert section["available"] is False
        assert section["reason"].startswith("rc=1: HTTP 401")
        assert len(section["reason"]) <= len("rc=1: ") + 120
        assert section["open_prs"] == []

    def test_p5_a_successful_read_is_available(self, tmp_path, gh_env):
        section = _build(tmp_path, FakeGh(open_prs=[_pr(1)]))
        assert section["available"] is True and section["reason"] is None
        assert "merged_today_error" not in section

    def test_p5_a_failing_merged_read_leaves_available_alone(self, tmp_path, gh_env):
        section = _build(tmp_path, FakeGh(open_prs=[_pr(1)], merged_fail="HTTP 502"))
        assert section["available"] is True
        assert section["merged_today"] == []
        assert section["merged_today_error"] == "rc=1: HTTP 502"

    def test_p5_an_unparseable_answer_is_a_failed_read(self, tmp_path, gh_env):
        fake = FakeGh()
        fake._pr_list = lambda state: FakeGh._done("not json")
        section = _build(tmp_path, fake)
        assert section["available"] is False and "unparseable" in section["reason"]

    def test_p5_a_missing_gh_binary_is_a_failed_read(self, tmp_path, gh_env):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("subprocess.run", side_effect=FileNotFoundError("gh")):
            section = build_pr_queue_state(state_dir, register_events=[], project_root=tmp_path)
        assert section["available"] is False and section["reason"].startswith("FileNotFoundError")

    def test_p5_a_raising_builder_gives_builder_failed(self, tmp_path):
        import build_t0_state as bts
        state_dir, _ = _make_dirs(tmp_path)
        with patch.object(bts, "_build_pqs", side_effect=RuntimeError("boom")):
            section = bts._build_pr_queue_section(state_dir)
        assert section["available"] is False
        assert section["reason"] == "builder_failed: RuntimeError"
        assert section["open_prs"] == [] and section["schema"] == "pr_queue/1.1"

    def test_the_builder_receives_the_project_root(self, tmp_path):
        import build_t0_state as bts
        state_dir, _ = _make_dirs(tmp_path)
        with patch.object(bts, "_build_pqs", return_value={"available": True}) as build:
            bts._build_pr_queue_section(state_dir)
        assert build.call_args.kwargs["project_root"] == bts._PROJECT_ROOT
