#!/usr/bin/env python3
"""cost_ledger_report.py — ``vnx costs report``: the ledger per model, role, day or provider (K5a, ADR-043).

One read (``cost_ledger.aggregate_groups``) gives a row per group and the totals
row for the same selection. ``day`` is the UTC date of ``occurred_from``; a
record whose key field is missing is grouped in ``(unknown)``. ``--since``
selects on the effective ``occurred_from`` (UTC). The read-level counts (spool
backlog, legacy and unreadable lines, torn tail, unlocked read) print once below
the totals, never in a group's sums. Money: two decimals here, unrounded in
``--json``.

``--by provider`` also carries the calibration of the same window (K5b) in ``--json``:
the measured spend next to the ledger's metered sum, with the deviation in percent,
or ``unavailable:<reason>`` — never a zero, and in no sum.

Exit codes: 0 a report (incomplete too: it says so), 1 an unreadable ledger,
2 usage (an unknown ``--by`` or a bad ``--since``).

Usage: python3 scripts/cost_ledger_report.py [--by model|role|day|provider]
       [--since YYYY-MM-DD] [--json] [--ledger PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(SCRIPT_DIR / "lib"), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import account_observations  # noqa: E402
import cost_ledger  # noqa: E402

EXIT_FAILED = 1
EXIT_USAGE = 2
DEFAULT_BY = "provider"
HEADER = ["group", "runs", "cost_usd", "api_equiv", *cost_ledger.TOKEN_FIELDS, "unknown_tokens", "unavailable",
          "complete"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx costs report",
                                     description="Report the cost ledger grouped by model, role, day or provider.")
    parser.add_argument("--by", default=DEFAULT_BY, metavar="model|role|day|provider",
                        help=f"the group key (default {DEFAULT_BY})")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="only runs whose effective occurred_from (UTC) is on or after this date")
    parser.add_argument("--json", action="store_true", help="print the report as one JSON object, amounts unrounded")
    parser.add_argument("--ledger", metavar="PATH", help="the cost ledger to read (default: this project's store)")
    return parser


def _ledger(argument: Optional[str]) -> Path:
    """``--ledger``, else the path ``aggregate_runs`` resolves (one resolver, no path of our own)."""
    if argument:
        return Path(argument)
    import provider_costs

    return provider_costs._resolve_costs_path()


def _window(since: Optional[str]) -> Optional[Callable[[Mapping[str, object]], bool]]:
    """The ``select`` of a ``--since`` window, or ``None``: a run without a readable UTC day is not in it."""
    if since is None:
        return None

    def selected(record: Mapping[str, object]) -> bool:
        day = cost_ledger.utc_day(record.get("occurred_from"))
        return day is not None and day >= since

    return selected


def _cells(name: str, totals) -> List[str]:
    """One table row: the group's numbers and its completeness as ``complete/total`` runs."""
    return [name, str(len(totals.runs)), f"{totals.cost_usd:.2f}", f"{totals.api_equivalent_usd:.2f}",
            *(str(int(totals.tokens[field])) for field in cost_ledger.TOKEN_FIELDS),
            str(totals.unknown_token_runs), str(len(totals.unavailable)),
            f"{totals.complete_runs}/{len(totals.runs)}"]


def _table(rows: List[List[str]]) -> List[str]:
    """``rows`` (the header first) in aligned columns: names left, numbers right."""
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return ["  ".join(cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i])
                      for i, cell in enumerate(row)).rstrip() for row in rows]


def _read_lines(totals) -> List[str]:
    """One ``onvolledig`` line per read-level count that is non-zero or true; nothing when the read is whole."""
    lines: List[str] = []
    if totals.spool_pending:
        lines.append(f"onvolledig: {totals.spool_pending} runs wachten op replay")
    if totals.spool_unreadable:
        lines.append("onvolledig: de spool is niet te lezen")
    if totals.spool_conflicts:
        lines.append(f"onvolledig: {totals.spool_conflicts} runs in de spool botsen met een andere uitvoering "
                     "in het grootboek")
    if totals.spool_quarantined:
        lines.append(f"onvolledig: {totals.spool_quarantined} spoolbestanden staan in quarantaine")
    if totals.legacy_lines:
        lines.append(f"onvolledig: {totals.legacy_lines} regels in het oude formaat (legacy_lines, niet opgeteld)")
    if totals.unreadable_lines:
        lines.append(f"onvolledig: {totals.unreadable_lines} onleesbare regels in het grootboek")
    if totals.torn_tail:
        lines.append("onvolledig: het grootboek eindigt op een afgebroken regel")
    if totals.unlocked_read:
        lines.append("onvolledig: gelezen zonder lockbestand, want dat bestaat niet en is niet aan te maken")
    return lines


def _ordered(report) -> List:
    """``[(group, LedgerTotals)]`` by spend (cost plus api-equivalent) high to low, then by name."""
    return sorted(report.groups.items(),
                  key=lambda item: (-(item[1].cost_usd + item[1].api_equivalent_usd), item[0]))


def _calibration(name: str, totals, ledger: Path, since: Optional[str]) -> Dict[str, object]:
    """The measured spend of the window next to the ledger's metered sum, or why it has no number."""
    account = account_observations.account_for(name)
    if account is None:
        return {"account": None, "spend_usd": None, "observations": 0, "deviation_pct": None,
                "unavailable": account_observations.NO_ACCOUNT, "ledger_cost_usd": totals.cost_usd}
    delta = account_observations.calibration_delta(account, since, ledger_path=ledger)
    return {"account": account, "spend_usd": delta["spend_usd"], "observations": delta["observations"],
            "unavailable": delta["unavailable"], "ledger_cost_usd": totals.cost_usd,
            "deviation_pct": account_observations.deviation_pct(totals.cost_usd, delta["spend_usd"])}


def _as_text(key: str, ledger: Path, since: Optional[str], report) -> str:
    title = f"cost ledger report by {key} (ledger: {ledger}" + (f", since: {since})" if since else ")")
    rows = [_cells(name, totals) for name, totals in _ordered(report)] + [_cells("total", report.totals)]
    lines = [title, "", *_table([HEADER, *rows])]
    read_lines = _read_lines(report.totals)
    return "\n".join(lines + (["", *read_lines] if read_lines else []))


def _row(name: str, totals, calibration: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """One group (or the total) as JSON: the sums unrounded, the counts as they are, and the calibration."""
    row: Dict[str, object] = {"group": name, "runs": len(totals.runs), "cost_usd": totals.cost_usd,
                              "api_equivalent_usd": totals.api_equivalent_usd, "tokens": dict(totals.tokens),
                              "tokens_unknown": dict(totals.tokens_unknown),
                              "unknown_token_runs": totals.unknown_token_runs,
                              "unavailable_runs": len(totals.unavailable), "complete_runs": totals.complete_runs}
    if calibration is not None:
        row["calibration"] = calibration
    return row


def _as_json(key: str, ledger: Path, since: Optional[str], report) -> Dict[str, object]:
    totals, measured = report.totals, key == "provider"
    return {"by": key, "since": since, "ledger": str(ledger),
            "groups": [_row(name, group, _calibration(name, group, ledger, since) if measured else None)
                       for name, group in _ordered(report)],
            "totals": _row("total", totals, _calibration("total", totals, ledger, since) if measured else None),
            "spool_pending": totals.spool_pending, "spool_unreadable": totals.spool_unreadable,
            "spool_conflicts": totals.spool_conflicts, "spool_quarantined": totals.spool_quarantined,
            "legacy_lines": totals.legacy_lines, "unreadable_lines": totals.unreadable_lines,
            "torn_tail": totals.torn_tail, "unlocked_read": totals.unlocked_read, "incomplete": totals.incomplete}


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.by not in cost_ledger.GROUP_KEYS:
        print(f"vnx costs report: --by {args.by!r} is not one of {', '.join(cost_ledger.GROUP_KEYS)}", file=sys.stderr)
        return EXIT_USAGE
    try:
        since = None if args.since is None else date.fromisoformat(args.since).isoformat()
    except ValueError as exc:
        print(f"vnx costs report: --since {args.since!r} is not a date YYYY-MM-DD: {exc}", file=sys.stderr)
        return EXIT_USAGE
    ledger = _ledger(args.ledger)
    try:
        report = cost_ledger.aggregate_groups(args.by, ledger, select=_window(since))
    except OSError as exc:
        print(f"vnx costs report: the ledger {ledger} cannot be read: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps(_as_json(args.by, ledger, since, report)) if args.json
          else _as_text(args.by, ledger, since, report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
