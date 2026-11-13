#!/usr/bin/env python3
"""rework_metrics.py - first-pass yield, rework within 14 days and passed-through rework (ADR-071).

Read-only: one entry point, :func:`compute_metrics`, returns a plain dict for the H7 report and
writes nothing. Definitions are those of ADR-071 besluit 5, 6, 10, 11, 12 and 14:

* merge facts come from the ``pr_merged`` receipt, else an injected GitHub reader, else the
  delivery is ``unattributed`` (never a commit date or a builder receipt time);
* the unit is the delivery ``dlv-<hex>`` with ``-rN``, ``-a`` and ``-b`` folded in, counted for
  the model of its first dispatch; two models for one dispatch id give ``model=conflict``;
* an origin counts in a 14-day measure only when merged at least 14 days before ``as_of``;
  younger ones are ``immature`` per cell; ``churn`` edges never count;
* the cost axis (:func:`cost_cells`) reads the cost ledger through ``cost_ledger.aggregate_runs`` and
  names every gap (member without a builder run, unpriced run, unlinked review run, no changed lines,
  no lead time) instead of counting it as zero; the weekly quota share is ``unmeasured``;
* a cell below ``MIN_N`` carries counts and ``pct: None`` and no ``ci95``; from ``MIN_N`` up a cell
  carries ``ci95: [low, high]``, the 95% Wilson interval of the same ``count`` and ``n`` as its ``pct``.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cost_ledger
from receipt_outcome import _clean_pr, _parse_ts, noise_reason
from review_rounds import RoundLedgerError, read_ledger
from rework_attribution import ROUND_CAUSES, load_edges

WINDOW_DAYS = 14
MIN_N = 20
PASSED_THROUGH_NOTE = (
    "upper bound: the fault may lie outside the diff the seat saw; not an error rate of the seat"
)
_DELIVERY = re.compile(r"^(dlv-[0-9a-f]+)((?:-r\d+|-[a-z])*)$")
_ROUND_OR_SPLIT = re.compile(r"-r\d+|-[b-z]")
_TRAILING_SPLIT = re.compile(r"-[a-z]$")  # a split part with no round after it
# The title rule of claudedocs/2026-11-07-naar-1-9/metingen/pr_grootte_uitsplitsing.py, byte for byte.
GRENS = re.compile(r"450|line cap|under the cap|\bsplit\b|follow-up PR|to `?-b`?|moves? to `?-b", re.I)
CI = re.compile(r"CI red|CI fix|mode tier|ratchet|function-size|function size|xdist|changelog fragment|"
                r"main in the branch|merge main|beacon register test|census", re.I)
CAUSE_COUNTS = ROUND_CAUSES + ("unknown",)
CAUSE_SOURCES = ("declared", "title", "unknown")
_BRANCH = re.compile(r"^dispatch/(.+)$")
_MODEL_IN_REPORT = re.compile(r"\*\*Model\*\*:\s*([^\s*]+)|^Model:\s*(\S+)", re.M)
_NOT_BUILD_PREFIXES = ("panel-", "plan-gate-", "plan-tiebreak-")

QUOTA_SHARE = {"status": "unmeasured", "reason": "no per-run weekly quota source"}
_PR_REF = re.compile(r"^pr-\d+$")

GithubReader = Callable[[int], Optional[Dict[str, Any]]]


def delivery_of(dispatch_id: str) -> str:
    """The delivery a dispatch folds into: ``dlv-<hex>`` without its round/split suffixes."""
    match = _DELIVERY.match(dispatch_id)
    return match.group(1) if match else dispatch_id


def _suffix_breaks_first_pass(dispatch_id: str) -> bool:
    match = _DELIVERY.match(dispatch_id)
    return bool(match and _ROUND_OR_SPLIT.search(match.group(2)))


def _read_receipts(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            # vnx-silent-except: a torn ledger line is not a receipt; it carries no evidence.
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _benchmark_ids(db: Path, project_id: str) -> set:
    uri = f"file:{Path(db)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT dispatch_id FROM dispatch_metadata WHERE project_id = ? "
            "AND (track = 'headless' OR terminal = 'headless')", (project_id,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return set()
        raise
    finally:
        conn.close()
    return {r[0] for r in rows}


def _identities(receipts: List[Dict[str, Any]], project_id: str) -> Tuple[dict, dict]:
    """dispatch_id -> identity (first receipt with provider and model; a different second is conflict)."""
    idents: Dict[str, dict] = {}
    noise: Dict[str, str] = {}
    for rec in receipts:
        did = str(rec.get("dispatch_id") or "").strip()
        if not did or rec.get("event_type") == "pr_merged":
            continue
        reason = noise_reason(rec, project_id)
        if reason:
            noise.setdefault(did, reason)
            continue
        info = idents.setdefault(did, {"builder": None, "task_class": "unknown", "risk_class": "unknown",
                                       "prs": [], "prompt_path": ""})
        if not info["prompt_path"] and isinstance(rec.get("final_prompt_path"), str):
            info["prompt_path"] = rec["final_prompt_path"].strip()
        if info["task_class"] == "unknown" and rec.get("task_class"):
            info["task_class"] = str(rec["task_class"])
        if info["risk_class"] == "unknown" and rec.get("risk_class"):
            info["risk_class"] = str(rec["risk_class"])
        for key in ("pr_number", "pr_id"):
            pr = _clean_pr(rec.get(key))
            if pr and pr.isdigit() and int(pr) not in info["prs"]:
                info["prs"].append(int(pr))
        provider, model = str(rec.get("provider") or "").strip(), str(rec.get("model") or "").strip()
        if provider and model:
            pair = f"{provider}/{model}"
            if info["builder"] is None:
                info["builder"] = pair
            elif info["builder"] != pair:
                info["builder"] = "model=conflict"
    return idents, noise


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _changed_lines(source: Dict[str, Any]) -> Optional[int]:
    """Changed lines of a merge fact: ``changed_lines``, else ``additions`` plus ``deletions``; None when absent."""
    if _is_count(source.get("changed_lines")):
        return source["changed_lines"]
    add, dele = source.get("additions"), source.get("deletions")
    return add + dele if _is_count(add) and _is_count(dele) else None


def _merge_fact(pr: int, merged: List[Dict[str, Any]], github: Optional[GithubReader]) -> Optional[dict]:
    """Besluit 10: the pr_merged receipt with a head, else GitHub, else None."""
    for rec in reversed(merged):
        seats = rec.get("review_seats")
        head = str((seats or {}).get("head_sha") or "").strip() if isinstance(seats, dict) else ""
        when = _parse_ts(rec.get("timestamp"))
        if head and when:
            return {"merged_at": when, "head_sha": head, "branch": str(rec.get("branch") or ""),
                    "merge_source": "receipt", "changed_lines": _changed_lines(rec)}
    fact = github(pr) if github else None
    if isinstance(fact, dict):
        when = _parse_ts(fact.get("mergedAt")) if fact.get("mergedAt") else None
        head = str(fact.get("headRefOid") or "").strip()
        if when and head:
            return {"merged_at": when, "head_sha": head, "branch": str(fact.get("headRefName") or ""),
                    "merge_source": "github", "changed_lines": _changed_lines(fact)}
    return None


def _resolve_delivery(members: List[str], idents: dict, merged_by_pr: dict,
                      github: Optional[GithubReader]) -> Tuple[Optional[dict], str]:
    """The merge fact of a delivery and its PR, or (None, reason)."""
    prs = sorted({pr for m in members for pr in idents[m]["prs"]})
    if not prs:
        return None, "no_pr"
    reason, best = "no_merge_fact", None
    for pr in prs:
        fact = _merge_fact(pr, merged_by_pr.get(pr, []), github)
        if fact is None:
            continue
        named = {delivery_of(d) for d, i in idents.items() if pr in i["prs"]}
        match = _BRANCH.match(fact["branch"])
        if match and named and delivery_of(match.group(1)) not in named:
            reason = "pr_join_conflict"
            continue
        fact["pr"] = pr
        if best is None or fact["merged_at"] > best["merged_at"]:
            best = fact
    return (best, "") if best else (None, reason)


def wilson_interval(count: int, n: int, z: float = 1.96) -> Optional[tuple]:
    """95% Wilson score interval ``(low, high)`` as fractions rounded to 3 decimals; ``None`` for ``n == 0``."""
    if n <= 0:
        return None
    p = count / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


def _cell(cells: dict, key: tuple) -> dict:
    return cells.setdefault(key, {"n": 0, "hits": 0, "immature": 0})


def _finish(cells: dict, fields: Tuple[str, ...]) -> List[dict]:
    out = []
    for key in sorted(cells):
        c = cells[key]
        row = dict(zip(fields, key))
        row.update(n=c["n"], count=c["hits"], immature=c["immature"],
                   pct=round(100.0 * c["hits"] / c["n"], 1) if c["n"] >= MIN_N else None)
        if c["n"] >= MIN_N:
            row["ci95"] = list(wilson_interval(c["hits"], c["n"]))
        out.append(row)
    return out


def _seat_model(line: Any) -> str:
    if line.reviewer_model:
        return line.reviewer_model
    try:
        head = Path(line.report_path).read_text(encoding="utf-8", errors="replace")[:3000] if line.report_path else ""
    except OSError:
        head = ""
    found = _MODEL_IN_REPORT.search(head)
    return (found.group(1) or found.group(2)) if found else "model=unknown"


def _last_verdicts(lines: Any, project_id: str) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """(pr, head) -> reviewer -> last verdict by recorded_at (a takeover counts for ``reviewer``)."""
    best: Dict[Tuple[int, str, str], Any] = {}
    for line in lines:
        if line.project_id != project_id:
            continue
        key = (line.pr_number, line.head_sha, line.reviewer)
        if key not in best or line.recorded_at >= best[key].recorded_at:
            best[key] = line
    grouped: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for (pr, head, reviewer), line in best.items():
        grouped.setdefault((pr, head), {})[reviewer] = line
    return grouped


def _classify(idents: dict, noise: dict, bench: set) -> Tuple[Dict[str, List[str]], Dict[str, int]]:
    """Group in-scope dispatches by delivery; count each excluded dispatch under one reason."""
    deliveries: Dict[str, List[str]] = {}
    excluded: Dict[str, int] = {}
    for did in list(idents) + [d for d in noise if d not in idents]:
        reason = ("benchmark" if did in bench else "gate" if "-gate-pr" in did
                  else "not_build" if did.startswith(_NOT_BUILD_PREFIXES)
                  else f"noise:{noise[did]}" if did in noise
                  else "no_pr" if not idents[did]["prs"] else "")
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        else:
            deliveries.setdefault(delivery_of(did), []).append(did)
    return deliveries, excluded


def compute_metrics(
    project_id: str, receipts_path: Path, rounds_path: Path, qi_db_path: Path,
    as_of: datetime, github: Optional[GithubReader] = None, ledger_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """The three measures per builder and per seat; reads only, writes nothing."""
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    receipts = _read_receipts(receipts_path)
    idents, noise = _identities(receipts, project_id)
    deliveries, excluded = _classify(idents, noise, _benchmark_ids(qi_db_path, project_id))
    merged_by_pr: Dict[int, List[dict]] = {}
    for rec in receipts:
        pr = _clean_pr(rec.get("pr_number"))
        if rec.get("event_type") == "pr_merged" and pr and pr.isdigit() and not noise_reason(rec, project_id):
            merged_by_pr.setdefault(int(pr), []).append(rec)
    rows: Dict[str, dict] = {}
    unattributed: List[dict] = []
    for base, members in sorted(deliveries.items()):
        fact, reason = _resolve_delivery(members, idents, merged_by_pr, github)
        first = idents[members[0]]
        if fact is None:
            unattributed.append({"delivery": base, "reason": reason})
            continue
        rows[base] = {**fact, "builder": first["builder"] or "model=unknown", "task_class": first["task_class"],
                      "risk_class": first["risk_class"], "members": members}
        rows[base]["prompts"] = {m: idents[m]["prompt_path"] for m in members}
    with closing(_ro(qi_db_path)) as conn:
        edges = [e for e in load_edges(conn, project_id) if e["class"] == "rework"]
    causes = {base: member_causes(row, edges) for base, row in rows.items()}
    fpy = _first_pass(rows, causes)
    reworked = _reworked_origins(rows, edges, as_of)
    out = {"project_id": project_id, "as_of": as_of.isoformat(), "window_days": WINDOW_DAYS, "min_n": MIN_N,
           "exclusions": excluded, "unattributed": unattributed, "complete": True,
           "merge_sources": {s: sum(1 for r in rows.values() if r["merge_source"] == s) for s in ("receipt", "github")},
           "first_pass_yield": fpy,
           "round_causes": _round_causes(rows, causes),
           "rework_14d": _rework_cells(rows, reworked, as_of),
           "cost": cost_cells(rows, ledger_path, project_id, Path(rounds_path).parent / "results",
                              _first_seen(receipts, project_id)),
           "passed_through": [], "passed_through_note": PASSED_THROUGH_NOTE}
    try:
        out["passed_through"] = _passed_through(rows, reworked, as_of, rounds_path, project_id)
    except RoundLedgerError as exc:
        out["complete"], out["rounds_error"] = False, str(exc)
    return out


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)


def _mature(row: dict, as_of: datetime) -> bool:
    return row["merged_at"] <= as_of - timedelta(days=WINDOW_DAYS)


_INSTRUCTION_MARKER = "DISPATCH INSTRUCTION:"


def _title_of(path: str) -> Optional[str]:
    """The first ``# `` line of the prompt file, read after the ``DISPATCH INSTRUCTION:`` line when the
    file has one (a final prompt opens with the worker role, e.g. ``# Base Worker Context``), else from
    the top. None when unreadable or without such a line."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    start = next((i + 1 for i, line in enumerate(lines) if line.strip() == _INSTRUCTION_MARKER), 0)
    return next((line for line in lines[start:] if line.startswith("# ")), None)


def _declared_causes(edges: List[dict]) -> Dict[str, str]:
    """rework dispatch id -> the ``round_cause`` in the evidence of its ``round`` edge (a valid one only)."""
    out: Dict[str, str] = {}
    for e in edges:
        if e["kind"] != "round":
            continue
        try:
            evidence = json.loads(e.get("evidence_json") or "{}")
        except (TypeError, ValueError):
            # vnx-silent-except: malformed evidence declares nothing; the member falls to the title rule.
            continue
        cause = evidence.get("round_cause") if isinstance(evidence, dict) else None
        if cause in ROUND_CAUSES:
            out[e["rework"]] = cause
    return out


def _title_cause(member: str, prompt_path: str) -> Tuple[str, str]:
    match = _DELIVERY.match(member)
    if match and _TRAILING_SPLIT.search(match.group(2)):
        return "grens", "title"
    title = _title_of(prompt_path)
    if title is None:
        return "unknown", "unknown"
    return ("grens" if GRENS.search(title) else "ci" if CI.search(title) else "review"), "title"


def member_causes(row: dict, edges: List[dict]) -> Dict[str, Tuple[str, str]]:
    """Round member -> (cause, source): declared in the edge evidence, else the title rule, else unknown."""
    base = delivery_of(row["members"][0])
    declared = _declared_causes([e for e in edges if delivery_of(e["origin"]) == base])
    round_edges = {e["rework"] for e in edges if e["kind"] == "round" and delivery_of(e["origin"]) == base}
    out: Dict[str, Tuple[str, str]] = {}
    suffixed = {m for m in row["members"] if _suffix_breaks_first_pass(m)}
    for member in sorted(suffixed | round_edges):
        if member in declared:
            out[member] = (declared[member], "declared")
        elif _suffix_breaks_first_pass(member):
            out[member] = _title_cause(member, row["prompts"].get(member, ""))
        else:
            out[member] = ("unknown", "unknown")
    return out


def _first_pass(rows: dict, causes: Dict[str, Dict[str, Tuple[str, str]]]) -> List[dict]:
    """A hit has no member round whose cause is ``review`` or ``unknown``; ci, grens and operator do not count."""
    cells: dict = {}
    for base, row in rows.items():
        c = _cell(cells, (row["builder"], row["task_class"], row["risk_class"]))
        c["n"] += 1
        if not any(cause in ("review", "unknown") for cause, _ in causes[base].values()):
            c["hits"] += 1
    return _finish(cells, ("builder", "task_class", "risk_class"))


def _round_causes(rows: dict, causes: Dict[str, Dict[str, Tuple[str, str]]]) -> Dict[str, Any]:
    """Rounds per cause for each builder, task class and risk class, plus the totals per source."""
    cells: Dict[tuple, Dict[str, int]] = {}
    sources = {s: 0 for s in CAUSE_SOURCES}
    for base, row in rows.items():
        key = (row["builder"], row["task_class"], row["risk_class"])
        for cause, source in causes[base].values():
            counts = cells.setdefault(key, {c: 0 for c in CAUSE_COUNTS})
            counts[cause] += 1
            sources[source] += 1
    table = [{"builder": b, "task_class": t, "risk_class": r, **cells[(b, t, r)]} for (b, t, r) in sorted(cells)]
    return {"cells": table, "sources": sources}


def _reworked_origins(rows: dict, edges: List[dict], as_of: datetime) -> set:
    """Mature origins with a non-round rework edge whose rework merged within 14 days after them."""
    hit = set()
    for e in edges:
        origin, rework = delivery_of(e["origin"]), delivery_of(e["rework"])
        if e["kind"] == "round" or origin == rework or origin not in rows or rework not in rows:
            continue
        gap = rows[rework]["merged_at"] - rows[origin]["merged_at"]
        if timedelta(0) < gap <= timedelta(days=WINDOW_DAYS):
            hit.add(origin)
    return hit


def _rework_cells(rows: dict, reworked: set, as_of: datetime) -> List[dict]:
    cells: dict = {}
    for base, row in rows.items():
        c = _cell(cells, (row["builder"], row["task_class"], row["risk_class"]))
        if not _mature(row, as_of):
            c["immature"] += 1
            continue
        c["n"] += 1
        c["hits"] += base in reworked
    return _finish(cells, ("builder", "task_class", "risk_class"))


def _passed_through(rows: dict, reworked: set, as_of: datetime, rounds_path: Path, project_id: str) -> List[dict]:
    verdicts = _last_verdicts(read_ledger(Path(rounds_path)).lines, project_id)
    cells: dict = {}
    for base, row in rows.items():
        for reviewer, line in verdicts.get((row["pr"], row["head_sha"]), {}).items():
            if line.verdict != "pass" or line.blocking_count != 0:
                continue
            c = _cell(cells, (reviewer, _seat_model(line), row["task_class"], row["risk_class"]))
            if not _mature(row, as_of):
                c["immature"] += 1
                continue
            c["n"] += 1
            c["hits"] += base in reworked
    return _finish(cells, ("reviewer", "model", "task_class", "risk_class"))


def _first_seen(receipts: List[Dict[str, Any]], project_id: str) -> Dict[str, datetime]:
    """dispatch_id -> its earliest receipt time (the start of a delivery's lead time)."""
    seen: Dict[str, datetime] = {}
    for rec in receipts:
        did, when = str(rec.get("dispatch_id") or "").strip(), _parse_ts(rec.get("timestamp"))
        if not did or not when or rec.get("event_type") == "pr_merged" or noise_reason(rec, project_id):
            continue
        if did not in seen or when < seen[did]:
            seen[did] = when
    return seen


def _percentile(values: List[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile ``q`` (0..1) of ``values``; None for no values."""
    xs = sorted(values)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    low = int(pos)
    high = min(low + 1, len(xs) - 1)
    return round(xs[low] + (xs[high] - xs[low]) * (pos - low), 6)


def _median(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 6) if values else None


def _split_runs(runs: Dict[str, dict]) -> Tuple[Dict[str, Dict[str, dict]], Dict[str, list], int]:
    """Effective ledger runs -> builder runs by ref, review runs by seat, and the count of unparseable run ids."""
    builders: Dict[str, Dict[str, dict]] = {}
    reviews: Dict[str, list] = {}
    unparsed = 0
    for run_id, rec in runs.items():
        try:
            _, producer, ref, _ = cost_ledger.split_run_id(run_id)
        except cost_ledger.InvalidRecord:
            unparsed += 1
            continue
        if rec.get("role") == "builder":
            builders.setdefault(ref, {})[run_id] = rec
        else:
            exec_id = rec.get("execution_id") if isinstance(rec.get("execution_id"), str) else ""
            reviews.setdefault(producer, []).append((run_id, ref, exec_id, rec))
    return builders, reviews, unparsed


def _gate_execution_id(results_dir: Optional[Path], pr: int, seat: str) -> Optional[str]:
    """The execution id the gate result of this PR and seat names; None when there is no usable result."""
    if results_dir is None:
        return None
    try:
        data = json.loads((Path(results_dir) / f"pr-{pr}-{seat}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # vnx-silent-except: no readable gate result means no link; the runs stay unattributed.
        return None
    if not isinstance(data, dict) or _clean_pr(data.get("pr_number")) != str(pr):
        return None
    exec_id = data.get("execution_id")
    return exec_id if isinstance(exec_id, str) and exec_id else None


def _priced_total(runs: Dict[str, dict]) -> Dict[str, Any]:
    """Cost of these runs in one unit: ``api_equivalent_usd`` for a subscription run, ``cost_usd`` for a metered one.

    A metered run (deepseek) carries its price in ``cost_usd`` and no API-equivalent; summing only the
    API-equivalent would show it as free. A run without a price is listed, never summed as 0.
    """
    sub = cost_ledger.totals_for_runs({r: v for r, v in runs.items() if v.get("billing") != "metered"})
    met = cost_ledger.totals_for_runs({r: v for r, v in runs.items() if v.get("billing") == "metered"})
    return {"usd": round(sub.api_equivalent_usd + met.cost_usd, 8),
            "tokens": {f: sub.tokens[f] + met.tokens[f] for f in cost_ledger.TOKEN_FIELDS},
            "tokens_unknown": {f: sub.tokens_unknown[f] + met.tokens_unknown[f] for f in cost_ledger.TOKEN_FIELDS},
            "unavailable": list(sub.unavailable + met.unavailable)}


def _delivery_review(pr: int, reviews: Dict[str, list], results_dir: Optional[Path], matched: set) -> Dict[str, dict]:
    """Per seat the review runs of this PR: by ``pr-<N>`` ref, or by the execution id its gate result names."""
    out: Dict[str, dict] = {}
    for seat, entries in sorted(reviews.items()):
        exec_id = _gate_execution_id(results_dir, pr, seat)
        own = {rid: rec for rid, ref, ex, rec in entries
               if ref == f"pr-{pr}" or (exec_id and exec_id in (ref, ex))}
        if not own:
            continue
        matched.update(own)
        total = _priced_total(own)
        # An unpriced run makes the seat's cost unknown for this delivery: None, never a partial sum.
        out[seat] = {"runs": len(own), "unavailable_runs": len(total["unavailable"]),
                     "api_equivalent_usd": None if total["unavailable"] else total["usd"]}
    return out


def _delivery_cost(base: str, row: dict, builders: dict, reviews: dict, results_dir: Optional[Path],
                   first_seen: Dict[str, datetime], matched: set) -> Dict[str, Any]:
    own: Dict[str, dict] = {}
    missing = [m for m in row["members"] if not builders.get(m)]
    for member in row["members"]:
        own.update(builders.get(member, {}))
    total = _priced_total(own)
    why = ([f"member without builder run: {', '.join(missing)}"] if missing else []) + (
        [f"{len(total['unavailable'])} builder run(s) without price"] if total["unavailable"] else [])
    times = [first_seen[m] for m in row["members"] if m in first_seen]
    minutes = (row["merged_at"] - min(times)).total_seconds() / 60 if times else None
    return {"delivery": base, "builder": row["builder"], "task_class": row["task_class"],
            "risk_class": row["risk_class"], "pr": row["pr"], "incomplete": why,
            "build": {"runs": len(own), "api_equivalent_usd": total["usd"], "tokens": total["tokens"],
                      "tokens_unknown": total["tokens_unknown"], "unavailable_runs": total["unavailable"],
                      "missing_members": missing},
            "review": _delivery_review(row["pr"], reviews, results_dir, matched),
            "lead_time_min": round(minutes, 1) if minutes is not None and minutes >= 0 else None,
            "changed_lines": row.get("changed_lines")}


def _seat_summary(done: List[dict], seat: str) -> Dict[str, Any]:
    info = [d["review"].get(seat, {"runs": 0, "unavailable_runs": 0, "api_equivalent_usd": None}) for d in done]
    return {"runs_median": _median([i["runs"] for i in info]), "deliveries": sum(1 for i in info if i["runs"]),
            "api_equivalent_usd_median": _median([i["api_equivalent_usd"] for i in info
                                                  if i["api_equivalent_usd"] is not None]),
            "unavailable_runs": sum(i["unavailable_runs"] for i in info)}


def _cost_cell(key: tuple, items: List[dict]) -> Dict[str, Any]:
    done = [d for d in items if not d["incomplete"]]
    usd = [d["build"]["api_equivalent_usd"] for d in done]
    per_1000 = [d["build"]["api_equivalent_usd"] * 1000.0 / d["changed_lines"]
                for d in done if d["changed_lines"]]
    lead = [d["lead_time_min"] for d in done if d["lead_time_min"] is not None]
    seats = sorted({s for d in done for s in d["review"]})
    return {"builder": key[0], "task_class": key[1], "risk_class": key[2], "n": len(items),
            "n_complete": len(done), "incomplete": len(items) - len(done),
            "comparable": len(done) >= MIN_N,
            "build_usd_median": _median(usd), "build_usd_p75": _percentile(usd, 0.75),
            "build_usd_per_1000_lines_median": _median(per_1000),
            "build_usd_per_1000_lines_p75": _percentile(per_1000, 0.75), "per_1000_n": len(per_1000),
            "review": {s: _seat_summary(done, s) for s in seats},
            "lead_time_median_min": _median(lead), "lead_time_n": len(lead),
            "quota_share": dict(QUOTA_SHARE)}


def _cost_gaps(deliveries: List[dict], reviews: dict, matched: set, unparsed: int) -> Dict[str, Any]:
    unattributed, outside, unavailable = Counter(), Counter(), Counter()
    for seat, entries in reviews.items():
        for rid, ref, _, _rec in entries:
            if rid not in matched:
                (outside if _PR_REF.match(ref) else unattributed)[seat] += 1
    for d in deliveries:
        for seat, info in d["review"].items():
            if info["unavailable_runs"]:
                unavailable[seat] += info["unavailable_runs"]
    done = [d for d in deliveries if not d["incomplete"]]
    return {"incomplete_deliveries": [{"delivery": d["delivery"], "why": d["incomplete"]}
                                      for d in deliveries if d["incomplete"]],
            "no_changed_lines": sum(1 for d in done if not d["changed_lines"]),
            "no_lead_time": sum(1 for d in done if d["lead_time_min"] is None),
            "unattributed_review_runs": dict(unattributed), "other_pr_review_runs": dict(outside),
            "unavailable_review_runs": dict(unavailable), "unparseable_runs": unparsed}


def cost_cells(rows: Dict[str, dict], ledger_path: Optional[Path], project_id: Optional[str] = None,
               results_dir: Optional[Path] = None, first_seen: Optional[Dict[str, datetime]] = None) -> Dict[str, Any]:
    """The cost axis per builder x task_class x risk: build and review cost per accepted delivery, lead time.

    Read-only. Every gap is named in ``gaps``; a delivery with a gap in its build cost is ``incomplete`` and
    stays out of the medians. Review runs link to a delivery by ``pr-<N>`` ref or by the execution id the
    gate result of that PR and seat names; the gate result keeps the latest execution only, so earlier
    execution-id runs of a seat stay ``unattributed``.
    """
    out: Dict[str, Any] = {"quota_share": dict(QUOTA_SHARE), "ledger_error": None, "ledger_incomplete": None,
                           "cells": [], "deliveries": [], "gaps": None}
    if ledger_path is None or not Path(ledger_path).is_file():
        out["ledger_error"] = "cost ledger not found: no cost axis"
        return out
    totals = cost_ledger.aggregate_runs(
        Path(ledger_path), select=lambda rec: (project_id is None or rec.get("project_id") == project_id)
        and rec.get("role") in ("builder", "review"))
    builders, reviews, unparsed = _split_runs(dict(totals.runs))
    matched: set = set()
    out["deliveries"] = [_delivery_cost(base, row, builders, reviews, results_dir, first_seen or {}, matched)
                         for base, row in sorted(rows.items())]
    cells: Dict[tuple, List[dict]] = {}
    for d in out["deliveries"]:
        cells.setdefault((d["builder"], d["task_class"], d["risk_class"]), []).append(d)
    out["cells"] = [_cost_cell(key, cells[key]) for key in sorted(cells)]
    out["gaps"] = _cost_gaps(out["deliveries"], reviews, matched, unparsed)
    out["ledger_incomplete"] = totals.incomplete
    return out
