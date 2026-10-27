#!/usr/bin/env python3
"""kimi_gate.py — temporary code-review gate via the GOVERNED kimi lane.

Diff-review gate that routes through the same governed mechanism the plan-gate
panel uses: ``plan_gate_panel._make_default_dispatcher`` runs the kimi worker via
``provider_dispatch`` (constraint-safe, emits a receipt, writes a unified report)
and returns the report text. The verdict is extracted with the provider-agnostic
```json``` scanner in ``codex_parser``. Writes a result record compatible with the
codex_gate result schema (``review_gates/results/pr-<N>-kimi_gate.json``).

Why this exists: codex usage is temporarily exhausted, so kimi stands in as the
review gate until codex is available again. Raw ``kimi --print`` is intentionally
NOT used — the governance dispatch-guard requires provider_dispatch so the review
stays on the receipt trail.

Usage:
    python3 scripts/kimi_gate.py --pr 378 --data-dir ~/.vnx-data/<project>
    python3 scripts/kimi_gate.py --diff-file /tmp/x.diff --pr 0   # offline diff source

Exit codes: 0 = pass, 2 = fail/blocked (a REAL parsed review verdict), 1 = infra
error / provider unavailable (no diff, dispatch failed, or no readable verdict).

Provider outages are NOT review verdicts (OI-1142): when the kimi CLI dies on a
quota-403/429/auth error, times out, or returns output without a verdict block,
the result status is ``unavailable`` — never ``fail``. Eleven quota-403 outages
were once booked as eleven rejected PRs because the no-verdict path defaulted to
"fail"; absence of evidence must surface as absence, not as a rejection.

OI-1291: ``unavailable`` is not a single cause. ``reason`` distinguishes
several disjoint states: ``dispatch_error`` (the dispatcher failed to deliver
a working run — either it raised outright with no report at all, or a report
DID come back but its own frontmatter stamps the underlying provider run as
failed; see below), ``no_verdict`` (the report was empty/whitespace — a
genuinely empty delivery), ``relabeled_verdict`` (kimi answered inline under
the plan-reviewer role's ```vnx-plan-verdict``` fence instead of this gate's
own ```json``` — see ``gate_report_recovery.extract_relabeled_verdict``),
``recovered_verdict`` (no verdict in the primary report, but a bounded
search found exactly one companion report carrying one — see
``gate_report_recovery.find_recovery_candidate``, dispatch-20260823-beta2-j),
``recovery_empty`` (that search found nothing), and ``recovery_ambiguous``
(that search found 2+ candidates — fail-closed, refuses rather than
guesses). Only ``dispatch_error``/``no_verdict`` may claim a provider outage
in their summary text; the recovery-related reasons must not, since kimi
plainly produced output.

OI-2066: ``timeout`` is the report whose frontmatter says the lane's own
deadline stopped the run (``deadline_exceeded``). It is checked before any
verdict extraction, so a partial verdict block in a cut-off report never
counts (``_unread_run_outcome``).

OI-1291 (fix-forward): the first cut of this split used "did a report come
back" as the discriminator between a parse miss and outage. That axis is
wrong — a real quota/auth outage still writes a report, because the
failure-path ``emit_unified_report`` call runs on the dispatcher's failure
path too. A spooled 403 error body reads as "content", so that axis booked a
real outage as "kimi did respond, parse miss" — the same failure class this
whole fix exists to remove, just mirrored. The report's own YAML frontmatter
carries a signal content can't fake: ``exit_code`` is stamped by the lane
from the actual spawn result, not guessed from the response text. A
non-zero ``exit_code`` with readable frontmatter means the provider run
itself failed — that is ``dispatch_error``, even though a report exists.
Only ``exit_code == 0`` with readable frontmatter and no verdict block gates
entry into the recovery path below (dispatch-20260823-beta2-j). When the
frontmatter can't be read at all (older/foreign report shape), the outcome
can't be read off the report — this falls back to the same recovery path,
which is intentionally too broad: a masked provider failure with no readable
``exit_code`` would still land here, but there is no other signal left to
tell the two apart.

OI-1291 (fix-forward, meetgat correction): the first cut of the frontmatter
axis also treated zero output tokens as a failure signal on its own. On the
kimi lane, post-run token capture is frequently unavailable — the lane
stamps ``token_usage_measured: false`` and ``output: 0`` on those runs, so
zero output tokens routinely means "not measured", not "the run produced
nothing". A count of 766 kimi reports on disk found 502 with exit_code 0,
real content, and ``token_usage_measured: false`` — successful runs the
raw-zero check booked as provider outages, reintroducing OI-1291's original
bug on a different axis. ``output_tokens`` now only counts as a failure
signal when the frontmatter's own ``token_usage_measured`` flag is true;
otherwise only ``exit_code`` decides.

OI-1178 (DLv2): a terminal (pass/fail) record must carry ``contract_hash`` +
``report_path`` or ``gate_status.has_complete_evidence`` — and therefore
``closure_verifier``'s merge check — refuses it as evidence, forever, no
matter how good the review was. ``contract_hash`` is computed by
``gate_artifacts._compute_contract_hash`` (the SAME function codex_gate's
own execution path calls), never a second hashing method. ``report_path``
points at the governed dispatch's own unified report
(``<data_dir>/unified_reports/<dispatch_id>.md``), already written to disk
by the time the dispatcher call returns. An ``unavailable`` result (OI-1142)
always carries an empty ``contract_hash`` — it is not terminal and must
never look like complete evidence, and ``gate_status.has_complete_evidence``
checks ``gate_status.is_terminal()`` before it ever looks at either field, so
that stays true regardless. As of OI-1477 ``report_path`` IS populated on an
unavailable result (pointing at this gate's own report, the one place a
provider failure's real text — a 403 body, a quota message — can be read
from when no lane log exists) — that does not weaken the guarantee above,
since ``is_terminal`` never admits ``unavailable`` in the first place.
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
    lift_lane_log_reason as _lift_lane_log_reason, parse_dispatch_window_start,
    provider_row, reprocess_original_commit_sha, review_dispatcher, run_gate_tail,
    status_summary, unread_run_outcome, verdict_to_status,
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

_VALID_VERDICTS = VALID_VERDICTS  # C6 step 3 + OI-1767: one source, not a third literal copy

# GE-5b: one body per helper now lives in ``governed_lane`` (ADR-046 §4); these names stay
# bound HERE because ``main`` resolves them off this module and the test patches on
# ``_get_diff`` / ``_make_default_dispatcher`` must keep reaching what ``main`` calls.
_PROVIDER = "kimi"
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
# glm_gate read. The aliases keep this gate's existing names for the readers
# below. The timeout has no alias since OI-2066: it is resolved per run, for
# the diff in hand (``_review_dispatcher``).
_MODEL_ENV, DEFAULT_MODEL = MODEL_DEFAULTS["kimi_gate"]
_VERDICT_CONTRACT = VERDICT_CONTRACT


def _gate_seams() -> GateSeams:
    """Phase 3-6 callables, resolved off THIS module at call time (tests patch these names here)."""
    return GateSeams(
        get_pr_head_sha=get_pr_head_sha, get_pr_head_branch=get_pr_head_branch,
        record_terminal_result=record_terminal_result, result_file_path=result_file_path,
        stamp_request_identity=stamp_request_identity, find_recovery_candidate=find_recovery_candidate,
    )


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Temporary GOVERNED kimi review gate.")
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
    # OI-1477 block below) — never read by has_complete_evidence/is_terminal
    # either way, since those gate on status, not on which fields happen to
    # be populated.
    report_path_informational = str(base_data_dir / "unified_reports" / f"{dispatch_id}.md")

    requests_dir = base_data_dir / "state" / "review_gates" / "requests"
    request_payload = {
        "gate": "kimi_gate",
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
    # Defect 4 (23-08 poort-schrijft-stil-niets dispatch): a request record
    # must exist BEFORE the gate ever dispatches — a record written only
    # after the run proves nothing was "requested" first. Same on-disk shape
    # (pr-<N>-<gate>.json under state/review_gates/requests/) as
    # ci_gate/codex_gate's own request writers
    # (gate_request_handler._request_ci_gate/_request_codex via
    # gate_recorder.persist_request) so a generic reader of the requests dir
    # sees no difference between this and any other gate's request.
    try:
        requests_dir.mkdir(parents=True, exist_ok=True)
        persist_request(requests_dir, "kimi_gate", request_payload, pr_number=pr_number, pr_id="")
    except OSError as exc:
        print(f"kimi_gate: FAILED to write request record: {exc}", file=sys.stderr)
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
                f"kimi_gate: --reprocess: no report on disk for dispatch_id={dispatch_id!r} "
                f"at {report_path_obj}", file=sys.stderr,
            )
            return 1
        report_text = report_path_obj.read_text(encoding="utf-8")
        dispatch_error = ""
        window_start = _parse_dispatch_window_start(dispatch_id, pr=args.pr)
        if window_start is None:
            print(
                f"kimi_gate: --reprocess: dispatch_id={dispatch_id!r} does not match the "
                f"kimi-gate-pr{args.pr}-<timestamp> shape this gate constructs — refusing "
                "to guess a recovery time window",
                file=sys.stderr,
            )
            return 1
        window_end = report_path_obj.stat().st_mtime
        prompt = None  # never fetched in reprocess mode — see contract_hash below
        # OI-1442: --reprocess never re-fetches the diff, so there is nothing
        # to scan. An empty list is the honest value — scanning the CURRENT
        # diff would attach findings from one commit to a verdict formalized
        # against another.
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
            print(f"kimi_gate: no diff for PR {args.pr}", file=sys.stderr)
            return 1

        # OI-1618: the diff IS the investigation for a single-shot lane — no
        # agentic tool loop to measure. diff_chars is the post-strip length
        # (the single-shot degeneracy floor), diff_truncated mirrors the same
        # per-gate cap (OI-1874: gate_lane_contract.max_diff_chars) gate_prompt.
        # wrap_untrusted_diff applies to the raw (pre-strip) text. OI-1851: plus
        # the cap and the cut files.
        # OI-1874 r3: resolved ONCE — see glm_gate.py's identical fix. `diff_cap`
        # is the single source passed to both `gate_depth.diff_coverage` and
        # `_build_prompt` below.
        diff_cap = max_diff_chars("kimi_gate")
        coverage = gate_depth.diff_coverage(diff, diff_cap)
        execution_depth = gate_depth.single_shot_depth(
            coverage["diff_chars"], coverage["diff_truncated"],
            diff_limit=coverage["diff_limit"],
            truncated_files=tuple(coverage["truncated_files"]),
        )

        # dispatch_id is already resolved above (shared with the request record).
        # role="review-gate" (dispatch-20260823-beta2-j): see glm_gate.py's
        # own comment at the identical call site. The default role
        # "plan-reviewer" carries agents/plan-reviewer/CLAUDE.md, written for
        # a plan-gate PANEL SEAT. That role gives the model THREE conflicting
        # instructions against this gate's own _VERDICT_CONTRACT below: a
        # different fence label (```vnx-plan-verdict``` vs ```json```), a
        # different verdict vocabulary (pass/revise/block vs
        # pass/fail/blocked), and a different destination (a self-authored
        # report FILE vs an inline response). agents/review-gate/CLAUDE.md is
        # this gate's OWN role: none of the three conflicts. A role-level
        # split, not a dispatch_id string check.
        # OI-1887: isolate on the PR's own branch instead of origin/main — see
        # glm_gate's identical call site for the measured incident (PR #1950,
        # via gate_runner's harness-lane path). `branch` is already resolved
        # above (get_pr_head_branch, empty only for an offline --diff-file run).
        dispatcher = _review_dispatcher(base_data_dir, branch, coverage, args.timeout)
        prompt = _build_prompt(diff, args.pr, diff_cap)
        # OI-1442: the deterministic half — see glm_gate's identical call site.
        # The prompt asks the model to report instruction-shaped text in the
        # diff; this scan does not depend on it having done so.
        scan_findings = scan_diff_for_instructions(diff)

        start = time.monotonic()
        report_text = ""
        dispatch_error = ""
        try:
            # Governed lane: provider_dispatch runs kimi, writes a unified report, returns its text.
            report_text = dispatcher(_ROW.lane_provider, args.model, prompt, dispatch_id)
        except Exception as exc:  # noqa: BLE001 — dispatch/report-read failure
            # OI-1142: do NOT bail without a record — a required gate that cannot fire
            # must surface as ``unavailable`` in the results dir, not vanish.
            # OI-2066: str(exc) of a TimeoutExpired is the whole argv, the diff
            # included — describe it without argv or prompt, bounded.
            dispatch_error = describe_dispatch_exception(exc, prompt=prompt)
            print(f"kimi_gate: governed kimi dispatch failed: {dispatch_error}", file=sys.stderr)
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
