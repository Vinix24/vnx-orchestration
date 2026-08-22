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
``fail``. ``reason`` distinguishes three disjoint states, identically to
kimi_gate: ``dispatch_error`` (the dispatcher raised outright with no report,
or a report came back but its own frontmatter stamps the underlying provider
run as failed), ``parse_error`` (a report came back from a run the
frontmatter itself stamps as clean, with content but no readable ```json```
verdict block), and ``no_verdict`` (the report was empty/whitespace). Only
``dispatch_error``/``no_verdict`` may claim a provider outage in their
summary text; ``parse_error`` must not, since the provider plainly produced
output. See ``kimi_gate._frontmatter_run_outcome`` (reused verbatim below —
same governed-report frontmatter shape, same ``exit_code``/
``token_usage_measured`` discrimination, provider-agnostic) for the full
reasoning.

OI-1178 / OI-1435: a terminal (pass/fail) record must carry ``contract_hash``
+ ``report_path`` or ``gate_status.has_complete_evidence`` — and therefore
``closure_verifier``'s merge check — refuses it as evidence, forever. Measured
on kimi_gate before its own fix: 9 of 9 terminal records lacked both fields,
and every one of those verdicts was permanently unable to close a PR.
``contract_hash`` is computed by ``gate_artifacts._compute_contract_hash``
(the SAME function codex_gate's and kimi_gate's own execution paths call,
never a second hasher). ``report_path`` points at the governed dispatch's own
unified report, already on disk by the time the dispatcher call returns.

An ``unavailable`` result carries NEITHER field, and this is more subtle than
it looks (OI-1435): ``has_complete_evidence`` only checks whether the two
fields are non-empty — it does not check whether a verdict exists at all. An
``unavailable`` result (``is_terminal=False``) that carried non-empty
``contract_hash``/``report_path`` would pass ``has_complete_evidence`` while
carrying no judgement whatsoever; the separation between "evidence exists"
and "a verdict exists" holds today only because callers check ``is_terminal``
first. Leaving both fields empty on every non-terminal status keeps that
separation intact regardless of check order in any future caller.

Model: GLM-5.2 only (``deprecated-glm-models``, provider_constraints.yaml,
operator directive 2026-08-03). GLM-4.5, GLM-4.6, base GLM-5, and GLM-5.1 are
blocked. This gate validates the model at its own entry point — before ever
reaching the dispatcher — so a blocked version is refused LOUDLY with an
explicit reason, not silently passed through to fail deep inside the
provider-dispatch constraint enforcer and surface indistinguishable from any
other "unavailable" outage.
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

_VALID_VERDICTS = {"pass", "fail", "blocked"}


def _extract_verdict(text: str) -> dict:
    """Extract the worker's verdict: the LAST ```json``` block whose ``verdict`` is a
    real verdict (pass/fail/blocked).

    Identical logic to ``kimi_gate._extract_verdict`` — kept as a single copy
    per gate module (not imported cross-module) so each gate's parser stays
    self-contained, but the RULE must never diverge: the same review read
    through kimi_gate and glm_gate must resolve to the same verdict shape.
    The governed worker report echoes the instruction, so the FIRST ```json```
    block is our own contract example (its ``verdict`` is the literal
    "pass|fail|blocked", which is not a valid single verdict and is skipped).
    Scanning from the end and requiring a real verdict value selects the
    worker's actual answer, never the echoed template.
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


DEFAULT_MODEL = "glm-5.2"
DEFAULT_TIMEOUT = 900
MAX_DIFF_CHARS = 50000

# deprecated-glm-models (provider_constraints.yaml): glm-5.2 is the ONLY
# admitted GLM version. This is an allowlist, not a blocklist — every other
# name (including a not-yet-released version) is refused until an operator
# decision admits it explicitly.
ALLOWED_MODELS = frozenset({"glm-5.2"})

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


def _validate_model(model: str) -> "str | None":
    """Return the canonical (lowercase) model string, or None to refuse.

    Operator directive 2026-08-03: only glm-5.2 is admitted. Matching is
    case-insensitive (accepts "GLM-5.2") but the canonical lowercase form is
    what actually gets dispatched, so the record's ``model`` field always
    matches the wave7_models.yaml registry key exactly.
    """
    normalized = (model or "").strip().lower()
    if normalized in ALLOWED_MODELS:
        return normalized
    return None


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
    parse miss (ported from ``kimi_gate._frontmatter_run_outcome`` — the
    governed unified-report frontmatter shape is provider-agnostic, so the
    same logic applies unchanged to the glm-harness lane).

    Content existing is not proof glm actually completed a review: the
    failure-path ``emit_unified_report`` call writes a report too, so a
    spooled error body is "content" by the same measure as a real review.
    The frontmatter's ``exit_code`` field is stamped by the lane from the
    actual spawn result, not guessed from the response text — that is the
    signal that can't be faked by error-page prose.

    ``exit_code`` alone decides the outcome. ``token_usage.output`` is only
    consulted when the frontmatter's own ``token_usage_measured`` flag is
    true — the same conditional weighting kimi_gate uses, so a lane where
    post-run token capture is unavailable does not get its unmeasured zero
    read as a failure signal.

    Returns ``(outcome, exit_code, output_tokens)`` where ``outcome`` is:
        True  — frontmatter is readable and the run did not complete cleanly:
                non-zero exit code, or (only when ``token_usage_measured`` is
                true) a measured zero output-token count. A provider-side
                failure, not a review outcome.
        False — frontmatter is readable and stamps a clean run (exit_code 0,
                and either tokens aren't measured or a measured non-zero
                count): a missing verdict block here is a genuine parse miss.
        None  — no readable frontmatter, or no ``exit_code`` field in it
                (older/foreign report shape). Callers fall back to the
                default (content present -> parse_error).
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


def _verdict_to_status(
    verdict: dict, report_text: str = "", *, provider_failed_detail: "str | None" = None
) -> "tuple[str, list, str]":
    """Map the extracted verdict to (status, blocking_findings, residual_risk).

    OI-1142: an empty ``verdict`` means no readable verdict could be extracted.
    If ``report_text`` itself is empty/whitespace, the provider delivered
    nothing at all (a real outage/empty-completion signal). If ``report_text``
    has content but no readable ```json``` block, the provider DID respond —
    the block just failed to parse, which is a parse miss, not an outage.
    Either way this maps to ``unavailable`` and only a REAL parsed verdict may
    ever produce ``fail``.

    ``provider_failed_detail``: the caller passes this when it has already
    read the report's own frontmatter via ``_frontmatter_run_outcome`` and
    found a non-zero exit code (or a measured-zero output-token count). When
    set, THIS function returns that provider-outage residual instead of the
    "glm did respond" parse-miss residual below — the report having content
    is not proof glm ran; the frontmatter's own exit_code is.
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
        return "fail", blocking, residual or "glm gate reported blocking findings"
    if residual:
        return "unavailable", [], residual
    if provider_failed_detail:
        return "unavailable", [], provider_failed_detail
    if (report_text or "").strip():
        return (
            "unavailable",
            [],
            f"glm returned a {len(report_text)}-char report, but it contained no "
            "readable ```json``` verdict block (parse miss — glm did respond)",
        )
    return (
        "unavailable",
        [],
        "glm produced no readable verdict (provider outage/quota/truncation) — not a review outcome",
    )


def _status_summary(status: str, blocking: list, reason: str = "", report_len: int = 0) -> str:
    """Summary line for the result record — outage vs parse-miss vs verdict must be unmistakable."""
    if status == "unavailable":
        if reason == "parse_error":
            return (
                f"glm gate: UNAVAILABLE (parse_error — glm returned a {report_len}-char "
                "report, but it contained no readable verdict block — NOT a review fail)"
            )
        return "glm gate: UNAVAILABLE (provider outage/no verdict — NOT a review fail)"
    return f"glm gate: {status} ({len(blocking)} blocking finding(s))"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="GOVERNED glm-harness review gate.")
    ap.add_argument("--pr", required=True, help="PR number (use 0 with --diff-file for offline test)")
    ap.add_argument("--diff-file", default=None, help="read the diff from a file instead of gh")
    ap.add_argument("--data-dir", default=os.environ.get("VNX_DATA_DIR", ""),
                    help="VNX data dir; report lands in <data-dir>/unified_reports/ and the "
                         "result in <data-dir>/state/review_gates/results/")
    ap.add_argument("--model", default=os.environ.get("VNX_GLM_GATE_MODEL", DEFAULT_MODEL))
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--json", action="store_true", help="print the result record as JSON")
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

    diff = _get_diff(args.pr, args.diff_file)
    if not diff:
        print(f"glm_gate: no diff for PR {args.pr}", file=sys.stderr)
        return 1

    data_dir = args.data_dir or None
    dispatch_id = f"glm-gate-pr{args.pr}-{int(time.time())}"
    # Resolved once, then handed to the dispatcher as an explicit data_dir so
    # this function's own report_path reconstruction below and the governed
    # subprocess's actual write path can never drift apart — both derive from
    # the SAME resolved base (see provider_dispatch.py's report_path, which is
    # this exact <data_dir>/unified_reports/<dispatch_id>.md).
    base_data_dir = _resolve_data_dir(data_dir)
    dispatcher = _make_default_dispatcher(str(base_data_dir), args.timeout)
    prompt = _build_prompt(diff, args.pr)

    start = time.monotonic()
    report_text = ""
    dispatch_error = ""
    try:
        # Governed lane: provider_dispatch runs glm (via the claude-CLI harness
        # -> local :4141 litellm proxy -> OpenRouter), writes a unified
        # report, returns its text. "glm-harness" is the real provider string
        # provider_dispatch.py dispatches on (see plan_gate_panel.py's own
        # diverse-assurance panel entry: provider="glm-harness").
        report_text = dispatcher("glm-harness", args.model, prompt, dispatch_id)
    except Exception as exc:  # noqa: BLE001 — dispatch/report-read failure
        # OI-1142: do NOT bail without a record — a required gate that cannot fire
        # must surface as ``unavailable`` in the results dir, not vanish.
        dispatch_error = str(exc)
        print(f"glm_gate: governed glm dispatch failed: {exc}", file=sys.stderr)
    duration = time.monotonic() - start

    if dispatch_error:
        verdict: dict = {}
        status, blocking = "unavailable", []
        residual = f"governed glm dispatch failed: {dispatch_error[:400]}"
        reason = "dispatch_error"
    else:
        verdict = _extract_verdict(report_text or "")
        provider_failed_detail = None
        run_failed = None
        if not verdict and (report_text or "").strip():
            # Only consult the frontmatter once a verdict extraction has
            # already failed — a real parsed verdict always wins regardless
            # of what exit_code says.
            run_failed, exit_code, output_tokens = _frontmatter_run_outcome(report_text or "")
            if run_failed is True:
                provider_failed_detail = (
                    "glm's own report frontmatter stamps this run as failed "
                    f"(exit_code={exit_code!r}, token_usage.output={output_tokens!r}) — "
                    "provider-side outage, not a review outcome"
                )
        status, blocking, residual = _verdict_to_status(
            verdict, report_text or "", provider_failed_detail=provider_failed_detail
        )
        if verdict:
            reason = "verdict"
        elif (report_text or "").strip():
            if run_failed is True:
                # The report has content, but its OWN frontmatter stamps the
                # underlying provider run as failed — this is the same outage
                # class as a raised dispatch_error, just discovered from the
                # report instead of an exception.
                reason = "dispatch_error"
            else:
                # run_failed is False (frontmatter readable, clean run — a
                # genuine parse miss) or None (no readable exit_code at all,
                # so there is no signal left to tell a masked provider
                # failure apart from a real parse miss). Both default to
                # parse_error; the None case is intentionally too broad.
                reason = "parse_error"
        else:
            reason = "no_verdict"

    # OI-1178 / OI-1435: a terminal verdict must carry the same evidence pair
    # codex_gate/kimi_gate stamp — contract_hash (gate_artifacts' single
    # canonical hasher) + report_path (the governed dispatch's own unified
    # report, already on disk by the time dispatcher() returned above). Only
    # ``verdict`` ever reaches pass/fail (see _verdict_to_status), so
    # report_text is guaranteed non-empty here. An unavailable result (no
    # readable verdict, or the dispatch itself failed) is deliberately left
    # with neither field: has_complete_evidence only checks non-emptiness, not
    # whether a verdict exists, so a filled-in unavailable record would pass
    # the evidence check while carrying no judgement (OI-1435) — leaving both
    # empty here is what keeps OI-1142's outage/verdict separation intact.
    if status in {"pass", "fail"}:
        contract_hash = _compute_contract_hash({"prompt": prompt}, "glm_gate")
        report_path = str(base_data_dir / "unified_reports" / f"{dispatch_id}.md")
    else:
        contract_hash = ""
        report_path = ""

    record = {
        "gate": "glm_gate",
        "pr_id": str(args.pr),
        "pr_number": int(args.pr) if str(args.pr).isdigit() else None,
        # Offline runs read the diff from a file (--diff-file) and are NOT tied
        # to a live GitHub PR; they must never count as gate evidence. The
        # closure verifier refuses records with test_run: true.
        "test_run": bool(args.diff_file),
        "status": status,
        "reason": reason,
        "duration_seconds": round(duration, 3),
        "summary": _status_summary(status, blocking, reason, len(report_text or "")),
        "contract_hash": contract_hash,
        "report_path": report_path,
        "provider": "glm-harness",
        "model": args.model,
        "dispatch_id": dispatch_id,
        "blocking_findings": blocking,
        "advisory_findings": [
            f for f in (verdict.get("findings") or []) if f not in blocking
        ],
        "required_reruns": [],
        "residual_risk": residual,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Stamp branch + commit_sha through the shared writer so the merge door
    # can join a glm_gate result on the PR head (GitHub), not the local
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
        out = results_dir / f"pr-{args.pr}-glm_gate.json"
        record_terminal_result(
            gate="glm_gate", pr_id=record["pr_id"], result_path=out, payload=record,
        )
        print(f"glm_gate: wrote {out}", file=sys.stderr)

    if args.json:
        print(json.dumps(record, indent=2))
    elif status == "unavailable":
        if reason == "parse_error":
            print(
                f"VERDICT: UNAVAILABLE  (parse_error — glm returned a {len(report_text or '')}-char "
                "report, but it contained no readable verdict block — NOT a review fail)"
            )
        else:
            print("VERDICT: UNAVAILABLE  (provider outage/no verdict — NOT a review fail)")
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
