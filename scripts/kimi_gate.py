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

OI-1291: ``unavailable`` is not a single cause. ``reason`` distinguishes three
disjoint states: ``dispatch_error`` (the dispatcher itself raised — no report
was ever produced), ``parse_error`` (a report came back with content but no
readable ```json``` verdict block — kimi DID respond, the block just didn't
parse), and ``no_verdict`` (the report was empty/whitespace — a genuinely
empty delivery). Only ``dispatch_error``/``no_verdict`` may claim a provider
outage in their summary text; ``parse_error`` must not, since the provider
plainly produced output.
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

from plan_gate_panel import _make_default_dispatcher  # governed provider_dispatch lane
from gate_recorder import (
    get_pr_head_branch,
    get_pr_head_sha,
    record_terminal_result,
    stamp_request_identity,
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


def _verdict_to_status(verdict: dict, report_text: str = "") -> "tuple[str, list, str]":
    """Map the extracted verdict to (status, blocking_findings, residual_risk).

    OI-1142: an empty ``verdict`` means no readable verdict could be extracted.
    OI-1291 splits that further: if ``report_text`` itself is empty/whitespace,
    the provider delivered nothing at all (a real outage/empty-completion
    signal). If ``report_text`` has content but no readable ```json``` block,
    the provider DID respond — the block just failed to parse, which is a
    parse miss, not an outage. Either way this maps to ``unavailable`` and
    only a REAL parsed verdict may ever produce ``fail``.
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


def _status_summary(status: str, blocking: list, reason: str = "", report_len: int = 0) -> str:
    """Summary line for the result record — outage vs parse-miss vs verdict must be unmistakable."""
    if status == "unavailable":
        if reason == "parse_error":
            return (
                f"kimi gate: UNAVAILABLE (parse_error — kimi returned a {report_len}-char "
                "report, but it contained no readable verdict block — NOT a review fail)"
            )
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
    args = ap.parse_args(argv)

    diff = _get_diff(args.pr, args.diff_file)
    if not diff:
        print(f"kimi_gate: no diff for PR {args.pr}", file=sys.stderr)
        return 1

    data_dir = args.data_dir or None
    dispatch_id = f"kimi-gate-pr{args.pr}-{int(time.time())}"
    dispatcher = _make_default_dispatcher(data_dir, args.timeout)

    start = time.monotonic()
    report_text = ""
    dispatch_error = ""
    try:
        # Governed lane: provider_dispatch runs kimi, writes a unified report, returns its text.
        report_text = dispatcher("kimi", args.model, _build_prompt(diff, args.pr), dispatch_id)
    except Exception as exc:  # noqa: BLE001 — dispatch/report-read failure
        # OI-1142: do NOT bail without a record — a required gate that cannot fire
        # must surface as ``unavailable`` in the results dir, not vanish.
        dispatch_error = str(exc)
        print(f"kimi_gate: governed kimi dispatch failed: {exc}", file=sys.stderr)
    duration = time.monotonic() - start

    if dispatch_error:
        verdict: dict = {}
        status, blocking = "unavailable", []
        residual = f"governed kimi dispatch failed: {dispatch_error[:400]}"
        reason = "dispatch_error"
    else:
        verdict = _extract_verdict(report_text or "")
        status, blocking, residual = _verdict_to_status(verdict, report_text or "")
        if verdict:
            reason = "verdict"
        elif (report_text or "").strip():
            # OI-1291: a report came back with content, it just didn't parse —
            # distinct from a genuinely empty delivery (see reason="no_verdict"
            # below) and from a dispatch_error (no report at all, above).
            reason = "parse_error"
        else:
            reason = "no_verdict"

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
        "summary": _status_summary(status, blocking, reason, len(report_text or "")),
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
        if reason == "parse_error":
            print(
                f"VERDICT: UNAVAILABLE  (parse_error — kimi returned a {len(report_text or '')}-char "
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
