#!/usr/bin/env python3
"""Measure whether the five-seat plan-gate panel changes its decision beyond seat 1.

The plan-gate (``scripts/lib/plan_gate_panel.py``) reviews a feature's PLAN with a
panel of up to five seats, one per provider family, in the fixed order declared in
``configs/plan_gate_panel.yaml``: opus, kimi, glm-5.2-harness, deepseek, codex.
Five seats means five model calls per plan. The question nobody had measured until
now: how often did the full panel reach a DIFFERENT decision than the FIRST seat
(opus) already proposed?

This is a MEASUREMENT script, not a behaviour change. It reads the plan-gate
unified reports and, as a cross-check, the per-seat verdict ledger
(``.vnx-attest/plan-gate-seats.ndjson``), and reports:

  * total complete rounds, and how many rounds were skipped (with the reason),
  * how often the panel outcome EQUALLED the first seat (absolute and percent),
  * how often it DIVERGED (absolute and percent),
  * the divergences split by direction: stricter than the first seat, or milder,
  * the marginal contribution per seat: in how many rounds the decision still
    changed after seat 2, after 3, after 4, after 5 -- the number the whole
    weight-ladder design rests on.

Round grouping (track-slug + time window): the panel dispatches seats strictly
sequentially (``run_panel``'s ``for member in panel`` loop), so within a round the
reports land in config order and the first seat anchors the round. A round is
every first-seat report plus the non-first-seat reports that follow it before the
next first-seat report. Retries (a seat that flakes and is re-dispatched) collapse
to the latest report for that seat in that round. Seat order is NEVER inferred
from mtime or filename order -- it comes from the config order, exactly as the
gate runs it.

Two important coverage facts, reported rather than hidden:

  * opus was ABSENT from 44 of 77 tracks (41.6% of reports): those panels ran
    kimi/glm/deepseek/codex without the claude lane. They have no first-seat
    (opus) verdict, so they are excluded from the primary first-seat-vs-panel
    metric and reported separately.
  * a secondary metric repeats the comparison over EVERY round using "the first
    seat that actually ran" (the first config-order seat present), so the pattern
    is not an artifact of opus alone.

Verdicts are read from the report BODY with ``plan_gate_panel.parse_verdict`` (the
same fence parser the gate itself uses), with a small fallback for the three
pre-fence reports in the corpus. A report the parser cannot turn into a verdict is
an INCOMPLETE round, counted separately -- never silently folded into a "pass".

Exit code 0 on success. No hardcoded paths: the data dir resolves through the
central-store resolver for the active project, and the seat ledger through the
repo root (or ``git rev-parse --git-common-dir`` when running from a worktree).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_LIB_DIR = _HERE.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from plan_gate_panel import (  # noqa: E402
    SEAT_LEDGER_RELPATH,
    PanelistResult,
    apply_panel_rule,
    load_panel_seats,
    parse_verdict,
)
from project_root import (  # noqa: E402
    resolve_central_data_dir,
    resolve_project_id,
    resolve_project_root,
)

REPORTS_SUBDIR = Path("unified_reports")

# plan-gate-<track-slug>-<provider-label>-<hash8>.md
_FILENAME_RE = re.compile(r"^plan-gate-(.*)-([0-9a-f]{8})\.md$")

# Historical filename provider labels that map onto today's five canonical seats.
# The label sits between the track-slug and the hash, but the label vocabulary
# drifted over time (deepseek-harness / deepseek-v4-pro / glm-5.2) before settling
# on the five labels in configs/plan_gate_panel.yaml. Longest-first so a
# multi-token label is matched before a shorter suffix that could also fit.
_CANONICAL_LABEL: Dict[str, str] = {
    "glm-5.2-harness": "glm-5.2-harness",
    "deepseek-v4-pro": "deepseek",
    "deepseek-harness": "deepseek",
    "glm-5.2": "glm-5.2-harness",
    "deepseek": "deepseek",
    "codex": "codex",
    "kimi": "kimi",
    "opus": "opus",
}
_LABEL_SUFFIXES: Tuple[str, ...] = tuple(sorted(_CANONICAL_LABEL, key=len, reverse=True))

# Markers that identify a governance-synthesized body: the lane ran but the worker
# never authored a verdict, so the seat produced NO verdict (distinct from a real
# report whose fence would not parse).
_SYNTHESIZED_MARKERS = (
    "body synthesized by governance",
    "contract_status: synthesized",
    "no worker report file",
)

# Strictness rank for the direction split: block (strictest) > revise > pass.
_STRICTNESS = {"pass": 1, "revise": 2, "block": 3}

# An anchor report is a retry (not a new round) if it lands within this many
# seconds of the previous anchor report for the same track. A flake re-dispatches
# immediately (well under this window); a genuine next round needs the operator
# to read REVISE, revise the plan, and re-run, which is never that fast.
DEFAULT_ANCHOR_RETRY_WINDOW = 600.0


@dataclass
class Report:
    """One parsed plan-gate unified report file."""

    track: str
    seat: str  # canonical seat label
    mtime: float
    filename: str
    verdict: str = "revise"  # pass | revise | block (fail-safe default from parse_verdict)
    parse_error: bool = False
    no_verdict: bool = False  # synthesized body: the lane never delivered a verdict
    rationale: str = ""


@dataclass
class Round:
    """One panel round: a first-seat anchor plus the seats that followed it."""

    track: str
    anchor_seat: str  # the first config-order seat present in this track
    seat_reports: Dict[str, Report] = field(default_factory=dict)
    anchor_mtime: float = 0.0


def _split_track_and_seat(filename: str) -> Optional[Tuple[str, str]]:
    """Split ``plan-gate-<track>-<label>-<hash8>.md`` into (track, canonical-seat).

    Returns ``None`` when the filename does not match the hash8 pattern (the two
    pre-hash8 reports in the corpus are excluded, not guessed at).
    """
    m = _FILENAME_RE.match(filename)
    if not m:
        return None
    middle = m.group(1)
    for label in _LABEL_SUFFIXES:
        suffix = f"-{label}"
        if middle.endswith(suffix):
            track = middle[: -len(suffix)]
            if not track:
                return None
            return track, _CANONICAL_LABEL[label]
    return None


def _parse_report_text(text: str) -> Tuple[str, bool, bool, str]:
    """Parse a report body into (verdict, parse_error, no_verdict, rationale).

    Primary: ``plan_gate_panel.parse_verdict`` (the exact fence parser the gate
    uses). Fallback for the three pre-fence reports: a prose ``Verdict:`` line, an
    ``Outcome:`` line, or a YAML ``verdict:`` field. A synthesized body is a
    no-verdict (never delivered), not a parse error.
    """
    parsed = parse_verdict(text)
    if not parsed["parse_error"]:
        return parsed["verdict"], False, False, parsed.get("rationale", "")

    lowered = text.lower()
    if any(marker in lowered for marker in _SYNTHESIZED_MARKERS):
        return "revise", False, True, "synthesized body (no worker verdict)"

    # Pre-fence prose verdicts: ``Verdict: **revise**.``, ``Outcome: REVISE``,
    # or a YAML frontmatter ``verdict: pass``.
    for pattern in (
        re.compile(r"verdict:\s*\*{0,2}(pass|revise|block)", re.IGNORECASE),
        re.compile(r"^outcome:\s*(pass|revise|block)", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^verdict:\s*(pass|revise|block)", re.IGNORECASE | re.MULTILINE),
    ):
        m = pattern.search(text)
        if m:
            return m.group(1).lower(), False, False, "prose verdict fallback"
    return "revise", True, False, parsed.get("rationale", "no verdict block found")


def load_reports(reports_dir: Path) -> Tuple[List[Report], List[str]]:
    """Parse every ``plan-gate-*.md`` report under ``reports_dir``.

    Returns ``(reports, skipped_filenames)``. A filename that does not match the
    hash8 pattern is returned in ``skipped_filenames`` (counted, never guessed).
    """
    reports: List[Report] = []
    skipped: List[str] = []
    for path in sorted(reports_dir.glob("plan-gate-*.md")):
        split = _split_track_and_seat(path.name)
        if split is None:
            skipped.append(path.name)
            continue
        track, seat = split
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped.append(f"{path.name} (unreadable)")
            continue
        verdict, parse_error, no_verdict, rationale = _parse_report_text(text)
        reports.append(
            Report(
                track=track,
                seat=seat,
                mtime=path.stat().st_mtime,
                filename=path.name,
                verdict=verdict,
                parse_error=parse_error,
                no_verdict=no_verdict,
                rationale=rationale,
            )
        )
    return reports, skipped


def _first_present_seat(seat_set: set, seats: List[str]) -> Optional[str]:
    for s in seats:
        if s in seat_set:
            return s
    return None


def group_rounds(
    reports: List[Report], seats: List[str], anchor_retry_window: float
) -> Tuple[List[Round], int]:
    """Group reports into rounds anchored on the first config-order seat present.

    The gate dispatches seats strictly sequentially in config order, so the first
    present seat anchors the round: every anchor-seat report opens a new round and
    each following report joins it until the next anchor report. This is the
    opus-anchoring when opus ran, and the kimi-anchoring for the 44 tracks where
    opus never ran. Reports before a track's first anchor report (the June
    parallel-dispatch era) are orphaned and counted, never assigned a round.

    Returns ``(rounds, orphaned_report_count)``.
    """
    by_track: Dict[str, List[Report]] = defaultdict(list)
    for r in reports:
        by_track[r.track].append(r)

    rounds: List[Round] = []
    orphaned = 0
    for track in sorted(by_track):
        ordered = sorted(by_track[track], key=lambda r: r.mtime)
        anchor_seat = _first_present_seat({r.seat for r in ordered}, seats)
        if anchor_seat is None:
            continue
        current: Optional[Round] = None
        for r in ordered:
            if r.seat == anchor_seat:
                if (
                    current is not None
                    and current.anchor_mtime
                    and (r.mtime - current.anchor_mtime) <= anchor_retry_window
                ):
                    # anchor flake + retry: fold the retry into the current round.
                    current.seat_reports[r.seat] = _latest(current.seat_reports.get(r.seat), r)
                    continue
                current = Round(track=track, anchor_seat=anchor_seat, anchor_mtime=r.mtime)
                current.seat_reports[r.seat] = r
                rounds.append(current)
                continue
            if current is None:
                orphaned += 1
                continue
            current.seat_reports[r.seat] = _latest(current.seat_reports.get(r.seat), r)
    return rounds, orphaned


def _latest(a: Optional[Report], b: Report) -> Report:
    """Pick the later-mtime report (retries supersede their flaked first attempt)."""
    if a is None:
        return b
    return b if b.mtime >= a.mtime else a


def _to_panelist(r: Report) -> PanelistResult:
    """Rebuild the ``PanelistResult`` the gate would have produced for a report.

    Mirrors ``plan_gate_panel._dispatch_one``'s outcome for the three categories:
    a synthesized body is ``no_verdict`` (dispatched, no parse error, never saw a
    verdict); an unparseable fence is ``parse_error`` (abstains); everything else
    is a scoring verdict.
    """
    return PanelistResult(
        label=r.seat,
        provider="",
        model="",
        verdict=r.verdict,
        dispatched=True,
        parse_error=r.parse_error,
        no_verdict=r.no_verdict,
        report_path=r.filename,
    )


def _seat_order(seats: List[str], present: List[str]) -> List[str]:
    """The present seats in config order (config order is the run order)."""
    present_set = set(present)
    return [s for s in seats if s in present_set]


def _classify_round(rnd: Round, anchor: Report, seats: List[str]) -> Tuple[str, str, List[str]]:
    """Return (anchor_verdict, panel_decision, ordered_present) for a round.

    ``anchor_verdict`` is the anchor's raw verdict; ``panel_decision`` is
    ``apply_panel_rule`` over every present seat in config order.
    """
    ordered_present = _seat_order(seats, list(rnd.seat_reports.keys()))
    panelists = [_to_panelist(rnd.seat_reports[s]) for s in ordered_present]
    decision = apply_panel_rule(panelists)["decision"]
    return anchor.verdict, decision, ordered_present


def _compare_first_vs_panel(
    rounds: List[Round], seats: List[str], first_seat: str
) -> Dict[str, Any]:
    """The first-seat-vs-panel comparison over a set of rounds.

    ``first_seat`` is the label of the config-order seat treated as "first" (opus
    for the primary metric). Only rounds anchored on ``first_seat`` with a scoring
    anchor verdict are compared; the rest are skipped with a reason.
    """
    complete: List[Round] = []
    skipped: List[Dict[str, str]] = []
    for rnd in rounds:
        anchor = rnd.seat_reports.get(first_seat)
        if anchor is None:
            skipped.append({"track": rnd.track, "reason": f"no {first_seat} report"})
            continue
        if anchor.no_verdict:
            skipped.append({"track": rnd.track, "reason": "first seat produced no verdict"})
            continue
        if anchor.parse_error:
            skipped.append({"track": rnd.track, "reason": "first-seat verdict unparseable"})
            continue
        complete.append(rnd)

    equal = 0
    diverged = 0
    stricter = 0
    milder = 0
    diverged_rounds: List[Dict[str, str]] = []
    for rnd in complete:
        anchor = rnd.seat_reports[first_seat]
        _, decision, _ = _classify_round(rnd, anchor, seats)
        seat_rank = _STRICTNESS.get(anchor.verdict, 2)
        panel_rank = _STRICTNESS.get(decision.lower(), 2)
        if decision.lower() == anchor.verdict:
            equal += 1
        else:
            diverged += 1
            if panel_rank > seat_rank:
                stricter += 1
                direction = "stricter"
            else:
                milder += 1
                direction = "milder"
            diverged_rounds.append(
                {
                    "track": rnd.track,
                    "first_seat": anchor.verdict,
                    "panel": decision,
                    "direction": direction,
                }
            )
    return {
        "first_seat": first_seat,
        "complete_rounds": len(complete),
        "skipped_rounds": len(skipped),
        "skipped_reasons": skipped,
        "equal": equal,
        "equal_pct": _pct(equal, len(complete)),
        "diverged": diverged,
        "diverged_pct": _pct(diverged, len(complete)),
        "diverged_stricter": stricter,
        "diverged_milder": milder,
        "diverged_rounds": diverged_rounds,
    }


def _marginal_contribution(rounds: List[Round], seats: List[str]) -> Dict[str, Dict[str, int]]:
    """The marginal contribution per config-order seat over a set of rounds.

    For each round, compute the prefix decisions (apply_panel_rule over seats 1..N
    in config order, present seats only). "changed after seat N" means the prefix
    decision changed when seat N joined. Only rounds with a full prefix from seat 1
    are counted for seat N's marginal (a round missing opus has no "after seat 2").
    """
    marginal: Dict[str, Dict[str, int]] = {}
    for idx in range(2, len(seats) + 1):
        marginal[str(idx)] = {"rounds_with_this_seat": 0, "decision_changed_after": 0}

    for rnd in rounds:
        # Only rounds whose first present seat actually scored: a no-verdict or
        # unparseable anchor means there is no "before" decision to compare against,
        # so the marginal contribution is undefined (and it keeps the denominator
        # consistent with the primary metric's "complete rounds").
        anchor = rnd.seat_reports.get(rnd.anchor_seat)
        if anchor is None or anchor.no_verdict or anchor.parse_error:
            continue
        ordered_present = _seat_order(seats, list(rnd.seat_reports.keys()))
        if not ordered_present:
            continue
        panelists = [_to_panelist(rnd.seat_reports[s]) for s in ordered_present]
        decisions: List[str] = []
        for i in range(1, len(ordered_present) + 1):
            decisions.append(apply_panel_rule(panelists[:i])["decision"])
        for i in range(1, len(ordered_present)):
            seat_idx = seats.index(ordered_present[i]) + 1  # 1-based config index
            if seat_idx < 2:
                continue
            key = str(seat_idx)
            marginal[key]["rounds_with_this_seat"] += 1
            if decisions[i] != decisions[i - 1]:
                marginal[key]["decision_changed_after"] += 1
    return marginal


def analyze(reports: List[Report], seats: List[str]) -> Dict[str, Any]:
    """Run the full measurement over the corpus.

    ``seats`` is the ordered seat list from ``load_panel_seats()``; ``seats[0]``
    is the first seat (opus). Returns a structured result dict (also the --json
    payload, minus CLI-added metadata).
    """
    first_seat = seats[0]
    rounds, orphaned = group_rounds(reports, seats, DEFAULT_ANCHOR_RETRY_WINDOW)

    # Coverage: tracks where the first seat (opus) never ran.
    by_track: Dict[str, List[Report]] = defaultdict(list)
    for r in reports:
        by_track[r.track].append(r)
    tracks_without_first_seat = [
        {"track": t, "reports": len(rs)}
        for t, rs in sorted(by_track.items())
        if not any(r.seat == first_seat for r in rs)
    ]
    reports_without_first_seat = sum(t["reports"] for t in tracks_without_first_seat)

    # Primary: first seat = opus, over rounds anchored on opus.
    opus_rounds = [r for r in rounds if r.anchor_seat == first_seat]
    primary = _compare_first_vs_panel(opus_rounds, seats, first_seat)

    # Secondary: "first present seat" over every round (includes panels where
    # opus never ran and kimi was the first seat present).
    all_comparison: List[Dict[str, str]] = []
    for rnd in rounds:
        anchor = rnd.seat_reports.get(rnd.anchor_seat)
        if anchor is None or anchor.no_verdict or anchor.parse_error:
            continue
        _, decision, _ = _classify_round(rnd, anchor, seats)
        seat_rank = _STRICTNESS.get(anchor.verdict, 2)
        panel_rank = _STRICTNESS.get(decision.lower(), 2)
        if decision.lower() == anchor.verdict:
            direction = "equal"
        else:
            direction = "stricter" if panel_rank > seat_rank else "milder"
        all_comparison.append(
            {
                "track": rnd.track,
                "anchor_seat": rnd.anchor_seat,
                "anchor_verdict": anchor.verdict,
                "panel": decision,
                "direction": direction,
            }
        )

    # Unparseable non-anchor seat reports across the primary complete rounds.
    unparseable: List[Dict[str, str]] = []
    for rnd in opus_rounds:
        anchor = rnd.seat_reports.get(first_seat)
        if anchor is None or anchor.no_verdict or anchor.parse_error:
            continue
        for seat, rep in rnd.seat_reports.items():
            if seat != first_seat and rep.parse_error and not rep.no_verdict:
                unparseable.append({"track": rnd.track, "seat": seat, "file": rep.filename})

    return {
        "seats": seats,
        "first_seat": first_seat,
        "total_reports_parsed": len(reports),
        "total_tracks": len(by_track),
        "total_rounds": len(rounds),
        "orphaned_reports": orphaned,
        "coverage_without_first_seat": {
            "tracks": len(tracks_without_first_seat),
            "reports": reports_without_first_seat,
            "tracks_detail": tracks_without_first_seat,
        },
        "primary_first_seat_vs_panel": primary,
        "marginal_contribution_opus_rounds": _marginal_contribution(opus_rounds, seats),
        "marginal_contribution_all_rounds": _marginal_contribution(rounds, seats),
        "first_present_seat_vs_panel": {
            "complete_rounds": len(all_comparison),
            "equal": sum(1 for c in all_comparison if c["direction"] == "equal"),
            "diverged": sum(1 for c in all_comparison if c["direction"] != "equal"),
            "diverged_stricter": sum(1 for c in all_comparison if c["direction"] == "stricter"),
            "diverged_milder": sum(1 for c in all_comparison if c["direction"] == "milder"),
            "rounds": all_comparison,
        },
        "unparseable_seat_reports": unparseable,
    }


def _pct(numer: int, denom: int) -> Optional[float]:
    return round(100.0 * numer / denom, 1) if denom else None


def load_seat_ledger_rounds(ledger_path: Path, seats: List[str]) -> List[Dict[str, Any]]:
    """Read the per-seat verdict ledger into rounds keyed by (track_id, run_at).

    The ledger appends one record per seat per run, in config order, all sharing
    the same ``run_at``. Used as an independent cross-check of the file-based
    grouping and verdicts, not as the primary measurement (it only covers rounds
    after OI-888 shipped, 2026-08-02).
    """
    rounds: List[Dict[str, Any]] = []
    if not ledger_path.is_file():
        return rounds
    by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    try:
        with ledger_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("type") != "plan_gate_seat":
                    continue
                by_key[(rec.get("track_id", ""), rec.get("run_at", ""))].append(rec)
    except (OSError, json.JSONDecodeError):
        return rounds
    for (track_id, _run_at), recs in sorted(by_key.items()):
        ordered = [r for r in recs if r.get("panelist_id") in seats]
        ordered.sort(key=lambda r: seats.index(r["panelist_id"]))
        rounds.append(
            {
                "track_id": track_id,
                "seats": ordered,
                "verdicts": {r["panelist_id"]: r.get("verdict") for r in ordered},
            }
        )
    return rounds


def resolve_seat_ledger_path(explicit: Optional[str]) -> Optional[Path]:
    """Resolve the seat ledger path, or ``None`` when it cannot be found.

    Order: explicit ``--seat-ledger``, then ``VNX_SEAT_LEDGER``, then the main
    checkout's ``.vnx-attest/`` via ``git rev-parse --git-common-dir`` (the
    authoritative append-only ledger — ``.vnx-attest/`` is NOT tracked, so a
    worktree's own copy is a stale/partial snapshot), then the repo root's
    ``.vnx-attest/`` as a fallback when there is no git common dir.
    """
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("VNX_SEAT_LEDGER")
    if env:
        candidates.append(Path(env))
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if common:
            candidates.append(Path(common).parent / SEAT_LEDGER_RELPATH)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        candidates.append(resolve_project_root() / SEAT_LEDGER_RELPATH)
    except Exception:  # vnx-silent-except: repo root optional for the cross-check
        pass
    for path in candidates:
        if path.is_file():
            return path
    return None


def resolve_data_dir(explicit: Optional[str]) -> Path:
    """Resolve the unified-reports data dir for the active project.

    Order: explicit ``--data-dir``, then the central store
    ``~/.vnx-data/<project_id>`` for the active project (the same store the panel
    writes reports to), mirroring ``plan_gate_panel._resolve_data_dir``.
    """
    if explicit:
        return Path(explicit)
    return resolve_central_data_dir(resolve_project_id())


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure plan-gate panel effectiveness: does the five-seat "
        "panel change its decision beyond the first seat?"
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="unified_reports data dir (default: central store for the active project)",
    )
    parser.add_argument(
        "--seat-ledger",
        default=None,
        help="path to .vnx-attest/plan-gate-seats.ndjson (cross-check only)",
    )
    parser.add_argument(
        "--anchor-retry-window",
        type=float,
        default=DEFAULT_ANCHOR_RETRY_WINDOW,
        help="seconds within which a second anchor report is a retry, not a new round",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit a machine-readable JSON result instead of the text report",
    )
    return parser


def render_text(result: Dict[str, Any], data_dir: Path, ledger_path: Optional[Path]) -> str:
    seats = result["seats"]
    primary = result["primary_first_seat_vs_panel"]
    cov = result["coverage_without_first_seat"]
    fp = result["first_present_seat_vs_panel"]
    lines: List[str] = []
    lines.append("Plan-gate panel effectiveness — first seat vs full panel")
    lines.append("=" * 64)
    lines.append(f"first seat: {result['first_seat']}  (seat order: {', '.join(seats)})")
    lines.append(
        f"reports parsed: {result['total_reports_parsed']}   tracks: {result['total_tracks']}   "
        f"rounds: {result['total_rounds']}"
    )

    lines.append("")
    lines.append("Coverage (reported, not hidden)")
    lines.append("-" * 40)
    lines.append(
        f"tracks where the first seat ({result['first_seat']}) never ran: "
        f"{cov['tracks']} tracks / {cov['reports']} reports — excluded from the "
        "primary metric (no first-seat verdict to compare)"
    )
    if result["orphaned_reports"]:
        lines.append(
            f"orphaned reports before a track's first anchor: {result['orphaned_reports']}"
        )

    lines.append("")
    lines.append(f"PRIMARY — first seat ({result['first_seat']}) vs panel outcome")
    lines.append("-" * 40)
    lines.append(f"complete rounds: {primary['complete_rounds']}   skipped: {primary['skipped_rounds']}")
    if primary["skipped_reasons"]:
        reasons = Counter(r["reason"] for r in primary["skipped_reasons"])
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  skipped ({count}): {reason}")
    lines.append(f"equal:     {primary['equal']}  ({primary['equal_pct']}%)")
    lines.append(f"diverged:  {primary['diverged']}  ({primary['diverged_pct']}%)")
    lines.append(f"  stricter than first seat: {primary['diverged_stricter']}")
    lines.append(f"  milder than first seat:   {primary['diverged_milder']}")

    lines.append("")
    lines.append("Marginal contribution per seat (decision changed after this seat joined)")
    lines.append("-" * 40)
    lines.append("  opus-anchored rounds:")
    _render_marginal_lines(lines, result["marginal_contribution_opus_rounds"], seats)
    lines.append("  all rounds (first present seat):")
    _render_marginal_lines(lines, result["marginal_contribution_all_rounds"], seats)

    lines.append("")
    lines.append("SECONDARY — first PRESENT seat vs panel (every round, incl. no-opus panels)")
    lines.append("-" * 40)
    denom = fp["complete_rounds"]
    lines.append(
        f"rounds: {denom}   equal: {fp['equal']} ({_pct(fp['equal'], denom)}%)   "
        f"diverged: {fp['diverged']} ({_pct(fp['diverged'], denom)}%)"
    )
    lines.append(f"  stricter: {fp['diverged_stricter']}   milder: {fp['diverged_milder']}")

    if result["unparseable_seat_reports"]:
        lines.append("")
        lines.append(
            f"unparseable non-anchor seat reports (abstained, did not score): "
            f"{len(result['unparseable_seat_reports'])}"
        )

    lines.append("")
    lines.append(f"data dir: {data_dir}")
    lines.append(f"seat ledger (cross-check): {ledger_path or 'not found'}")
    return "\n".join(lines)


def _render_marginal_lines(lines: List[str], marginal: Dict[str, Dict[str, int]], seats: List[str]) -> None:
    for idx in range(2, len(seats) + 1):
        mc = marginal.get(str(idx), {"rounds_with_this_seat": 0, "decision_changed_after": 0})
        denom = mc["rounds_with_this_seat"]
        changed = mc["decision_changed_after"]
        pct = _pct(changed, denom) if denom else None
        lines.append(
            f"    seat {idx} ({seats[idx-1]}): changed in {changed} of {denom} rounds"
            f"{f' ({pct}%)' if pct is not None else ''}"
        )


def main(argv: Optional[List[str]] = None) -> int:
    args = build_cli().parse_args(argv)
    data_dir = resolve_data_dir(args.data_dir)
    reports_dir = data_dir / REPORTS_SUBDIR
    if not reports_dir.is_dir():
        print(f"no unified_reports dir at {reports_dir}", file=sys.stderr)
        return 1

    reports, skipped_filenames = load_reports(reports_dir)
    seats = [s["label"] for s in load_panel_seats()]
    result = analyze(reports, seats)
    result["skipped_filenames"] = skipped_filenames

    ledger_path = resolve_seat_ledger_path(args.seat_ledger)
    ledger_rounds = load_seat_ledger_rounds(ledger_path, seats) if ledger_path else []
    result["seat_ledger"] = {
        "path": str(ledger_path) if ledger_path else None,
        "rounds": len(ledger_rounds),
        "full_five_seat_rounds": sum(
            1 for r in ledger_rounds if len(r["seats"]) == len(seats)
        ),
    }

    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render_text(result, data_dir, ledger_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
