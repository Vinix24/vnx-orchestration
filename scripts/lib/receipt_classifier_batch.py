"""Hourly batch processor for queued receipts (ARC-3).

Run by the launchd job `com.vnx.receipt-classifier-batch`. Drains the queue
written by `_append_to_queue`, builds a single batch prompt, runs the
configured provider, queues any significant suggested edits.

CLI:
    python3 scripts/lib/receipt_classifier_batch.py [--dry-run] [--max-receipts N]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from receipt_classifier import (  # noqa: E402
    classify_batch,
    drain_queue,
    is_budget_exhausted,
    is_enabled,
)

logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Receipt classifier hourly batch (ARC-3)")
    parser.add_argument("--dry-run", action="store_true", help="Drain queue but do not call provider")
    parser.add_argument(
        "--max-receipts",
        type=int,
        default=200,
        help="Cap receipts per batch call (defaults to 200 to keep prompt small)",
    )
    parser.add_argument("--force", action="store_true", help="Run even when disabled (debugging only)")
    args = parser.parse_args(argv)

    if not is_enabled() and not args.force:
        print(json.dumps({"status": "skipped", "reason": "disabled"}))
        return 0

    receipts = drain_queue()
    if not receipts:
        print(json.dumps({"status": "ok", "drained": 0}))
        return 0

    if args.dry_run:
        print(json.dumps({"status": "dry_run", "drained": len(receipts)}))
        return 0

    if is_budget_exhausted():
        print(
            json.dumps(
                {"status": "skipped", "reason": "budget_exhausted", "drained": len(receipts)}
            )
        )
        return 0

    cap = max(1, int(args.max_receipts))
    batch_slice = receipts[:cap]
    result = classify_batch(batch_slice)
    result["drained"] = len(receipts)
    result["processed"] = len(batch_slice)
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
