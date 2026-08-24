"""gate_report_recovery.py — bounded companion-report search for glm_gate/kimi_gate
(dispatch-20260823-beta2-j: "de tweede rapportput").

The bug this recovers from: a review-gate worker (glm-harness/kimi, under the OLD
role="plan-reviewer") sometimes wrote its full review — INCLUDING the verdict
fence — to a second, self-chosen report file instead of (or as well as) answering
inline, because agents/plan-reviewer/CLAUDE.md's "write your report to the
mandated path" instruction contradicted the gate's own inline-```json```-fence
contract. glm_gate.py/kimi_gate.py now dispatch under role="review-gate" (see
agents/review-gate/CLAUDE.md), which removes that contradiction for NEW runs —
this module is the bounded-search fallback for runs where it still happens (a
model can always choose to write a file anyway) and the reprocessing path for
runs that already happened before the fix shipped.

Measured 23-08 across four real runs (three glm, one kimi) whose worker wrote a
full review WITH the verdict fence to a second file, 7-15 seconds BEFORE the
harness wrote its own (fence-less) report:

    tweede put (fence)                           gate-rapport (no fence)
    20260823-pr1672-1787474011-gate-review.md    glm-gate-pr1672-1787474011.md
    pr1674-glm-gate-review.md                    glm-gate-pr1674-1787474864.md
    kimi-gate-pr1674-ledger-health-launchd.md     kimi-gate-pr1674-1787475223.md
    pr-1674-glm-gate-review.md                    glm-gate-pr1674-1787475223.md

The four companion filenames share NO common substring pattern (timestamp vs no
timestamp, extra dash after "pr" vs none, dispatch slug vs PR number only) — a
path DERIVATION (guessing the companion's name from the dispatch_id) finds
exactly one of these four and misses the other three. This module searches on
PROPERTIES instead: a candidate is a ``.md`` file under ``unified_reports/``
that (1) carries a ```json``` fence with a real ``verdict`` key, (2) mentions
this dispatch's PR number in its filename, (3) has an mtime inside the run's own
time window, and (4) is not the gate's own report file.

Fail-closed on ambiguity (>=2 candidates): two gates racing on the same PR at
the same time is a real scenario, and the mtime-window heuristic alone cannot
tell their companion files apart — refuse rather than guess.  Zero candidates
is not a failure of this module: it means no recoverable evidence exists, and
the caller must never synthesize a verdict from that absence.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_VALID_VERDICTS = {"pass", "fail", "blocked"}

# The exact same fence-scan rule glm_gate._extract_verdict / kimi_gate._extract_verdict
# use, duplicated here on purpose (not imported cross-module) — this module's job is
# CANDIDATE SELECTION (does this file carry a real verdict at all), not verdict
# INTERPRETATION, but the two gates' own extractors must never diverge from each
# other, and this module must never diverge from either: all three scan for the
# same shape (a fenced ```json``` block whose "verdict" is pass/fail/blocked).
_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass(frozen=True)
class RecoveryCandidate:
    path: Path
    verdict: dict
    text: str


class AmbiguousRecoveryCandidates(RuntimeError):
    """Raised when the bounded search finds 2+ candidates — fail-closed, never guess."""

    def __init__(self, candidates: "list[Path]"):
        self.candidates = list(candidates)
        joined = ", ".join(str(p) for p in self.candidates)
        super().__init__(
            f"{len(self.candidates)} recovery candidates found (expected at most 1), "
            f"refusing to guess which one is the real verdict: {joined}"
        )


# The plan-reviewer role's OWN verdict fence label and vocabulary
# (agents/plan-reviewer/CLAUDE.md lines 27-37). Recognized here because a
# review-gate dispatch that inherited (or, pre-fix, always inherited) that
# role's contract can answer under THIS label instead of the gate's own
# ```json``` — measured 23-08 on glm-gate-pr1677-1787477675.md: glm delivered
# a complete, well-formed verdict block, inline, at the end of its response,
# just fenced ```vnx-plan-verdict``` instead of ```json```. Recognizing this
# label when reading a review-gate's OWN dispatch response is a different
# thing from the plan-gate's anti-spoofing neutralization of the same label
# in UNTRUSTED document text (plan_gate_panel._sanitize_doc) — that guards
# against a document injecting a fake verdict into what the plan-gate reads;
# this only widens what a review gate accepts as OUTPUT of its own dispatch.
_PLAN_VERDICT_FENCE_RE = re.compile(r"```vnx-plan-verdict\s*(\{.*?\})\s*```", re.DOTALL)

# plan-reviewer's verdict vocabulary (pass/revise/block) mapped onto the
# review-gate's own (pass/fail/blocked):
#   "block"  -> "blocked"  — direct semantic match: both mean "a fundamental
#                            flaw makes this unsafe to proceed with as-is".
#   "revise" -> "fail"     — plan-reviewer's own rubric defines "revise" as
#                            "real, fixable gaps remain" (agents/plan-reviewer
#                            /CLAUDE.md) — that is explicitly NOT nothing, so
#                            it must never silently resolve to "pass". The
#                            review-gate has no middle verdict, so "revise"
#                            maps to the conservative side: a diff with real,
#                            named gaps does not clear the gate silently.
#   "pass"   -> "pass"     — identical meaning in both rubrics.
_PLAN_VERDICT_WORD_MAP = {"pass": "pass", "revise": "fail", "block": "blocked"}


def _translate_plan_verdict(obj: dict) -> "Optional[dict]":
    """Translate a parsed ```vnx-plan-verdict``` payload into the review-gate's
    own verdict shape ({verdict, findings, residual_risk}), or None when *obj*
    is not a recognizable plan-verdict payload (unknown verdict word)."""
    if not isinstance(obj, dict):
        return None
    mapped = _PLAN_VERDICT_WORD_MAP.get(str(obj.get("verdict", "")).strip().lower())
    if mapped is None:
        return None
    findings = [
        {"severity": "error", "message": str(item)}
        for item in (obj.get("blocking_findings") or [])
        if str(item).strip()
    ]
    return {
        "verdict": mapped,
        "findings": findings,
        "residual_risk": obj.get("rationale") or None,
    }


def extract_relabeled_verdict(text: str) -> dict:
    """Return a verdict translated from the LAST valid ```vnx-plan-verdict```
    fence in *text* (see ``_translate_plan_verdict``), or {} if none. Callers
    try the gate's own ```json``` fence first — this is the fallback for a
    response that answered under the plan-reviewer role's label instead."""
    blocks = _PLAN_VERDICT_FENCE_RE.findall(text or "")
    for block in reversed(blocks):
        try:
            obj = json.loads(block)
        except (ValueError, TypeError):
            continue
        translated = _translate_plan_verdict(obj)
        if translated:
            return translated
    return {}


def _extract_verdict_block(text: str) -> dict:
    """Return the last fenced ```json``` block whose ``verdict`` is pass/fail/blocked,
    or {} if none. See module docstring for why this is a deliberate duplicate of
    glm_gate._extract_verdict / kimi_gate._extract_verdict rather than an import."""
    blocks = _FENCE_RE.findall(text or "")
    for block in reversed(blocks):
        try:
            obj = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and str(obj.get("verdict", "")).strip().lower() in _VALID_VERDICTS:
            return obj
    return {}


def _pr_token_pattern(pr_id: str) -> "re.Pattern[str]":
    """Match "pr<N>" or "pr-<N>" (case-insensitive) as a filename substring, not
    followed by another digit — matches all four measured companion filenames
    (see module docstring) without matching e.g. pr16720 for pr=1672."""
    return re.compile(rf"(?i)\bpr-?{re.escape(str(pr_id).strip())}(?!\d)")


def find_recovery_candidate(
    unified_reports_dir: Path,
    *,
    pr_id: str,
    exclude_name: str,
    window_start: float,
    window_end: float,
) -> Optional[RecoveryCandidate]:
    """Bounded search for a companion report carrying the verdict this dispatch's
    own (harness-captured) report is missing.

    A candidate is a ``.md`` file directly under *unified_reports_dir* that:
      1. carries a parseable verdict block — either the gate's own ```json```
         fence or the plan-reviewer role's ```vnx-plan-verdict``` fence
         (translated; see ``extract_relabeled_verdict``),
      2. mentions *pr_id* in its filename (a "pr<N>"/"pr-<N>" token),
      3. has an mtime in the inclusive window [*window_start*, *window_end*], and
      4. is not *exclude_name* (the gate's own report for this exact dispatch).

    Returns ``None`` on zero matches (absence of evidence — not an error).
    Raises ``AmbiguousRecoveryCandidates`` on 2+ matches (fail-closed).
    """
    if not str(pr_id or "").strip():
        return None
    if not unified_reports_dir.is_dir():
        return None

    pattern = _pr_token_pattern(pr_id)
    candidates: "list[RecoveryCandidate]" = []
    for path in sorted(unified_reports_dir.iterdir()):
        if not path.is_file() or path.suffix != ".md":
            continue
        if path.name == exclude_name:
            continue
        if not pattern.search(path.name):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < window_start or mtime > window_end:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        verdict = _extract_verdict_block(text) or extract_relabeled_verdict(text)
        if not verdict:
            continue
        candidates.append(RecoveryCandidate(path=path, verdict=verdict, text=text))

    if len(candidates) > 1:
        raise AmbiguousRecoveryCandidates([c.path for c in candidates])
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Deel 3 precondition #3: a second source may FORMALIZE a verdict the primary
# response already implied, never PRODUCE one the primary response contradicts.
# ---------------------------------------------------------------------------

_LOOSE_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(pass|fail|blocked)"', re.IGNORECASE)
_LOOSE_SEVERITY_RE = re.compile(r'"severity"\s*:\s*"(error|blocked|blocker)"', re.IGNORECASE)

# Matches glm_gate._verdict_to_status / kimi_gate._verdict_to_status's own
# "blocking finding" severity filter exactly — must never diverge from it.
_BLOCKING_SEVERITIES = {"error", "blocked", "blocker"}


def recovered_verdict_conflicts(primary_text: str, recovered_verdict: dict) -> "Optional[str]":
    """Return a human-readable conflict description, or ``None`` if the recovered
    verdict does not contradict *primary_text* (the run's own harness-captured
    response — the one whose ```json``` fence failed to parse cleanly, which is
    WHY this module was consulted at all).

    This is a LOOSE, non-strict scan of *primary_text* for verdict words and
    blocking-severity mentions that survive even a malformed/truncated fence —
    a belt-and-suspenders check, not a second parser: if a strict fence had
    parsed cleanly, the caller would never have reached recovery in the first
    place. A recovered companion file may only ever add evidence the primary
    response is silent about; it may never override what the primary response
    already, however messily, said.
    """
    verdict_words = [m.group(1).lower() for m in _LOOSE_VERDICT_RE.finditer(primary_text or "")]
    blocking_mentions = len(_LOOSE_SEVERITY_RE.findall(primary_text or ""))

    recovered_v = str(recovered_verdict.get("verdict", "")).strip().lower()
    recovered_blocking = len([
        f for f in (recovered_verdict.get("findings") or [])
        if isinstance(f, dict) and str(f.get("severity", "")).strip().lower() in _BLOCKING_SEVERITIES
    ])

    disagreeing = sorted({w for w in verdict_words if w != recovered_v})
    if disagreeing:
        return (
            f"primary response contains verdict word(s) {disagreeing!r} that disagree "
            f"with the recovered verdict {recovered_v!r}"
        )
    if blocking_mentions > 0 and recovered_blocking == 0:
        return (
            f"primary response mentions {blocking_mentions} blocking-severity marker(s) "
            "but the recovered verdict carries zero blocking findings"
        )
    return None
