"""R7: the claude review seat, ``claude_gate`` (dispatch dlv-9906a482950d).

Operator decision 2026-10-22: a read-only review seat on the operator's claude
subscription (``claude -p``, sonnet), registered everywhere a gate name is,
standing between kimi and deepseek in the default takeover chain, never with an
``ANTHROPIC_BASE_URL`` or ``ANTHROPIC_API_KEY`` in its environment, and booking
its run at cost 0 with source ``subscription``.

The seat's own run is tested end to end: the real ``claude_gate_lane`` ->
``spawn_claude`` -> ``SubprocessAdapter`` -> worktree path, with the ``claude``
binary replaced on ``PATH`` by a script that records what it was started with
(the external process is the one thing replaced) and a real git repo with a
local ``origin`` as the PR. No real claude review runs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import claude_gate_lane
import closure_verifier
import config_registry
import domain_plan_validator
import event_store
import gate_lane_contract
import gate_recorder
import gate_request_handler
import pr_readiness
import prior_round_injector
import provider_costs
import provider_switch
import smart_router
import stop_conditions
from codex_parser import extract_verdict_block
from dispatch_spec import Gate, REGISTERED_GATE_NAMES
from gate_billing_table import GATE_BILLING_SUBSCRIPTION, gate_billing
from unified_report_schema import parse_frontmatter

GATE = "claude_gate"
NEW_CHAIN = "codex_gate,kimi_gate,claude_gate,deepseek_gate"


# ---------------------------------------------------------------------------
# Registered everywhere a gate name is registered
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_the_runnable_registry_carries_it_as_a_harness_lane_on_claude(self):
        assert gate_recorder.GATE_PROVIDERS[GATE] == (gate_recorder.GATE_PROVIDER_HARNESS_LANE, "claude")

    def test_it_is_a_legal_gate_name_and_an_enum_member(self):
        assert GATE in REGISTERED_GATE_NAMES
        assert Gate(GATE) is Gate.CLAUDE_GATE

    def test_it_is_billed_as_a_subscription(self):
        assert gate_billing(GATE) == GATE_BILLING_SUBSCRIPTION

    def test_the_lane_contract_runs_sonnet_with_its_own_keys(self):
        assert gate_lane_contract.MODEL_DEFAULTS[GATE] == ("VNX_CLAUDE_GATE_MODEL", "sonnet")
        timeout_key, _ = gate_lane_contract.TIMEOUT_CONFIG[GATE]
        diff_key, _ = gate_lane_contract.DIFF_CHAR_CONFIG[GATE]
        assert {timeout_key, diff_key} <= set(config_registry.CONFIG_REGISTRY)

    def test_the_merge_door_has_a_handler_and_it_is_a_review_peer(self):
        assert GATE in closure_verifier._GATE_HANDLERS
        assert GATE in closure_verifier._KNOWN_GATES

    def test_the_readers_know_it(self):
        assert GATE in pr_readiness.GATE_COST
        assert pr_readiness.GATE_COST[GATE].usd == "subscription"
        assert stop_conditions.GATE_LEDGER_PROVIDERS[GATE] >= {"claude"}
        assert GATE in prior_round_injector._KNOWN_GATES
        assert smart_router._GATE_WEIGHT[GATE] == smart_router._GATE_WEIGHT["kimi_gate"]
        assert GATE in domain_plan_validator.IMPLEMENTED_GATES

    def test_a_gate_request_reaches_its_own_request_branch(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            gate_request_handler.GateRequestHandlerMixin, "_request_claude",
            lambda self, *args: called.append(args) or {"gate": GATE, "status": "requested"},
        )
        payload = gate_request_handler.GateRequestHandlerMixin()._dispatch_one_review(
            GATE, 7, "feat/x", "medium", [], "per_pr", "",
        )
        assert payload["gate"] == GATE and len(called) == 1


# ---------------------------------------------------------------------------
# The provider switch (ADR-044) leaves claude outside it, and never refuses the seat
# ---------------------------------------------------------------------------


def _switch_everything_off(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"version": 1, "global": {
        family: {"enabled": False, "changed_at": "2026-10-22T08:00:00Z", "changed_by": "op", "reason": "r7"}
        for family in provider_switch.SWITCHABLE_FAMILIES
    }, "projects": {}}), encoding="utf-8")
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(path))


class TestProviderSwitch:
    def test_claude_is_not_a_switchable_family(self):
        assert provider_switch.CLAUDE not in provider_switch.SWITCHABLE_FAMILIES
        assert provider_switch.family_of(GATE) == provider_switch.CLAUDE

    def test_the_switch_never_refuses_the_seat(self, monkeypatch, tmp_path):
        _switch_everything_off(monkeypatch, tmp_path)
        assert gate_recorder.gate_never_switched(GATE) is True
        assert gate_recorder.gate_switch_refusal(GATE) is None
        assert gate_recorder.gate_is_available(GATE) is True

    def test_a_deepseek_seat_is_refused_by_the_same_switch_file(self, monkeypatch, tmp_path):
        """Control: the file above does switch a switchable family off."""
        _switch_everything_off(monkeypatch, tmp_path)
        assert gate_recorder.gate_switch_refusal("deepseek_gate") is not None


# ---------------------------------------------------------------------------
# The chain: subscription reviewers before API credit
# ---------------------------------------------------------------------------


class TestChain:
    def test_the_default_chain_has_claude_between_kimi_and_deepseek(self):
        assert gate_request_handler._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN == NEW_CHAIN
        assert config_registry.CONFIG_REGISTRY["VNX_REVIEW_GATE_TAKEOVER_CHAIN"].default == NEW_CHAIN

    def test_every_subscription_gate_reads_before_every_api_credit_gate(self):
        chain = NEW_CHAIN.split(",")
        billing = [gate_billing(g) for g in chain]
        first_metered = next(i for i, b in enumerate(billing) if b != GATE_BILLING_SUBSCRIPTION)
        assert all(b == GATE_BILLING_SUBSCRIPTION for b in billing[:first_metered])
        assert all(b != GATE_BILLING_SUBSCRIPTION for b in billing[first_metered:])
        assert chain[:first_metered] == ["codex_gate", "kimi_gate", "claude_gate"]

    def test_the_registry_text_states_the_policy(self):
        text = config_registry.CONFIG_REGISTRY["VNX_REVIEW_GATE_TAKEOVER_CHAIN"].description
        assert "claude_gate" in text and "fallback only" in text and "vendor" in text


# ---------------------------------------------------------------------------
# The seat's environment: the subscription, never a key or a redirect
# ---------------------------------------------------------------------------


_PARENT_ANTHROPIC = {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_API_KEY": "sk-r7-test-parent-key",
    "ANTHROPIC_AUTH_TOKEN": "r7-test-parent-token",
    "ANTHROPIC_MODEL": "deepseek-v4-pro",
}


def test_the_scrub_set_removes_every_anthropic_variable_and_keeps_the_account(monkeypatch):
    """The env SubprocessAdapter.deliver hands the child, built by the adapter's own builder."""
    from subprocess_adapter import _child_env

    for key, value in _PARENT_ANTHROPIC.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/r7-account")
    env = _child_env("claude-gate-pr7-1", {"VNX_WORKER_ROLE": "review-gate"},
                     claude_gate_lane.SUBSCRIPTION_SCRUB_KEYS)
    assert not [key for key in env if key.startswith("ANTHROPIC_")]
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/r7-account"
    assert env["VNX_WORKER_ROLE"] == "review-gate"


# The seat's whole capability scope, pinned literally (claude 2.1.308 --help):
# only Read, Grep and Glob exist and are pre-approved, the MCP namespace is
# removed, nothing outside the allow-list can be approved, and no MCP server loads.
SEAT_TOOLS = "Read,Grep,Glob"
SEAT_SCOPE = [
    "--tools", SEAT_TOOLS,
    "--allowedTools", SEAT_TOOLS,
    "--disallowedTools", "mcp__*",
    "--permission-mode", "dontAsk",
    "--permission-prompts", "none",
    "--mcp-config", '{"mcpServers":{}}',
    "--strict-mcp-config",
]


def test_the_seat_scope_is_the_read_only_allow_list_and_never_bare():
    args = claude_gate_lane.seat_scope_args()
    assert args == SEAT_SCOPE
    assert args[-1] == "--strict-mcp-config", "the boolean ends the last variadic flag"
    assert "--bare" not in args


# How the fake run ends: R7_FAKE_RESULT is a JSON object laid over the success
# completion, or "omit" for a stream with no completion event; R7_FAKE_EXIT is
# the exit code. The streamed answer and the completion both carry a PASS
# verdict, so a run that ends badly still holds one.
_FAKE_CLAUDE = r'''
import json, os, sys

out = os.environ["R7_FAKE_CLAUDE_OUT"]
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
               "files": sorted(os.listdir("."))}, fh)


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


ANSWER = "The change is sound.\n```json\n{\"verdict\": \"pass\", \"findings\": [], \"residual_risk\": null}\n```"
emit({"type": "system", "subtype": "init", "session_id": "s-r7", "model": "claude-sonnet-5-5"})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": ANSWER}]}})
ending = os.environ.get("R7_FAKE_RESULT", "{}")
if ending != "omit":
    result = {"type": "result", "subtype": "success", "is_error": False, "session_id": "s-r7",
              "result": ANSWER,
              "usage": {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 7}}
    result.update(json.loads(ending))
    emit(result)
sys.exit(int(os.environ.get("R7_FAKE_EXIT", "0")))
'''


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed in {cwd}: {result.stderr}")
    return result.stdout


@pytest.fixture
def pr_repo(tmp_path):
    """A project with a local bare ``origin`` and a pushed PR branch ``feat/r7``."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    project = tmp_path / "project"
    _git(tmp_path, "init", "-b", "main", str(project))
    for key, value in (("user.email", "r7@example.com"), ("user.name", "R7"), ("commit.gpgsign", "false")):
        _git(project, "config", key, value)
    (project / ".gitignore").write_text(".vnx-data/\n", encoding="utf-8")
    (project / "README.md").write_text("base\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-u", "origin", "main")
    _git(project, "checkout", "-b", "feat/r7")
    (project / "change.py").write_text("x = 1\n", encoding="utf-8")
    _git(project, "add", "change.py")
    _git(project, "commit", "-m", "the change under review")
    _git(project, "push", "-u", "origin", "feat/r7")
    _git(project, "checkout", "main")
    return project


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n" + _FAKE_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    out = tmp_path / "claude-started-with.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("R7_FAKE_CLAUDE_OUT", str(out))
    for key in ("VNX_TOTAL_DEADLINE", "VNX_CHUNK_TIMEOUT", "R7_FAKE_RESULT", "R7_FAKE_EXIT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in _PARENT_ANTHROPIC.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    return out


class TestTheSeatRunsOnTheSubscription:
    @pytest.fixture
    def run(self, pr_repo, fake_claude):
        data_dir = Path(os.environ["VNX_DATA_DIR"])
        dispatch_id = "claude-gate-pr7-1792700000"
        report = claude_gate_lane.make_claude_gate_dispatcher(
            data_dir, base_ref="origin/feat/r7", total_deadline_seconds=120, project_root=pr_repo,
        )("claude", "sonnet", "REVIEW PROMPT with the verdict contract", dispatch_id)
        started_with = json.loads(fake_claude.read_text(encoding="utf-8"))
        return {"report": report, "started_with": started_with, "data_dir": data_dir,
                "dispatch_id": dispatch_id, "project": pr_repo}

    def test_no_anthropic_variable_reaches_the_seat_although_the_parent_has_them(self, run):
        env = run["started_with"]["env"]
        assert "ANTHROPIC_BASE_URL" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert not [key for key in env if key.startswith("ANTHROPIC_")]
        assert "CLAUDE_CODE_USE_BEDROCK" not in env
        assert os.environ["ANTHROPIC_BASE_URL"] == _PARENT_ANTHROPIC["ANTHROPIC_BASE_URL"], (
            "the parent process keeps its own environment"
        )
        assert env["VNX_WORKER_ROLE"] == "review-gate"

    def test_the_seat_runs_sonnet_read_only_with_mcp_off_in_its_own_worktree(self, run):
        argv = run["started_with"]["argv"]
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert "--strict-mcp-config" in argv and "--bare" not in argv
        assert argv[argv.index("--tools") + 1] == SEAT_TOOLS
        prompt = next(a for a in argv if a.startswith(claude_gate_lane.SEAT_NOTICE))
        assert prompt.endswith("REVIEW PROMPT with the verdict contract")
        cwd = Path(run["started_with"]["cwd"]).resolve()
        worktrees = (run["project"] / ".vnx-data" / "worktrees").resolve()
        assert worktrees in cwd.parents, f"the seat ran in {cwd}, not in its own worktree"
        assert "change.py" in run["started_with"]["files"], "the worktree is the PR head, not main"
        assert not cwd.exists(), "the disposable worktree is removed after the run"

    def test_the_report_is_the_final_answer_with_the_frontmatter_the_runner_reads(self, run):
        frontmatter = parse_frontmatter(run["report"])
        assert (frontmatter["provider"], frontmatter["sub_provider"]) == ("claude", "anthropic")
        assert frontmatter["exit_code"] == 0 and "failure_reason" not in frontmatter
        assert frontmatter["cost_usd"] == 0.0 and frontmatter["cost_source"] == "subscription"
        assert frontmatter["token_usage"] == {"input": 120, "output": 40, "cache_read": 7}
        assert frontmatter["role"] == "review-gate"
        assert extract_verdict_block(run["report"])["verdict"] == "pass"

    def test_the_cost_line_is_zero_and_says_subscription(self, run):
        ledger = run["data_dir"] / "events" / "provider_costs.ndjson"
        lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        [line] = [entry for entry in lines if entry.get("dispatch_id") == run["dispatch_id"]]
        assert (line["provider"], line["cost_usd_estimate"]) == ("claude", 0.0)
        assert (line["cost_source"], line["billing_mode"]) == ("subscription", "subscription")
        assert (line["input_tokens"], line["output_tokens"]) == (120, 40)

    def test_the_run_has_a_receipt_and_an_archived_stream(self, run):
        receipts = run["data_dir"] / "state" / "t0_receipts.ndjson"
        rows = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines() if line.strip()]
        [receipt] = [row for row in rows if row.get("dispatch_id") == run["dispatch_id"]]
        assert receipt["provider"] == "claude" and receipt["status"] == "success"
        archive = event_store.resolve_archive_file(
            run["data_dir"] / "events" / "archive" / claude_gate_lane.SEAT_TERMINAL, run["dispatch_id"],
        )
        assert archive is not None

    def test_the_lane_runs_claude_only(self, tmp_path):
        dispatcher = claude_gate_lane.make_claude_gate_dispatcher(
            tmp_path, base_ref="origin/x", total_deadline_seconds=60,
        )
        with pytest.raises(ValueError, match="claude"):
            dispatcher("deepseek-harness", "deepseek-flash", "p", "claude-gate-pr1-1")


def test_a_run_with_no_answer_is_a_failure_not_a_review():
    class _Result:
        returncode = 0
        completion = {"text": "", "subtype": "success", "is_error": False}
        completion_text = ""

    failure = claude_gate_lane.run_failure(_Result())
    assert failure is not None and "no answer" in failure
    assert claude_gate_lane.seat_exit_code(_Result(), failure) == 1


# ---------------------------------------------------------------------------
# Round 2 (codex_gate on 11700c12): no adapter default reaches the seat's argv,
# and only a run that ends in a successful completion is a review
# ---------------------------------------------------------------------------

_SCOPE_ENV = ("VNX_WORKER_SCOPED", "VNX_WORKER_BLANKET_SKIP", "VNX_ENFORCE_WORKER_PERMISSIONS")
_REVIEW_PROMPT = "REVIEW PROMPT with the verdict contract"


def _run_seat(project: Path, dispatch_id: str) -> str:
    return claude_gate_lane.make_claude_gate_dispatcher(
        Path(os.environ["VNX_DATA_DIR"]), base_ref="origin/feat/r7", total_deadline_seconds=120,
        project_root=project,
    )("claude", "sonnet", _REVIEW_PROMPT, dispatch_id)


@pytest.mark.parametrize("scope_env", [
    {}, {"VNX_WORKER_SCOPED": "1"}, {"VNX_WORKER_SCOPED": "0"},
    {"VNX_WORKER_BLANKET_SKIP": "1"}, {"VNX_ENFORCE_WORKER_PERMISSIONS": "1"},
], ids=["scoped-unset", "scoped-1", "scoped-0", "blanket-skip", "enforcement-on"])
def test_the_final_argv_is_the_allow_list_whatever_the_adapter_defaults(
    pr_repo, fake_claude, monkeypatch, scope_env,
):
    for key in _SCOPE_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in scope_env.items():
        monkeypatch.setenv(key, value)
    _run_seat(pr_repo, "claude-gate-pr7-1792700001")
    argv = json.loads(fake_claude.read_text(encoding="utf-8"))["argv"]
    prompt = f"{claude_gate_lane.SEAT_NOTICE}\n\n{_REVIEW_PROMPT}"
    assert argv == ["-p", "--output-format", "stream-json", "--verbose", "--model", "sonnet", prompt, *SEAT_SCOPE]
    flags = [token for token in argv if token != prompt]
    assert "--dangerously-skip-permissions" not in flags
    assert "--allow-dangerously-skip-permissions" not in flags
    assert "bypassPermissions" not in flags and flags.count("--permission-mode") == 1
    assert not [token for token in flags if "Bash" in token]


# Every way a claude -p run can end that is not a review (claude 2.1.308
# stream-json ``result``: subtype "success" or an error subtype, is_error on
# both), with what the report and the gate record have to name and the exit code
# the report carries. Each run streamed and completed with a PASS verdict.
_BAD_ENDINGS = {
    "error-max-turns": ({"R7_FAKE_RESULT": json.dumps({"subtype": "error_max_turns", "is_error": False})},
                        "subtype 'error_max_turns'", 1),
    "is-error-true": ({"R7_FAKE_RESULT": json.dumps({"is_error": True})}, "is_error True", 1),
    "no-completion": ({"R7_FAKE_RESULT": "omit"}, "no completion event", 1),
    "exit-nonzero": ({"R7_FAKE_EXIT": "3"}, "exited with code 3", 3),
}


@pytest.mark.parametrize("ending", sorted(_BAD_ENDINGS))
def test_a_run_that_did_not_complete_successfully_is_a_failure_and_its_pass_is_not_in_the_report(
    pr_repo, fake_claude, monkeypatch, ending,
):
    env, named, exit_code = _BAD_ENDINGS[ending]
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    dispatch_id = f"claude-gate-pr7-{1792700100 + sorted(_BAD_ENDINGS).index(ending)}"
    report = _run_seat(pr_repo, dispatch_id)
    frontmatter = parse_frontmatter(report)
    assert frontmatter["exit_code"] == exit_code
    assert named in report
    assert extract_verdict_block(report) == {}, "the run's PASS is never read from its report"
    receipts = Path(os.environ["VNX_DATA_DIR"]) / "state" / "t0_receipts.ndjson"
    rows = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines() if line.strip()]
    [receipt] = [row for row in rows if row.get("dispatch_id") == dispatch_id]
    assert receipt["status"] == "failure" and named in receipt["failure_reason"]


def _run_gate(project: Path, monkeypatch, tmp_path: Path, pr_number: int) -> dict:
    """``claude_gate`` through GateRunner's harness-lane path, the seat run real and claude faked."""
    import gate_runner

    state = tmp_path / "gate-data" / "state"
    for sub in ("requests", "results"):
        (state / "review_gates" / sub).mkdir(parents=True)
    reports = tmp_path / "gate-data" / "unified_reports"
    reports.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setattr(
        gate_runner.GateRunner, "_fetch_gh_pr_diff",
        staticmethod(lambda _pr: "diff --git a/change.py b/change.py\n+x = 1\n"),
    )
    runner = gate_runner.GateRunner(state_dir=state, reports_dir=reports, project_root=project)
    payload = {
        "gate": GATE, "status": "requested", "branch": "feat/r7", "pr_number": pr_number,
        "commit_sha": _git(project, "rev-parse", "origin/feat/r7").strip(),
        "requested_at": "2026-10-22T15:00:00Z", "report_path": str(reports / f"{GATE}-pr{pr_number}.md"),
    }
    runner.run(gate=GATE, request_payload=payload, pr_number=pr_number)
    return json.loads((state / "review_gates" / "results" / f"pr-{pr_number}-{GATE}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("ending", sorted(_BAD_ENDINGS))
def test_the_runner_books_an_execution_failure_with_the_cause_and_never_the_pass(
    pr_repo, fake_claude, monkeypatch, tmp_path, ending,
):
    env, named, _ = _BAD_ENDINGS[ending]
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    record = _run_gate(pr_repo, monkeypatch, tmp_path, pr_number=7)
    assert record["status"] == "unavailable", record
    assert record["reason"] == "harness_lane_exit_nonzero"
    assert named in record["reason_detail"]
    assert not record.get("verdict_source"), "no verdict was read from the run"


def test_a_successful_completion_with_a_pass_still_passes_the_runner(pr_repo, fake_claude, monkeypatch, tmp_path):
    record = _run_gate(pr_repo, monkeypatch, tmp_path, pr_number=7)
    assert record["status"] == "completed", record
    assert record["blocking_findings"] == [] and record["verdict_source"] == "report"


# ---------------------------------------------------------------------------
# gate_runner hands the claude seat to claude_gate_lane, never to provider_dispatch
# ---------------------------------------------------------------------------


def test_gate_runner_builds_the_claude_lane_for_claude_gate_and_the_panel_lane_otherwise(monkeypatch, tmp_path):
    import gate_runner
    import plan_gate_panel

    built = []
    monkeypatch.setattr(
        claude_gate_lane, "make_claude_gate_dispatcher",
        lambda data_dir, **kw: built.append(("claude", kw)) or "claude-dispatcher",
    )
    monkeypatch.setattr(
        plan_gate_panel, "_make_default_dispatcher",
        lambda data_dir, timeout, **kw: built.append(("panel", kw)) or "panel-dispatcher",
    )
    payload = {}
    assert gate_runner._build_harness_lane_dispatcher(
        GATE, tmp_path, "origin/feat/x", payload, "prompt", project_root=tmp_path,
    ) == "claude-dispatcher"
    assert built[-1] == ("claude", {
        "base_ref": "origin/feat/x", "total_deadline_seconds": payload["timeout_seconds"],
        "project_root": tmp_path,
    })
    assert gate_runner._build_harness_lane_dispatcher(
        "deepseek_gate", tmp_path, "origin/feat/x", {}, "prompt",
    ) == "panel-dispatcher"


# ---------------------------------------------------------------------------
# The merge door: a seat filled by claude_gate counts like any takeover, on the exact head
# ---------------------------------------------------------------------------


PR_ID = "2168"
BRANCH = "dispatch/dlv-c089a8e32893"
HEAD = "5f03d004109e3a0e3364fdbb4ad78572c587d340"
OTHER_HEAD = "e9c9e3d579473569d5204c10d5bf4d346bef1ba2"


def _merge_door(tmp_path: Path, claude_head: str) -> dict:
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    report = tmp_path / "claude-gate-report.md"
    report.write_text("# Gate report\n\nAll findings reviewed, nothing blocking.\n", encoding="utf-8")
    (results_dir / f"pr-{PR_ID}-kimi_gate.json").write_text(json.dumps({
        "gate": "kimi_gate", "pr_id": PR_ID, "status": "unavailable", "contract_hash": "",
        "report_path": "", "branch": BRANCH, "commit_sha": HEAD,
    }), encoding="utf-8")
    (results_dir / f"pr-{PR_ID}-claude_gate.json").write_text(json.dumps({
        "gate": GATE, "pr_id": PR_ID, "status": "pass", "blocking_count": 0, "blocking_findings": [],
        "contract_hash": "15270d8bdfc53bcf", "report_path": str(report), "branch": BRANCH,
        "commit_sha": claude_head, "takeover": True, "takeover_from": "kimi_gate",
        "takeover_path": [{"gate": "kimi_gate", "status": "unavailable", "reason": "lane_exhausted"}],
    }), encoding="utf-8")
    return closure_verifier.check_review_gate_for_merge(
        PR_ID, "kimi_gate", results_dir, branch=BRANCH, head_sha=HEAD, seats=["kimi_gate"],
    )


def test_the_merge_door_accepts_a_seat_filled_by_claude_gate_on_the_exact_head(tmp_path):
    verdict = _merge_door(tmp_path, HEAD)
    assert verdict["verdict"] == "GO", verdict["message"]
    assert verdict["evidence_gate"] == GATE


def test_the_merge_door_refuses_a_claude_gate_pass_on_another_head(tmp_path):
    verdict = _merge_door(tmp_path, OTHER_HEAD)
    assert verdict["verdict"] == "NO-GO"


# ---------------------------------------------------------------------------
# The cost event carries the billing mode its caller states
# ---------------------------------------------------------------------------


def _cost_lines() -> list:
    path = provider_costs._resolve_costs_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_subscription_run_books_zero_as_subscription_not_metered():
    provider_costs.emit_provider_cost(
        "claude", "sonnet", 10, 5, 0.0, dispatch_id="r7-cost-1", cost_source="subscription",
        billing_mode="subscription",
    )
    [line] = [entry for entry in _cost_lines() if entry["dispatch_id"] == "r7-cost-1"]
    assert (line["cost_usd_estimate"], line["billing_mode"]) == (0.0, "subscription")


def test_an_unknown_billing_mode_is_refused_and_nothing_is_written():
    with pytest.raises(ValueError, match="billing_mode"):
        provider_costs.emit_provider_cost("claude", "sonnet", 1, 1, 0.0, dispatch_id="r7-cost-2",
                                          billing_mode="free")
    assert not [entry for entry in _cost_lines() if entry["dispatch_id"] == "r7-cost-2"]


def test_without_a_stated_mode_a_cost_still_books_metered():
    provider_costs.emit_provider_cost("deepseek-harness", "deepseek-flash", 1, 1, 0.01, dispatch_id="r7-cost-3")
    [line] = [entry for entry in _cost_lines() if entry["dispatch_id"] == "r7-cost-3"]
    assert line["billing_mode"] == "metered"
