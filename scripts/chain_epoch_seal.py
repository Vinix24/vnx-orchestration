#!/usr/bin/env python3
"""ADR-029 chain-epoch seal migration.

Appends a ``chain_epoch_start`` marker to a receipt ledger BEFORE enabling
``VNX_CHAIN_RECEIPTS`` default-on, so that new receipts chain within a fresh
epoch and ``verify_chain`` returns ``verified-segmented`` (healthy) instead of
``broken`` (the naive-flip footgun). The immutable pre-adoption entries stay
epoch 0.

Guarantees:
- Idempotent: a ledger that is already chaining (its last entry carries
  ``prev_hash``) is a no-op — running the seal twice never double-appends.
- Append-only: never rewrites a historical line (ADR-005 preserved).

Usage:
    python3 scripts/chain_epoch_seal.py <ledger.ndjson> [<ledger2.ndjson> ...]
    python3 scripts/chain_epoch_seal.py --dry-run <ledger.ndjson>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from ndjson_hash_chain import (  # type: ignore[import]
    append_epoch_marker,
    epoch_state,
    seal_chain_origin,
    verify_chain,
)


def seal_ledger(path: Path) -> dict:
    """Idempotently seal ``path`` with a chain-epoch marker. Returns a result dict.

    - ``action="noop"`` when the ledger is already chaining or absent.
    - ``action="sealed"`` when a marker was appended, with the new ``epoch`` and
      the post-seal ``status`` (should be ``verified-segmented`` or ``verified``).

    ADR-033: also pins the ledger's chain-origin anchor (idempotent — a no-op
    once an anchor exists). This only happens on the FIRST seal; a later
    re-seal (opening epoch 2+) leaves the original anchor untouched, since the
    origin is the ledger's earliest chained point, not its latest.
    """
    if not path.exists() or path.stat().st_size == 0:
        # A fresh/empty ledger needs no seal: the first appended receipt chains
        # from GENESIS on its own.
        return {"ledger": str(path), "action": "noop", "reason": "ledger empty or absent"}

    max_epoch, chaining_active = epoch_state(path)
    if chaining_active:
        seal_chain_origin(path)
        return {"ledger": str(path), "action": "noop", "reason": "already chaining", "epoch": max_epoch}

    epoch = max_epoch + 1
    marker_hash = append_epoch_marker(path, epoch)
    _ok, _violations, status = verify_chain(path)
    seal_chain_origin(path)
    return {
        "ledger": str(path),
        "action": "sealed",
        "epoch": epoch,
        "marker_hash": marker_hash,
        "status": status,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ADR-029 chain-epoch seal migration")
    ap.add_argument("ledger", nargs="+", help="path(s) to NDJSON ledger(s) to seal")
    ap.add_argument("--dry-run", action="store_true", help="report what would happen; do not write")
    args = ap.parse_args(argv)

    rc = 0
    for raw in args.ledger:
        path = Path(raw)
        if args.dry_run:
            if not path.exists() or path.stat().st_size == 0:
                print(f"[dry-run] {path}: noop (empty or absent)")
                continue
            max_epoch, active = epoch_state(path)
            if active:
                print(f"[dry-run] {path}: noop (already chaining, epoch {max_epoch})")
            else:
                print(f"[dry-run] {path}: would seal epoch {max_epoch + 1}")
            continue

        res = seal_ledger(path)
        if res["action"] == "sealed":
            print(f"[seal] {res['ledger']}: sealed epoch {res['epoch']} -> status={res['status']}")
            if res["status"] not in ("verified-segmented", "verified"):
                print(f"[seal] WARNING: post-seal status is {res['status']!r}, not healthy", file=sys.stderr)
                rc = 1
        else:
            print(f"[seal] {res['ledger']}: noop ({res.get('reason')})")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
