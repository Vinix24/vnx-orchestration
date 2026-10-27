#!/usr/bin/env python3
"""glm_gate.py — code-review gate via the GOVERNED glm-harness lane (DLv1 nieuwbouw).

Same shape as ``kimi_gate.py`` — a diff-review gate that routes through the
governed mechanism ``plan_gate_panel._make_default_dispatcher`` already
drives for provider lanes: ``provider_dispatch`` runs the glm worker via the
claude-CLI harness -> local :4141 litellm proxy -> OpenRouter, emits a
receipt, writes a unified report, and returns its text. The verdict is
extracted with the SAME parser kimi_gate uses (``_extract_verdict`` — the
provider-agnostic ```json``` scanner) so the same review, run through either
gate, reads the same way. Writes a result record via
``gate_recorder.record_terminal_result``, compatible with the shared
``gate_status``/``closure_verifier`` evidence contract every other gate
(codex_gate/ci_gate/kimi_gate) is held to.

``gate_request_handler`` recognizes ``glm_gate`` as a registered ``Gate``
member and refuses requests with ``reason=gate_runner_missing`` until this
file exists on disk (``_glm_gate_available``). This is that runner.

Usage:
    python3 scripts/glm_gate.py --pr 378 --data-dir ~/.vnx-data/<project>
    python3 scripts/glm_gate.py --diff-file /tmp/x.diff --pr 0   # offline diff source

Exit codes: 0 = pass, 2 = fail/blocked (a REAL parsed review verdict), 1 = infra
error / provider unavailable / blocked model (no diff, dispatch failed, no
readable verdict, or a non-glm-5.2 model was requested).

Provider outages are NOT review verdicts (OI-1142, ported from kimi_gate): when
the glm-harness lane dies on a quota/auth error, times out, or returns output
without a verdict block, the result status is ``unavailable`` — never
``fail``. ``reason`` distinguishes several disjoint states, identically to
kimi_gate: ``dispatch_error`` (the dispatcher raised outright with no report,
or a report came back but its own frontmatter stamps the underlying provider
run as failed), ``no_verdict`` (the report was empty/whitespace),
``relabeled_verdict`` (the verdict was answered inline under the
plan-reviewer role's ```vnx-plan-verdict``` fence instead of this gate's own
```json``` — see ``gate_report_recovery.extract_relabeled_verdict``),
``recovered_verdict`` (no verdict in the primary report, but a bounded search
found exactly one companion report carrying one — see
``gate_report_recovery.find_recovery_candidate``, dispatch-20260823-beta2-j),
``recovery_empty`` (that search found nothing), and ``recovery_ambiguous``
(that search found 2+ candidates — fail-closed, refuses rather than guesses).
Only ``dispatch_error``/``no_verdict`` may claim a provider outage in their
summary text; the recovery-related reasons must not, since the provider
plainly produced output. See ``governed_lane.frontmatter_run_outcome`` (same governed-report
frontmatter shape, same ``exit_code``/``token_usage_measured`` discrimination) for the
dispatch_error/genuine-parse-miss split gating entry into the recovery path.

OI-2066: ``timeout`` is the report whose frontmatter says the lane's own
deadline stopped the run (``deadline_exceeded``). It is checked before any
verdict extraction, so a partial verdict block in a cut-off report never
counts (``_unread_run_outcome``).

OI-1178 / OI-1435: a terminal (pass/fail) record must carry ``contract_hash``
+ ``report_path`` or ``gate_status.has_complete_evidence`` — and therefore
``closure_verifier``'s merge check — refuses it as evidence, forever. Measured
on kimi_gate before its own fix: 9 of 9 terminal records lacked both fields,
and every one of those verdicts was permanently unable to close a PR.
``contract_hash`` is computed by ``gate_artifacts._compute_contract_hash``
(the SAME function codex_gate's and kimi_gate's own execution paths call,
never a second hasher). ``report_path`` points at the governed dispatch's own
unified report, already on disk by the time the dispatcher call returns.

An ``unavailable`` result carries an empty ``contract_hash`` always, and (as
of OI-1477) a POPULATED ``report_path`` — pointing at this gate's own report,
which is what actually carries the provider's failure text (a 402/403 body,
an outage message) when there is no lane log to lift it from instead. This
does not weaken the OI-1435 guarantee: ``gate_status.has_complete_evidence``
checks ``gate_status.is_terminal()`` BEFORE it ever looks at
``contract_hash``/``report_path``, and ``is_terminal`` only admits
pass/fail/not_executable — never ``unavailable``. A populated ``report_path``
on an unavailable record therefore still cannot pass ``has_complete_evidence``
(nor ``closure_verifier``'s merge check, which checks ``gate_is_terminal``
first for the identical reason); only ``contract_hash`` being empty was ever
load-bearing for that separation, and it stays empty on every non-terminal
status.

Model: glm-5.2, glm-5.3, or glm-5.3-flash (``deprecated-glm-models``,
provider_constraints.yaml, operator directive 2026-09-14; glm-5.2 remains
default). GLM-4.5, GLM-4.6, base GLM-5, and GLM-5.1 are blocked. This gate
validates the model at its own entry point — before ever
reaching the dispatcher — so a blocked version is refused LOUDLY with an
explicit reason, not silently passed through to fail deep inside the
provider-dispatch constraint enforcer and surface indistinguishable from any
other "unavailable" outage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from functools import partial
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from plan_gate_panel import _make_default_dispatcher, _resolve_data_dir  # governed provider_dispatch lane
from governed_lane import (  # GE-5a/GE-5b: one shared adapter for the harness lanes
    GateRunState, GateSeams, build_prompt, extract_verdict,
    frontmatter_duration_seconds as _frontmatter_duration_seconds,
    frontmatter_run_outcome as _frontmatter_run_outcome, get_diff as _get_diff,
    lift_lane_log_reason as _lift_lane_log_reason,
    lift_report_failure_reason as _lift_report_failure_reason, parse_dispatch_window_start,
    provider_row, reprocess_original_commit_sha, review_dispatcher, run_gate_tail, status_summary,
    unread_run_outcome, verdict_to_status,
)
from gate_recorder import (
    get_pr_head_branch,
    get_pr_head_sha,
    persist_request,
    record_terminal_result,
    result_file_path,
    stamp_request_identity,
)
import gate_depth  # OI-1618: a verdict without investigation is no verdict, on every lane
from gate_artifacts import _compute_contract_hash  # canonical hasher; the tail uses governed_lane's binding
from gate_lane_contract import (  # C6 step 3: one source, three readers
    MODEL_DEFAULTS,
    VALID_VERDICTS,
    VERDICT_CONTRACT,
    describe_dispatch_exception,
    max_diff_chars,
)
from gate_prompt import scan_diff_for_instructions  # OI-1442: the diff is data, not instruction
from gate_report_recovery import find_recovery_candidate

_VALID_VERDICTS = VALID_VERDICTS  # C6 step 3 + OI-1767: one source, not a fourth literal copy

# GE-5b: one body per helper now lives in ``governed_lane`` (ADR-046 §4); these names stay
# bound HERE because ``main`` resolves them off this module and the test patches on
# ``_get_diff`` / ``_make_default_dispatcher`` must keep reaching what ``main`` calls.
_PROVIDER = "glm"
_ROW = provider_row(_PROVIDER)  # gate/provider names, dispatch-id prefix and lifts

_extract_verdict = partial(extract_verdict, provider=_PROVIDER)  # text is the first argument
_build_prompt = partial(build_prompt, _PROVIDER)
_parse_dispatch_window_start = partial(parse_dispatch_window_start, _PROVIDER)
_reprocess_original_commit_sha = partial(reprocess_original_commit_sha, _PROVIDER)
_verdict_to_status = partial(verdict_to_status, _PROVIDER)
_unread_run_outcome = partial(unread_run_outcome, _PROVIDER)
_status_summary = partial(status_summary, _PROVIDER)


def _review_dispatcher(base_data_dir: Path, branch: str, coverage: dict, explicit_timeout):
    """This gate's governed dispatcher (OI-2066): the one body, THIS module's factory."""
    return review_dispatcher(
        _PROVIDER, base_data_dir, branch, coverage, explicit_timeout,
        dispatcher_factory=_make_default_dispatcher,
    )


# One source (C6 step 3): model env var/default, timeout, diff cap and verdict
# contract come from gate_lane_contract — the SAME objects gate_runner and
# kimi_gate read. The aliases keep this gate's existing names for the readers
# below; ALLOWED_MODELS stays here because it is glm-specific. The timeout has
# no alias since OI-2066: it is resolved per run, for the diff in hand
# (``_review_dispatcher``).
_MODEL_ENV, DEFAULT_MODEL = MODEL_DEFAULTS["glm_gate"]
_VERDICT_CONTRACT = VERDICT_CONTRACT

# This gate pins to exactly the configured default model (MODEL_DEFAULTS /
# VNX_GLM_GATE_MODEL) — narrower by design than deprecated-glm-models, which
# since operator directive 2026-09-14 admits glm-5.2, glm-5.3, and
# glm-5.3-flash (provider_constraints.yaml) so all three can be measured
# elsewhere. The gate itself stays pinned to one model (glm-5.2, unchanged)
# for a deterministic reviewer; widening the gate's own model choice is a
# separate operator decision, not implied by the wider dispatch allowlist.
ALLOWED_MODELS = frozenset({DEFAULT_MODEL})


def _validate_model(model: str) -> "str | None":
    """Return the canonical (lowercase) model string, or None to refuse.

    Only the configured default (``glm-5.2``, MODEL_DEFAULTS["glm_gate"]) is
    admitted here. Matching is case-insensitive (accepts "GLM-5.2") but the
    canonical lowercase form is what actually gets dispatched, so the
    record's ``model`` field always matches the wave7_models.yaml registry
    key exactly.
    """
    normalized = (model or "").strip().lower()
    if normalized in ALLOWED_MODELS:
        return normalized
    return None


# OI-1710: the report-frontmatter failure lift now lives in ``governed_lane`` and is on the glm
# register row (``report_failure_lift``), so the shared tail calls it without a provider branch.
# The alias stays here for readers of this module.


def _gate_seams() -> GateSeams:
    """Phase 3-6 callables, resolved off THIS module at call time (tests patch these names here)."""
    return GateSeams(
        get_pr_head_sha=get_pr_head_sha, get_pr_head_branch=get_pr_head_branch,
        record_terminal_result=record_terminal_result, result_file_path=result_file_path,
        stamp_request_identity=stamp_request_identity, find_recovery_candidate=find_recovery_candidate,
    )


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="GOVERNED glm-harness review gate.")
    ap.add_argument("--pr", required=True, help="PR number (use 0 with --diff-file for offline test)")
    ap.add_argument("--diff-file", default=None, help="read the diff from a file instead of gh")
    ap.add_argument("--data-dir", default=os.environ.get("VNX_DATA_DIR", ""),
                    help="VNX data dir; report lands in <data-dir>/unified_reports/ and the "
                         "result in <data-dir>/state/review_gates/results/")
    ap.add_argument("--model", default=os.environ.get(_MODEL_ENV, DEFAULT_MODEL))
    # OI-2066: no fixed default — unset, the deadline is resolved for the diff.
    ap.add_argument("--timeout", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="print the result record as JSON")
    ap.add_argument(
        "--reprocess", metavar="DISPATCH_ID", default=None,
        help="reprocess an already-completed dispatch from its on-disk report "
             "instead of calling the dispatcher again — no diff fetch, no model "
             "call, no new receipt. Recovers a verdict a companion report or a "
             "wrong-label fence already carries. Requires --pr for identity/"
             "staleness verification.",
    )
    args = ap.parse_args(argv)

    canonical_model = _validate_model(args.model)
    if canonical_model is None:
        print(
            f"glm_gate: REFUSING model {args.model!r} — only {sorted(ALLOWED_MODELS)} is "
            "admitted for GLM (deprecated-glm-models: GLM-4.5/4.6/5/5.1 are blocked, "
            "operator directive 2026-08-03)",
            file=sys.stderr,
        )
        return 1
    args.model = canonical_model

    data_dir = args.data_dir or None
    base_data_dir = _resolve_data_dir(data_dir)

    pr_number = int(args.pr) if str(args.pr).isdigit() else None
    # dispatch_id must exist before the request record below AND before the
    # if/else that follows (both --reprocess and a live run need the SAME
    # dispatch_id their downstream code uses — see the (now-redundant)
    # reassignments removed from both arms below this block).
    dispatch_id = args.reprocess if args.reprocess else f"{_ROW.dispatch_id_prefix}{args.pr}-{int(time.time())}"
    # Offline runs read the diff from a file (--diff-file) and are NOT tied to
    # a live GitHub PR — branch/commit_sha are computed once, here, and reused
    # for both the request record below and the result record's identity
    # stamp further down, so a real ``gh pr view`` lookup never runs twice for
    # the same run.
    test_run = bool(args.diff_file)
    branch = "" if test_run else get_pr_head_branch(pr_number)
    commit_sha = "" if test_run else get_pr_head_sha(pr_number)
    # Points at the governed dispatch's own unified report regardless of
    # outcome. OI-1477: this SAME string is also what ``report_path`` gets
    # set to on a non-terminal ``unavailable`` result (see the OI-1178/
    # OI-1435/OI-1477 block below) — never read by has_complete_evidence/
    # is_terminal either way, since those gate on status, not on which
    # fields happen to be populated.
    report_path_informational = str(base_data_dir / "unified_reports" / f"{dispatch_id}.md")

    requests_dir = base_data_dir / "state" / "review_gates" / "requests"
    request_payload = {
        "gate": "glm_gate",
        "status": "requested",
        "provider": _ROW.lane_provider,
        "branch": branch,
        "pr_number": pr_number,
        "commit_sha": commit_sha,
        "report_path": report_path_informational,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dispatch_id": dispatch_id,
        "test_run": test_run,
    }
    # OI-1435 residu (defect 4): a request record must exist BEFORE the gate
    # ever dispatches — a record written only after the run proves nothing
    # was "requested" first. Same on-disk shape (pr-<N>-<gate>.json under
    # state/review_gates/requests/) as ci_gate/codex_gate's own request
    # writers (gate_request_handler._request_ci_gate/_request_codex via
    # gate_recorder.persist_request) so a generic reader of the requests dir
    # sees no difference between this and any other gate's request.
    try:
        requests_dir.mkdir(parents=True, exist_ok=True)
        persist_request(requests_dir, "glm_gate", request_payload, pr_number=pr_number, pr_id="")
    except OSError as exc:
        print(f"glm_gate: FAILED to write request record: {exc}", file=sys.stderr)
        return 1

    if args.reprocess:
        # Deel 3 (dispatch-20260823-beta2-j): re-run the SAME parse/recover/
        # record pipeline below against a dispatch that already ran, without a
        # new model call. `report_text = dispatcher(...)` is skipped and
        # replaced by a read of the report the ORIGINAL run already wrote to
        # disk — everything after (verdict extraction, recovery search,
        # record) is unchanged code, so a recovered verdict here was produced
        # by the gate itself, on the run's own commit, in the run's own
        # contract shape — not prose-to-verdict promotion of a new source.
        # dispatch_id is already resolved above (shared with the request record).
        report_path_obj = base_data_dir / "unified_reports" / f"{dispatch_id}.md"
        if not report_path_obj.is_file():
            print(
                f"glm_gate: --reprocess: no report on disk for dispatch_id={dispatch_id!r} "
                f"at {report_path_obj}", file=sys.stderr,
            )
            return 1
        report_text = report_path_obj.read_text(encoding="utf-8")
        dispatch_error = ""
        window_start = _parse_dispatch_window_start(dispatch_id, pr=args.pr)
        if window_start is None:
            print(
                f"glm_gate: --reprocess: dispatch_id={dispatch_id!r} does not match the "
                f"glm-gate-pr{args.pr}-<timestamp> shape this gate constructs — refusing "
                "to guess a recovery time window",
                file=sys.stderr,
            )
            return 1
        window_end = report_path_obj.stat().st_mtime
        prompt = None  # never fetched in reprocess mode — see contract_hash below
        # OI-1442: --reprocess never re-fetches the diff, so there is nothing
        # to scan. An empty list here is the honest value — the alternative
        # (re-fetching the CURRENT diff to scan it) would attach findings from
        # one commit to a verdict formalized against another.
        scan_findings: list = []
        duration = _frontmatter_duration_seconds(report_text)
        # OI-1618: --reprocess never re-fetches the diff (see above), so there
        # is nothing to measure here either — the "not measured" depth, which
        # gate_depth.is_degenerate always reads as unmeasured rather than
        # degenerate, never re-evaluated retroactively against a floor this
        # run was never held to when it originally executed.
        execution_depth = gate_depth.ExecutionDepth()
    else:
        diff = _get_diff(args.pr, args.diff_file)
        if not diff:
            print(f"glm_gate: no diff for PR {args.pr}", file=sys.stderr)
            return 1

        # OI-1618: the diff IS the investigation for a single-shot lane — no
        # agentic tool loop to measure. diff_chars is the post-strip length
        # (the single-shot degeneracy floor), diff_truncated mirrors the same
        # per-gate cap (OI-1874: gate_lane_contract.max_diff_chars) gate_prompt.
        # wrap_untrusted_diff applies to the raw (pre-strip) text. OI-1851: plus
        # the cap and the cut files.
        # OI-1874 r3: resolved ONCE here — `diff_cap` is the single source
        # passed to both `gate_depth.diff_coverage` (below) and `_build_prompt`
        # (below it), so a mutable-config change between the two calls this
        # gate used to make can no longer make the recorded `diff_truncated`
        # disagree with what the prompt actually truncated.
        diff_cap = max_diff_chars("glm_gate")
        coverage = gate_depth.diff_coverage(diff, diff_cap)
        execution_depth = gate_depth.single_shot_depth(
            coverage["diff_chars"], coverage["diff_truncated"],
            diff_limit=coverage["diff_limit"],
            truncated_files=tuple(coverage["truncated_files"]),
        )

        # dispatch_id is already resolved above (shared with the request record).
        # role="review-gate" (dispatch-20260823-beta2-j): the default role of
        # _make_default_dispatcher is "plan-reviewer", which carries
        # agents/plan-reviewer/CLAUDE.md — a role written for a PLAN-GATE
        # PANEL SEAT. That role gives the model THREE conflicting instructions
        # against this gate's own _VERDICT_CONTRACT below: a different fence
        # label (```vnx-plan-verdict``` vs ```json```), a different verdict
        # vocabulary (pass/revise/block vs pass/fail/blocked), and a different
        # destination (a self-authored report FILE vs an inline response —
        # provider_dispatch's governed lane already captures the response text
        # as the report, so the model writing one itself is not just unneeded,
        # it is what caused the second-report-pit bug). agents/review-gate/
        # CLAUDE.md is this gate's OWN role: none of the three conflicts.
        # Distinguishing this dispatch by an explicit role= kwarg (a spec
        # value already threaded through _make_default_dispatcher/
        # provider_dispatch's --role) keeps plan_gate_panel's own
        # plan-reviewer panelists (which DO need the file-authoring, fence,
        # and vocabulary this role change removes) untouched — a role-level
        # split, not a dispatch_id string check.
        # OI-1887: isolate the governed dispatch on the PR's own branch instead
        # of the default origin/main — measured 27-09 on PR #1950 via
        # gate_runner's harness-lane path (same _make_default_dispatcher call,
        # same missing base_ref): with no PR branch of its own to isolate on,
        # the agent fetched/checked out the branch itself, in the
        # orchestrator's own checkout. `branch` is already resolved above
        # (get_pr_head_branch, empty only for an offline --diff-file run,
        # which has no real branch to isolate on either).
        dispatcher = _review_dispatcher(base_data_dir, branch, coverage, args.timeout)
        prompt = _build_prompt(diff, args.pr, diff_cap)
        # OI-1442: the deterministic half. The prompt tells the model to report
        # instruction-shaped text in the diff as a finding; this scan does not
        # depend on it having done so. The two are joined at the record below.
        scan_findings = scan_diff_for_instructions(diff)

        start = time.monotonic()
        report_text = ""
        dispatch_error = ""
        try:
            # Governed lane: provider_dispatch runs glm (via the claude-CLI harness
            # -> local :4141 litellm proxy -> OpenRouter), writes a unified
            # report, returns its text. "glm-harness" is the real provider string
            # provider_dispatch.py dispatches on (see plan_gate_panel.py's own
            # diverse-assurance panel entry: provider="glm-harness").
            report_text = dispatcher(_ROW.lane_provider, args.model, prompt, dispatch_id)
        except Exception as exc:  # noqa: BLE001 — dispatch/report-read failure
            # OI-1142: do NOT bail without a record — a required gate that cannot fire
            # must surface as ``unavailable`` in the results dir, not vanish.
            # OI-2066: str(exc) of a TimeoutExpired is the whole argv, the diff
            # included — describe it without argv or prompt, bounded.
            dispatch_error = describe_dispatch_exception(exc, prompt=prompt)
            print(f"glm_gate: governed glm dispatch failed: {dispatch_error}", file=sys.stderr)
        duration = time.monotonic() - start

        report_path_obj = base_data_dir / "unified_reports" / f"{dispatch_id}.md"
        # (a) OI-recovery-venster: reuse dispatch_id's OWN embedded
        # int(time.time()) floor (set moments earlier when dispatch_id was
        # constructed above) instead of a fresh sub-second time.time() read.
        # Measured 26-08 as a real CI flake
        # (test_search_recovers_verdict_from_companion[kimi], same commit
        # 7a4d4a93: pass then fail) — red on the Linux CI runner, green
        # locally on macOS. Likely cause (T0 measurement, 26-08, 8/8 writes
        # sub-millisecond AFTER time.time() on APFS, zero truncated): Linux
        # stamps file mtimes from a COARSE clock
        # (ktime_get_coarse_real_ts64, refreshed once per timer tick) while
        # time.time() reads the fine vDSO clock — a file written a fraction
        # of a millisecond after a time.time() read can land ONE TICK
        # EARLIER than that reading. This is clock SKEW between two
        # differently-grained sources, not filesystem truncation to whole
        # seconds — APFS shows no truncation at all, yet the same skew
        # windowing logic must still hold there. The fix does not depend on
        # diagnosing the skew precisely: floor(t) is a full second below the
        # real construction instant, comfortably larger than any millisecond-
        # scale tick skew, and lowering window_start can only ever ADMIT
        # more files, never exclude ones the old (higher) bound would have
        # included — so this closes the race regardless of the exact
        # mechanism.
        #
        # Cannot be None here: dispatch_id was just constructed above (this
        # exact branch, this exact args.pr, this exact shape) — the None
        # path only matters for --reprocess, where dispatch_id is externally
        # supplied. That guarantee depends on the construction ~130 lines up
        # staying in this exact shape; not enforced at this call site.
        window_start = _parse_dispatch_window_start(dispatch_id, pr=args.pr)
        window_end = report_path_obj.stat().st_mtime if report_path_obj.is_file() else time.time()

    state = GateRunState(
        provider=_PROVIDER, row=_ROW, args=args, base_data_dir=base_data_dir,
        pr_number=pr_number, dispatch_id=dispatch_id, branch=branch, commit_sha=commit_sha,
        requests_dir=requests_dir, report_path_obj=report_path_obj, report_text=report_text,
        dispatch_error=dispatch_error, duration=duration, window_start=window_start,
        window_end=window_end, prompt=prompt, scan_findings=scan_findings,
        execution_depth=execution_depth, seams=_gate_seams(),
    )
    # GE-5b-2: phases 3-6 are ONE body in governed_lane, fed this module's own seam bindings.
    return run_gate_tail(state)


if __name__ == "__main__":
    raise SystemExit(main())
