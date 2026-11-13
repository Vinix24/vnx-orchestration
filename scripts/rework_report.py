#!/usr/bin/env python3
"""rework_report.py - render the rework metrics (ADR-071 besluit 8 and 14) as JSON or Markdown.

Calls ``rework_metrics.compute_metrics`` once and renders its result. The ``## Voorstellen``
section holds deterministic threshold rules and nothing else: a proposal is text for the operator.
This module has no write path to routing or configuration; the only file it writes is ``--out``.

Usage:
    python3 scripts/rework_report.py --json [--project-id ID] [--data-dir DIR] [--as-of ISO] [--out PATH]
    python3 scripts/rework_report.py --md   [--project-id ID] [--data-dir DIR] [--as-of ISO] [--out PATH]

``--data-dir`` is the project's central data dir (it holds ``state/``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

EXIT_OK, EXIT_ERROR = 0, 1
MIN_N = 20


def _cell_text(row: Dict[str, Any]) -> str:
    """``n`` always; the count with it; the percentage only when the metrics result carries one."""
    text = f"n={row['n']}, telling {row['count']}"
    if row.get("pct") is None:
        return text
    ci = row.get("ci95")
    return f"{text}, {row['pct']}% ({_ci_text(ci)}, n={row['n']})" if ci else f"{text}, {row['pct']}%"


def _ci_text(ci: Any) -> str:
    return f"{round(100 * ci[0])}-{round(100 * ci[1])}%"


def _table(title: str, rows: List[Dict[str, Any]], keys: List[str]) -> List[str]:
    lines = [f"### {title}", ""]
    if not rows:
        return lines + ["Geen rijen.", ""]
    lines += ["| " + " | ".join(keys + ["resultaat", "immature"]) + " |",
              "|" + "---|" * (len(keys) + 2)]
    for row in rows:
        lines.append("| " + " | ".join(str(row[k]) for k in keys)
                     + f" | {_cell_text(row)} | {row['immature']} |")
    return lines + [""]


def _causes_table(result: Dict[str, Any]) -> List[str]:
    keys = ["builder", "task_class", "risk_class"]
    causes = ["review", "ci", "grens", "operator", "unknown"]
    lines = ["### Rondes per oorzaak", ""]
    rows = result["round_causes"]["cells"]
    if not rows:
        return lines + ["Geen rondes.", ""]
    lines += ["| " + " | ".join(keys + causes) + " |", "|" + "---|" * (len(keys) + len(causes))]
    for row in rows:
        lines.append("| " + " | ".join(str(row[k]) for k in keys + causes) + " |")
    return lines + [""]


def _pct_of(n: int, count: int) -> Optional[float]:
    return round(100.0 * count / n, 1) if n >= MIN_N else None


def _builder_proposals(rows: List[Dict[str, Any]]) -> List[str]:
    """Same task_class and risk class, both builders at n >= 20, 95% Wilson intervals that do not overlap."""
    strata: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in rows:
        # No ci95 means no evidence of separation: the cell stays out (fail-closed).
        if row["pct"] is not None and row.get("ci95") and not str(row["builder"]).startswith("model="):
            strata.setdefault((row["task_class"], row["risk_class"]), []).append(row)
    out: List[str] = []
    for (task, risk), cells in sorted(strata.items()):
        best = min(cells, key=lambda c: (c["pct"], c["builder"]))
        for other in sorted(cells, key=lambda c: c["builder"]):
            if other is not best and best["ci95"][1] < other["ci95"][0]:
                out.append(
                    f"Bouwer: vloer voor taakklasse {task} (risico {risk}) naar {best['builder']}. "
                    f"Herwerk binnen 14 dagen {best['pct']}% ({_ci_text(best['ci95'])}, n={best['n']}) tegen "
                    f"{other['pct']}% ({_ci_text(other['ci95'])}, n={other['n']}) voor {other['builder']}: "
                    f"de 95%-intervallen overlappen niet.")
    return out


def _seat_proposals(rows: List[Dict[str, Any]]) -> List[str]:
    """Seats within one risk class, task classes pooled, both at n >= 20, 95% Wilson intervals not overlapping."""
    from rework_metrics import wilson_interval

    pooled: Dict[tuple, Dict[str, int]] = {}
    for row in rows:
        seat = pooled.setdefault((row["risk_class"], row["reviewer"], row["model"]), {"n": 0, "count": 0})
        seat["n"] += row["n"]
        seat["count"] += row["count"]
    strata: Dict[str, List[tuple]] = {}
    for (risk, reviewer, model), seat in pooled.items():
        pct = _pct_of(seat["n"], seat["count"])
        ci = wilson_interval(seat["count"], seat["n"])
        if pct is not None and ci is not None and model != "model=unknown":
            strata.setdefault(risk, []).append((pct, f"{reviewer}/{model}", seat["n"], ci))
    out: List[str] = []
    for risk, seats in sorted(strata.items()):
        best = min(seats)
        for other in sorted(seats):
            if other is not best and best[3][1] < other[3][0]:
                out.append(
                    f"Stoel: in risicoklasse {risk} liet {other[1]} meer door dan {best[1]}: doorgelaten "
                    f"herwerk {other[0]}% ({_ci_text(other[3])}, n={other[2]}) tegen {best[0]}% "
                    f"({_ci_text(best[3])}, n={best[2]}): de 95%-intervallen overlappen niet. "
                    f"Bovengrens, geen foutenpercentage van de stoel.")
    return out


def proposals(result: Dict[str, Any]) -> List[str]:
    return _builder_proposals(result["rework_14d"]) + _seat_proposals(result["passed_through"])


def _basis_lines(result: Dict[str, Any]) -> List[str]:
    reasons: Dict[str, int] = {}
    for item in result["unattributed"]:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    lines = [f"- Peilmoment: {result['as_of']}, venster {result['window_days']} dagen, "
             f"percentage vanaf n={result['min_n']}.",
             f"- Uitgesloten per reden: {json.dumps(result['exclusions'], sort_keys=True)}.",
             f"- Unattributed: {len(result['unattributed'])} {json.dumps(reasons, sort_keys=True)}.",
             f"- Mergebron: {json.dumps(result['merge_sources'], sort_keys=True)}."]
    lines.append(f"- Oorzaak van rondes per bron: {json.dumps(result['round_causes']['sources'], sort_keys=True)}.")
    if not result["complete"]:
        lines.append(f"- Onvolledig: {result.get('rounds_error', 'onbekende reden')}.")
    return lines


def render_markdown(result: Dict[str, Any]) -> str:
    lines = [f"# Herwerkrapport {result['project_id']}", "", "## Basis", ""] + _basis_lines(result) + [""]
    lines += ["## Per bouwer", ""]
    lines += _table("First-pass yield (telt alleen reviewrondes)", result["first_pass_yield"],
                    ["builder", "task_class", "risk_class"])
    lines += _causes_table(result)
    lines += _table("Herwerk binnen 14 dagen", result["rework_14d"], ["builder", "task_class", "risk_class"])
    lines += ["## Per stoel", "", f"Doorgelaten herwerk is een bovengrens: {result['passed_through_note']}.", ""]
    lines += _table("Doorgelaten herwerk", result["passed_through"],
                    ["reviewer", "model", "task_class", "risk_class"])
    lines += ["## Voorstellen", ""]
    found = proposals(result)
    if found:
        lines += [f"- {text}" for text in found]
    else:
        lines.append(f"Geen laag haalt de drempel (n >= {MIN_N} en niet-overlappende Wilson-intervallen): geen voorstel.")
    lines += ["", "Een voorstel is tekst. De operator beslist."]
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _parse_as_of(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    fmt = parser.add_mutually_exclusive_group(required=True)
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--md", action="store_true")
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    from project_root import resolve_central_data_dir, resolve_project_id, resolve_project_root
    from provider_costs import costs_path_in
    from review_rounds import ledger_path
    from rework_metrics import compute_metrics

    try:
        project_id = args.project_id or resolve_project_id(resolve_project_root(__file__))
        as_of = _parse_as_of(args.as_of)
    except (RuntimeError, ValueError) as exc:
        print(f"rework_report: {exc}", file=sys.stderr)
        return EXIT_ERROR
    data_dir = Path(args.data_dir) if args.data_dir else resolve_central_data_dir(project_id)
    state = data_dir / "state"
    receipts, db = state / "t0_receipts.ndjson", state / "quality_intelligence.db"
    missing = [str(p) for p in (receipts, db) if not p.is_file()]
    if missing:
        print(f"rework_report: missing input: {', '.join(missing)}", file=sys.stderr)
        return EXIT_ERROR
    result = compute_metrics(project_id, receipts, ledger_path(state / "review_gates"), db, as_of,
                             ledger_path=costs_path_in(data_dir))
    text = json.dumps(result, sort_keys=True, indent=2) + "\n" if args.json else render_markdown(result)
    if args.out:
        _write_atomic(Path(args.out), text)
    else:
        sys.stdout.write(text)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
