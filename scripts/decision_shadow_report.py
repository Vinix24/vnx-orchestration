#!/usr/bin/env python3
"""Report the decision-judge SHADOW divergence (ADR-028 Phase 2).

Prints how often the shadow judge agreed with T0's actual decision — the data that
justifies (or blocks) turning on Phases 3-4. Reads the per-project central store by
default; pass --state-dir to target another.

    python3 scripts/decision_shadow_report.py [--state-dir DIR] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from decision_shadow import DIVERGENCE_LEDGER, divergence_summary  # noqa: E402


def _recent(state_dir: Path, limit: int) -> list[dict]:
    path = state_dir / DIVERGENCE_LEDGER
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows[-limit:]


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="ADR-028 Phase 2 shadow divergence report")
    parser.add_argument("--state-dir", default=None, help="central state dir (default: resolved)")
    parser.add_argument("--limit", type=int, default=10, help="recent divergences to show")
    args = parser.parse_args(argv)

    if args.state_dir is not None:
        state_dir = Path(args.state_dir)
    else:
        from vnx_paths import resolve_state_dir  # noqa: PLC0415
        state_dir = resolve_state_dir()

    summary = divergence_summary(state_dir=state_dir)
    print(f"VNX decision-judge SHADOW divergence — {state_dir}")
    if summary["total"] == 0:
        print("  no divergence records yet (shadow off, or no decisions logged).")
        return 0

    rate = summary["agree_rate"]
    print(f"  decisions compared : {summary['total']}")
    print(f"  judge agreed       : {summary['agree']}")
    print(f"  judge diverged     : {summary['disagree']}")
    print(f"  agreement rate     : {rate:.1%}" if rate is not None else "  agreement rate     : n/a")

    recent = [r for r in _recent(state_dir, args.limit) if r.get("agree") is False]
    if recent:
        print(f"\n  recent divergences (judge != T0), last {len(recent)}:")
        for r in recent:
            print(
                f"    {r.get('decision_id','?')}: T0={r.get('actual_action')!r} "
                f"judge={r.get('advisory_action')!r} (conf {r.get('advisory_confidence')})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
