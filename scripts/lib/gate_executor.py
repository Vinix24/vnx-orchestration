"""Gate execution orchestration (GateExecutorMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle gate execution, contract-driven execution, and status queries.
Request creation methods are in gate_request_handler.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from gate_status import REQUEST_TERMINAL_STATUSES

logger = logging.getLogger(__name__)

# `gh pr checks --json` (gh 2.76.2, measured 2026-08-20) has no `status`/
# `conclusion` fields — it exposes `bucket`, which gh itself documents
# (`gh pr checks --help`) as categorizing the raw `state` into exactly one
# of: pass, fail, pending, skipping, cancel. "pending" is the only
# non-terminal bucket; a bucket outside this known set is treated as
# non-terminal too, so an unrecognized future value holds the verdict at
# "running" instead of silently completing.
_CI_CHECK_BUCKET_TERMINAL = {"pass", "fail", "skipping", "cancel"}
_CI_CHECK_BUCKET_SKIP = {"skipping", "cancel"}


def _format_ci_gate_report(
    *,
    pr_number: Optional[int],
    branch: str,
    checks: List[Dict[str, Any]],
    passed: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    status: str,
    generated_at: str,
) -> str:
    """Format ci_gate results as a normalized headless report."""
    verdict_icon = {"pass": "PASS", "fail": "FAIL", "running": "RUNNING"}.get(status, status.upper())
    lines = [
        "# ci_gate — Headless Gate Report",
        "",
        f"**PR**: {pr_number or 'unknown'}",
        f"**Branch**: {branch}",
        f"**Gate**: ci_gate",
        f"**Generated**: {generated_at}",
        f"**Verdict**: {verdict_icon}",
        "",
        "---",
        "",
        "## CI Check Results",
        "",
    ]
    if not checks:
        lines.append("_(no checks found — vacuously passing)_")
    else:
        if passed:
            lines.append("### Passed")
            for c in passed:
                lines.append(f"- [OK] {c.get('name', '?')} — {c.get('state', 'SUCCESS')}")
            lines.append("")
        if failed:
            lines.append("### Failed [BLOCKING]")
            for c in failed:
                lines.append(f"- [BLOCKING] {c.get('name', '?')} — {c.get('state', 'FAILURE')}")
                lines.append(f"  - **Severity**: blocking")
            lines.append("")
        if skipped:
            lines.append("### Skipped / Cancelled")
            for c in skipped:
                lines.append(f"- [ADVISORY] {c.get('name', '?')} — {c.get('state', 'SKIPPED')}")
            lines.append("")
    lines.append(f"**Status**: {'FAIL' if failed else 'PASS' if status == 'pass' else status.upper()}")
    lines.append(f"Passed: {len(passed)} | Failed: {len(failed)} | Skipped: {len(skipped)}")
    lines.append("")
    return "\n".join(lines) + "\n"


class GateExecutorMixin:
    """Mixin providing gate execution and status methods for ReviewGateManager."""

    def execute_gate(
        self,
        *,
        gate: str,
        pr_number: Optional[int] = None,
        pr_id: str = "",
    ) -> Dict[str, Any]:
        """Execute a gate: transition requested->executing->completed|failed (GATE-1).

        Loads the request record, starts the gate subprocess with bounded timeout
        and stall detection, then writes result records atomically (GATE-11/12).
        ci_gate is executed inline via gh CLI (no stall-detection subprocess loop).

        R10: every run, from every caller (``request_and_execute``, the
        ``execute`` CLI, the obligation runner), launches here, under the run
        lock of (project, PR, gate, head) (``gate_run_guard``), held until the
        run ends. A lock held elsewhere returns an ``in_flight`` payload: no
        run, nothing written, never a pass. Under the lock the round ledger is
        read again (:meth:`_verdict_landed_on_head`): a verdict another run
        booked on this head while this request waited is the answer, not a
        second run.
        """
        import gate_run_guard
        from gate_runner import GateRunner

        if pr_id:
            request_payload = self._load_contract_request_payload(gate, pr_id)
        elif pr_number is not None:
            request_payload = self._load_request_payload(gate, pr_number)
        else:
            raise ValueError("pr_number or pr_id is required")

        if not request_payload:
            raise ValueError(f"No request record found for gate={gate}")

        status = request_payload.get("status", "")
        if status in REQUEST_TERMINAL_STATUSES:
            # One shared list, not a local copy (GE-4c, kimi advisory (c)): a
            # request ended ``unavailable`` by commit_gate_outcome used to fall
            # through here and re-run a gate whose attempt was over.
            return request_payload

        head_sha = str(request_payload.get("commit_sha") or "")
        pr_ref = pr_number if pr_number is not None else pr_id
        with gate_run_guard.held_run(self.state_dir, pr_ref=pr_ref, gate=gate, head_sha=head_sha) as lock:
            if not lock.acquired:
                return gate_run_guard.in_flight_payload(lock, pr_number=pr_number, pr_id=pr_id)
            landed = self._verdict_landed_on_head(gate, request_payload, pr_number, head_sha)
            if landed is not None:
                return landed

            if gate == "ci_gate":
                return self._execute_ci_gate(
                    gate=gate, pr_number=pr_number, pr_id=pr_id,
                    request_payload=request_payload,
                )

            runner = GateRunner(
                state_dir=self.state_dir,
                reports_dir=self.reports_dir,
                project_root=Path(self.paths["PROJECT_ROOT"]),
            )
            return runner.run(
                gate=gate,
                request_payload=request_payload,
                pr_number=pr_number,
                pr_id=pr_id,
            )

    def _verdict_landed_on_head(
        self, gate: str, request_payload: Dict[str, Any], pr_number: Optional[int], head_sha: str,
    ) -> Optional[Dict[str, Any]]:
        """The seat's verdict on ``head_sha`` when one landed after the request, else ``None`` (R10).

        The request round refuses a seat that already has a verdict on the head
        (R1, ``_round_refusal``), but it decides before the run lock is taken: a
        concurrent run of the same seat can book its verdict in between. Read
        under the lock, that line is reported exactly as the request round
        reports it (``_standing_round_payload``), so the seat's answer is that
        verdict and no second reviewer runs. Only a seat with a known PR and
        head has a line to find; a ledger that cannot be read here finds none
        (the request round and the writer each refuse on it themselves).
        """
        import review_rounds

        seat = str(request_payload.get("seat") or "")
        if not seat or pr_number is None or not head_sha:
            return None
        review_gates_dir = self.results_dir.parent
        try:
            ledger = review_rounds.read_ledger(review_rounds.ledger_path(review_gates_dir))
        except review_rounds.RoundLedgerError as exc:
            # The run goes on: the writer refuses a verdict on an unreadable ledger itself.
            logger.warning("gate_executor: round ledger unreadable for pr=%s seat %s: %s", pr_number, seat, exc)
            return None
        project_id = review_rounds.store_project_id(review_gates_dir)
        line = next(
            (line for line in ledger.for_pr(project_id, pr_number) if line.seat == seat and line.head_sha == head_sha),
            None,
        )
        if line is None:
            return None
        return self._standing_round_payload(
            line, pr_number=pr_number, branch=str(request_payload.get("branch") or ""),
            seat_fields={"seat": seat},
        )

    def _execute_ci_gate(
        self,
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute ci_gate: call gh pr checks, parse results, write report and result JSON.

        Maps GitHub Actions check conclusions to blocking/advisory findings:
        - FAILURE → blocking_finding
        - SKIPPED / CANCELLED → advisory_finding
        - SUCCESS → passed_check
        Vacuously passes when there are no checks at all (Case D).
        """
        import gate_recorder as _rec
        from governance_receipts import utc_now_iso

        # Mark as executing
        request_payload["status"] = "executing"
        request_payload["started_at"] = utc_now_iso()
        request_payload["runner_pid"] = os.getpid()
        _rec.persist_request(
            self.requests_dir, gate, request_payload,
            pr_number=pr_number, pr_id=pr_id,
        )

        if not shutil.which("gh"):
            return _rec.record_not_executable(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="provider_not_installed",
                reason_detail="gh binary not found in PATH",
                request_payload=request_payload,
                requests_dir=self.requests_dir,
                results_dir=self.results_dir,
                state_dir=self.state_dir,
            )

        start = time.monotonic()
        try:
            proc = subprocess.run(
                ["gh", "pr", "checks", str(pr_number), "--json", "name,bucket,state"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except subprocess.TimeoutExpired:
            return _rec.record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="timeout",
                reason_detail="gh pr checks exceeded 60s timeout",
                request_payload=request_payload,
                requests_dir=self.requests_dir,
                results_dir=self.results_dir,
            )
        except OSError as exc:
            return _rec.record_failure_simple(
                gate=gate, pr_number=pr_number, pr_id=pr_id,
                reason="subprocess_error",
                reason_detail=str(exc),
                request_payload=request_payload,
                requests_dir=self.requests_dir,
                results_dir=self.results_dir,
            )

        duration = time.monotonic() - start

        # Parse gh output — treat "no checks" (empty or returncode!=0 with that message) as vacuous pass
        checks_list: List[Dict[str, Any]] = []
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                checks_list = json.loads(proc.stdout)
            except json.JSONDecodeError:
                pass
        elif proc.returncode != 0:
            stderr_lower = (proc.stderr or "").lower()
            if "no checks" not in stderr_lower and "has no checks" not in stderr_lower:
                return _rec.record_failure_simple(
                    gate=gate, pr_number=pr_number, pr_id=pr_id,
                    reason="exit_nonzero",
                    reason_detail=f"gh pr checks exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}",
                    request_payload=request_payload,
                    requests_dir=self.requests_dir,
                    results_dir=self.results_dir,
                )
            # no checks → vacuous pass; checks_list stays empty

        passed = [c for c in checks_list if (c.get("bucket") or "").lower() == "pass"]
        failed = [c for c in checks_list if (c.get("bucket") or "").lower() == "fail"]
        skipped = [c for c in checks_list if (c.get("bucket") or "").lower() in _CI_CHECK_BUCKET_SKIP]
        all_complete = all(
            (c.get("bucket") or "").lower() in _CI_CHECK_BUCKET_TERMINAL for c in checks_list
        ) if checks_list else True  # vacuously complete when no checks

        # Determine terminal verdict
        if not all_complete:
            verdict = "running"
        elif failed:
            verdict = "fail"
        else:
            verdict = "pass"

        now = utc_now_iso()

        # In contract-backed mode the request carries the review contract's content_hash,
        # which is what closure_verifier compares against.  Fall back to an
        # execution-scoped sha256 only in legacy (pr_number-only) mode.
        request_contract_hash = request_payload.get("contract_hash", "")
        if request_contract_hash:
            contract_hash = request_contract_hash
        else:
            head_sha = _rec.get_pr_head_sha(pr_number)
            hash_input = json.dumps(
                {"gate_name": gate, "head_sha": head_sha, "pr_number": pr_number},
                sort_keys=True,
            )
            contract_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

        blocking_findings = [
            {
                "severity": "blocking",
                "title": c.get("name", "unknown"),
                "description": f"CI check '{c.get('name', '?')}' concluded with {c.get('state', 'FAILURE')}",
            }
            for c in failed
        ]
        advisory_findings = [
            {
                "severity": "advisory",
                "title": c.get("name", "unknown"),
                "description": f"CI check '{c.get('name', '?')}' was {c.get('state', 'SKIPPED')}",
            }
            for c in skipped
        ]

        # Write report for terminal verdicts
        report_path = request_payload.get("report_path", "")
        report_written = False
        if report_path and verdict in ("pass", "fail"):
            report_content = _format_ci_gate_report(
                pr_number=pr_number,
                branch=request_payload.get("branch", ""),
                checks=checks_list,
                passed=passed,
                failed=failed,
                skipped=skipped,
                status=verdict,
                generated_at=now,
            )
            try:
                Path(report_path).parent.mkdir(parents=True, exist_ok=True)
                Path(report_path).write_text(report_content, encoding="utf-8")
                report_written = True
            except OSError:
                pass

        result_payload: Dict[str, Any] = {
            "gate": gate,
            "pr_id": pr_id or (str(pr_number) if pr_number is not None else ""),
            "pr_number": pr_number,
            "status": verdict,
            "summary": _ci_gate_summary(verdict, passed, failed, skipped),
            "passed_checks": [c.get("name", "") for c in passed],
            "failed_checks": [c.get("name", "") for c in failed],
            "blocking_findings": blocking_findings,
            "advisory_findings": advisory_findings,
            "blocking_count": len(failed),
            "advisory_count": len(skipped),
            "contract_hash": contract_hash if verdict in ("pass", "fail") else "",
            "report_path": report_path if (report_written and verdict in ("pass", "fail")) else "",
            "required_reruns": [],
            "residual_risk": (
                "" if verdict == "pass"
                else f"CI gate {verdict}: {len(failed)} failed check(s)"
            ),
            "duration_seconds": duration,
            "recorded_at": now,
        }
        _rec.stamp_request_identity(result_payload, request_payload)
        return self._book_ci_gate_result(gate, pr_number, pr_id, result_payload, request_payload, now)

    def _book_ci_gate_result(
        self, gate: str, pr_number: Optional[int], pr_id: str,
        result_payload: Dict[str, Any], request_payload: Dict[str, Any], now: str,
    ) -> Dict[str, Any]:
        """Book a pass/fail and end its request in one ``commit_gate_outcome`` (GE-4b).

        A ``running`` snapshot is no outcome (ADR-046 §1) and keeps the overwrite guard alone.
        """
        import gate_recorder as _rec
        from gate_outcome import GateOutcome, GateRequest, commit_gate_outcome

        rf = _rec.result_file_path(
            self.results_dir, gate, pr_number=pr_number, pr_id=pr_id,
        )
        if result_payload["status"] != "running":
            if not rf:
                return result_payload
            payload_on_disk, written = commit_gate_outcome(
                GateOutcome.from_record(gate, result_payload, pr_id=pr_id), results_dir=self.results_dir,
                result_path=rf, request=GateRequest(self.requests_dir, request_payload, {"completed_at": now}),
            )
            return payload_on_disk if written else _rec.annotate_refused_write(payload_on_disk, result_payload)
        if rf:
            # OI-1469/OI-1470: refuse a write that would downgrade an existing
            # terminal, evidenced result (e.g. a stale "running" re-check must
            # never overwrite an already-decided pass/fail).
            payload_on_disk, written = _rec.write_result_guarded(
                rf, result_payload, gate=gate, pr_ref=pr_id or str(pr_number or ""),
            )
            result_payload = (
                payload_on_disk if written
                else _rec.annotate_refused_write(payload_on_disk, result_payload)
            )

        # When checks are still running, reset request to "requested" so it
        # can be re-executed once GitHub reports a terminal conclusion.
        # Setting it to "completed" would cause execute_gate to short-circuit on
        # the next call (status in ("not_executable", "completed", "failed")),
        # leaving the gate permanently stuck with a "running" snapshot.
        request_payload["status"] = "requested"
        request_payload.pop("started_at", None)
        request_payload.pop("runner_pid", None)
        _rec.persist_request(
            self.requests_dir, gate, request_payload,
            pr_number=pr_number, pr_id=pr_id,
        )

        return result_payload

    def _execute_requested_gates(
        self,
        request_result: Dict[str, Any],
        pr_number: int,
        head_sha: Optional[str] = None,
    ) -> tuple:
        """Execute all requested gates and classify results.

        Returns (gates_list, has_required_failure) tuple.

        ``head_sha`` is the PR head the caller already observed for this call
        (``request_and_execute``); ``None`` looks it up here.

        has_required_failure is a UNION across gates, not a majority: it flips
        True the moment ANY gate in this call is not decided_pass — there is no
        averaging or vote-counting here. Ground (measured 22-08): glm_gate and
        kimi_gate returned OPPOSITE verdicts on the identical diff with the
        identical contract_hash (glm FAIL with a blocking finding, kimi PASS
        with none) — neither reader outranks the other, so one blocking
        verdict is enough to block.

        The two branches answer the same question — "must this call report
        success?" — but from different inputs:

        - an EXECUTED gate (request status "requested") that did not decide
          PASS blocks regardless of the merge policy's ``required`` flag. A
          caller that asked a seat into its review stack and got a blocker
          back (blocking findings, failed, unavailable, incomplete evidence,
          or a verdict about another commit) has not had a successful run;
          ``required`` answers "does the final merge need this seat?", not
          "did the seat we just ran return a verdict?" (measured live: a
          per-PR ordinary-change codex_gate request carries required=false,
          returned one blocking finding, and the call exited 0).
        - a PRE-BOOKED gate (any other status) blocks only when ``required``
          is true. That branch holds the advisory contract: wiring_gate in
          shadow mode books status "advisory" with required=false and is
          deliberately non-blocking (see ``_request_wiring_gate``). It also
          keeps the OI-1265 inversion — a required gate with any non-pass
          status, known or unknown, blocks.

        ``required`` is untouched on the request records and in
        ``auto_merge_policy``; only this call's rollup stops reading an
        executed gate through it.
        """
        import gate_recorder as _rec

        # OI-1488: a result record is evidence about the commit it names, and
        # nothing had been checking that this is the commit being merged. The
        # overwrite guard preserves a decided verdict rather than let a later,
        # less-decided write replace it (OI-1469/OI-1470) -- correct, and it
        # means a run on a NEW head can come back holding the OLD head's
        # record, complete and valid-looking, with no field that reads as
        # wrong. Measured on PR #1705: a request about 1425faa1 answered with
        # the full record of 109181d2. All seven headless-review invariants
        # hold on such a record; every one of them is satisfied by a verdict
        # about a different commit.
        #
        # Fetched once per call, not per gate: it is a gh round-trip.
        if head_sha is None:
            head_sha = _rec.get_pr_head_sha(pr_number)

        rows = [
            self._execute_one_request(req, pr_number, head_sha)
            for req in request_result.get("requested", [])
        ]
        return [entry for entry, _blocks in rows], any(blocks for _entry, blocks in rows)

    def _execute_one_request(
        self, req: Dict[str, Any], pr_number: int, head_sha: str,
    ) -> "tuple[Dict[str, Any], bool]":
        """Run (or read back) one requested gate: ``(report entry, blocks the call)``.

        The rules are :meth:`_execute_requested_gates`'s, one request at a time.
        The entry carries the ``seat`` its request record states (OI-2058, R5a),
        so a reader of the report credits the seat this request filled, not one
        an earlier round's takeover annotation names.
        """
        entry, blocks = self._execute_or_read_request(req, pr_number, head_sha)
        if req.get("seat"):
            entry["seat"] = req["seat"]
        return entry, blocks

    def _execute_or_read_request(
        self, req: Dict[str, Any], pr_number: int, head_sha: str,
    ) -> "tuple[Dict[str, Any], bool]":
        """:meth:`_execute_one_request` without the seat stamp."""
        from gate_status import has_complete_evidence, is_pass

        gate_name = req.get("gate", "")
        req_status = req.get("status", "")

        if req_status == "requested":
            exec_result = self.execute_gate(
                gate=gate_name,
                pr_number=pr_number,
            )
            passed, pass_reason = is_pass(exec_result)
            # A gate is decided PASS only when it both passed semantically
            # AND carried a complete evidence trail. An infra failure books
            # ``unavailable`` (never ``failed``) and carries no contract_hash
            # or report_path; either missing means "did not run", which must
            # never sum up to a PASS (OI-1178).
            decided_pass = bool(passed) and has_complete_evidence(exec_result)
            result_sha = str(exec_result.get("commit_sha") or "")
            sha_binding = _classify_sha_binding(head_sha, result_sha)
            if sha_binding == "mismatch":
                # Evidence about another commit is not evidence about this
                # one. Never the reverse: an unknown sha on either side is
                # an unmeasured binding, not a mismatch, and must not
                # downgrade a gate that is otherwise decided -- that is the
                # difference between "we checked and it is wrong" and "we
                # could not check".
                decided_pass = False
                pass_reason = (
                    f"result records commit {result_sha[:8]} but the PR head "
                    f"is {head_sha[:8]} — this verdict is about other code"
                )
            entry = {
                "gate": gate_name,
                "request_status": req_status,
                "execution_status": exec_result.get("status", "unknown"),
                "passed": decided_pass,
                "pass_reason": pass_reason,
                "report_path": exec_result.get("report_path", ""),
                "contract_hash": exec_result.get("contract_hash", ""),
                "sha_binding": sha_binding,
                "head_sha": head_sha,
                "result_commit_sha": result_sha,
                "write_refused": bool(exec_result.get("write_refused")),
                "detail": exec_result,
            }
            # An executed gate blocks on its verdict, not on the merge
            # policy's ``required`` flag: the caller asked this seat into
            # its stack, so a non-PASS it returned is a failure of this
            # call whatever ``required`` says. ``claude_github_optional``
            # stays optional by name — it is not a seat this call's
            # success depends on.
            return entry, gate_name != "claude_github_optional" and not decided_pass

        passed, pass_reason = is_pass(req)
        entry = {
            "gate": gate_name,
            "request_status": req_status,
            "execution_status": req_status,
            "passed": passed,
            "pass_reason": pass_reason,
            "reason": req.get("reason", ""),
            "reason_detail": req.get("reason_detail", ""),
            "detail": req,
        }
        # OI-1265: count what did NOT pass, not a closed list of known
        # failing statuses. A required gate pre-booked with any non-pass
        # status (blocked, an unknown gate, fail, …) must block; a new
        # status added later must not silently slip past enforcement.
        return entry, bool(gate_name != "claude_github_optional" and req.get("required", True) and not passed)

    def request_and_execute(
        self,
        *,
        pr_number: int,
        branch: str,
        review_stack: Optional[Iterable[str]] = None,
        risk_class: str,
        changed_files: Iterable[str],
        mode: str,
        dispatch_id: str = "",
        override_b3: str = "",
        override_b3_carrier: str = "",
        force_rerun: str = "",
    ) -> Dict[str, Any]:
        """Request and immediately execute all gates atomically.

        ``force_rerun`` (R10) is the reason of an operator force-rerun from the
        obligation runner: it travels on every request of the round into the
        result record (``gate_recorder.stamp_request_identity``). It lifts
        nothing here: the round ledger and the run lock judge the run as always.

        Sets ``VNX_CODEX_HEADLESS_ENABLED=1`` in the process environment before
        checking availability so codex is never silently disabled during
        enforcement.

        The PR head is looked up ONCE for the whole call and handed to both
        halves: the request round judges a switched-off seat's standing verdict
        against it, and the executor binds every result to it. Two separate gh
        round-trips could disagree, and a standing verdict would then be checked
        against a different head than the one this call reports.

        A seat whose reviewer comes back exhausted in this call is handed to the
        next distinct reviewer in this call (:meth:`_advance_exhausted_seats`,
        OI-2058 R5a case d). The exhausted entry stays in ``gates`` with
        ``superseded_by`` naming its successor, and only the seat's final
        reviewer decides ``has_required_failure``. A reviewer that decided, PASS
        or FAIL, is never superseded, so a blocking verdict always reaches
        ``has_required_failure``. ``review_seats`` is the seat list the round
        requested (``request_reviews``). ``override_b3`` goes to the request
        round, where the round ledger judges every seat (R1);
        ``override_b3_carrier`` (``flag`` or ``env``, RG-5) travels with it.
        """
        import gate_recorder as _rec

        os.environ["VNX_CODEX_HEADLESS_ENABLED"] = "1"
        head_sha = _rec.get_pr_head_sha(pr_number)
        changed_files = [str(path).strip() for path in changed_files if str(path).strip()]

        request_result = self.request_reviews(
            pr_number=pr_number,
            branch=branch,
            review_stack=review_stack,
            risk_class=risk_class,
            changed_files=changed_files,
            mode=mode,
            dispatch_id=dispatch_id,
            head_sha=head_sha,
            **({"override_b3": override_b3} if override_b3 else {}),
            **({"override_b3_carrier": override_b3_carrier} if override_b3_carrier else {}),
            **({"force_rerun": force_rerun} if force_rerun else {}),
        )

        requested = list(request_result.get("requested", []))
        rows = [self._execute_one_request(req, pr_number, head_sha) for req in requested]
        # The round's derived class (R3a); a round that names none advances a seat at
        # high, never at the caller's class, which may be lower than the diff's.
        review_risk = request_result.get("review_risk")
        derived_class = review_risk.get("risk_class") if isinstance(review_risk, dict) else None
        self._advance_exhausted_seats(
            requested, rows, pr_number=pr_number, branch=branch, risk_class=derived_class or "high",
            changed_files=changed_files, mode=mode, dispatch_id=dispatch_id, head_sha=head_sha,
        )

        return {
            "pr_number": pr_number,
            "branch": branch,
            "gates": [entry for entry, _blocks in rows],
            "has_required_failure": any(
                blocks for entry, blocks in rows if not entry.get("superseded_by")
            ),
            "review_seats": request_result.get("review_seats"),
            "review_risk": review_risk,
        }

    def _advance_exhausted_seats(
        self,
        requested: List[Dict[str, Any]],
        rows: List["tuple[Dict[str, Any], bool]"],
        *,
        pr_number: int,
        branch: str,
        risk_class: str,
        changed_files: List[str],
        mode: str,
        dispatch_id: str,
        head_sha: str,
    ) -> None:
        """Hand every seat whose reviewer just ran out to its next distinct reviewer, in this call.

        Measured 2026-10-15/16 (#2066, #2072): with kimi at its weekly limit, a
        ``request-and-execute --review-stack kimi_gate`` ran kimi, booked
        ``unavailable / lane_exhausted`` and stopped; only a second identical
        request read that record and gave the seat to deepseek. Here the walk
        runs again right after the round, over the records the round left
        (``_advance_seat``), and a seat that moves gets its new reviewer
        requested and run before the call returns.

        ``requested`` and ``rows`` (``(entry, blocks)`` per request) grow in
        place: the new request is appended, and the entry it replaces gets
        ``superseded_by``. Only the reviewers that ran in the previous pass are
        walked again.

        Only a confirmed exhaustion hands a seat on (codex FAIL on #2073). An
        entry whose reviewer decided, PASS or FAIL, is the seat's answer: it is
        never superseded and its seat is never re-requested in this call, or a
        blocking verdict would vanish from ``has_required_failure`` behind a
        later reviewer's pass. Every other entry goes to ``_advance_seat``,
        which moves the seat only on its reviewer's own exhaustion record on
        this head, and only forward along the chain from that reviewer: an
        earlier reviewer whose exhaustion record expired meanwhile never gets
        the seat back. Every move goes strictly further down a chain that
        names each gate at most once, so the bound below (the number of
        registered gates) is the termination argument, not a guess.
        """
        from dispatch_spec import REGISTERED_GATE_NAMES
        from gate_status import decided_verdict
        # At call time: gate_request_handler imports this module at its top.
        from gate_request_handler import _held_reviewers

        pending = list(range(len(requested)))
        for _pass in range(len(REGISTERED_GATE_NAMES)):
            advanced: List[int] = []
            for index in pending:
                if not requested[index].get("seat"):
                    continue  # an extra gate (ci_gate, an optional gate) fills no seat to hand on
                if decided_verdict(rows[index][0]["detail"]):
                    continue  # a decided verdict, PASS or FAIL, keeps its seat
                live = [req for req, (entry, _b) in zip(requested, rows) if not entry.get("superseded_by")]
                successor = self._advance_seat(
                    requested[index], _held_reviewers(live), pr_number=pr_number, branch=branch,
                    risk_class=risk_class, changed_files=changed_files, mode=mode,
                    dispatch_id=dispatch_id, head_sha=head_sha,
                )
                if successor is None:
                    continue
                rows[index][0]["superseded_by"] = successor["gate"]
                requested.append(successor)
                rows.append(self._execute_one_request(successor, pr_number, head_sha))
                advanced.append(len(requested) - 1)
            if not advanced:
                return
            pending = advanced

    def status(self, pr_number: int) -> Dict[str, Any]:
        results = []
        for path in sorted(self.results_dir.glob(f"pr-{pr_number}-*.json")):
            results.append(json.loads(path.read_text(encoding="utf-8")))
        requests = []
        for path in sorted(self.requests_dir.glob(f"pr-{pr_number}-*.json")):
            requests.append(json.loads(path.read_text(encoding="utf-8")))
        return {"pr_number": pr_number, "requests": requests, "results": results}


def _classify_sha_binding(head_sha: str, result_sha: str) -> str:
    """Three answers, not two: ``match``, ``mismatch``, ``unknown``.

    ``unknown`` is not a polite ``mismatch``. Either sha can be absent for
    reasons that say nothing about the code under review -- ``gh`` unreachable,
    a gate whose writer never stamped identity, an offline run. Folding that
    into ``mismatch`` would fail every gate the moment GitHub is briefly
    unavailable; folding it into ``match`` would restore the hole this check
    closes. It is its own state and callers get told which one it is.
    """
    if not head_sha or not result_sha:
        return "unknown"
    return "match" if head_sha == result_sha else "mismatch"


def _ci_gate_summary(
    verdict: str,
    passed: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
) -> str:
    if verdict == "running":
        return "CI checks still running — verdict pending"
    if verdict == "fail":
        names = ", ".join(c.get("name", "?") for c in failed[:3])
        tail = f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
        return f"CI gate FAIL: {len(failed)} failed check(s): {names}{tail}"
    if not passed and not failed and not skipped:
        return "CI gate PASS: no checks configured (vacuous pass)"
    return f"CI gate PASS: {len(passed)} check(s) passed, {len(skipped)} advisory"
