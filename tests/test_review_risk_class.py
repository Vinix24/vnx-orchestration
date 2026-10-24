"""R3a, track review-stopregel-en-lichte-stoelen: the review risk class comes from the PR diff.

Operator decision 2026-10-17: codex is the only required seat per round, and the
second seat runs only on a high head. That needs one class per head that no caller
can talk down. What is pinned here:

- B1: the class is derived from the PR diff against the merge base, through the one
  plan-scope path table (``smart_router.plan_scope_path_category``); every category
  is high, the rest is medium. The real file lists of #2178 and #2184 are high, those
  of #2190 and #2183 medium (``gh pr view <n> --json files``, read 2026-10-23).
- B1/B7: the caller's ``--risk-class`` only raises it; ``low`` on a high diff stays
  high and the explanation names the path.
- B2: an unreadable diff, an empty file list, an unnormalisable path, an unknown
  caller value and an unreadable spec are each high.
- B3: ``request_reviews`` records ``review_risk`` on every seat's request record; the
  head a merge is about is read from its own records, recomputed when the merge base
  or the rule version differs, and a later head that adds a gate path is high.
- The widened path list: every added gate/merge path is gate-merge, so a build
  dispatch on such a path now raises its track to architecture (the plan-gate).
- R3b round 3: the diff is read against the PR's own base (``baseRefName``), never a
  default; a base that is missing or no plain branch name is high, and a record read
  against another base than the reader's is recomputed.

Every git read runs real git on a throwaway repository under ``tmp_path``.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import BRANCH, PR, build_project_store, isolate_config_registry

import review_risk_class as rrc
import smart_router

#: The base a PR for main is read against; ``read_pr_diff`` has no default base (R3b round 3).
MAIN = "origin/main"


def _reading(*files: str) -> rrc.DiffReading:
    return rrc.DiffReading(tuple(files), merge_base="a" * 40)


# ---------------------------------------------------------------------------
# B1: derived from the diff, one path table
# ---------------------------------------------------------------------------

def test_docs_tests_and_ordinary_lib_code_are_medium():
    risk = rrc.derive_risk_class(_reading("docs/core/HORIZON_PLANNING.md", "tests/test_x.py", "scripts/lib/report_writer.py"))
    assert (risk.risk_class, risk.derived, risk.deciding) == ("medium", "medium", ())
    assert "no high-risk path in 3 file(s)" in risk.explain()


@pytest.mark.parametrize("path, category", [
    ("schemas/runtime_coordination_v10.sql", "schema"),
    ("schemas/migrations/0042_new_table.sql", "schema"),
    ("scripts/migrations/0043_x.py", "schema"),
    ("scripts/lib/gate_executor.py", "gate-merge"),
    ("scripts/pr_merge.py", "gate-merge"),
    ("scripts/lib/review_seat_evaluation.py", "gate-merge"),
    ("scripts/lib/provider_spawns/kimi_spawn.py", "security"),
    (".github/workflows/vnx-ci.yml", "security"),
    ("scripts/lib/vnx_paths.py", "store-resolution"),
    ("../outside/repo.py", "outside-repo"),
])
def test_a_high_risk_path_makes_the_review_high(path, category):
    risk = rrc.derive_risk_class(_reading("docs/x.md", path))
    assert risk.risk_class == "high"
    assert (smart_router.normalize_dispatch_path(path), category) in risk.deciding
    assert category in risk.reason


# ``gh pr view <n> --json files``, read 2026-10-23.
_PR_FILES = {
    2178: ("changelog.d/dlv-38447134aeec.md", "docs/core/DISPATCH_RULES.md", "scripts/build_t0_state.py",
           "scripts/gate_obligation_runner.py", "scripts/lib/gate_executor.py", "scripts/lib/gate_obligations.py",
           "scripts/lib/gate_recorder.py", "scripts/lib/gate_request_handler.py", "scripts/lib/gate_run_guard.py",
           "scripts/lib/open_outcomes.py", "scripts/review_gate_manager.py", "tests/test_gate_executor_run_lock.py",
           "tests/test_gate_obligation_runner_one_run_per_head.py", "tests/test_gate_run_guard.py",
           "tests/test_open_outcomes_gate_retry_cap.py"),
    2184: ("changelog.d/dlv-116e1cc7a245.md", "docs/core/SUBSYSTEMS.md", "scripts/ci/xdist_parity_history.py",
           "tests/test_ci_xdist_parity_history.py"),
    2190: (".claude/skills/backend-developer/SKILL.md", "agents/quality-engineer/CLAUDE.md",
           "changelog.d/dlv-48b4e15d7ebc.md", "scripts/lib/fail_closed_evidence.py", "scripts/lib/prompt_assembler.py",
           "scripts/lib/prompts/fail_closed_evidence.md", "scripts/lib/prompts/roles/backend-developer.md",
           "scripts/lib/skill_context.py", "skills/backend-developer/SKILL.md", "tests/data/dispatch_envelope_census.json",
           "tests/test_fail_closed_evidence_block.py"),
    2183: ("changelog.d/dlv-df8c5d8f5ed0.md", "tests/test_codex_model_registry.py",
           "tests/test_model_normalizer_registry_isolation.py"),
}


@pytest.mark.parametrize("pr, expected, decider", [
    (2178, "high", "scripts/gate_obligation_runner.py"),
    (2184, "high", "scripts/ci/xdist_parity_history.py"),
    (2190, "medium", None),
    (2183, "medium", None),
])
def test_the_measured_prs_get_their_class(pr, expected, decider):
    risk = rrc.derive_risk_class(_reading(*_PR_FILES[pr]))
    assert risk.risk_class == expected
    if decider:
        assert (decider, "gate-merge") in risk.deciding


# ---------------------------------------------------------------------------
# B1/B7: the caller only raises it
# ---------------------------------------------------------------------------

def test_caller_low_on_a_high_diff_stays_high_and_names_the_path():
    risk = rrc.derive_risk_class(_reading("scripts/lib/gate_executor.py"), caller_class="low")
    assert risk.risk_class == "high"
    assert "gate-merge path scripts/lib/gate_executor.py" in risk.explain()
    assert "--risk-class low does not lower it" in risk.explain()
    assert risk.to_record()["caller"] == "low"


@pytest.mark.parametrize("diff, caller, expected", [
    (("docs/x.md",), "high", "high"),
    (("docs/x.md",), "critical", "critical"),
    (("scripts/pr_merge.py",), "critical", "critical"),
    (("docs/x.md",), "medium", "medium"),
    (("docs/x.md",), "low", "medium"),
    (("docs/x.md",), None, "medium"),
])
def test_a_known_caller_class_only_raises(diff, caller, expected):
    assert rrc.derive_risk_class(_reading(*diff), caller_class=caller).risk_class == expected


# ---------------------------------------------------------------------------
# B2: fail-closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reading, why", [
    (rrc.DiffReading(None, error="no merge base of origin/main and 9f3c2a1b7e4d"), "no merge base"),
    (rrc.DiffReading(None), "the diff cannot be read"),
    (rrc.DiffReading(()), "the diff names no file"),
    (rrc.DiffReading(("docs/x.md", ".")), "cannot be normalised"),
    (rrc.DiffReading(("docs/x.md", "   ")), "cannot be normalised"),
    (rrc.DiffReading(("docs/x.md", "a\nb.py")), "cannot be normalised"),
    (rrc.DiffReading(("docs/x.md", "bad\udcffname.py")), "cannot be normalised"),
], ids=["unreadable", "unreadable-no-reason", "empty", "dot", "blank", "newline", "undecodable"])
def test_a_diff_that_cannot_be_read_is_high(reading, why):
    risk = rrc.derive_risk_class(reading, caller_class="low")
    assert risk.risk_class == "high"
    assert why in risk.reason


@pytest.mark.parametrize("caller", ["hihg", "", "HIGHEST", "0"])
def test_an_unknown_caller_value_is_high(caller):
    risk = rrc.derive_risk_class(_reading("docs/x.md"), caller_class=caller)
    assert risk.risk_class == "high"
    assert "is no known class" in risk.reason


@pytest.mark.parametrize("flag, expected", [
    (rrc.SpecFlag(True, "spec.json"), "high"),
    (rrc.SpecFlag(None, "spec.json cannot be read"), "high"),
    (rrc.SpecFlag("yes", "spec.json"), "high"),
    (rrc.SpecFlag(False, "no spec"), "medium"),
])
def test_an_irreversible_or_unreadable_spec_is_high(flag, expected):
    assert rrc.derive_risk_class(_reading("docs/x.md"), spec=flag).risk_class == expected


def _spec(data_dir: Path, did: str, body) -> None:
    path = data_dir / "dispatches" / "active" / did / "dispatch-spec.json"
    path.parent.mkdir(parents=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


@pytest.mark.parametrize("body, expected", [
    ({"irreversible": True}, True),
    ({"irreversible": False}, False),
    ({}, False),
    ({"irreversible": "true"}, None),
    ("{not json", None),
    ([1, 2], None),
])
def test_spec_flag_reads_the_dispatch_spec(tmp_path, body, expected):
    _spec(tmp_path, "dlv-x", body)
    assert rrc.spec_flag("dlv-x", data_dir=tmp_path).irreversible is expected


def test_spec_flag_without_a_dispatch_or_spec_declares_nothing_and_a_path_id_is_unreadable(tmp_path):
    assert rrc.spec_flag("", data_dir=tmp_path).irreversible is False
    assert rrc.spec_flag("dlv-none", data_dir=tmp_path).irreversible is False
    assert rrc.spec_flag("../dlv-x", data_dir=tmp_path).irreversible is None


# ---------------------------------------------------------------------------
# The diff, read with real git
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(root: Path, files: dict, message: str) -> str:
    for name, text in files.items():
        path = root / name
        if text is None:
            _git(root, "rm", "-q", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(root, "add", name)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    base = _commit(root, {"docs/readme.md": "x\n", "scripts/lib/gate_status.py": "A = 1\n"}, "base")
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    return root, base


def test_read_pr_diff_reads_against_the_merge_base_and_names_both_sides_of_a_rename(repo):
    root, base = repo
    _git(root, "checkout", "-q", "-b", "feature")
    _git(root, "mv", "scripts/lib/gate_status.py", "scripts/lib/plain_status.py")
    head = _commit(root, {"docs/new.md": "y\n"}, "rename a gate module away")
    _git(root, "checkout", "-q", "main")
    _commit(root, {"scripts/pr_merge.py": "main moved on\n"}, "main moves")
    _git(root, "update-ref", "refs/remotes/origin/main", "main")

    reading = rrc.read_pr_diff(head, base_ref=MAIN, cwd=root)
    assert reading.merge_base == base
    assert set(reading.files) == {"docs/new.md", "scripts/lib/gate_status.py", "scripts/lib/plain_status.py"}
    risk = rrc.derive_risk_class(reading, head_sha=head)
    assert risk.risk_class == "high"
    assert risk.deciding == (("scripts/lib/gate_status.py", "gate-merge"),)


@pytest.mark.parametrize("head, why", [
    ("", "could not be resolved"),
    ("--output=x", "is no commit sha"),
    ("0123456789abcdef0123456789abcdef01234567", "no merge base"),
])
def test_read_pr_diff_of_a_head_that_cannot_be_read_is_unreadable(repo, head, why):
    root, _base = repo
    reading = rrc.read_pr_diff(head, base_ref=MAIN, cwd=root)
    assert reading.files is None and why in reading.error
    assert rrc.derive_risk_class(reading).risk_class == "high"


# ---------------------------------------------------------------------------
# R3b round 3: the base is the PR's own, never a default
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base, expected", [
    ("main", "origin/main"), ("release", "origin/release"), ("release/1.7", "origin/release/1.7"),
])
def test_the_base_is_the_prs_own_branch_on_origin(base, expected):
    assert rrc.base_ref_for(base) == (expected, "")


@pytest.mark.parametrize("base, why", [
    (None, "is not known"), ("", "is not known"), (7, "is not known"), (["main"], "is not known"),
    ("-main", "no plain branch name"), ("+main", "no plain branch name"), (" main", "no plain branch name"),
    ("main:refs/heads/x", "no plain branch name"), ("a..b", "no plain branch name"),
    ("main/", "no plain branch name"), ("main.lock", "no plain branch name"), ("a/.b", "no plain branch name"),
])
def test_a_base_that_is_missing_or_no_plain_branch_name_gives_no_ref(base, why):
    ref, reason = rrc.base_ref_for(base)
    assert ref is None and why in reason


@pytest.mark.parametrize("base_ref", ["", None, "-p", "--output=x"])
def test_read_pr_diff_without_a_usable_base_is_unreadable_and_high(repo, base_ref):
    root, base = repo
    reading = rrc.read_pr_diff(base, base_ref=base_ref, cwd=root)
    assert reading.files is None and "no base ref" in reading.error
    assert rrc.derive_risk_class(reading).risk_class == "high"


# ---------------------------------------------------------------------------
# B3: the class belongs to the head
# ---------------------------------------------------------------------------

def _record(requests_dir: Path, gate: str, risk: rrc.RiskClass, *, head: str) -> None:
    requests_dir.mkdir(parents=True, exist_ok=True)
    (requests_dir / f"pr-{PR}-{gate}.json").write_text(json.dumps({
        "gate": gate, "pr_number": PR, "commit_sha": head, "requested_at": "2026-10-23T10:00:00Z",
        rrc.RECORD_KEY: risk.to_record(),
    }), encoding="utf-8")


def test_a_later_head_that_adds_a_gate_path_reads_high(repo, tmp_path):
    root, _base = repo
    _git(root, "checkout", "-q", "-b", "feature")
    first = _commit(root, {"docs/plan.md": "plan\n"}, "first head")
    second = _commit(root, {"scripts/lib/gate_executor.py": "B = 2\n"}, "second head adds a gate path")
    requests_dir = tmp_path / "requests"
    _record(requests_dir, "codex_gate", rrc.derive_risk_class(rrc.read_pr_diff(first, base_ref=MAIN, cwd=root), head_sha=first), head=first)

    assert rrc.head_risk_class(requests_dir, str(PR), first, rrc.read_pr_diff(first, base_ref=MAIN, cwd=root)).risk_class == "medium"
    risk = rrc.head_risk_class(requests_dir, str(PR), second, rrc.read_pr_diff(second, base_ref=MAIN, cwd=root))
    assert (risk.risk_class, risk.source) == ("high", rrc.SOURCE_RECOMPUTED)
    assert "no request of head" in risk.note
    assert ("scripts/lib/gate_executor.py", "gate-merge") in risk.deciding


def test_a_record_with_the_current_merge_base_and_rule_version_stands(tmp_path):
    reading = _reading("docs/x.md")
    _record(tmp_path, "codex_gate", rrc.derive_risk_class(reading, caller_class="high", head_sha="b" * 40), head="b" * 40)
    risk = rrc.head_risk_class(tmp_path, str(PR), "b" * 40, reading)
    assert (risk.risk_class, risk.source) == ("high", rrc.SOURCE_RECORD)


@pytest.mark.parametrize("field, value", [("merge_base", "c" * 40), ("rule_version", "0000000000000000")])
def test_a_record_with_another_merge_base_or_rule_version_is_recomputed(tmp_path, field, value):
    reading = _reading("scripts/lib/gate_executor.py")
    stale = replace(rrc.derive_risk_class(_reading("docs/x.md"), head_sha="b" * 40), **{field: value})
    _record(tmp_path, "codex_gate", stale, head="b" * 40)
    risk = rrc.head_risk_class(tmp_path, str(PR), "b" * 40, reading)
    assert (risk.risk_class, risk.source) == ("high", rrc.SOURCE_RECOMPUTED)
    assert "made under merge base" in risk.note


_MISSING = object()


@pytest.mark.parametrize("recorded_base, holds", [
    ("origin/release", True), ("origin/main", False), ("", False), (None, False), (7, False), (_MISSING, False),
], ids=["same-base", "other-base", "empty", "none", "malformed", "missing"])
def test_a_record_read_against_another_base_is_recomputed(tmp_path, recorded_base, holds):
    """Same head and merge base, but the record was read for a merge into another branch: it does not hold."""
    reading = rrc.DiffReading(("docs/x.md",), merge_base="a" * 40, base_ref="origin/release")
    recorded = rrc.derive_risk_class(
        rrc.DiffReading(("scripts/lib/gate_executor.py",), merge_base="a" * 40, base_ref="origin/release"),
        head_sha="b" * 40,
    ).to_record()
    if recorded_base is _MISSING:
        del recorded["base_ref"]
    else:
        recorded["base_ref"] = recorded_base
    tmp_path.joinpath(f"pr-{PR}-codex_gate.json").write_text(json.dumps({
        "gate": "codex_gate", "pr_number": PR, "commit_sha": "b" * 40, "requested_at": "2026-10-23T10:00:00Z",
        rrc.RECORD_KEY: recorded,
    }), encoding="utf-8")

    risk = rrc.head_risk_class(tmp_path, str(PR), "b" * 40, reading)

    if holds:
        assert (risk.risk_class, risk.source) == ("high", rrc.SOURCE_RECORD)
        assert "against origin/release" in risk.note
    else:
        assert (risk.risk_class, risk.source) == ("medium", rrc.SOURCE_RECOMPUTED)
        assert "made under merge base" in risk.note and "now aaaaaaaaaaaa against origin/release" in risk.note


def test_a_recompute_keeps_the_raise_a_caller_recorded_on_the_head(tmp_path):
    """The runner's later "medium" on the same head does not lower the T0's "critical"."""
    for gate, caller in (("codex_gate", "critical"), ("kimi_gate", "medium")):
        recorded = rrc.derive_risk_class(_reading("docs/x.md"), caller_class=caller, head_sha="b" * 40)
        _record(tmp_path, gate, replace(recorded, merge_base="c" * 40), head="b" * 40)
    risk = rrc.head_risk_class(tmp_path, str(PR), "b" * 40, _reading("docs/x.md"))
    assert (risk.risk_class, risk.source) == ("critical", rrc.SOURCE_RECOMPUTED)


def test_the_door_reading_of_an_unreadable_diff_is_high_whatever_the_record_says(tmp_path):
    _record(tmp_path, "codex_gate", rrc.derive_risk_class(_reading("docs/x.md"), head_sha="b" * 40), head="b" * 40)
    risk = rrc.head_risk_class(tmp_path, str(PR), "b" * 40, rrc.DiffReading(None, error="git failed"))
    assert (risk.risk_class, risk.source) == ("high", rrc.SOURCE_RECOMPUTED)


@pytest.mark.parametrize("table", ["_PLAN_SCOPE_PREFIXES", "_SECURITY_PREFIXES"])
def test_the_rule_version_follows_the_path_table(monkeypatch, table):
    version = smart_router.plan_scope_rule_version
    # The function reads the tables from its own globals. In a run over the whole
    # tests/ tree `smart_router` is the tests/smart_router package shim, a copy of
    # the real module's names, so a setattr on it would not reach those globals.
    tables = version.__globals__
    monkeypatch.setitem(tables, table, (("a/", "gate-merge"),))
    first = version()
    monkeypatch.setitem(tables, table, (("b/", "gate-merge"),))
    second = version()
    monkeypatch.setitem(tables, table, (("a/", "gate-merge"),))
    assert first != second
    assert version() == first, "the same table gives the same version"


# ---------------------------------------------------------------------------
# B3: request_reviews records the class on every seat's request record
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


def test_request_reviews_records_the_derived_class_and_requests_with_it(tmp_path, monkeypatch, capsys):
    store = build_project_store(tmp_path, monkeypatch)
    root = store["project_root"]
    _git(root, "init", "-q", "-b", "main")
    base = _commit(root, {"docs/readme.md": "x\n"}, "base")
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    head = _commit(root, {"scripts/lib/gate_executor.py": "B = 2\n"}, "a gate path")
    import gate_request_handler
    import review_gate_manager

    monkeypatch.setenv("VNX_PROJECT_ROOT", str(root))
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda _pr: head)
    monkeypatch.setattr(gate_request_handler, "get_pr_base_branch", lambda _pr: "main")
    monkeypatch.setattr(gate_request_handler.GateRequestHandlerMixin, "_codex_headless_available", lambda self: True)
    monkeypatch.setattr(review_gate_manager, "emit_governance_receipt", lambda *a, **kw: None)
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")

    result = review_gate_manager.ReviewGateManager().request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="low",
        changed_files=["docs/readme.md"], mode="per_pr", dispatch_id="",
    )
    record = json.loads((store["requests_dir"] / f"pr-{PR}-codex_gate.json").read_text(encoding="utf-8"))
    risk = record[rrc.RECORD_KEY]
    assert (risk["risk_class"], risk["caller"], risk["head_sha"], risk["merge_base"]) == ("high", "low", head, base)
    assert risk["base_ref"] == MAIN
    assert risk["deciding"] == [{"path": "scripts/lib/gate_executor.py", "category": "gate-merge"}]
    assert risk["rule_version"] == smart_router.plan_scope_rule_version()
    assert record["risk_class"] == "high", "the seat is requested with the derived class, not the caller's"
    assert result[rrc.RECORD_KEY] == risk

    review_gate_manager._print_risk_class(result)
    assert "RISK CLASS high: gate-merge path scripts/lib/gate_executor.py" in capsys.readouterr().err


@pytest.mark.parametrize("base, expected", [
    ("main", ("medium", MAIN, "main", ())),
    ("release", ("high", "origin/release", "release", (("scripts/pr_merge.py", "gate-merge"),))),
    ("", ("high", "", "", ())),
], ids=["main", "release", "no-base"])
def test_request_reviews_reads_the_class_against_the_prs_own_base(tmp_path, monkeypatch, base, expected):
    """A PR for release off main carries main's merge logic: a doc change against main, high against release."""
    store = build_project_store(tmp_path, monkeypatch)
    root = store["project_root"]
    _git(root, "init", "-q", "-b", "main")
    tips = {"release": _commit(root, {"docs/readme.md": "x\n"}, "base")}
    _git(root, "update-ref", "refs/remotes/origin/release", tips["release"])
    tips["main"] = _commit(root, {"scripts/pr_merge.py": "M = 1\n"}, "merge logic lands on main")
    _git(root, "update-ref", "refs/remotes/origin/main", tips["main"])
    head = _commit(root, {"docs/release-notes.md": "notes\n"}, "a doc change for release")
    import gate_request_handler
    import review_gate_manager

    monkeypatch.setenv("VNX_PROJECT_ROOT", str(root))
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda _pr: head)
    monkeypatch.setattr(gate_request_handler, "get_pr_base_branch", lambda _pr: base)
    monkeypatch.setattr(gate_request_handler.GateRequestHandlerMixin, "_codex_headless_available", lambda self: True)
    monkeypatch.setattr(review_gate_manager, "emit_governance_receipt", lambda *a, **kw: None)
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")

    review_gate_manager.ReviewGateManager().request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="low",
        changed_files=["docs/release-notes.md"], mode="per_pr", dispatch_id="",
    )
    risk = json.loads((store["requests_dir"] / f"pr-{PR}-codex_gate.json").read_text(encoding="utf-8"))[rrc.RECORD_KEY]

    cls, base_ref, tip, deciding = expected
    assert (risk["risk_class"], risk["base_ref"], risk["merge_base"]) == (cls, base_ref, tips.get(tip, ""))
    assert [(d["path"], d["category"]) for d in risk["deciding"]] == list(deciding)
    if not base:
        assert "the PR's base branch is not known" in risk["reason"]


# ---------------------------------------------------------------------------
# The widened path list, and its plan-gate effect
# ---------------------------------------------------------------------------

_ADDED_GATE_MERGE = [
    "scripts/gate_runner.py", "scripts/gate_obligation_runner.py", "scripts/lib/gate_executor.py",
    "scripts/review_gate_manager.py", "scripts/closure_verifier.py", "scripts/ci/xdist_parity_history.py",
    "scripts/ci/check_xdist_parity.py", "scripts/ci/record_xdist_parity_verdict.py", "scripts/review_rounds_cli.py",
    "scripts/codex_final_gate.py", "scripts/kimi_gate.py", "scripts/glm_gate.py", "scripts/cleanup_orphan_gates.py",
    "scripts/t0_gate_enforcement.sh", "scripts/commands/gate.sh", "scripts/forge/branch_protection.yaml",
    "scripts/lib/forge_gate_publisher.py", "scripts/lib/forge_protection_drift.py",
    "scripts/lib/contract_invalid_ledger.py", "scripts/lib/codex_severity_translator.py",
    "scripts/lib/claude_gate_lane.py", "scripts/lib/final_answer_verdict.py", "scripts/lib/headless_review_receipt.py",
    "scripts/lib/governance_enforcer.py", "scripts/lib/pr_readiness.py", "scripts/lib/attest_override.py",
    "scripts/lib/attestation.py", "scripts/lib/smart_router.py", "scripts/lib/plan_scope.py",
]


@pytest.mark.parametrize("path", _ADDED_GATE_MERGE)
def test_every_added_gate_or_merge_path_is_gate_merge_and_raises_the_track(path):
    import plan_scope

    assert smart_router.plan_scope_path_category(path) == "gate-merge"
    assert plan_scope.dispatch_rule([path]) == f"gate-merge path {path}"


def test_a_base_schema_is_schema_and_raises_the_track():
    import plan_scope

    assert plan_scope.dispatch_rule(["schemas/runtime_coordination_v10.sql"]) == (
        "schema path schemas/runtime_coordination_v10.sql"
    )


@pytest.mark.parametrize("path", [
    "scripts/lib/auto_gate_trigger.py", "scripts/lib/profile_gate_resolver.py",
    "scripts/lib/wiring_gate.py", "scripts/lib/reviewer_roles.py",
    "scripts/lib/file_scope_overlap.py", "scripts/backfill_pr_merged_receipts.py",
])
def test_the_measured_modules_left_out_stay_outside_the_table(path):
    assert smart_router.plan_scope_path_category(path) is None
