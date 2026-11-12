"""Deltareview DR-5: the shadow delta measured against the full codex round, per pair (ADR-064 "Drempel").

A pair is a shadow line with a ``delta_verdict`` whose head has a codex round in the ledger inside the window.
A missed blocker is a blocker the full round found and the delta did not. Evidence that cannot be read makes a
pair ``unmeasurable``: listed, never counted as a pair and never as zero missed (ADR-063).
"""
from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import review_rounds
from codex_parser import extract_verdict_block
from review_delta import _blocking_new
from review_delta_shadow import read_shadow, shadow_path
from review_findings import id_for_finding, symbolic_context

PASS, FAIL, INSUFFICIENT = "pass", "fail", "insufficient"
MIN_PAIRS, MIN_PLANTED, LINE_WINDOW, PLANTED_SCHEMA = 20, 10, 10, "delta_planted/1"
UNKNOWN = "unknown"
SourceOf = Callable[[str, str], Optional[str]]


class CompareError(ValueError):
    """Unusable input: a window or manifest that cannot be read."""


def parse_time(text: str, *, now: Optional[datetime] = None) -> datetime:
    """An ISO time with a zone, or ``Nd`` for N days before now."""
    raw = (text or "").strip()
    days = re.fullmatch(r"(\d+)d", raw)
    if days:
        return (now or datetime.now(timezone.utc)) - timedelta(days=int(days.group(1)))
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompareError(f"cannot read time {text!r}: use an ISO time with a zone or Nd") from exc
    if parsed.tzinfo is None:
        raise CompareError(f"time {text!r} has no zone")
    return parsed


def exact_interval(k: int, n: int, confidence: float = 0.95) -> Tuple[float, float]:
    """Two-sided exact (Clopper-Pearson) interval for k of n, by bisection on the binomial tail."""
    alpha = 1 - confidence

    def tail_ge(p: float) -> float:  # P(X >= k)
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))

    def solve(target: float, rising: bool, fn: Callable[[float], float]) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(100):
            mid = (lo + hi) / 2
            if (fn(mid) < target) == rising:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    lower = 0.0 if k == 0 else solve(alpha / 2, True, tail_ge)
    upper = 1.0 if k == n else solve(alpha / 2, False, lambda p: 1 - tail_ge(p) + math.comb(n, k) * p ** k * (1 - p) ** (n - k))
    return round(lower, 3), round(upper, 3)


def _near(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Same file and within LINE_WINDOW lines; when either side has no line the same file is enough."""
    if str(a.get("file_path") or "") != str(b.get("file_path") or ""):
        return False
    la, lb = a.get("line"), b.get("line")
    if not (isinstance(la, int) and isinstance(lb, int)) or isinstance(la, bool) or isinstance(lb, bool):
        return True
    return abs(la - lb) <= LINE_WINDOW


def _blocking_new_findings(line: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    found = line.get("new_findings")
    return [f for f in found if isinstance(f, Mapping) and _blocking_new(f)] if isinstance(found, list) else []


def full_blockers(round_line: review_rounds.RoundLine, source_of: SourceOf) -> List[Dict[str, Any]]:
    """The blockers of the full round with their ids; ``ValueError`` when the report cannot be read in full."""
    try:
        text = Path(round_line.report_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"report unreadable: {exc}") from exc
    block = extract_verdict_block(text)
    raw = block.get("findings") if isinstance(block, dict) else None
    if not isinstance(raw, list):
        raise ValueError("report holds no verdict block")
    out: List[Dict[str, Any]] = []
    for finding in (f for f in raw if isinstance(f, Mapping) and _blocking_new(f)):
        path = str(finding.get("file_path") or "")
        source = source_of(round_line.head_sha, path) if path else None
        if path and source is None:
            raise ValueError(f"source of {path} at {round_line.head_sha[:12]} unreadable")
        try:
            fid = id_for_finding(round_line.seat, finding, source)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"blocker without identity: {exc}") from exc
        out.append({**finding, "id": fid, "symbolic_context": symbolic_context(source, finding.get("line"))})
    if round_line.blocking_count > len(out):
        raise ValueError(f"ledger counts {round_line.blocking_count} blockers, the report yields {len(out)}")
    return out


def missed_blockers(blockers: Sequence[Mapping[str, Any]], line: Mapping[str, Any]) -> Tuple[List[str], List[Mapping[str, Any]]]:
    """(ids of the full round's blockers the delta missed, delta blockers the full round did not have)."""
    raw_statuses = line.get("statuses")
    statuses = raw_statuses if isinstance(raw_statuses, dict) else {}
    delta_new, matched, missed = _blocking_new_findings(line), set(), []
    for blocker in blockers:
        bid = blocker["id"]
        if bid in statuses:
            if statuses[bid] not in ("open", "regressed"):  # fixed, or an answer we cannot read: a miss
                missed.append(bid)
            continue
        hits = [i for i, f in enumerate(delta_new) if _near(blocker, f)]
        matched.update(hits)
        if not hits:
            missed.append(bid)
    return missed, [f for i, f in enumerate(delta_new) if i not in matched]


@dataclass
class Comparison:
    pairs: List[Dict[str, Any]] = field(default_factory=list)
    unmeasurable: List[Dict[str, Any]] = field(default_factory=list)
    not_pairs: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    planted: Optional[List[Dict[str, Any]]] = None
    outcome: str = INSUFFICIENT
    reasons: List[str] = field(default_factory=list)
    missed_pairs: int = 0
    missed_blockers: int = 0
    interval: Optional[Tuple[float, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        by_depth = {d: len([p for p in self.pairs if p["depth"] == d]) for d in (1, 2)}
        return {"outcome": self.outcome, "reasons": self.reasons, "pairs": self.pairs, "pair_count": len(self.pairs),
                "pairs_by_depth": by_depth, "missed_pairs": self.missed_pairs, "missed_blockers": self.missed_blockers,
                "interval_95": list(self.interval) if self.interval else None, "unmeasurable": self.unmeasurable,
                "not_pairs": self.not_pairs, "planted": self.planted}


def load_planted(path: Path) -> List[Dict[str, Any]]:
    import json
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompareError(f"planted manifest {path} cannot be read: {exc}") from exc
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("schema") != PLANTED_SCHEMA or not isinstance(cases, list):
        raise CompareError(f"planted manifest {path} is not {PLANTED_SCHEMA} with a cases list")
    if not all(isinstance(c, dict) and isinstance(c.get("head"), str) and c.get("head") for c in cases):
        raise CompareError(f"planted manifest {path}: every case needs pr_number, head, file_path, line")
    return cases


def check_planted(cases: Sequence[Mapping[str, Any]], shadow: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Per case whether the shadow line on its head failed with a blocking new finding near the planted line."""
    results = []
    for case in cases:
        lines = [l for l in shadow if l.get("head") == case["head"] and l.get("delta_verdict") is not None
                 and (case.get("pr_number") is None or l.get("pr_number") == case.get("pr_number"))]
        found = any(l.get("delta_verdict") == "fail" and any(_near(case, f) for f in _blocking_new_findings(l)) for l in lines)
        results.append({**case, "found": found})
    return results


def decide(cmp: Comparison) -> None:
    """Set outcome and reasons: any miss or unfound planted case fails; too little evidence is insufficient."""
    failing = [f"{cmp.missed_blockers} missed blocker(s) in {cmp.missed_pairs} pair(s)"] if cmp.missed_blockers else []
    unfound = [c for c in cmp.planted or [] if not c["found"]]
    if unfound:
        failing.append(f"{len(unfound)} planted case(s) not found")
    thin = [f"{len(cmp.pairs)} pairs, at least {MIN_PAIRS} needed"] if len(cmp.pairs) < MIN_PAIRS else []
    if cmp.planted is None:
        thin.append("no planted manifest")
    elif len(cmp.planted) < MIN_PLANTED:
        thin.append(f"{len(cmp.planted)} planted cases, at least {MIN_PLANTED} needed")
    cmp.outcome, cmp.reasons = (FAIL, failing) if failing else (INSUFFICIENT, thin) if thin else (PASS, [])


def _in_window(stamp: str, since: datetime, until: Optional[datetime]) -> Optional[bool]:
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if when.tzinfo is None:
        return None
    return when >= since and (until is None or when <= until)


def git_source_of(repo: Path) -> SourceOf:
    def read(head: str, path: str) -> Optional[str]:
        done = subprocess.run(["git", "show", f"{head}:{path}"], cwd=str(repo), capture_output=True, text=True, timeout=30)
        return done.stdout if done.returncode == 0 else None
    return read


def compare(state_dir: Path, *, since: datetime, until: Optional[datetime] = None, source_of: SourceOf,
            planted: Optional[Sequence[Mapping[str, Any]]] = None) -> Comparison:
    gates = Path(state_dir) / "review_gates"
    ledger = review_rounds.read_ledger(review_rounds.ledger_path(gates))
    project = review_rounds.store_project_id(gates)
    shadow = read_shadow(shadow_path(gates))
    delta_heads = {(l.get("pr_number"), l.get("head")) for l in shadow if l.get("delta_verdict") is not None}
    cmp = Comparison()
    for line in shadow:
        if line.get("delta_verdict") is None:
            reason = str(line.get("full_review_reason") or (line.get("reasons") or ["unreadable"])[0])
            cmp.not_pairs.setdefault(reason, []).append({"pr_number": line.get("pr_number"), "head": line.get("head")})
            continue
        pr, head = line.get("pr_number"), str(line.get("head") or "")
        ident = {"pr_number": pr, "head": head}
        rounds = review_rounds.codex_rounds(ledger.for_pr(project, pr)) if isinstance(pr, int) else ()
        rnd = next((r for r in rounds if r.head_sha == head), None)
        if rnd is None:
            cmp.unmeasurable.append({**ident, "reason": "no codex round in the ledger for this head"})
            continue
        inside = _in_window(rnd.recorded_at, since, until)
        if inside is False:
            continue
        try:
            if inside is None:
                raise ValueError(f"ledger time {rnd.recorded_at!r} unreadable")
            blockers = full_blockers(rnd, source_of)
        except ValueError as exc:
            cmp.unmeasurable.append({**ident, "reason": str(exc)})
            continue
        missed, extra = missed_blockers(blockers, line)
        depth = 2 if (pr, line.get("parent_head")) in delta_heads else 1
        cmp.pairs.append({**ident, "depth": depth, "full_blockers": len(blockers), "missed": missed,
                          "extra_delta_blockers": len(extra), "delta_duration_seconds": line.get("duration_seconds"),
                          "full_duration_seconds": UNKNOWN, "tokens": line.get("tokens", UNKNOWN)})
    cmp.missed_blockers = sum(len(p["missed"]) for p in cmp.pairs)
    cmp.missed_pairs = len([p for p in cmp.pairs if p["missed"]])
    if cmp.pairs:
        cmp.interval = exact_interval(cmp.missed_pairs, len(cmp.pairs))
    cmp.planted = check_planted(planted, shadow) if planted is not None else None
    decide(cmp)
    return cmp
