#!/usr/bin/env python3
"""VNX PR merge: merge a PR and emit pr_merged receipt + dispatch register event.

T0 calls this instead of raw ``gh pr merge`` so every merge is captured in the
audit trail (t0_receipts.ndjson + dispatch_register.ndjson).  Without this,
FPY/rework-rate/history have no linkage between merged PRs and receipts.

Merge gate (OI-1216): before merging, the CLI runs a fail-closed check that the
VNX CI workflow has a run with ``conclusion=success`` for exactly the PR head
SHA (see ``_run_ci_gate`` -> ``merge_preflight_ci_check.check_ci_run_for_head``).
A run on an older head does not count, zero runs is a refusal, and any
unverifiable state is a refusal — never a silent pass. ``--override-reason``
skips the check visibly with a mandatory reason.

Review gate (20260816-gate-never-skippable): a second fail-closed check runs
after the CI gate — a passing, fully-evidenced review-gate result must exist
for this PR (see ``_run_review_gate`` ->
``closure_verifier.check_review_gate_for_merge``). A missing result, an empty
``contract_hash``/``report_path``, or a verdict contradicting its report is a
refusal. The same ``--override-reason`` valve applies with a mandatory reason.

Merge pinning (OI-1264): the merge is pinned to the exact head the gates
approved. ``_run_ci_gate`` establishes the head SHA, and ``_do_merge`` passes
it as ``gh pr merge --match-head-commit <sha>`` so ``gh`` refuses to merge any
other commit. A push to the branch after the gates ran moves the head, and the
merge is then refused with a "gates must re-run" message instead of silently
merging a commit the gates never saw.

Outcome verification + conditional --auto (OI-1399 / OI-1386): ``gh pr merge
--auto`` exits 0 both when it merges immediately AND when it only *enables*
auto-merge (the actual merge stays pending on required checks/reviews) — the
two are indistinguishable from the exit code alone. ``_do_merge`` therefore
never trusts ``gh``'s exit code as proof of a merge: after a zero exit it
re-queries the PR (``_pr_actually_merged``) and only reports success when
``state == "MERGED"``. Separately, ``--auto`` is now conditional
(``_repo_auto_merge_allowed``): it is only added when the repository actually
has "Allow auto-merge" enabled, so a repo with it disabled takes the plain
``gh pr merge`` path instead of having the merge rejected outright for
requesting a repo feature that is off.

Usage:
    python3 scripts/pr_merge.py --pr 123
    python3 scripts/pr_merge.py --pr 123 --dispatch-id 20260526-gov2-something
    python3 scripts/pr_merge.py --pr 123 --squash          # default merge strategy
    python3 scripts/pr_merge.py --pr 123 --rebase
    python3 scripts/pr_merge.py --pr 123 --merge
    python3 scripts/pr_merge.py --pr 123 --dry-run         # no merge, no write
    python3 scripts/pr_merge.py --pr 123 --override-reason 'gate flaked, re-verified'

contract_invalid gate (Golf B, B7 / OI-1638 gevolg 3): a third fail-closed
check runs after the review gate — the latest DELIVERABLE-OUTCOME receipt on
record for the dispatch must not be ``contract_invalid`` (see
``contract_invalid_ledger.is_deliverable_acceptable``): a dispatch whose
deliverable never satisfied the report-body contract must not be silently
closed by a merge. "Outcome receipt", not "latest receipt": the review gate
writes ``review_gate_request`` on the same dispatch_id between the worker's
report and the merge, and judging the plain latest therefore read the gate
request instead of the deliverable — measured 07-09, that masked all 8
dispatch-ids in the live ledger that merged over a contract_invalid receipt.
An unreadable ledger is a refusal with its cause, never an empty read.
An empty ``--dispatch-id`` is resolved from the PR number first (the same
lookup that stamps the receipt), so omitting the flag is not a bypass; only
a PR with no dispatch_id in the register skips the check, loudly.

The escape hatch is its own flag, ``--override-contract-invalid``, separate
from ``--override-reason``: accepting a contract_invalid deliverable anyway
is a distinct judgment call from overriding a flaky CI run or an unrun
review gate. It is resolved AFTER the check: on a chain that is clean anyway
the flag is reported as unnecessary and nothing is stamped, so
``contract_invalid_override`` on a receipt always marks a real bypass. On a
refused chain a non-empty reason overrides visibly and is stamped onto the
``pr_merged`` receipt; an empty reason is refused (no silent bypass).

The hatch covers ONE refusal, the contract_invalid one (golf Bx, D4 /
OI-1666). The gate's other two refusals — an unreadable ledger and an empty
dispatch_id — are not judgments about the deliverable and stand regardless of
the flag; an unreadable ledger is repaired, not merged past. The door decides
on ``contract_invalid_ledger``'s machine-readable acceptance CODE, never on
the Dutch reason text.

Receipt written to t0_receipts.ndjson:
    event_type  : "pr_merged"
    pr_number   : <int>
    dispatch_id : <str, optional>
    conclusion  : "merged"
    merge_method: "squash" | "merge" | "rebase"
    pr_title    : <from gh api>
    branch      : <from gh api>
    preflight_gates: <list of 5 dicts — every preflight's own verdict; see
                                 "Preflight ledger" below>
    contract_invalid_override: <dict, optional — {"flag", "reason"}, only
                                 present when --override-contract-invalid
                                 was used to bypass a contract_invalid latest
                                 receipt>

Preflight ledger (Golf Bx, D3 / OI-1665): the five preflights above each
printed a verdict to the terminal and none of them reached the ledger, so
"which gate approved this merge" survived only as long as the pane did, and a
REFUSED merge left no record at all — the five ``return EXIT_ERROR`` branches
in ``main()`` wrote nothing. Both branches now carry the same field,
``preflight_gates``: one record per gate in ``PREFLIGHT_ORDER``, each
``{"gate", "verdict", "message", "head_sha", "overridden",
"override_unnecessary", "override_not_applicable", "reason_code"}``.

``reason_code`` is the machine-readable cause the gate decided on, next to the
Dutch ``message`` (golf Bx, D4 / OI-1666). D3 landed this ledger an hour before
D4 landed those codes and the two never met: the door decided on a code and
recorded only prose, so telling three distinguishable refusals apart afterwards
was back to matching Dutch text — the exact failure class D4 removed inside the
door, reintroduced in the record it writes. Today only the contract_invalid
preflight publishes a code vocabulary
(``contract_invalid_ledger.ACCEPTANCE_CODES`` plus ``REASON_CODE_GATE_SKIPPED``
for its own skip); the other four carry ``""``, and the key is written on every
record either way.

The door SHORT-CIRCUITS on the first NO-GO (each preflight returns
EXIT_ERROR on its own, before the next one runs) and that stays exactly as
it was — it is the fail-closed property golf A and B built. So a refusal can
never carry five real verdicts: the gates after the deciding one never ran.
They are recorded as ``verdict: "not_evaluated"`` naming the gate that
stopped the door, which is the point of the field — without it a gate that
never ran is simply absent from the record, and absence reads as silent
approval.

Refusal receipt written to t0_receipts.ndjson (a refused merge):
    event_type  : "pr_merge_refused"
    status      : "blocked"
    pr_number   : <int>
    dispatch_id : <str, optional — --dispatch-id, else resolved from the PR>
    conclusion  : "refused"
    refused_by  : <str — the gate whose NO-GO stopped the merge>
    head_sha    : <str — the head the gates judged, "" when unresolvable>
    preflight_gates: <list of 5 dicts, as above>

Its own event_type, NOT a ``pr_merged`` carrying ``conclusion: "refused"``:
every merged-PR reader in the tree filters on the literal string
``pr_merged`` and none of them reads ``conclusion`` (track_reconciler.py:320,
build_feature_plan.py:212, build_t0_state.py:999, pr_queue_state.py:128,
traceability_audit.py:610, digest/collectors/progress.py:63), so reusing the
event type would make every one of them count a refused merge as a merge.
``--dry-run`` writes no refusal receipt: that flag is documented as "no
merge, no write", and a governance record is a write.

Register event written to dispatch_register.ndjson:
    event       : "pr_merged"
    pr_number   : <int>
    dispatch_id : <str, optional>
    terminal    : "T0"

Branch-protection preflight (Golf B, B1): a fourth fail-closed gate after
contract_invalid — live branch protection on main must match
``scripts/forge/branch_protection.yaml`` as committed on main, and this PR's
own copy of that YAML must not weaken main's (see
``_run_branch_protection_gate`` and ``scripts/lib/forge_protection_drift.py``
for the full four-step check). A 404 on that exact path when reading main's
YAML is the bootstrap case (no PR has ever applied one yet) and is a loud
no-op, not a refusal. The only override is ``--allow-weaken "<reason>"``,
which accepts ONLY a weakening the PR's own YAML edit introduces relative to
main — a drift between live state and main's own declared YAML has no
override at all (run ``apply_branch_protection.py`` first).

BILLING SAFETY: No Anthropic SDK. No direct API calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt
from merge_preflight_ci_check import check_ci_run_for_head, _resolve_override_reason
from merge_preflight_adr_check import check_adr_numbers_for_pr
from contract_invalid_ledger import (
    CODE_CONTRACT_INVALID,
    evaluate_deliverable_acceptance,
)
from forge_protection_drift import (
    PROTECTION_YAML_RELATIVE_PATH,
    ProtectionConfigError,
    compare as compare_protection_state,
    fetch_live_protection,
    fetch_yaml_from_ref,
    is_weakening as protection_is_weakening,
    parse_protection_config,
    to_normalized_dict as protection_to_normalized_dict,
)

EXIT_OK = 0
EXIT_ERROR = 1

# Matches internal PR labels: pure numeric (PR-42) and alphanumeric (PR-HYG-1, PR-TMUX-3, PR-ROUTE-1).
_PR_LABEL_RE = re.compile(r"\bPR-([A-Z0-9]+(?:-[A-Z0-9]+)*)\b", re.IGNORECASE)

# OI-1518: AppendResult.status values that count as a BINDING receipt landed.
# A grep of `AppendResult(` across the whole tree yields exactly two producers
# (lib/append_receipt_internals/idempotency.py: "appended" + "duplicate";
# lib/report_to_receipt_converter.py: "duplicate"). No third value exists, so
# this is an allowlist, not a blocklist: any other value is refused. The
# "unknown" default (a missing `append_status` key) is NOT a third value — it
# means "this code could not read the outcome", which is its own third branch:
# neither success nor a known fault, so it fails safe. Two failure shapes land
# here together: _emit_receipt raising, and _emit_receipt returning a status
# that is not recognized. Both must make the CLI exit non-zero.
_RECEIPT_OK_STATUSES = frozenset({"appended", "duplicate"})

#: The five preflights, in the exact order ``main()`` runs them. This order is
#: what makes a refusal record readable: the door short-circuits on the first
#: NO-GO, so everything at a later position never ran.
PREFLIGHT_ORDER: tuple[str, ...] = (
    "ci",
    "review",
    "adr",
    "contract_invalid",
    "branch_protection",
)

#: Third verdict value beside "GO" and "NO-GO" on a preflight record: this gate
#: never got a turn because an earlier one refused. It is written EXPLICITLY
#: rather than left out, because a gate that is simply missing from the record
#: is indistinguishable from one that approved (OI-1665).
VERDICT_NOT_EVALUATED = "not_evaluated"

#: The ``not_evaluated`` message for a merge whose CALLER never ran the door's
#: preflights at all (``merge_pr`` invoked directly, not via ``main()``). The
#: same principle one level up: a ``pr_merged`` receipt that simply OMITS
#: ``preflight_gates`` is indistinguishable from the ~23k receipts written
#: before the field existed, so the first reader has to fail open on both. The
#: key is therefore always written, and this message is what separates "no gate
#: ran, ever" from the short-circuit padding a refusal produces.
GATES_NOT_RUN_MESSAGE = "niet uitgevoerd: deze aanroep draaide de preflights van de deur niet"

#: ``reason_code`` for the contract_invalid preflight's own skip: no dispatch_id
#: was given and none could be derived from the PR, so the ledger was never
#: read and no acceptance code exists.
#:
#: Deliberately a DOOR literal and deliberately NOT a member of
#: ``contract_invalid_ledger.ACCEPTANCE_CODES``: that set is the closed
#: vocabulary of judgments the ledger produced, and a reader validating a code
#: against it must not be handed a value the ledger never returned. What it may
#: not be is the empty string — that is what a gate with no code vocabulary at
#: all carries, and reusing it here would make "this gate published no code"
#: and "this gate skipped without reading anything" the same record.
REASON_CODE_GATE_SKIPPED = "gate_skipped_no_dispatch_id"


def _preflight_record(name: str, gate: Dict[str, Any], head_sha: str = "") -> Dict[str, Any]:
    """One preflight's outcome, in the shape the ledger stores it.

    Read straight off the gate result the door already holds — no gate is ever
    re-run to fill this in (see ``TestShortCircuitUnchanged``). ``head_sha`` is
    the commit that gate judged; it is the same head for all five (established
    once by ``_run_ci_gate``) and is carried per record so a single record is
    self-contained evidence rather than a pointer to a sibling field.

    ``reason_code`` is the machine-readable cause the gate DECIDED on (golf Bx,
    D4 / OI-1666). Without it this record held the verdict and the Dutch
    message and nothing else, so establishing afterwards WHICH refusal fired —
    a real contract_invalid, an unreadable ledger, an empty dispatch_id — meant
    matching that prose. Deciding on prose is the failure class D4 removed
    inside the door; a record that keeps only the prose puts it back one level
    up, where the reader is a digest or a human weeks later. The door and its
    record now name the same fact the same way.

    The two override fields travel for the same reason. ``overridden`` alone
    covers only one of the three things that can happen to an override flag:
    it was applied, it was passed but does not cover this refusal
    (``override_not_applicable``), or it was passed while the chain was clean
    and nothing needed bypassing (``override_unnecessary``). An operator who
    TRIED to override and was held is an audit fact of its own, and with only
    the first field that attempt and "no flag was passed at all" produce an
    identical record.

    Every key is written on every record, including for the four gates that
    publish no code at all — they decide on their own logic and their record
    carries ``reason_code: ""``. That is the same rule ``VERDICT_NOT_EVALUATED``
    exists for: a key that is present on some records and absent on others
    forces its first reader to guess, and the natural repair for the resulting
    KeyError is a permissive default.
    """
    return {
        "gate": name,
        "verdict": str(gate.get("verdict") or "unknown"),
        "message": str(gate.get("message") or ""),
        "head_sha": head_sha or "",
        "overridden": bool(gate.get("overridden")),
        "override_unnecessary": bool(gate.get("override_unnecessary")),
        "override_not_applicable": bool(gate.get("override_not_applicable")),
        "reason_code": str(gate.get("reason_code") or ""),
    }


def _preflight_ledger(
    evaluated: list[Dict[str, Any]],
    *,
    refused_by: str = "",
    unevaluated_message: str = "",
) -> list[Dict[str, Any]]:
    """The five preflights in ``PREFLIGHT_ORDER``: those that ran, plus those
    that never got a turn marked ``not_evaluated``.

    ``refused_by`` names the gate whose NO-GO stopped the door; it goes into
    the message of every unevaluated record so a reader never has to
    reconstruct WHY a gate has no verdict. On the merge branch all five have
    run and this pads nothing.

    ``unevaluated_message`` overrides that text for a caller whose gates did
    not run for a different reason than a short-circuit — see
    ``GATES_NOT_RUN_MESSAGE``. Both shapes use the same ``not_evaluated``
    verdict, so the message is what tells the two apart.

    A gate that did not run judged no commit, so its ``head_sha`` is empty —
    deliberately not the PR head, which would suggest it looked at it. Its
    ``reason_code`` is empty for the same reason: a gate that never ran reached
    no cause. The padding is built from ``_preflight_record`` itself rather than
    from a second literal, so the two shapes cannot drift apart — when D4's
    ``reason_code`` landed on the one and not the other, a reader met the key on
    three of five records and a KeyError on the rest.
    """
    default_message = (
        f"niet uitgevoerd: de deur stopte op de {refused_by}-preflight"
        if refused_by
        else "niet uitgevoerd"
    )
    by_name = {record["gate"]: record for record in evaluated}
    ledger: list[Dict[str, Any]] = []
    for name in PREFLIGHT_ORDER:
        record = by_name.get(name)
        if record is not None:
            ledger.append(record)
            continue
        ledger.append(_preflight_record(name, {
            "verdict": VERDICT_NOT_EVALUATED,
            "message": unevaluated_message or default_message,
        }))
    return ledger


def _extract_pr_id(subject: str) -> Optional[str]:
    """Extract internal PR-N/PR-LABEL from commit subject or PR title.

    Returns the first match as an uppercase string, e.g. "PR-HYG-1" or "PR-42".
    Returns None if no internal PR label is found.
    """
    m = _PR_LABEL_RE.search(subject)
    if m:
        return f"PR-{m.group(1).upper()}"
    return None


def _lookup_dispatch_id_by_pr_number(pr_number: int) -> str:
    """Look up dispatch_id in dispatch_register by pr_number. Best-effort, returns '' on miss."""
    try:
        from dispatch_register import read_events
        events = read_events()
        for ev in reversed(events):
            if ev.get("pr_number") == pr_number and ev.get("dispatch_id"):
                return str(ev["dispatch_id"])
    except Exception as e:
        log.warning("dispatch lookup failed: %s", e)
    return ""


def _gh(args: list[str], *, check: bool = False, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a gh command and return the CompletedProcess."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _query_pr(pr_number: int) -> Optional[Dict[str, Any]]:
    """Return PR metadata from GitHub, or None on failure."""
    result = _gh([
        "pr", "view", str(pr_number),
        "--json", "number,title,state,headRefName,baseRefName,headRefOid,mergedAt,mergeCommit",
    ])
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _run_ci_gate(
    pr_number: int,
    *,
    override_reason: Optional[str] = None,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Fail-closed merge gate: VNX CI must have conclusion=success for the PR head.

    Resolves the PR head (sha + branch) via ``gh pr view`` and delegates the
    workflow-conclusion check to
    ``merge_preflight_ci_check.check_ci_run_for_head``. Returns ``(gate,
    pr_data)``; the caller merges only when ``gate["verdict"] == "GO"``.

    A failed/empty PR query (no head sha or branch) is a NO-GO — the GitHub API
    not answering is a refusal with a message, never a silent pass.
    """
    pr_data = _query_pr(pr_number)
    head_sha = (pr_data or {}).get("headRefOid") or ""
    branch = (pr_data or {}).get("headRefName") or ""
    if not head_sha or not branch:
        return {
            "verdict": "NO-GO",
            "message": (
                f"PR-head (sha/branch) kon niet worden bepaald voor #{pr_number}: "
                "deze merge is niet toetsbaar"
            ),
            "overridden": False,
            "override_reason": None,
        }, pr_data
    gate = check_ci_run_for_head(
        SCRIPT_DIR.parent,
        branch=branch,
        head_sha=head_sha,
        override_reason=override_reason,
    )
    return gate, pr_data


def _norm_pr_id(pr_id: str) -> str:
    """Deprecated alias for ``gate_obligations.normalise_pr_id``.

    Kept as a name so existing imports and tests keep resolving; the logic
    lives in gate_obligations, which the readiness report reads from too.
    """
    from gate_obligations import normalise_pr_id

    return normalise_pr_id(pr_id)


def _resolve_declared_gate(pr_number: int, *, state_dir: Path) -> str:
    """Resolve a PR's declared review gate from its door obligation.

    Delegates the join to ``gate_obligations.declared_gates_for_pr`` and keeps
    this function's own contract unchanged: the LAST declared gate wins, and
    an unreadable obligation store degrades to "" (a refusal at the merge
    gate) rather than raising into the merge path.
    """
    try:
        from gate_obligations import declared_gates_for_pr

        matches = declared_gates_for_pr(state_dir, pr_number)
        if matches:
            return matches[-1]
    except (ValueError, OSError) as exc:
        log.warning("review-gate obligation lookup failed: %s", exc)
    return ""


def _run_review_gate(
    pr_number: int,
    *,
    override_reason: Optional[str] = None,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Fail-closed review-gate check: a passing, evidenced review-gate result
    must exist for this PR before merge.

    Resolves the PR branch via ``gh pr view``, resolves the declared gate from
    the door's obligation record (joined on the GitHub PR number), then
    delegates result validation to
    ``closure_verifier.check_review_gate_for_merge`` — the SAME truth the
    closure verifier uses. Returns ``(gate, pr_data)``; the caller merges only
    when ``gate["verdict"] == "GO"``.

    The result key is the bare PR number string (``str(pr_number)``): the
    governed obligation runner records gate results under that key regardless
    of any PR-N label the spec declared. A failed PR query, a missing
    obligation, or a missing/contradictory result is a NO-GO — an unverifiable
    state is a refusal, never a silent pass. ``override_reason`` is the escape
    hatch with the same semantics as the CI gate: non-empty skips the check
    visibly, empty is refused.
    """
    pr_data = _query_pr(pr_number)
    if pr_data is None:
        return {
            "verdict": "NO-GO",
            "message": (
                f"PR-data kon niet worden opgevraagd voor #{pr_number}: "
                "review-gate-check niet toetsbaar"
            ),
            "overridden": False,
            "override_reason": None,
            "gate": None,
        }, pr_data
    branch = pr_data.get("headRefName") or ""
    head_sha = pr_data.get("headRefOid") or ""
    if not head_sha or not branch:
        # OI-1318: the sibling CI gate refuses here and this one did not. It
        # carried on with empty strings into check_review_gate_for_merge, whose
        # matcher then read "" as "no constraint" and accepted any result for
        # the PR — so the one path that could not establish which commit it was
        # merging was also the path that stopped asking. Same refusal, same
        # words, as _run_ci_gate.
        return {
            "verdict": "NO-GO",
            "message": (
                f"PR-head (sha/branch) kon niet worden bepaald voor #{pr_number}: "
                "review-gate-check niet toetsbaar"
            ),
            "overridden": False,
            "override_reason": None,
            "gate": None,
        }, pr_data

    # ── Escape hatch (same resolution + semantics as the CI gate) ─────────
    reason = _resolve_override_reason(override_reason)
    if reason is not None:
        if not reason:
            return {
                "verdict": "NO-GO",
                "message": (
                    "override zonder reden geweigerd: een override vereist een "
                    "niet-lege reden (geen stille bypass)"
                ),
                "overridden": True,
                "override_reason": reason,
                "gate": None,
            }, pr_data
        return {
            "verdict": "GO",
            "message": f"OVERRIDE: review-gate-check overgeslagen voor merge ({reason})",
            "overridden": True,
            "override_reason": reason,
            "gate": None,
        }, pr_data

    # ── Declared gate from the door's obligation ──────────────────────────
    paths = ensure_env()
    state_dir = Path(paths["VNX_STATE_DIR"])
    gate_name = _resolve_declared_gate(pr_number, state_dir=state_dir)
    if not gate_name:
        return {
            "verdict": "NO-GO",
            "message": (
                f"geen review-gate-verplichting gevonden voor PR #{pr_number}: "
                "weiger merge zonder gate-verdict"
            ),
            "overridden": False,
            "override_reason": None,
            "gate": None,
        }, pr_data

    from closure_verifier import check_review_gate_for_merge

    results_dir = state_dir / "review_gates" / "results"
    gate = check_review_gate_for_merge(
        str(pr_number),
        gate_name,
        results_dir,
        branch=branch,
        head_sha=head_sha,
    )
    return gate, pr_data


def _run_adr_gate(pr_number: int, *, pr_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fail-closed merge gate (Golf B, B6): an ADR file added by this PR must
    not reuse a number already on the real base branch.

    Delegates to ``merge_preflight_adr_check.check_adr_numbers_for_pr``, which
    reads the PR's added files and the real base-branch tree via the GitHub
    API — never a local ``origin/main`` ref (see that module's docstring for
    why: the door never fetches, so a local ref is only as fresh as the last
    incidental fetch). No override: a colliding ADR number is always a
    refusal.

    OI-1518 recovery (leeszetel finding 2): ``pr_data`` — already resolved by
    ``_run_ci_gate`` — carries the PR's ``state``. When ``state == "MERGED"``,
    this call is the OI-1518 recovery route: an operator re-running
    ``pr_merge.py --pr N`` after a merge whose receipt did not land. The PR's
    own ADR file is by then already sitting on the base branch (the merge put
    it there), so the live check would refuse every such recovery run on an
    ADR PR by construction (#1790/#1792 measured NO-GO once merged). Skipped
    here, loudly, ONLY for an already-merged PR — never via an override flag,
    which would defeat "a colliding number is always a refusal" for a PR that
    is still open.

    Leeszetel finding 7: the base branch to check against is the PR's own
    ``baseRefName`` from ``pr_data``, not a hardcoded ``"main"`` — a PR
    targeting a non-main base is compared against its real base instead of
    silently assuming main.
    """
    if (pr_data or {}).get("state") == "MERGED":
        return {
            "verdict": "GO",
            "message": (
                f"ADR-preflight overgeslagen: PR #{pr_number} staat al op GitHub als "
                "MERGED (OI-1518-herstelroute: dit ADR-nummer staat door de merge zelf "
                "al op de basisbranch)"
            ),
            "colliding_number": None,
            "pr_file": None,
            "main_file": None,
        }
    base_ref = (pr_data or {}).get("baseRefName") or "main"
    return check_adr_numbers_for_pr(pr_number, project_root=SCRIPT_DIR.parent, base_ref=base_ref)


def _run_contract_invalid_gate(
    dispatch_id: str,
    *,
    pr_number: Optional[int] = None,
    override_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Fail-closed merge gate (Golf B, B7): the latest DELIVERABLE-OUTCOME
    receipt on record for ``dispatch_id`` must not be ``contract_invalid``
    (OI-1638 gevolg 3).

    DISPATCH_RULES.md §14 gevolg 3 names ``is_deliverable_acceptable`` as the
    gate a closer must call before treating a dispatch's deliverable as
    done, and originally pointed at ``verify_pr_closure()`` as the intended
    call site. That call site is wrong: the merge door is the one place that
    sees the real ``main`` at the moment of merge, while the closure
    verifier only reads afterwards and cannot stop a merge already in
    flight. This is the corrected call site (see that doc section, corrected
    alongside this dispatch).

    Reads from the SAME receipts file ``emit_governance_receipt`` resolves to
    when given no ``receipts_file`` (``ensure_env()``'s ``VNX_STATE_DIR`` /
    ``t0_receipts.ndjson``) — the check and the write it gates must see the
    same ledger.

    An empty ``dispatch_id`` no longer walks straight past the gate: the door
    resolves it from ``pr_number`` first, via the SAME
    ``_lookup_dispatch_id_by_pr_number`` that ``merge_pr`` already uses to
    stamp the ``pr_merged`` receipt. Omitting ``--dispatch-id`` therefore
    stops being an escape hatch that needs no reason — the merge still gets
    the receipt chain stamped on it either way, so the gate must judge that
    same chain. Only when the register has no dispatch_id for this PR does
    the check skip, LOUDLY (``pr_merge`` has never required ``--dispatch-id``
    — an unresolvable PR genuinely has no chain to check).

    ``override_reason`` (``--override-contract-invalid``) is a SEPARATE
    escape hatch from ``--override-reason`` (already wired to the CI and
    review gates): accepting a contract_invalid deliverable anyway is a
    distinct judgment call from overriding a flaky CI run or an unrun review
    gate, so it gets its own flag rather than silently piggy-backing on
    ``--override-reason``.

    Unlike the other gates' hatch, this one is resolved AFTER the ledger read,
    not before it. Checked before, the flag alone produced ``overridden:
    True`` on a chain that was clean anyway, and ``main()`` then stamped
    ``contract_invalid_override`` onto the ``pr_merged`` receipt — an audit
    field claiming a bypass that never happened, which reads in the trail as
    a governed failure someone waved through. So: run the check first, and
    only let the flag matter when the verdict without it would have been
    NO-GO. On a clean chain the flag is reported as unnecessary and nothing
    is stamped; on a dirty chain a non-empty reason overrides visibly and an
    empty one is refused (no silent bypass).

    And it only covers its OWN refusal (golf Bx, D4 / OI-1666). The gate
    refuses in three distinguishable cases — the real contract_invalid, an
    unreadable ledger, and an empty dispatch_id — and this branch used to set
    GO on all three, because ``is_deliverable_acceptable`` returned a bool
    plus Dutch prose and nothing that could be decided on. An operator who
    means "I know about that contract_invalid" was thereby silently also
    waving through "I cannot read the ledger", where the right move is to
    repair it. The decision is now on
    ``evaluate_deliverable_acceptance``'s ``code``: only
    ``CODE_CONTRACT_INVALID`` may be overridden, any other refusal stands
    with ``override_not_applicable: True`` and ``overridden: False`` (no
    bypass happened, so nothing may be stamped as one). Deliberately not a
    match on the message text — that prose is a message for a human and
    breaks on the first rewording, in the permissive direction.

    Applicability is judged BEFORE the reason's emptiness: a flag that does
    not cover this refusal is no override at all, so there is nothing to
    demand a reason for. Both orders refuse; this one names the real cause.

    ``overridden`` is True on exactly one branch: the one that returns GO
    because a real reason bypassed a real contract_invalid. Every other use of
    the flag — inapplicable, unnecessary, or refused for lacking a reason —
    leaves it False, because no bypass happened on any of them and the field is
    copied verbatim into the ``pr_merged``/``pr_merge_refused`` record.

    ``reason_code`` travels out of this function on every branch, so the record
    it lands in can name the cause the door decided on instead of only the
    Dutch message. Its values are ``contract_invalid_ledger.ACCEPTANCE_CODES``
    plus this module's ``REASON_CODE_GATE_SKIPPED`` for the skip above, which
    never reaches the ledger.
    """
    did = (dispatch_id or "").strip()
    resolved_from_pr = False
    if not did and pr_number is not None:
        did = _lookup_dispatch_id_by_pr_number(pr_number).strip()
        resolved_from_pr = bool(did)

    if not did:
        suffix = f" (en niet af te leiden uit PR #{pr_number})" if pr_number is not None else ""
        return {
            "verdict": "GO",
            "message": f"contract_invalid-check overgeslagen: geen dispatch-id{suffix}",
            "skipped": True,
            "overridden": False,
            "override_reason": None,
            "override_unnecessary": False,
            "override_not_applicable": False,
            "reason_code": REASON_CODE_GATE_SKIPPED,
            "resolved_from_pr": False,
        }

    paths = ensure_env()
    receipts_path = Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson"
    acceptance = evaluate_deliverable_acceptance(did, receipts_path)
    acceptable, reason = acceptance.acceptable, acceptance.reason

    origin = f" (dispatch-id afgeleid uit PR #{pr_number})" if resolved_from_pr else ""
    result: Dict[str, Any] = {
        "verdict": "GO" if acceptable else "NO-GO",
        "message": f"{reason}{origin}",
        "skipped": False,
        "overridden": False,
        "override_reason": None,
        "override_unnecessary": False,
        "override_not_applicable": False,
        "reason_code": acceptance.code,
        "resolved_from_pr": resolved_from_pr,
    }

    if override_reason is None:
        return result

    # ── Escape hatch, resolved against the verdict the check just produced ──
    if acceptable:
        result["message"] = (
            f"{result['message']} — --override-contract-invalid was niet nodig "
            f"(de keten is schoon); geen override op de receipt"
        )
        result["override_unnecessary"] = True
        return result

    # The refusal must be the one this flag is FOR. Judged on the acceptance
    # code, never on the message: those strings are Dutch prose for a human.
    if acceptance.code != CODE_CONTRACT_INVALID:
        result["message"] = (
            f"{result['message']} — --override-contract-invalid geldt alleen voor "
            f"een contract_invalid-uitkomst, niet voor '{acceptance.code}'; "
            f"deze weigering blijft staan"
        )
        result["override_not_applicable"] = True
        return result

    reason_text = override_reason.strip()
    if not reason_text:
        result["verdict"] = "NO-GO"
        result["message"] = (
            "override zonder reden geweigerd: --override-contract-invalid "
            "vereist een niet-lege reden (geen stille bypass)"
        )
        # NOT ``overridden: True``. The verdict stays NO-GO, so nothing was
        # bypassed — the same rule the branch two above already applies to an
        # inapplicable flag, and the same one this function's docstring states
        # for a clean chain. It is not a cosmetic field either: ``main`` builds
        # the ``contract_invalid_override`` audit stamp from it, and
        # ``_preflight_record`` copies it straight into the preflight ledger, so
        # a True here put "this merge door was overridden" into the very record
        # written to prove it refused.
        #
        # ``override_reason`` keeps the empty string rather than ``None``: the
        # flag WAS passed here, and the distinction from the branches that leave
        # it None is the only in-band trace of an attempt that carried no reason.
        result["overridden"] = False
        result["override_reason"] = reason_text
        return result

    result["verdict"] = "GO"
    result["message"] = (
        f"contract_invalid-check overgeslagen voor {did} ({reason_text}) — "
        f"zonder de vlag was dit NO-GO: {reason}"
    )
    result["overridden"] = True
    result["override_reason"] = reason_text
    return result


def _no_go_protection(message: str) -> Dict[str, Any]:
    return {"verdict": "NO-GO", "message": message, "overridden": False, "override_reason": None}


#: Every file whose CONTENT decides what this door refuses. Hashing only the
#: entry point was not enough: the entry point delegates its actual judgment
#: to these libraries, so a one-line edit to ``is_weakening`` neutralizes the
#: branch-protection preflight while ``scripts/pr_merge.py`` stays
#: byte-identical to main and the integrity check reports GO (measured by the
#: B1 read-seat on 2026-09-07 — that exact mutation passed unseen while 14
#: tests went red on it).
_DOOR_INTEGRITY_PATHS = (
    "scripts/pr_merge.py",
    "scripts/lib/forge_protection_drift.py",
    "scripts/lib/merge_preflight_adr_check.py",
    "scripts/lib/merge_preflight_ci_check.py",
    "scripts/lib/contract_invalid_ledger.py",
)


def _door_blob_hash_gate(project_root: Path) -> Dict[str, Any]:
    """Golf B, B1: the merge door only runs its preflight checks from the
    checkout ON main — a fix-forward pushed straight to a feature branch
    (bypassing review of the door's own code) must not be able to weaken
    what this door enforces just by running from a stale or edited local
    checkout. Compares the git blob hash of every file in
    ``_DOOR_INTEGRITY_PATHS`` (``git hash-object``, computed locally) against
    the sha GitHub reports for that same path on ``main`` (the contents API's
    ``sha`` field IS a git blob hash — measured equal on this repo's own
    ``scripts/pr_merge.py`` on 2026-09-07). Any mismatch refuses, naming the
    files that differ; an unreadable local or remote hash refuses too
    (fail-closed).
    """
    mismatched: list[str] = []
    for path in _DOOR_INTEGRITY_PATHS:
        local = subprocess.run(
            ["git", "hash-object", path],
            cwd=str(project_root), capture_output=True, text=True, timeout=15,
        )
        if local.returncode != 0:
            return _no_go_protection(
                f"git hash-object op {path} faalde: deur-integriteit niet toetsbaar "
                f"({(local.stderr or '').strip()[:200]})"
            )
        local_hash = (local.stdout or "").strip()

        remote = _gh([
            "api", f"repos/{{owner}}/{{repo}}/contents/{path}?ref=main", "--jq", ".sha",
        ])
        if remote.returncode != 0:
            return _no_go_protection(
                f"sha van {path} op main kon niet worden opgevraagd: deur-integriteit "
                f"niet toetsbaar ({(remote.stderr or '').strip()[:200]})"
            )
        remote_hash = (remote.stdout or "").strip()
        if not remote_hash or local_hash != remote_hash:
            mismatched.append(path)

    if mismatched:
        return _no_go_protection(
            "de draaiende deur wijkt af van de versie op main (" + ", ".join(mismatched)
            + "): de deur draait alleen uit de hoofd-checkout op main"
        )
    return {
        "verdict": "GO",
        "message": f"deur-integriteit: {len(_DOOR_INTEGRITY_PATHS)} deurbestanden identiek aan main",
        "overridden": False, "override_reason": None,
    }


def _run_branch_protection_gate(
    pr_number: int,
    *,
    pr_data: Optional[Dict[str, Any]] = None,
    allow_weaken_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Fail-closed merge gate (Golf B, B1): live branch protection on main
    must match ``scripts/forge/branch_protection.yaml`` as committed on
    main, and this PR's own copy of that YAML must not weaken main's.

    Four checks, in order, each a refusal on its own:

    (a) The RUNNING door must be byte-identical to main's copy
        (``_door_blob_hash_gate`` over ``_DOOR_INTEGRITY_PATHS``) — the door
        only runs any of the checks below from the main checkout. FIRST, not
        last: every step after this one has a branch that returns early, and
        a check that proves the door is unedited is worthless when the
        edited door can route around it. The bootstrap no-op in (b) did
        exactly that.
    (b) Read main's YAML via the contents API (never a local ref — the door
        never fetches). A 404 on EXACTLY that path, at a ref confirmed to
        exist, is the bootstrap case (no PR has ever applied a
        branch_protection.yaml to main yet): a no-op GO, loudly. Any other
        read/parse failure blocks — including a 404 whose ref cannot be
        confirmed, which is indistinguishable from an unknown ref or an
        unresolvable repo (see ``forge_protection_drift._confirm_ref_exists``).
    (c) Live protection on main must match main's own declared YAML — any
        drift blocks, with the differing fields named. No override: a drift
        here means ``apply_branch_protection.py`` must be run first, not
        that this merge should be waved through.
    (d) This PR's own version of the YAML (read at the PR's head sha, same
        contents API) must not weaken main's version
        (``forge_protection_drift.is_weakening``). Deleting the file counts
        as the ultimate weakening. Blocks without ``--allow-weaken
        "<reason>"`` (empty reason refused, no silent bypass).

        Reader-for-field contract: the parser used here (``parse_protection_config``)
        is the LOCAL checkout's code, which step (a) has just proven
        byte-identical to main — never the PR's own copy. A PR that adds a
        new schema field to ``branch_protection.yaml`` AND teaches the
        reader that field in the same PR can therefore never pass this
        step: main's reader, the only one running, does not know the field
        yet. Extending the schema is always two PRs, reader first, field
        second. See docs/operations/FORGE_GATE.md § "Contract:
        branch_protection.yaml uitbreiden gaat in twee PR's (OI-1672)" for
        the measured cases and the exact refusal text.

    No override besides ``--allow-weaken`` — a drift found in (c) or an
    unreadable/unparseable state anywhere has no escape hatch.
    """
    project_root = SCRIPT_DIR.parent

    door_check = _door_blob_hash_gate(project_root)
    if door_check["verdict"] != "GO":
        return door_check

    main_yaml = fetch_yaml_from_ref(project_root, "main", PROTECTION_YAML_RELATIVE_PATH)
    if main_yaml.not_found:
        return {
            "verdict": "GO",
            "message": "branch-protection-drift: geen YAML op main, preflight overgeslagen",
            "bootstrap": True,
            "overridden": False,
            "override_reason": None,
        }
    if main_yaml.error:
        return _no_go_protection(f"branch-protection-YAML op main niet leesbaar: {main_yaml.error}")
    try:
        main_config = parse_protection_config(main_yaml.text or "")
    except ProtectionConfigError as exc:
        return _no_go_protection(f"branch-protection-YAML op main ongeldig: {exc}")
    main_norm = protection_to_normalized_dict(main_config)

    try:
        live_norm = fetch_live_protection(project_root, branch="main")
    except Exception as exc:  # noqa: BLE001 — any unreadable live state blocks
        return _no_go_protection(f"live branch-protection niet leesbaar: {exc}")
    diffs = compare_protection_state(main_norm, live_norm)
    if diffs:
        fields = ", ".join(sorted({d["field"] for d in diffs}))
        return _no_go_protection(
            f"branch-protection wijkt af van scripts/forge/branch_protection.yaml op main: {fields}"
        )

    head_sha = (pr_data or {}).get("headRefOid") or ""
    if not head_sha:
        return _no_go_protection(
            f"PR-head kon niet worden bepaald voor #{pr_number}: branch-protection-preflight niet toetsbaar"
        )
    pr_yaml = fetch_yaml_from_ref(project_root, head_sha, PROTECTION_YAML_RELATIVE_PATH)
    if pr_yaml.not_found:
        return _no_go_protection(
            f"deze PR verwijdert {PROTECTION_YAML_RELATIVE_PATH}: branch-protection kan niet "
            "meer worden gehandhaafd"
        )
    if pr_yaml.error:
        return _no_go_protection(f"branch-protection-YAML op de PR-head niet leesbaar: {pr_yaml.error}")
    try:
        pr_config = parse_protection_config(pr_yaml.text or "")
    except ProtectionConfigError as exc:
        return _no_go_protection(f"branch-protection-YAML op de PR-head ongeldig: {exc}")
    pr_norm = protection_to_normalized_dict(pr_config)

    weakening, weak_fields = protection_is_weakening(main_norm, pr_norm)
    reason = None
    if weakening:
        reason = (allow_weaken_reason or "").strip()
        if allow_weaken_reason is None:
            return _no_go_protection(
                "deze PR verzwakt branch-protection t.o.v. main zonder --allow-weaken: "
                + ", ".join(weak_fields)
            )
        if not reason:
            return {
                "verdict": "NO-GO",
                "message": "override zonder reden geweigerd: --allow-weaken vereist een niet-lege reden",
                "overridden": True,
                "override_reason": reason,
            }

    if weakening:
        return {
            "verdict": "GO",
            "message": (
                f"OVERRIDE: branch-protection-verzwakking geaccepteerd ({reason}): "
                + ", ".join(weak_fields)
            ),
            "overridden": True,
            "override_reason": reason,
        }
    return {
        "verdict": "GO",
        "message": "branch-protection: geen drift, PR verzwakt niets",
        "overridden": False,
        "override_reason": None,
    }


_HEAD_MOVED_MARKERS = (
    "head branch is not up to date",
    "head branch was modified",
    "head was modified",
    "head changed",
    "head has changed",
)


def _is_head_moved_refusal(output: str) -> bool:
    """True when a failed ``gh pr merge`` refusal signals the PR head moved.

    ``gh`` surfaces the GitHub server's refusal text verbatim and has no stable
    exit-code vocabulary for a ``--match-head-commit`` mismatch, so this matches
    the phrasings GitHub emits when the head no longer equals the pinned commit.
    A non-match falls through to the raw error, never to a silent pass.
    """
    low = (output or "").lower()
    return any(marker in low for marker in _HEAD_MOVED_MARKERS)


def _head_moved_refusal_message(pr_number: int, head_sha: str, raw: str) -> str:
    """Explain a head-moved refusal as the gates working, not a crash."""
    return (
        f"merge van PR #{pr_number} geweigerd: de head van de branch is "
        f"verschoven na de goedkeuring (goedgekeurd op {head_sha}). "
        "Er is na de CI-/review-gate naar de branch gepusht; de gates moeten "
        "opnieuw draaien op de nieuwe head voordat deze PR gemerged mag worden. "
        f"gh: {raw}"
    )


def _repo_auto_merge_allowed() -> Optional[bool]:
    """Whether the repo has "Allow auto-merge" enabled — required for ``gh --auto``.

    Neither ``gh pr view`` nor ``gh repo view --json`` expose this setting; the
    REST repo object carries it as ``allow_auto_merge``, so it is queried via
    ``gh api``. Returns ``None`` when it could not be determined (``gh``
    failure, unparseable output) — the caller must treat that the same as
    "not available", never as "available": assuming availability is exactly
    OI-1386 (``--auto`` requested on a repo where the setting is off, and
    ``gh`` rejects the merge outright for it).
    """
    result = _gh(["api", "repos/{owner}/{repo}", "--jq", ".allow_auto_merge"])
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip().lower()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def _pr_actually_merged(pr_number: int) -> tuple[bool, str]:
    """Verify a PR is really merged — never trust a ``gh pr merge`` exit code alone.

    ``gh pr merge --auto`` exits 0 both when it merges immediately AND when it
    only *enables* auto-merge (the merge itself stays pending on required
    checks/reviews that have not landed yet). Both look identical on the exit
    code; only the PR's own state distinguishes them (OI-1399). An
    unqueryable PR state is treated as "not merged" — never a silent pass.
    """
    pr_data = _query_pr(pr_number)
    if pr_data is None:
        return False, (
            f"gh pr merge meldde succes voor #{pr_number}, maar de PR-state kon niet "
            "worden opgevraagd om dat te bevestigen: behandel dit als een mislukte merge"
        )
    state = (pr_data.get("state") or "").upper()
    if state == "MERGED":
        return True, ""
    return False, (
        f"gh pr merge meldde succes voor #{pr_number}, maar de PR staat nog op "
        f"state={state or 'onbekend'} (geen MERGED): vermoedelijk staat auto-merge nog "
        "te wachten op openstaande vereisten. Dit telt niet als een voltooide merge."
    )


def _do_merge(pr_number: int, method: str, head_sha: str = "") -> tuple[bool, str]:
    """Execute gh pr merge pinned to the approved head and return (success, error).

    ``head_sha`` is the exact commit the CI and review gates approved. It is
    passed as ``--match-head-commit`` so ``gh`` refuses to merge any other
    commit: a push to the branch after the gates ran moves the head, and the
    merge must then be refused (so the gates re-run) rather than silently merged.
    A head-moved refusal is reported as that — the system working — not as a
    generic ``gh`` failure.

    ``--auto`` is added only when the repo actually supports it
    (``_repo_auto_merge_allowed``) — see module docstring (OI-1386). A ``gh``
    exit 0 is not itself treated as success: the PR state is re-queried
    (``_pr_actually_merged``) and only ``state == "MERGED"`` counts (OI-1399).
    """
    method_flag = f"--{method}"
    args = ["pr", "merge", str(pr_number), method_flag]
    if head_sha:
        args += ["--match-head-commit", head_sha]
    if _repo_auto_merge_allowed():
        args = args + ["--auto"]

    result = _gh(args)
    if result.returncode != 0:
        raw = (result.stderr or result.stdout or "gh pr merge failed").strip()
        if head_sha and _is_head_moved_refusal(raw):
            return False, _head_moved_refusal_message(pr_number, head_sha, raw)
        return False, raw
    return _pr_actually_merged(pr_number)


def _emit_receipt(
    *,
    pr_number: int,
    dispatch_id: str,
    merge_method: str,
    pr_title: str,
    branch: str,
    pr_id: str = "",
    pr_id_resolution: str = "",
    receipts_file: Optional[str] = None,
    contract_invalid_override: Optional[Dict[str, Any]] = None,
    preflight_gates: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Write pr_merged receipt to t0_receipts.ndjson with dual-scheme linkage.

    Dual-scheme: pr_number (GitHub numeric) + pr_id (internal PR-N/PR-LABEL).
    pr_id_resolution='unmatched' when no internal label could be derived.

    ``contract_invalid_override`` (Golf B, B7): the audit field stamped onto
    this receipt when ``--override-contract-invalid`` was used to bypass a
    contract_invalid latest receipt — ``{"flag": "--override-contract-invalid",
    "reason": <str>}``. ``None`` (the normal case) omits the field entirely.

    ``preflight_gates`` (Golf Bx, D3): the five preflight verdicts that
    approved this merge, on THIS receipt rather than in a second gate
    registration beside it — one merge, one evidence record.

    The key is written ALWAYS, never omitted. Omitting it on a caller that ran
    no gates gave the absence three readings at once — a merge from before this
    field existed, a caller that skipped the door, and an empty list — over a
    ledger holding ~23k older ``pr_merged`` lines that carry none of it. That
    is the same argument ``VERDICT_NOT_EVALUATED`` exists for, applied to the
    list itself instead of only to entries inside it. A gateless caller gets
    the full five-record ledger with every verdict ``not_evaluated`` and
    ``GATES_NOT_RUN_MESSAGE`` as the reason, so a reader sees one uniform shape
    and field-presence alone separates a post-D3 receipt from a pre-D3 one.
    """
    kwargs: Dict[str, Any] = {
        "pr_number": pr_number,
        "conclusion": "merged",
        "merge_method": merge_method,
        "pr_title": pr_title,
        "branch": branch,
    }
    if pr_id:
        kwargs["pr_id"] = pr_id
    elif pr_id_resolution:
        kwargs["pr_id_resolution"] = pr_id_resolution
    if dispatch_id:
        kwargs["dispatch_id"] = dispatch_id
    if contract_invalid_override:
        kwargs["contract_invalid_override"] = contract_invalid_override
    kwargs["preflight_gates"] = preflight_gates or _preflight_ledger(
        [], unevaluated_message=GATES_NOT_RUN_MESSAGE,
    )
    return emit_governance_receipt(
        "pr_merged",
        receipt_kind="state_mutation",
        status="success",
        terminal="T0",
        source="pr_merge",
        receipts_file=receipts_file,
        **kwargs,
    )


def _emit_refusal_receipt(
    *,
    pr_number: int,
    dispatch_id: str,
    preflight_gates: list[Dict[str, Any]],
    refused_by: str,
    head_sha: str = "",
    receipts_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the ``pr_merge_refused`` receipt for a merge the door refused.

    Its OWN event_type, not a ``pr_merged`` with ``conclusion: "refused"`` —
    see the module docstring for the six readers that filter on the literal
    ``pr_merged`` and would each count a refusal as a merge.

    ``status="blocked"`` is taken from the canonical vocabulary in
    ``event_outcome_semantics`` (a governed failure literal) rather than a new
    literal like "refused": a status this ledger has never seen is refused by
    ``resolve_status_category`` on the write side, and adding one is a
    fleet-wide vocabulary change, not a merge-door change.
    ``receipt_kind="state_mutation"`` matches the ``pr_merged`` sibling — the
    merge door's attempt to mutate main, here with its outcome being "no".

    ``commit_sha`` and ``gate`` carry ADR-038 outcome identity. That ADR
    identifies an outcome by
    ``(dispatch_id, event_type, status, commit_sha, gate, pr_number)``
    (``outcome_identity.OUTCOME_ID_FIELDS``) and dedups against a durable index
    spanning the WHOLE ledger, not a time window. This receipt first shipped
    carrying only ``head_sha`` and ``refused_by`` — neither is in that tuple —
    with a constant ``status="blocked"``, so every refusal of one PR under one
    dispatch collapsed onto a single identity and ``_write_receipt_under_lock``
    dropped all but the first. Measured on an isolated ledger: a CI refusal on
    head aaa111 appended, a branch-protection refusal on head bbb222 returned
    ``duplicate``, one line total.

    ``commit_sha`` repeats ``head_sha`` deliberately rather than replacing it.
    ``head_sha`` is this record's own field, asserted by the D3 tests and
    echoed in the CLI's JSON; ``commit_sha`` is the fleet-canonical name that
    ``closure_verifier``, ``gate_recorder``, ``receipt_provenance`` and
    ``forge_gate_publisher`` already read, and the name ADR-038 hashes.

    ``gate`` is stamped ONLY alongside a real dispatch_id, and that condition
    is load-bearing in both directions. ``ghost_receipt_filter.is_gate_event``
    treats ANY non-empty ``gate`` as a headless gate event, and
    ``should_route_to_gate_stream`` then diverts the receipt to
    ``gate_events.ndjson`` whenever the dispatch_id is a ghost value — and
    ``""`` is one, which is exactly what ``_lookup_dispatch_id_by_pr_number``
    returns on a miss. Stamping it unconditionally would push the least
    traceable refusals straight out of the ledger this record exists to land
    in (measured: reroute False today, True with an unconditional ``gate``).
    Nothing is lost by the condition: ADR-038's durable check only runs when
    ``has_outcome_identity`` sees a real dispatch_id, so ``gate`` adds
    discrimination in precisely the case where discrimination is consulted.
    ``refused_by`` carries the same gate name unconditionally for readers.
    """
    kwargs: Dict[str, Any] = {
        "pr_number": pr_number,
        "conclusion": "refused",
        "refused_by": refused_by,
        "preflight_gates": preflight_gates,
        "head_sha": head_sha or "",
        "commit_sha": head_sha or "",
    }
    if dispatch_id:
        kwargs["dispatch_id"] = dispatch_id
        kwargs["gate"] = refused_by
    return emit_governance_receipt(
        "pr_merge_refused",
        receipt_kind="state_mutation",
        status="blocked",
        terminal="T0",
        source="pr_merge",
        receipts_file=receipts_file,
        **kwargs,
    )


def _refuse_merge(
    *,
    pr_number: int,
    dispatch_id: str,
    gate_name: str,
    gate: Dict[str, Any],
    evaluated: list[Dict[str, Any]],
    head_sha: str,
    json_output: bool,
    json_key: str,
    dry_run: bool,
    receipts_file: Optional[str] = None,
) -> int:
    """Record a refused merge and return the CLI's exit code.

    Called from every one of the five NO-GO branches in ``main()``; each of
    them still does its own ``return`` on the value this produces, so the
    short-circuit is untouched — this function never runs a gate.

    Writing the record must not be able to turn a refusal into a crash: a
    failing emit is reported loudly on stderr and the refusal still exits
    EXIT_ERROR. The receipt's status travels back in the JSON payload so an
    unwritten record is visible to a machine reader too, not just in the log.

    ``duplicate`` stays inside ``_RECEIPT_OK_STATUSES`` here, and that is a
    decision rather than an oversight. Before ADR-038 identity was stamped on
    this receipt (see ``_emit_refusal_receipt``), ``duplicate`` was reachable
    for any second refusal of the same PR and meant "an unrelated earlier
    refusal swallowed this one" — a silent evidence loss that the OK-list was
    hiding. With ``commit_sha`` and ``gate`` on the tuple it is reachable only
    when dispatch_id, PR, gate, head AND status are all identical, i.e. the
    operator re-ran the door and got the very same refusal back. The ledger
    already holds a byte-equivalent evidence line for that outcome, so ADR-038
    suppressing the copy is the correct one-outcome-one-line behaviour and not
    a fault to warn about. It remains visible to a machine reader either way:
    ``refusal_receipt_status`` reports it verbatim in the JSON payload.
    """
    ledger = _preflight_ledger(evaluated, refused_by=gate_name)

    receipt_status = "skipped: dry-run"
    if not dry_run:
        resolved_dispatch_id = dispatch_id or _lookup_dispatch_id_by_pr_number(pr_number)
        try:
            receipt = _emit_refusal_receipt(
                pr_number=pr_number,
                dispatch_id=resolved_dispatch_id,
                preflight_gates=ledger,
                refused_by=gate_name,
                head_sha=head_sha,
                receipts_file=receipts_file,
            )
            receipt_status = (receipt or {}).get("append_status", "unknown")
        # Broad by intent: a failed record must never mask the refusal itself.
        except Exception as exc:
            receipt_status = f"error: {exc}"
        if receipt_status not in _RECEIPT_OK_STATUSES:
            print(
                f"WARN: PR #{pr_number} was refused by the {gate_name} preflight, but the "
                f"pr_merge_refused record did NOT land (append_status={receipt_status!r}). "
                f"The refusal itself stands; only its audit-trail evidence is missing.",
                file=sys.stderr,
            )

    if json_output:
        print(json.dumps({
            "success": False,
            "pr_number": pr_number,
            "error": gate["message"],
            json_key: gate,
            "refused_by": gate_name,
            "preflight_gates": ledger,
            "refusal_receipt_status": receipt_status,
        }, indent=2))
    else:
        print(f"NO-GO: {gate['message']}", file=sys.stderr)
    return EXIT_ERROR


def _emit_register_event(
    *,
    pr_number: int,
    dispatch_id: str,
    merge_method: str,
) -> bool:
    """Write pr_merged event to dispatch_register.ndjson. Best-effort, never raises."""
    try:
        from dispatch_register import append_event
        return append_event(
            "pr_merged",
            pr_number=pr_number,
            dispatch_id=dispatch_id or "",
            terminal="T0",
            extra={"merge_method": merge_method, "conclusion": "merged"},
        )
    except Exception:
        return False


def merge_pr(
    pr_number: int,
    *,
    dispatch_id: str = "",
    merge_method: str = "squash",
    dry_run: bool = False,
    receipts_file: Optional[str] = None,
    head_sha: str = "",
    pr_data: Optional[Dict[str, Any]] = None,
    contract_invalid_override: Optional[Dict[str, Any]] = None,
    preflight_gates: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Merge a PR and emit audit trail.

    ``contract_invalid_override`` (Golf B, B7): forwarded verbatim onto the
    ``pr_merged`` receipt's ``contract_invalid_override`` field when set —
    see ``_emit_receipt`` and ``_run_contract_invalid_gate``. ``None``
    (default) omits the field, matching a merge whose contract_invalid gate
    was never overridden.

    ``preflight_gates`` (Golf Bx, D3): the five preflight verdicts ``main()``
    collected on the way here, forwarded onto the ``pr_merged`` receipt.
    ``None`` (default) omits the field for callers that never ran the gates.

    ``head_sha`` is the exact commit the gates approved; it is threaded into
    ``gh pr merge --match-head-commit`` so the merge refuses any other commit.
    It is established once by ``_run_ci_gate`` and must not be re-fetched here
    (a second fetch would be a second source for the same identity). Empty
    (default) preserves the pre-pinning behavior for callers that never ran a
    gate.

    ``pr_data`` lets the caller pass PR metadata it already fetched (``main()``
    already has it from ``_run_ci_gate``) so this does not re-query ``gh`` a
    third time per invocation for the same PR just to read title/branch.
    ``None`` (default) preserves the previous self-fetching behavior for
    callers that never ran a gate.

    Returns a dict with keys: success, pr_number, dispatch_id, merge_method,
    pr_title, branch, receipt_status, receipt_ok, register_ok, error.

    ``success`` means "the merge happened on GitHub" — it is NOT the CLI
    verdict. A merge whose receipt could not be written has ``success=True``
    but ``receipt_ok=False``, and the CLI exits non-zero (OI-1518). Use
    ``receipt_ok`` to know whether the binding audit-trail evidence landed.
    """
    result: Dict[str, Any] = {
        "success": False,
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
        "merge_method": merge_method,
        "pr_title": "",
        "branch": "",
        "receipt_status": None,
        "receipt_ok": False,
        "register_ok": False,
        "error": "",
        "dry_run": dry_run,
        "overlaps": [],
    }

    # PR metadata for the receipt (title/branch). Reuse what the caller
    # already fetched when given; otherwise fetch it here (pre-pinning
    # behavior for callers that never ran a gate).
    if pr_data is None:
        pr_data = _query_pr(pr_number)
    if pr_data:
        result["pr_title"] = pr_data.get("title", "")
        result["branch"] = pr_data.get("headRefName", "")

    # OI-1091: warn (never block) when another OPEN dispatch branch touches the same files as
    # the branch being merged. Best-effort; a git/network failure degrades to no warning.
    # OI-1641: the scan itself is now bounded (one bulk fetch + one bulk gh lookup instead of a
    # fetch/gh-call per branch, plus a per-command timeout and a wall-clock scan ceiling) so it
    # can no longer turn into minutes of serial network round-trips on every merge.
    if result["branch"]:
        try:
            from file_scope_overlap import warn_overlaps  # noqa: PLC0415
            result["overlaps"] = warn_overlaps(result["branch"], repo=SCRIPT_DIR.parent)
        except Exception as exc:  # noqa: BLE001 — an overlap check must never block a merge
            log.warning("file-scope overlap check failed for PR #%s: %s", pr_number, exc)

    if dry_run:
        result["success"] = True
        result["error"] = "dry_run: no merge executed"
        print(f"[dry-run] Would merge PR #{pr_number} via {merge_method}")
        if dispatch_id:
            print(f"[dry-run] dispatch_id: {dispatch_id}")
        return result

    # Execute the merge, pinned to the head the gates approved.
    ok, err = _do_merge(pr_number, merge_method, head_sha)
    if not ok:
        result["error"] = err
        print(f"ERROR: gh pr merge failed for #{pr_number}: {err}", file=sys.stderr)
        return result

    result["success"] = True

    # Derive internal PR-N label from title for dual-scheme receipt
    pr_id = _extract_pr_id(result["pr_title"] or result["branch"])
    if not dispatch_id:
        dispatch_id = _lookup_dispatch_id_by_pr_number(pr_number)
    result["dispatch_id"] = dispatch_id

    # Emit receipt to t0_receipts.ndjson. OI-1518: the receipt is BINDING — a
    # merge whose proof could not be written must NOT exit 0. Two failure
    # shapes both land here: _emit_receipt raising (handled below), and
    # _emit_receipt returning an `append_status` that is not a recognized
    # success value (including a missing key -> "unknown" default). Both set
    # receipt_ok=False so the CLI exits non-zero. The register event stays
    # best-effort and is not touched.
    try:
        receipt = _emit_receipt(
            pr_number=pr_number,
            dispatch_id=dispatch_id,
            merge_method=merge_method,
            pr_title=result["pr_title"],
            branch=result["branch"],
            pr_id=pr_id or "",
            pr_id_resolution="" if pr_id else "unmatched",
            receipts_file=receipts_file,
            contract_invalid_override=contract_invalid_override,
            preflight_gates=preflight_gates,
        )
        append_status = (receipt or {}).get("append_status", "unknown")
        result["receipt_status"] = append_status
        result["receipt_ok"] = append_status in _RECEIPT_OK_STATUSES
        if not result["receipt_ok"]:
            print(
                f"FATAL: merge happened for PR #{pr_number} but the receipt did NOT "
                f"land (append_status={append_status!r}). The merge on GitHub is "
                f"irreversible. Re-run `python3 scripts/pr_merge.py --pr {pr_number}` "
                f"after fixing the receipts file so the audit-trail evidence is captured.",
                file=sys.stderr,
            )
    except Exception as exc:
        result["receipt_status"] = f"error: {exc}"
        result["receipt_ok"] = False
        print(
            f"FATAL: merge happened for PR #{pr_number} but the receipt could NOT be "
            f"written: {exc}. The merge on GitHub is irreversible. Re-run "
            f"`python3 scripts/pr_merge.py --pr {pr_number}` after fixing the error "
            f"so the audit-trail evidence is captured.",
            file=sys.stderr,
        )

    # Emit event to dispatch_register.ndjson (best-effort)
    result["register_ok"] = _emit_register_event(
        pr_number=pr_number,
        dispatch_id=dispatch_id,
        merge_method=merge_method,
    )

    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge a PR and emit pr_merged receipt + register event",
    )
    parser.add_argument("--pr", type=int, required=True, help="GitHub PR number")
    parser.add_argument(
        "--dispatch-id", default="",
        help="Dispatch-ID to link this merge to a receipt chain",
    )
    merge_group = parser.add_mutually_exclusive_group()
    merge_group.add_argument("--squash", dest="merge_method", action="store_const", const="squash", default=None)
    merge_group.add_argument("--rebase", dest="merge_method", action="store_const", const="rebase")
    merge_group.add_argument("--merge", dest="merge_method", action="store_const", const="merge")
    parser.add_argument("--dry-run", action="store_true", help="Skip merge and receipt write")
    parser.add_argument(
        "--override-reason", default=None,
        help="Escape hatch: skip the CI gate with this required reason (empty is refused). "
             "Also read from VNX_MERGE_OVERRIDE_REASON.",
    )
    parser.add_argument(
        "--override-contract-invalid", default=None,
        help="Escape hatch (golf B, B7): accept a dispatch whose latest receipt is "
             "contract_invalid, with this required reason (empty is refused). Separate "
             "from --override-reason. Covers ONLY that refusal: an unreadable ledger "
             "or an unresolvable dispatch_id stands regardless of this flag.",
    )
    parser.add_argument(
        "--allow-weaken", default=None,
        help="Escape hatch (golf B, B1): accept a PR that weakens "
             "scripts/forge/branch_protection.yaml relative to main, with this required "
             "reason (empty is refused). No other override applies to this gate.",
    )
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    args = parser.parse_args(argv)

    method = args.merge_method or "squash"

    # Golf Bx, D3: every preflight's own verdict, appended as it is decided.
    # The door still returns on the FIRST NO-GO — this list is what the gates
    # after that one are recorded as `not_evaluated` from, never a reason to
    # keep running them.
    evaluated: list[Dict[str, Any]] = []

    # ── Merge gate: VNX CI conclusion=success for the exact PR head ──────
    gate, pr_data = _run_ci_gate(args.pr, override_reason=args.override_reason)
    # The head SHA the gates approve is established once, here: the merge is
    # pinned to it (--match-head-commit) and every preflight record names it.
    head_sha = (pr_data or {}).get("headRefOid") or ""
    evaluated.append(_preflight_record("ci", gate, head_sha))

    def refuse(gate_name: str, gate_result: Dict[str, Any], json_key: str) -> int:
        return _refuse_merge(
            pr_number=args.pr,
            dispatch_id=args.dispatch_id or "",
            gate_name=gate_name,
            gate=gate_result,
            evaluated=evaluated,
            head_sha=head_sha,
            json_output=args.json,
            json_key=json_key,
            dry_run=args.dry_run,
        )

    if gate["verdict"] != "GO":
        return refuse("ci", gate, "ci_gate")
    if gate.get("overridden"):
        print(f"OVERRIDE: {gate['message']}")
    else:
        print(f"CI gate: {gate['message']}")

    # ── Review gate: a passing, evidenced review-gate result must exist ────
    review_gate, _ = _run_review_gate(args.pr, override_reason=args.override_reason)
    evaluated.append(_preflight_record("review", review_gate, head_sha))
    if review_gate["verdict"] != "GO":
        return refuse("review", review_gate, "review_gate")
    if review_gate.get("overridden"):
        print(f"OVERRIDE: {review_gate['message']}")
    else:
        print(f"Review gate: {review_gate['message']}")

    # ── ADR-number preflight: an added ADR file must not collide with a ────
    # number already on main (Golf B, B6). No override — always a refusal.
    adr_gate = _run_adr_gate(args.pr, pr_data=pr_data)
    evaluated.append(_preflight_record("adr", adr_gate, head_sha))
    if adr_gate["verdict"] != "GO":
        return refuse("adr", adr_gate, "adr_gate")
    print(f"ADR gate: {adr_gate['message']}")

    # ── contract_invalid gate: the latest OUTCOME receipt for the dispatch ──
    # must not be contract_invalid (Golf B, B7). A missing --dispatch-id is
    # resolved from the PR number first, so omitting the flag is not a bypass.
    contract_gate = _run_contract_invalid_gate(
        args.dispatch_id,
        pr_number=args.pr,
        override_reason=args.override_contract_invalid,
    )
    evaluated.append(_preflight_record("contract_invalid", contract_gate, head_sha))
    if contract_gate["verdict"] != "GO":
        return refuse("contract_invalid", contract_gate, "contract_invalid_gate")
    if contract_gate.get("overridden"):
        print(f"OVERRIDE: {contract_gate['message']}")
    else:
        print(f"contract_invalid gate: {contract_gate['message']}")

    # ── Branch-protection preflight (Golf B, B1): main's live protection ───
    # must match scripts/forge/branch_protection.yaml, and this PR must not
    # weaken that YAML relative to main. Only --allow-weaken overrides a
    # weakening; drift between live and main's own YAML has no override.
    protection_gate = _run_branch_protection_gate(
        args.pr, pr_data=pr_data, allow_weaken_reason=args.allow_weaken,
    )
    evaluated.append(_preflight_record("branch_protection", protection_gate, head_sha))
    if protection_gate["verdict"] != "GO":
        return refuse("branch_protection", protection_gate, "branch_protection_gate")
    if protection_gate.get("overridden"):
        print(f"OVERRIDE: {protection_gate['message']}")
    else:
        print(f"Branch-protection gate: {protection_gate['message']}")

    # All five ran and approved: the ledger pads nothing here.
    preflight_gates = _preflight_ledger(evaluated)

    contract_invalid_override = (
        {"flag": "--override-contract-invalid", "reason": contract_gate["override_reason"]}
        if contract_gate.get("overridden") else None
    )

    result = merge_pr(
        pr_number=args.pr,
        dispatch_id=args.dispatch_id or "",
        merge_method=method,
        dry_run=args.dry_run,
        head_sha=head_sha,
        pr_data=pr_data,
        contract_invalid_override=contract_invalid_override,
        preflight_gates=preflight_gates,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    elif args.dry_run:
        # dry-run: no merge, no receipt, exit 0 (unchanged behavior).
        print(f"OK: PR #{args.pr} (dry-run) via {method}")
    elif result["success"] and result["receipt_ok"]:
        # merge gebeurd + receipt geland -> exit 0
        print(f"OK: PR #{args.pr} merged via {method}")
        if result.get("receipt_status"):
            print(f"    receipt: {result['receipt_status']}")
        print(f"    register: {'ok' if result['register_ok'] else 'warn-not-written'}")
    elif result["success"] and not result["receipt_ok"]:
        # merge gebeurd + receipt NIET geland -> non-zero, tekst maakt
        # onmiskenbaar duidelijk dat de merge wél is doorgegaan en alleen
        # het bewijs ontbreekt (OI-1518). De FATAL-regel is al op stderr
        # gezet in merge_pr(); hier de korte samenvatting op stdout.
        print(
            f"ERROR: PR #{args.pr} WAS MERGED but the receipt did not land "
            f"(status={result.get('receipt_status')!r}). The merge is irreversible; "
            f"only the audit-trail proof is missing. See stderr.",
            file=sys.stderr,
        )
    else:
        # merge niet gebeurd -> non-zero, bestaande tekst
        print(f"ERROR: {result['error']}", file=sys.stderr)

    # CLI verdict (OI-1518): exit 0 only on dry-run, or on a merge whose
    # binding receipt actually landed. A merge without proof is non-zero.
    if args.dry_run:
        return EXIT_OK
    if result["success"] and result["receipt_ok"]:
        return EXIT_OK
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
