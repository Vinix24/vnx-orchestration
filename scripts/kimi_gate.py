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
carries neither: it is not terminal and must never look like complete
evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from plan_gate_panel import _make_default_dispatcher, _resolve_data_dir  # governed provider_dispatch lane
from gate_recorder import (
    get_pr_head_branch,
    get_pr_head_sha,
    record_terminal_result,
    stamp_request_identity,
)
from gate_artifacts import _compute_contract_hash  # canonical hash source — never a second hasher
from unified_report_schema import SchemaViolation, parse_frontmatter
from gate_report_recovery import (
    AmbiguousRecoveryCandidates,
    extract_relabeled_verdict,
    find_recovery_candidate,
    recovered_verdict_conflicts,
)

_VALID_VERDICTS = {"pass", "fail", "blocked"}


def _extract_verdict(text: str) -> dict:
    """Extract the worker's verdict: the LAST ```json``` block whose ``verdict`` is a
    real verdict (pass/fail/blocked).

    The governed worker report echoes the instruction, so the FIRST ```json``` block is
    our own contract example (its ``verdict`` is the literal "pass|fail|blocked", which is
    not a valid single verdict and is skipped). Scanning from the end and requiring a real
    verdict value selects the worker's actual answer, never the echoed template.
    """
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text or "", re.DOTALL)
    for block in reversed(blocks):
        try:
            obj = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and str(obj.get("verdict", "")).strip().lower() in _VALID_VERDICTS:
            return obj
    return {}

DEFAULT_MODEL = "kimi-k3"
DEFAULT_TIMEOUT = 900
MAX_DIFF_CHARS = 50000

_VERDICT_CONTRACT = (
    "When done, end your report with a structured JSON verdict ONLY, in a fenced block:\n"
    "```json\n"
    "{\n"
    '  "verdict": "pass|fail|blocked",\n'
    '  "findings": [{"severity": "error|warning|info", "message": "..."}],\n'
    '  "residual_risk": "remaining risk or null"\n'
    "}\n"
    "```\n"
    "verdict=fail/blocked ONLY for a real, blocking correctness/security/governance issue "
    "introduced by THIS diff. Style nits are severity=info, never blocking.\n"
)


def _build_prompt(diff_text: str, pr: str) -> str:
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS] + "\n\n[... diff truncated for the gate ...]"
    return (
        f"You are a strict code-review gate for PR {pr}. Review ONLY the unified diff "
        "below. Look for correctness bugs, security issues, governance/contract "
        "violations, and regressions introduced by THIS diff. Be a skeptic; do not "
        "rubber-stamp, but do not invent issues.\n\n"
        f"{_VERDICT_CONTRACT}\n"
        "DIFF:\n"
        f"{diff_text}\n"
    )


def _get_diff(pr: str, diff_file: "str | None") -> "str | None":
    if diff_file:
        p = Path(diff_file)
        return p.read_text(encoding="utf-8") if p.is_file() else None
    try:
        res = subprocess.run(
            ["gh", "pr", "diff", pr],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
        )
        return res.stdout if res.returncode == 0 and res.stdout.strip() else None
    except (subprocess.SubprocessError, OSError):
        return None


def _frontmatter_run_outcome(report_text: str) -> "tuple[bool | None, object, object]":
    """Read ``exit_code``/``token_usage.output`` off the report's own YAML
    frontmatter to tell a provider-side run failure apart from a genuine
    parse miss (OI-1291 fix-forward, meetgat correction).

    Content existing is not proof kimi actually completed a review: the
    failure-path ``emit_unified_report`` call writes a report too, so a
    spooled 403 body is "content" by the same measure as a real review. The
    frontmatter's ``exit_code`` field is stamped by the lane from the actual
    spawn result, not guessed from the response text — that is the signal
    that can't be faked by error-page prose.

    ``exit_code`` alone decides the outcome. ``token_usage.output`` is only
    consulted when the frontmatter's own ``token_usage_measured`` flag is
    true. On the kimi lane, post-run token capture is frequently unavailable
    (kimi-cli's stream-json output carries no usage accounting) — the lane
    stamps ``token_usage_measured: false`` and ``output: 0`` for those runs.
    A measured count of 766 kimi reports on disk found 502 with exit_code 0,
    non-trivial body content, and ``token_usage_measured: false`` — real,
    successful reviews that a bare ``output_tokens == 0`` check would have
    booked as provider outages. Only 4 reports carried a genuinely measured
    token count. Treating an unmeasured zero as a failure signal reintroduces
    the exact bug OI-1291 exists to remove, just gated on a different field.

    Returns ``(outcome, exit_code, output_tokens)`` where ``outcome`` is:
        True  — frontmatter is readable and the run did not complete cleanly:
                non-zero exit code, or (only when ``token_usage_measured`` is
                true) a measured zero output-token count. A provider-side
                failure, not a review outcome.
        False — frontmatter is readable and stamps a clean run (exit_code 0,
                and either tokens aren't measured or a measured non-zero
                count): a missing verdict block here is a genuine parse miss.
        None  — no readable frontmatter, or no ``exit_code`` field in it
                (older/foreign report shape). The outcome can't be read off
                the report at all; callers fall back to the same default as
                False (content present, no signal either way -> attempt
                recovery rather than assume a provider failure).
    """
    try:
        frontmatter = parse_frontmatter(report_text)
    except SchemaViolation:
        return None, None, None
    if "exit_code" not in frontmatter:
        return None, None, None
    exit_code = frontmatter.get("exit_code")
    token_usage = frontmatter.get("token_usage")
    output_tokens = token_usage.get("output") if isinstance(token_usage, dict) else None
    tokens_measured = bool(frontmatter.get("token_usage_measured"))
    if exit_code != 0:
        return True, exit_code, output_tokens
    if tokens_measured and output_tokens == 0:
        return True, exit_code, output_tokens
    return False, exit_code, output_tokens


def _parse_reprocess_window_start(dispatch_id: str, *, pr: str) -> "float | None":
    """Extract the original dispatch's start time (unix seconds) from its own
    dispatch_id, which this gate always constructs as
    ``f"kimi-gate-pr{pr}-{int(time.time())}"``. Returns None when *dispatch_id*
    does not match that exact shape for *pr* — refuse rather than guess a
    recovery time window."""
    prefix = f"kimi-gate-pr{pr}-"
    if not dispatch_id.startswith(prefix):
        return None
    ts_part = dispatch_id[len(prefix):]
    if not ts_part.isdigit():
        return None
    return float(ts_part)


def _frontmatter_duration_seconds(report_text: str) -> float:
    """Best-effort ``duration_seconds`` read off a report's own frontmatter —
    used only by ``--reprocess``, where no live dispatcher call happens to
    time directly."""
    try:
        frontmatter = parse_frontmatter(report_text)
    except SchemaViolation:
        return 0.0
    try:
        return float(frontmatter.get("duration_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _reprocess_original_commit_sha(base_data_dir: Path, pr: str, dispatch_id: str) -> "str | None":
    """Return the commit_sha this EXACT dispatch's own result record was
    stamped with, or None when no addressable identity exists for it.

    Results are keyed by PR number, not dispatch_id
    (``pr-<N>-kimi_gate.json``) — a LATER dispatch for the same PR overwrites
    an EARLIER one's slot. When the stored record's own ``dispatch_id`` no
    longer matches the dispatch being reprocessed, there is no reliable
    historical commit_sha left to verify staleness against, and
    ``--reprocess`` must refuse rather than guess one."""
    path = base_data_dir / "state" / "review_gates" / "results" / f"pr-{pr}-kimi_gate.json"
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("dispatch_id") != dispatch_id:
        return None
    sha = record.get("commit_sha") or ""
    return sha or None


def _verdict_to_status(
    verdict: dict, report_text: str = "", *, provider_failed_detail: "str | None" = None
) -> "tuple[str, list, str]":
    """Map the extracted verdict to (status, blocking_findings, residual_risk).

    OI-1142: an empty ``verdict`` means no readable verdict could be extracted.
    OI-1291 splits that further: if ``report_text`` itself is empty/whitespace,
    the provider delivered nothing at all (a real outage/empty-completion
    signal). If ``report_text`` has content but no readable ```json``` block,
    the provider DID respond — the block just failed to parse, which is a
    parse miss, not an outage. Either way this maps to ``unavailable`` and
    only a REAL parsed verdict may ever produce ``fail``.

    ``provider_failed_detail`` (OI-1291 fix-forward): the caller passes this
    when it has already read the report's own frontmatter via
    ``_frontmatter_run_outcome`` and found a non-zero exit code (or a
    measured-zero output-token count, when the frontmatter's own
    ``token_usage_measured`` flag says the count is real). When set, THIS
    function returns that provider-outage residual
    instead of the "kimi did respond" parse-miss residual below — the report
    having content is not proof kimi ran; the frontmatter's own exit_code is.
    """
    v = (verdict.get("verdict") or "").strip().lower() if verdict else ""
    findings = verdict.get("findings") or [] if verdict else []
    blocking = [
        f for f in findings
        if isinstance(f, dict) and str(f.get("severity", "")).lower() in {"error", "blocked", "blocker"}
    ]
    residual = (verdict.get("residual_risk") if verdict else "") or ""
    if v == "pass" and not blocking:
        return "pass", [], residual
    if v in {"fail", "blocked"} or blocking:
        return "fail", blocking, residual or "kimi gate reported blocking findings"
    if residual:
        return "unavailable", [], residual
    if provider_failed_detail:
        return "unavailable", [], provider_failed_detail
    if (report_text or "").strip():
        return (
            "unavailable",
            [],
            f"kimi returned a {len(report_text)}-char report, but it contained no "
            "readable ```json``` verdict block (parse miss — kimi did respond)",
        )
    return (
        "unavailable",
        [],
        "kimi produced no readable verdict (provider outage/quota/truncation) — not a review outcome",
    )


# dispatch-20260823-beta2-j: reason-specific unavailable summaries. "parse_error"
# (a single bucket for "no readable verdict on a clean run") is retired — every
# case that used to land there now either recovers (reason="relabeled_verdict"
# / "recovered_verdict") or gets its OWN distinguishing reason, so a reader can
# tell "searched, found nothing" apart from "searched, found too much" apart
# from "never searched at all" (T0 correction: these were silently the same
# reason before, which erased exactly the information the search exists to add).
_UNAVAILABLE_SUMMARIES = {
    "recovery_empty": "recovery_empty — searched for a companion report, found none",
    "recovery_ambiguous": "recovery_ambiguous — multiple companion reports found, refusing to guess",
    "recovery_conflict": "recovery_conflict — a companion report's verdict conflicted with the primary response",
    "reprocess_no_identity_anchor": "reprocess_no_identity_anchor — no addressable original result record to verify against",
    "reprocess_stale_evidence": "reprocess_stale_evidence — PR head has moved since this dispatch ran",
}


def _status_summary(status: str, blocking: list, reason: str = "") -> str:
    """Summary line for the result record — outage vs recovery vs verdict must be unmistakable."""
    if status == "unavailable":
        detail = _UNAVAILABLE_SUMMARIES.get(reason)
        if detail:
            return f"kimi gate: UNAVAILABLE ({detail} — NOT a review fail)"
        return "kimi gate: UNAVAILABLE (provider outage/no verdict — NOT a review fail)"
    return f"kimi gate: {status} ({len(blocking)} blocking finding(s))"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Temporary GOVERNED kimi review gate.")
    ap.add_argument("--pr", required=True, help="PR number (use 0 with --diff-file for offline test)")
    ap.add_argument("--diff-file", default=None, help="read the diff from a file instead of gh")
    ap.add_argument("--data-dir", default=os.environ.get("VNX_DATA_DIR", ""),
                    help="VNX data dir; report lands in <data-dir>/unified_reports/ and the "
                         "result in <data-dir>/state/review_gates/results/")
    ap.add_argument("--model", default=os.environ.get("VNX_KIMI_GATE_MODEL", DEFAULT_MODEL))
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
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

    if args.reprocess:
        # Deel 3 (dispatch-20260823-beta2-j): re-run the SAME parse/recover/
        # record pipeline below against a dispatch that already ran, without a
        # new model call. `report_text = dispatcher(...)` is skipped and
        # replaced by a read of the report the ORIGINAL run already wrote to
        # disk — everything after (verdict extraction, recovery search,
        # record) is unchanged code, so a recovered verdict here was produced
        # by the gate itself, on the run's own commit, in the run's own
        # contract shape — not prose-to-verdict promotion of a new source.
        dispatch_id = args.reprocess
        report_path_obj = base_data_dir / "unified_reports" / f"{dispatch_id}.md"
        if not report_path_obj.is_file():
            print(
                f"kimi_gate: --reprocess: no report on disk for dispatch_id={dispatch_id!r} "
                f"at {report_path_obj}", file=sys.stderr,
            )
            return 1
        report_text = report_path_obj.read_text(encoding="utf-8")
        dispatch_error = ""
        window_start = _parse_reprocess_window_start(dispatch_id, pr=args.pr)
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
        duration = _frontmatter_duration_seconds(report_text)
    else:
        diff = _get_diff(args.pr, args.diff_file)
        if not diff:
            print(f"kimi_gate: no diff for PR {args.pr}", file=sys.stderr)
            return 1

        dispatch_id = f"kimi-gate-pr{args.pr}-{int(time.time())}"
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
        dispatcher = _make_default_dispatcher(str(base_data_dir), args.timeout, role="review-gate")
        prompt = _build_prompt(diff, args.pr)

        wall_start = time.time()
        start = time.monotonic()
        report_text = ""
        dispatch_error = ""
        try:
            # Governed lane: provider_dispatch runs kimi, writes a unified report, returns its text.
            report_text = dispatcher("kimi", args.model, prompt, dispatch_id)
        except Exception as exc:  # noqa: BLE001 — dispatch/report-read failure
            # OI-1142: do NOT bail without a record — a required gate that cannot fire
            # must surface as ``unavailable`` in the results dir, not vanish.
            dispatch_error = str(exc)
            print(f"kimi_gate: governed kimi dispatch failed: {exc}", file=sys.stderr)
        duration = time.monotonic() - start

        report_path_obj = base_data_dir / "unified_reports" / f"{dispatch_id}.md"
        window_start = wall_start
        window_end = report_path_obj.stat().st_mtime if report_path_obj.is_file() else time.time()

    recovered_path: "Path | None" = None

    if dispatch_error:
        verdict: dict = {}
        status, blocking = "unavailable", []
        residual = f"governed kimi dispatch failed: {dispatch_error[:400]}"
        reason = "dispatch_error"
    else:
        verdict = _extract_verdict(report_text or "")
        if verdict:
            reason = "verdict"
            status, blocking, residual = _verdict_to_status(verdict, report_text or "")
        elif not (report_text or "").strip():
            reason = "no_verdict"
            status, blocking, residual = _verdict_to_status({}, report_text or "")
        else:
            # dispatch-20260823-beta2-j: cheap check BEFORE the frontmatter/
            # search path — the verdict may be sitting right here, inline,
            # under the plan-reviewer role's fence label instead of ours. No
            # search needed.
            relabeled = extract_relabeled_verdict(report_text or "")
            if relabeled:
                reason = "relabeled_verdict"
                verdict = relabeled
                status, blocking, residual = _verdict_to_status(relabeled, report_text or "")
            else:
                # OI-1291 fix-forward: only consult the frontmatter once BOTH
                # verdict extractions have failed — a real parsed verdict
                # always wins regardless of what exit_code says.
                run_failed, exit_code, output_tokens = _frontmatter_run_outcome(report_text or "")
                if run_failed is True:
                    reason = "dispatch_error"
                    provider_failed_detail = (
                        "kimi's own report frontmatter stamps this run as failed "
                        f"(exit_code={exit_code!r}, token_usage.output={output_tokens!r}) — "
                        "provider-side outage, not a review outcome"
                    )
                    status, blocking, residual = _verdict_to_status(
                        {}, report_text or "", provider_failed_detail=provider_failed_detail
                    )
                else:
                    # A genuine parse miss on a clean (or unreadable-frontmatter)
                    # run: search for a companion report before giving up (Deel 2).
                    try:
                        candidate = find_recovery_candidate(
                            base_data_dir / "unified_reports",
                            pr_id=str(args.pr),
                            exclude_name=f"{dispatch_id}.md",
                            window_start=window_start,
                            window_end=window_end,
                        )
                    except AmbiguousRecoveryCandidates as exc:
                        reason = "recovery_ambiguous"
                        status, blocking = "unavailable", []
                        residual = (
                            f"kimi returned a {len(report_text)}-char report with no readable "
                            f"verdict block; {len(exc.candidates)} companion reports were "
                            f"found for PR {args.pr} in the run's time window — refusing to "
                            f"guess which is real: {', '.join(str(p) for p in exc.candidates)}"
                        )
                    else:
                        if candidate is None:
                            reason = "recovery_empty"
                            status, blocking = "unavailable", []
                            residual = (
                                f"kimi returned a {len(report_text)}-char report with no "
                                "readable ```json``` verdict block; searched "
                                f"unified_reports/ for a companion report for PR {args.pr} "
                                "in the run's time window and found none (parse miss — "
                                "no recoverable evidence)"
                            )
                        else:
                            conflict = recovered_verdict_conflicts(report_text or "", candidate.verdict)
                            if conflict:
                                reason = "recovery_conflict"
                                status, blocking = "unavailable", []
                                residual = (
                                    f"a companion report ({candidate.path}) carried a "
                                    f"parseable verdict, but it conflicts with the primary "
                                    f"response: {conflict} — abstaining rather than "
                                    "overriding the primary run with a second source"
                                )
                            else:
                                reason = "recovered_verdict"
                                verdict = candidate.verdict
                                status, blocking, recovered_residual = _verdict_to_status(candidate.verdict)
                                residual = (
                                    f"{recovered_residual + ' — ' if recovered_residual else ''}"
                                    f"verdict recovered from companion report {candidate.path} "
                                    "(same dispatch run, found via bounded search; the "
                                    "harness-captured primary report lacked the fence)"
                                )
                                recovered_path = candidate.path

    # dispatch-20260823-beta2-j, Deel 3 precondition #2: a reprocessed verdict
    # may only be formalized against the commit_sha the PR carried WHEN THIS
    # DISPATCH RAN. Results are keyed by PR number, not dispatch_id, so this
    # is only checkable when the stored pr-<N>-kimi_gate.json record still
    # belongs to THIS exact dispatch_id; a stale or superseded slot refuses
    # rather than guesses. Only runs once a terminal verdict is about to be
    # formalized — an already-unavailable status needs no staleness check.
    if args.reprocess and status in {"pass", "fail"}:
        original_sha = _reprocess_original_commit_sha(base_data_dir, args.pr, dispatch_id)
        if original_sha is None:
            status, blocking = "unavailable", []
            reason = "reprocess_no_identity_anchor"
            residual = (
                f"--reprocess found no addressable original result record for "
                f"dispatch_id={dispatch_id!r} (pr-{args.pr}-kimi_gate.json is missing, or "
                "now reflects a LATER dispatch for this PR — results are keyed by PR "
                "number, not dispatch_id) — cannot verify the commit_sha this run "
                "evaluated against, refusing rather than guessing"
            )
        else:
            current_sha = get_pr_head_sha(int(args.pr)) if str(args.pr).isdigit() else ""
            if not current_sha or current_sha != original_sha:
                status, blocking = "unavailable", []
                reason = "reprocess_stale_evidence"
                residual = (
                    f"--reprocess: PR {args.pr} head has moved since this dispatch ran "
                    f"(evaluated against commit_sha={original_sha!r}, current head is "
                    f"{current_sha!r}) — the recovered evidence is stale, refusing to "
                    "formalize it"
                )

    # OI-1178 (DLv2): a terminal verdict must carry the same evidence pair
    # codex_gate/gemini_review stamp — contract_hash (gate_artifacts' single
    # canonical hasher) + report_path. An unavailable result (no readable
    # verdict, or the dispatch itself failed) is deliberately left with
    # neither field: OI-1142's outage/verdict separation must not be bypassed
    # by a record that merely LOOKS complete.
    if status in {"pass", "fail"}:
        report_path = str(recovered_path) if recovered_path is not None else str(report_path_obj)
        if prompt is not None:
            contract_hash = _compute_contract_hash({"prompt": prompt}, "kimi_gate")
        else:
            # --reprocess never re-fetches the diff (Deel 3), so there is no
            # prompt to hash — fall back to gate_artifacts' own branch-based
            # fallback, seeded with the PR's real branch so the hash at least
            # varies per PR instead of being a gate-wide constant.
            contract_hash = _compute_contract_hash(
                {"branch": get_pr_head_branch(int(args.pr)) if str(args.pr).isdigit() else ""},
                "kimi_gate",
            )
    else:
        contract_hash = ""
        report_path = ""

    record = {
        "gate": "kimi_gate",
        "pr_id": str(args.pr),
        "pr_number": int(args.pr) if str(args.pr).isdigit() else None,
        # Offline runs read the diff from a file (--diff-file) and are NOT tied
        # to a live GitHub PR; they must never count as gate evidence. The
        # closure verifier refuses records with test_run: true.
        "test_run": bool(args.diff_file),
        "status": status,
        "reason": reason,
        "duration_seconds": round(duration, 3),
        "summary": _status_summary(status, blocking, reason),
        "contract_hash": contract_hash,
        "report_path": report_path,
        "provider": "kimi",
        "model": args.model,
        "dispatch_id": dispatch_id,
        "blocking_findings": blocking,
        "advisory_findings": [
            f for f in (verdict.get("findings") or []) if f not in blocking
        ],
        "required_reruns": [],
        "residual_risk": residual,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # dispatch-20260823-beta2-j: audit marker distinguishing a record
        # produced by a live model call from one formalized by --reprocess
        # against evidence that already existed on disk from an earlier run.
        "evidence_source": "reprocessed" if args.reprocess else "live",
    }

    # A4: stamp branch + commit_sha through the shared writer so the merge
    # door can join a kimi_gate result on the PR head (GitHub), not the local
    # checkout HEAD. Offline runs (--diff-file) are not tied to a live PR and
    # legitimately carry neither.
    stamp_request_identity(
        record,
        {
            "gate": record["gate"],
            "pr_id": record["pr_id"],
            "branch": "" if record["test_run"] else get_pr_head_branch(record["pr_number"]),
            "commit_sha": "" if record["test_run"] else get_pr_head_sha(record["pr_number"]),
        },
    )

    if data_dir:
        results_dir = Path(data_dir) / "state" / "review_gates" / "results"
        out = results_dir / f"pr-{args.pr}-kimi_gate.json"
        record_terminal_result(
            gate="kimi_gate", pr_id=record["pr_id"], result_path=out, payload=record,
        )
        print(f"kimi_gate: wrote {out}", file=sys.stderr)

    if args.json:
        print(json.dumps(record, indent=2))
    elif status == "unavailable":
        print(f"VERDICT: UNAVAILABLE  ({reason})")
        print(f"  {residual}")
    else:
        print(f"VERDICT: {status.upper()}  ({len(blocking)} blocking)")
        for f in blocking:
            print(f"  · [{f.get('severity')}] {f.get('message')}")

    # 0 = pass, 2 = a REAL parsed fail/blocked verdict, 1 = unavailable/infra.
    if status == "pass":
        return 0
    return 2 if status == "fail" else 1


if __name__ == "__main__":
    raise SystemExit(main())
