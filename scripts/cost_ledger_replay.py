#!/usr/bin/env python3
"""cost_ledger_replay.py — ``vnx costs replay [--json]``: bring the cost ledger's spool back (ADR-043 §6).

Track ``kosten-per-model-in-het-grootboek``, K2a-3b. One bounded call of
``cost_ledger.replay_spool`` on this project's store. The store comes from
``vnx_paths.resolve_paths`` (``VNX_DATA_DIR`` only with
``VNX_DATA_DIR_EXPLICIT=1``), the ledger in it from
``provider_costs.costs_path_in``; no path is written out here. The bounds come
from ``VNX_COST_LEDGER_REPLAY_BATCH``, ``VNX_COST_LEDGER_REPLAY_BUDGET_SEC``
and ``VNX_COST_LEDGER_LOCK_WAIT_SEC``.

The fabric's ``bin/vnx costs replay`` and the pip CLI's ``vnx costs replay``
both run this script.

Exit codes:
  0  done, nothing to do, or a bound left files for the next call
  1  the spool directory cannot be listed, a write failed, a spooled record is in the ledger under its
     ``record_id`` with other content (``readback_mismatch``: its file stays), or the store cannot be resolved
  3  the lock file stayed taken for the whole wait, or under it the ledger ``flock`` or the receipt ledger's
     append lock for what was left of the wait and the budget (``lock_busy``): nothing more was written

Usage:
  python3 scripts/cost_ledger_replay.py [--json]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(SCRIPT_DIR / "lib"), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

EXIT_FAILED = 1


def _ledger_path() -> Path:
    """The cost ledger of this project's store."""
    import provider_costs
    from vnx_paths import resolve_paths

    return provider_costs.costs_path_in(Path(resolve_paths()["VNX_DATA_DIR"]))


def _summary(ledger: Path, result) -> str:
    """The result in one line for a person."""
    line = (
        f"cost ledger {ledger}: {result.files_replayed} spool files replayed "
        f"({result.records_written} records written, {result.records_already_landed} already landed, "
        f"{result.records_renumbered} renumbered), {result.files_quarantined} quarantined, "
        f"{result.files_mismatched} kept for a mismatch, {result.files_remaining} remaining"
    )
    if result.stopped:
        line += f"; stopped: {result.stopped}" + (f" ({result.detail})" if result.detail else "")
    return line


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vnx costs replay",
        description="Bring the cost ledger's spool back into the ledger, bounded (ADR-043 §6).",
    )
    parser.add_argument("--json", action="store_true", help="print the result as one JSON object")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    import cost_ledger

    try:
        ledger = _ledger_path()
    except (OSError, ValueError) as exc:
        print(f"vnx costs replay: the store cannot be resolved: {exc}", file=sys.stderr)
        return EXIT_FAILED
    result = cost_ledger.replay_spool(ledger)
    print(json.dumps(result.as_dict()) if args.json else _summary(ledger, result))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
