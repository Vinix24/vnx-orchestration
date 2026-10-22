"""claude_gate_lane.py — the lane the claude review seat runs on (R7).

``claude_gate`` is a read-only review seat on the operator's Claude
SUBSCRIPTION: ``claude -p`` headless, model ``sonnet``
(``gate_lane_contract.MODEL_DEFAULTS``). Operator decision 2026-10-22
(dispatch dlv-9906a482950d): with kimi at its weekly limit the takeover chain
had only API-credit reviewers left, and on PR #2168 DeepSeek reviewed a head
DeepSeek built.

It runs the way ``deepseek_gate`` runs as a harness-lane gate.
``gate_runner._run_harness_lane_path`` fetches the PR branch, mints the gate's
own dispatch id, snapshots the orchestrator's checkout (OI-1887), builds the
prompt with the isolation notice and the verdict contract, and reads the
returned governed report (deadline, failure_reason, exit_code, verdict). The
one difference is the dispatcher: provider_dispatch refuses claude (the door
owns claude routing, PR-5), so the seat is started here, through the same
``spawn_claude`` -> ``SubprocessAdapter`` path the door's headless lane and the
plan-gate's claude seat use. :func:`make_claude_gate_dispatcher` has the call
shape of ``plan_gate_panel._make_default_dispatcher``:
``(provider, model, prompt, dispatch_id) -> report text``.

The hardening the deepseek seat has, and more:
  - its own disposable worktree on the PR head (``create_dispatch_worktree`` on
    ``base_ref``, removed with ``review_only=True``, OI-1629b);
  - MCP fully off: ``--mcp-config '{"mcpServers":{}}' --strict-mcp-config`` and
    the ``mcp__*`` namespace denied;
  - read-only by allow-list (:func:`seat_scope_args`, round 2 of R7): only
    ``Read``, ``Grep`` and ``Glob`` exist in the session, nothing else can be
    approved, and the adapter's own scope (``--dangerously-skip-permissions`` or
    the review-gate role's Bash-capable fallback profile) never reaches the argv;
  - the isolation notice and the verdict contract in the prompt, and the
    verdict block read from the report body, which is the model's final answer,
    only when the run ended in a successful completion (:func:`run_failure`).

What it never gets: ``ANTHROPIC_BASE_URL``, ``ANTHROPIC_API_KEY`` or any other
``ANTHROPIC_*`` variable, nor a Bedrock/Vertex switch. Each is scrubbed from the
child environment (:data:`SUBSCRIPTION_SCRUB_KEYS`) even when this process
carries it (the deepseek key loader sets them): the seat is the subscription,
never an API key and never a redirected endpoint. ``CLAUDE_CONFIG_DIR`` is kept,
so the seat runs on the account of the T0 whose gate run started it. No
``--bare`` and no harness config dir: both are for a redirected endpoint, and
``--bare`` would stop the CLI from reading the subscription login.

Governance: the run's cost line (0, source ``subscription``, operator rule:
every call books a cost line with its source), its receipt and its unified
report are written here, in that order (ADR-005). No Anthropic SDK is imported
anywhere (``no-anthropic-sdk``): only ``claude`` through SubprocessAdapter.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from env_scrub_patterns import DEFAULT_SCRUB_KEY_PATTERNS
import event_store
from worker_permissions import MCP_NAMESPACE_DENY

logger = logging.getLogger(__name__)

# The provider string GATE_PROVIDERS registers claude_gate under.
CLAUDE_LANE_PROVIDER = "claude"
SEAT_GATE = "claude_gate"
SEAT_TERMINAL = event_store.HARNESS_SEAT_TERMINAL
SEAT_ROLE = "review-gate"
# The fabric's canonical class for reviewer work (phantom_guard.REVIEW_TASK_CLASSES).
SEAT_TASK_CLASS = "research_structured"

# Never in the seat's environment. DEFAULT_SCRUB_KEY_PATTERNS already removes the
# keys and tokens (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN);
# ANTHROPIC_* adds the redirect (ANTHROPIC_BASE_URL) and every model or header
# override, and the two cloud switches would bill another account.
SUBSCRIPTION_SCRUB_KEYS: frozenset = frozenset(
    DEFAULT_SCRUB_KEY_PATTERNS | {"ANTHROPIC_*", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"}
)

# The seat's tools, the whole set. In claude 2.1.308 ``--tools`` makes only these
# built-in tools exist in the session and ``--allowedTools`` pre-approves them:
# no Bash in any form, no write, edit, web, task or skill tool. The diff is in
# the prompt and files are read with Read.
SEAT_TOOLS = ("Read", "Grep", "Glob")
# "Don't prompt for permissions, deny if not pre-approved" (the CLI's own
# permission-mode schema). Named on the command line it also replaces a settings
# ``defaultMode`` such as ``auto``, whose classifier approves calls by itself.
SEAT_PERMISSION_MODE = "dontAsk"
# Nobody answers a permission prompt in ``claude -p``: what would prompt is denied.
SEAT_PERMISSION_PROMPTS = "none"
MCP_OFF_CONFIG = '{"mcpServers":{}}'

# The completion a review is read from. claude 2.1.308 ends a stream-json turn in
# one ``result`` message: ``subtype`` "success" or an error subtype
# (error_during_execution, error_max_turns, error_max_budget_usd,
# error_max_structured_output_retries), ``is_error`` required on both and true on
# a "success" whose text is an API error. SubprocessAdapter keeps both fields in
# the normalized ``completion``.
COMPLETION_SUCCESS_SUBTYPE = "success"

# The cost line of a subscription run: 0 at the margin, the source names why.
SUBSCRIPTION_COST_SOURCE = "subscription"
SUBSCRIPTION_BILLING_MODE = "subscription"

# Put before the gate's prompt: the seat's answer is its report.
SEAT_NOTICE = (
    "You are a read-only review seat. Your final answer is the report: end it with the "
    "verdict block below. Do not write, edit or create any file."
)

# A model-authored file at the report path is set aside under this suffix, never
# under ``.md``: every reader of unified_reports/ takes the exact <id>.md or a
# *.md glob, so no reader books a verdict from it (provider_dispatch's rule).
SEAT_AUTHORED_SUFFIX = ".seat-authored"

DispatcherFn = Callable[[str, str, str, str], str]


def seat_scope_args() -> List[str]:
    """The seat's whole capability scope, handed to the adapter in place of the role's.

    SubprocessAdapter puts it after the prompt (``scope_args``), where it would
    otherwise put ``--dangerously-skip-permissions`` or the review-gate role's
    Bash-capable fallback profile; with this list neither reaches the argv,
    whatever ``VNX_WORKER_SCOPED`` says. ``mcp__*`` is denied because an
    extension bridge surfaces MCP tools outside ``--mcp-config``
    (``worker_permissions.MCP_NAMESPACE_DENY``) and a settings allow rule would
    pre-approve them. The variadic flags each end at the next flag, and the
    boolean ``--strict-mcp-config`` ends the last one.
    """
    tools = ",".join(SEAT_TOOLS)
    return [
        "--tools", tools,
        "--allowedTools", tools,
        "--disallowedTools", MCP_NAMESPACE_DENY,
        "--permission-mode", SEAT_PERMISSION_MODE,
        "--permission-prompts", SEAT_PERMISSION_PROMPTS,
        "--mcp-config", MCP_OFF_CONFIG,
        "--strict-mcp-config",
    ]


def seat_env_overlay(dispatch_id: str, data_dir: Path) -> Dict[str, str]:
    """The VNX variables the seat's worker gets on top of the scrubbed process env."""
    import provider_switch

    overlay = provider_switch.worker_env_stamps(dispatch_id, state_dir=Path(data_dir) / "state")
    overlay.update({
        "VNX_WORKER_ROLE": SEAT_ROLE,
        "VNX_DATA_DIR": str(data_dir),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(Path(data_dir) / "state"),
    })
    return overlay


def make_claude_gate_dispatcher(
    data_dir: Path, *, base_ref: str, total_deadline_seconds: int,
    project_root: Optional[Path] = None,
) -> DispatcherFn:
    """The dispatcher gate_runner's harness-lane path calls for claude_gate."""
    base = Path(data_dir)

    def _dispatch(provider: str, model: str, prompt: str, dispatch_id: str) -> str:
        if provider != CLAUDE_LANE_PROVIDER:
            raise ValueError(
                f"claude_gate_lane runs provider {CLAUDE_LANE_PROVIDER!r} only, not {provider!r}"
            )
        return run_claude_seat(
            dispatch_id=dispatch_id, model=model, prompt=prompt, data_dir=base,
            base_ref=base_ref, deadline_seconds=total_deadline_seconds, project_root=project_root,
        )

    return _dispatch


@dataclass(frozen=True)
class _SeatRun:
    dispatch_id: str
    model: str
    result: Any
    started: datetime
    ended: datetime
    events_path: Optional[str]
    data_dir: Path


def run_claude_seat(
    *, dispatch_id: str, model: str, prompt: str, data_dir: Path, base_ref: str,
    deadline_seconds: int, project_root: Optional[Path] = None,
) -> str:
    """Run one claude review seat on the PR head and return its governed report text.

    A worktree that cannot be created raises (``create_dispatch_worktree`` fails
    loud, no fallback to the shared checkout), and gate_runner books that as a
    lane failure. The VNX data-dir and deadline variables are pinned on this
    process for the run (:func:`_lane_env_pins`), so the adapter's event store,
    the worktree teardown and the cost ledger all resolve ``data_dir``.
    """
    from dispatch_worktree_isolation import (
        create_dispatch_worktree, remove_dispatch_worktree, resolve_consumer_project_root,
    )
    from provider_spawns.claude_spawn import spawn_claude

    base = Path(data_dir)
    root = Path(project_root) if project_root is not None else resolve_consumer_project_root()
    with _lane_env_pins(base, deadline_seconds):
        worktree = create_dispatch_worktree(dispatch_id, project_root=root, base_ref=base_ref)
        started = datetime.now(timezone.utc)
        try:
            result = spawn_claude(
                prompt=f"{SEAT_NOTICE}\n\n{prompt}", model=model, dispatch_id=dispatch_id,
                terminal_id=SEAT_TERMINAL, cwd=worktree, role=SEAT_ROLE,
                total_deadline=float(deadline_seconds), scope_args=seat_scope_args(),
                extra_env=seat_env_overlay(dispatch_id, base), scrub_env_keys=SUBSCRIPTION_SCRUB_KEYS,
            )
        finally:
            _remove_seat_worktree(remove_dispatch_worktree, dispatch_id, root)
            archived = event_store.EventStore(events_dir=base / "events").archive_completed_stream(
                SEAT_TERMINAL, dispatch_id,
            )
        run = _SeatRun(
            dispatch_id=dispatch_id, model=getattr(result, "model_resolved", None) or model,
            result=result, started=started, ended=datetime.now(timezone.utc),
            events_path=str(archived) if archived is not None else None, data_dir=base,
        )
        return _govern(run)


def _remove_seat_worktree(remove: Callable[..., None], dispatch_id: str, project_root: Path) -> None:
    """Reset and remove the seat's worktree (``review_only``, OI-1629b); a failure is logged, never raised.

    Raised from the ``finally`` of the run, a teardown error would replace the
    run's own outcome; the worktree allocator keeps the claim for the reaper.
    """
    try:
        remove(dispatch_id, project_root=project_root, terminal_id=SEAT_TERMINAL, review_only=True)
    except Exception as exc:  # vnx-silent-except: logged with the dispatch id; teardown must not mask the run
        logger.warning("claude_gate_lane: worktree teardown failed for %s: %s", dispatch_id, exc)


@contextmanager
def _lane_env_pins(data_dir: Path, deadline_seconds: int) -> Iterator[None]:
    """The seat's VNX variables on ``os.environ`` for one run, restored exactly afterwards.

    On ``os.environ`` because they bind in this process: the adapter's
    EventStore, the worktree teardown event and the cost ledger resolve the data
    dir from the environment, and ``spawn_claude`` reads ``VNX_TOTAL_DEADLINE``
    over the deadline it is given. A key that did not exist before the run does
    not exist after it.
    """
    pins = {
        "VNX_DATA_DIR": str(data_dir),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_TOTAL_DEADLINE": str(deadline_seconds),
    }
    restore = {key: os.environ.get(key) for key in pins}
    try:
        os.environ.update(pins)
        yield
    finally:
        for key, previous in restore.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def final_answer(result: Any) -> str:
    """The model's final answer: the ``result`` event's text, else the streamed text."""
    completion = getattr(result, "completion", None)
    text = completion.get("text", "") if isinstance(completion, dict) else ""
    return (text or getattr(result, "completion_text", "") or "").strip()


def _returncode(result: Any) -> int:
    returncode = getattr(result, "returncode", 1)
    return returncode if isinstance(returncode, int) and not isinstance(returncode, bool) else 1


def _completion_causes(completion: Any) -> List[str]:
    """Why the completion event does not end a review: none when it is a successful one."""
    if not isinstance(completion, dict) or not completion:
        return ["the stream ended with no completion event"]
    subtype = completion.get("subtype")
    if subtype != COMPLETION_SUCCESS_SUBTYPE:
        return [f"the completion event has subtype {subtype!r}"]
    if completion.get("is_error") is not False:
        return [f"the completion event has subtype 'success' and is_error {completion.get('is_error')!r}"]
    return []


def run_failure(result: Any) -> Optional[str]:
    """Why this run is not a review, or None when it is one.

    A review is a run whose stream ended in a completion event with subtype
    ``success`` and ``is_error`` false, whose claude process exited 0 within its
    deadline, and whose completion carries an answer. Any other end is named
    here, and :func:`_govern` then never reads the run's answer.
    """
    refused = getattr(result, "spawn_refused", None)
    if refused:
        return f"claude never started: {refused}"
    causes: List[str] = []
    if getattr(result, "error", None):
        causes.append(f"the stream read failed: {result.error}")
    if getattr(result, "timed_out", False):
        causes.append("the run was stopped by its deadline or a stalled stream")
    causes.extend(_completion_causes(getattr(result, "completion", None)))
    code = _returncode(result)
    if code != 0:
        causes.append(f"claude exited with code {code}")
    if not causes and not final_answer(result):
        causes.append("the completion carries no answer")
    return "; ".join(causes) or None


def seat_exit_code(result: Any, failure: Optional[str]) -> int:
    """The exit code the report carries: claude's own, and never 0 for a run that is not a review."""
    code = _returncode(result)
    return 1 if failure is not None and code == 0 else code


def _status(result: Any, failure: Optional[str]) -> str:
    if getattr(result, "timed_out", False):
        return "timeout"
    return "failure" if failure is not None else "success"


def _report_response(run: _SeatRun, answer: str, failure: Optional[str]) -> Tuple[str, Optional[str]]:
    """The report's response text and spawn error.

    A review's response is its final answer. A claude that never started gives a
    spawn error and no response, which the report stamps as ``failure_reason``
    (OI-1748). Every other run that is not a review gets the statement of its
    cause instead of its answer: the runner books it on the report's
    ``exit_code`` with that cause in ``reason_detail`` (OI-1753), and no verdict
    the run produced is in the report to be read. The stream archive keeps the text.
    """
    if failure is None:
        return answer, None
    if getattr(run.result, "spawn_refused", None):
        return "", failure
    archive = f" Its stream is archived at {run.events_path}." if run.events_path else ""
    return (
        "_Not a review: this claude_gate run did not end in a successful completion, so its "
        f"answer is left out of this report and no verdict is read from it._{archive}\n\n"
        f"Cause: {failure}.\n"
    ), None


def _token_counts(result: Any) -> Dict[str, Optional[int]]:
    """``input``/``output``/``cache_read`` from the run's usage; None when it reported none."""
    usage = getattr(result, "token_usage", None)
    if not isinstance(usage, dict) or not usage:
        return {"input": None, "output": None, "cache_read": None}
    return {
        "input": int(usage.get("input_tokens", 0) or 0),
        "output": int(usage.get("output_tokens", 0) or 0),
        "cache_read": int(usage.get("cache_read_input_tokens", 0) or 0),
    }


def _govern(run: _SeatRun) -> str:
    """Cost line, receipt, report, in that order (ADR-005); returns the report text.

    The answer is read only from a run that is a review (:func:`run_failure`).
    """
    failure = run_failure(run.result)
    answer = final_answer(run.result) if failure is None else ""
    exit_code = seat_exit_code(run.result, failure)
    status = _status(run.result, failure)
    tokens = _token_counts(run.result)
    _book_subscription_cost(run, tokens)
    report_path = Path(run.data_dir) / "unified_reports" / f"{run.dispatch_id}.md"
    _emit_receipt(run, status, tokens, failure, report_path)
    _write_report(run, answer, exit_code, status, tokens, failure, report_path)
    return report_path.read_text(encoding="utf-8")


def _book_subscription_cost(run: _SeatRun, tokens: Dict[str, Optional[int]]) -> None:
    """The run's cost line: 0 at the margin, billed as the subscription. Raises on a failed write."""
    from harness_run_cost import ledger_project_id
    from provider_costs import emit_provider_cost

    emit_provider_cost(
        provider=CLAUDE_LANE_PROVIDER, model=run.model,
        input_tokens=tokens["input"], output_tokens=tokens["output"],
        cost_usd_estimate=0.0, dispatch_id=run.dispatch_id,
        project_id=ledger_project_id(Path(run.data_dir) / "state"),
        metadata={"gate": SEAT_GATE, "role": SEAT_ROLE},
        cost_source=SUBSCRIPTION_COST_SOURCE, billing_mode=SUBSCRIPTION_BILLING_MODE,
    )


def _emit_receipt(
    run: _SeatRun, status: str, tokens: Dict[str, Optional[int]], cause: Optional[str], report_path: Path,
) -> None:
    from governance_emit import emit_dispatch_receipt

    failure: Dict[str, Optional[str]] = {"failure_reason": None, "failure_class": None}
    if status != "success":
        from failure_classification import classify_failure_safe

        failure = classify_failure_safe(
            status=status, error=cause, completion_text="",
            timed_out=bool(getattr(run.result, "timed_out", False)), provider=CLAUDE_LANE_PROVIDER,
            duration_seconds=(run.ended - run.started).total_seconds(),
            returncode=getattr(run.result, "returncode", None), dispatch_id=run.dispatch_id,
        )
    usage: Dict[str, Any] = {
        "input": tokens["input"] or 0, "output": tokens["output"] or 0, "cache_hit": tokens["cache_read"] or 0,
    }
    if tokens["input"] is None:
        usage["unavailable"] = True
    emit_dispatch_receipt(
        dispatch_id=run.dispatch_id, terminal_id=SEAT_TERMINAL, provider=CLAUDE_LANE_PROVIDER,
        model=run.model, pr_id=None, status=status,
        completion_pct=100 if status == "success" else 0, risk=0.0, findings=[],
        duration_seconds=(run.ended - run.started).total_seconds(), token_usage=usage,
        cost_usd=0.0, cost_source=SUBSCRIPTION_COST_SOURCE, state_dir=Path(run.data_dir) / "state",
        report_path=str(report_path), events_path=run.events_path, role=SEAT_ROLE,
        receipt_kind="dispatch", task_class=SEAT_TASK_CLASS,
        session_id=getattr(run.result, "session_id", None),
        failure_reason=failure.get("failure_reason"), failure_class=failure.get("failure_class"),
        verification={"method": "pending-report", "tests_run": None, "tests_passed": None,
                      "tests_failed": None, "command": None, "pr_ref": None,
                      "push_verified": None, "spec_deviation": None},
    )


def _write_report(
    run: _SeatRun, answer: str, exit_code: int, status: str,
    tokens: Dict[str, Optional[int]], failure: Optional[str], report_path: Path,
) -> None:
    """The governed report: frontmatter gate_runner reads, the final answer as the body.

    A run that is not a review has the statement of its cause as the body instead
    (:func:`_report_response`).
    """
    from governance_emit import emit_unified_report
    from unified_report_schema import SCHEMA_VERSION, report_cost_fields

    _set_aside_seat_authored_report(report_path)
    frontmatter: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "dispatch_id": run.dispatch_id,
        "provider": CLAUDE_LANE_PROVIDER, "sub_provider": "anthropic", "model": run.model,
        "terminal_id": SEAT_TERMINAL, "pool_id": "headless", "role": SEAT_ROLE,
        "task_class": SEAT_TASK_CLASS, "pr_id": "none",
        "duration_seconds": round((run.ended - run.started).total_seconds(), 3),
        "exit_code": exit_code,
        "token_usage": {key: value or 0 for key, value in tokens.items()},
        **report_cost_fields(0.0, SUBSCRIPTION_COST_SOURCE),
        "route_decision": {
            "strategy": "review-gate", "selected_provider": CLAUDE_LANE_PROVIDER,
            "selected_model": run.model,
        },
    }
    deadline = getattr(run.result, "deadline_exceeded_seconds", None)
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        frontmatter.update({"deadline_exceeded": True, "deadline_seconds": float(deadline)})
    response_text, spawn_error = _report_response(run, answer, failure)
    emit_unified_report(
        dispatch_id=run.dispatch_id, terminal_id=SEAT_TERMINAL, provider=CLAUDE_LANE_PROVIDER,
        instruction="", response_text=response_text, findings=None,
        duration_seconds=(run.ended - run.started).total_seconds(), data_dir=Path(run.data_dir),
        frontmatter=frontmatter, model=run.model,
        spawn_error=spawn_error, preserve_partial=status != "success",
    )


def _set_aside_seat_authored_report(report_path: Path) -> None:
    """Move a report file the model wrote itself out of the governed report's way.

    The governed report is written only when no file is at its path. The seat has
    no tool that writes (:func:`seat_scope_args`), so a file there is not this
    lane's; it is kept, under :data:`SEAT_AUTHORED_SUFFIX`, so the evidence the
    runner reads is this lane's.
    """
    if not report_path.exists():
        return
    aside = report_path.with_name(report_path.stem + SEAT_AUTHORED_SUFFIX)
    os.replace(report_path, aside)
    logger.warning(
        "claude_gate_lane: the seat wrote %s itself; set aside as %s, the governed report replaces it",
        report_path, aside,
    )
