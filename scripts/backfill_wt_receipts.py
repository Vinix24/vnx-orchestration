#!/usr/bin/env python3
"""Backfill recent worktree-only receipts into the central receipt ledger.

Dry-run is the default. Pass --apply to create a verified backup and append the
selected archive lines with a provenance marker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from project_root import resolve_state_dir  # noqa: E402

DEFAULT_CENTRAL = resolve_state_dir(caller_file=__file__) / "t0_receipts.ndjson"
CUTOFF_DATE = "20260515"
BACKFILLED_FROM = "wt-archive-20260614"
KEY_FIELDS = ("dispatch_id", "cmd_id", "trace_token", "task_id")


@dataclass(frozen=True)
class ReceiptLine:
    key: str
    key_field: str | None
    raw: str
    record: dict[str, Any]


@dataclass(frozen=True)
class ParsedLedger:
    path: Path
    receipts: tuple[ReceiptLine, ...]
    blank_lines: int
    unparseable_lines: int

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(receipt.key for receipt in self.receipts)


@dataclass(frozen=True)
class BackfillPlan:
    archive: ParsedLedger
    central: ParsedLedger
    selected_keys: tuple[str, ...]
    lines_to_append: tuple[ReceiptLine, ...]


@dataclass(frozen=True)
class ApplyResult:
    backup_path: Path | None
    appended_count: int
    verbatim_count: int
    old_line_count: int
    new_line_count: int


def receipt_key(record: dict[str, Any], raw_line: str) -> str:
    """Return the first usable receipt identifier, or a stable raw-line hash."""
    field = receipt_key_field(record)
    if field is not None:
        return str(record[field]).strip()
    return hashlib.sha1(raw_line.encode("utf-8")).hexdigest()


def receipt_key_field(record: dict[str, Any]) -> str | None:
    """Return the field supplying the stable key, or None for SHA-1 fallback."""
    for field in KEY_FIELDS:
        value = record.get(field)
        if value is not None and str(value).strip():
            return field
    return None


def parse_ledger(path: Path) -> ParsedLedger:
    """Parse an NDJSON ledger, counting and skipping blank or invalid lines."""
    receipts: list[ReceiptLine] = []
    blank_lines = 0
    unparseable_lines = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_with_newline in handle:
            raw = raw_with_newline.rstrip("\r\n")
            if not raw.strip():
                blank_lines += 1
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                unparseable_lines += 1
                continue
            if not isinstance(record, dict):
                unparseable_lines += 1
                continue
            receipts.append(
                ReceiptLine(receipt_key(record, raw), receipt_key_field(record), raw, record)
            )

    return ParsedLedger(path, tuple(receipts), blank_lines, unparseable_lines)


def _receipt_date(record: dict[str, Any]) -> str | None:
    """Return an eight-digit receipt date when the timestamp exposes one."""
    timestamp = str(record.get("timestamp") or "").strip()
    candidate = timestamp[:10].replace("-", "")
    return candidate if len(candidate) == 8 and candidate.isdigit() else None


def _non_date_key_is_not_known_old(receipts: Sequence[ReceiptLine]) -> bool:
    """Keep non-date IDs unless their receipt timestamps prove they are old.

    The archive contains old non-date dispatch IDs and SHA-1 fallback keys. Without
    this guard, the general non-date inclusion rule would cross the deliberate
    archive boundary. Missing timestamps remain included because their age cannot
    be proven.
    """
    dates = [_receipt_date(receipt.record) for receipt in receipts]
    if any(date is None for date in dates):
        return True
    return any(date > CUTOFF_DATE for date in dates if date is not None)


def build_plan(archive_path: Path, central_path: Path) -> BackfillPlan:
    """Build an ordered, read-only plan of archive receipt lines to append."""
    archive = parse_ledger(archive_path)
    central = parse_ledger(central_path)
    archive_only = archive.keys - central.keys
    archive_by_key: dict[str, list[ReceiptLine]] = defaultdict(list)
    for receipt in archive.receipts:
        archive_by_key[receipt.key].append(receipt)

    selected_keys: list[str] = []
    selected_set: set[str] = set()
    for receipt in archive.receipts:
        prefix = receipt.key[:8]
        has_date_prefix = receipt.key_field is not None and prefix.isdigit()
        non_date_allowed = (
            not has_date_prefix
            and _non_date_key_is_not_known_old(archive_by_key[receipt.key])
        )
        if (
            receipt.key in archive_only
            and receipt.key not in selected_set
            and (has_date_prefix and prefix > CUTOFF_DATE or non_date_allowed)
        ):
            selected_keys.append(receipt.key)
            selected_set.add(receipt.key)

    lines_to_append = tuple(
        receipt for receipt in archive.receipts if receipt.key in selected_set
    )
    return BackfillPlan(archive, central, tuple(selected_keys), lines_to_append)


def _line_count(path: Path) -> int:
    """Count physical lines, including a final line without a newline."""
    line_count = 0
    final_byte = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            line_count += chunk.count(b"\n")
            final_byte = chunk[-1:]
    if final_byte and final_byte != b"\n":
        line_count += 1
    return line_count


def _ends_with_newline(path: Path) -> bool:
    if path.stat().st_size == 0:
        return True
    with path.open("rb") as handle:
        handle.seek(-1, 2)
        return handle.read(1) == b"\n"


def _render_backfilled_line(raw_line: str) -> tuple[str, bool]:
    """Add the provenance marker, falling back to verbatim for invalid JSON."""
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError:
        return raw_line, True
    if not isinstance(record, dict):
        return raw_line, True
    record["backfilled_from"] = BACKFILLED_FROM
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")), False


def apply_plan(plan: BackfillPlan) -> ApplyResult:
    """Back up central and append the planned lines, then verify line counts."""
    central_path = plan.central.path
    old_line_count = _line_count(central_path)
    appended_count = len(plan.lines_to_append)
    if appended_count == 0:
        return ApplyResult(None, 0, 0, old_line_count, old_line_count)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = Path(f"{central_path}.bak-backfill-{stamp}")
    if backup_path.exists():
        raise RuntimeError(f"refusing to overwrite existing backup: {backup_path}")
    shutil.copy2(central_path, backup_path)
    if backup_path.stat().st_size != central_path.stat().st_size:
        raise RuntimeError(
            f"backup size mismatch: central={central_path.stat().st_size}, "
            f"backup={backup_path.stat().st_size}"
        )

    needs_separator = not _ends_with_newline(central_path)
    verbatim_count = 0
    with central_path.open("a", encoding="utf-8", newline="\n") as handle:
        if needs_separator:
            handle.write("\n")
        for receipt in plan.lines_to_append:
            rendered, verbatim = _render_backfilled_line(receipt.raw)
            handle.write(rendered)
            handle.write("\n")
            verbatim_count += int(verbatim)

    new_line_count = _line_count(central_path)
    expected_line_count = old_line_count + appended_count
    if new_line_count != expected_line_count:
        raise RuntimeError(
            f"line-count verification failed: expected {expected_line_count}, "
            f"found {new_line_count}"
        )

    return ApplyResult(
        backup_path,
        appended_count,
        verbatim_count,
        old_line_count,
        new_line_count,
    )


def print_plan(plan: BackfillPlan) -> None:
    print(f"Archive: {plan.archive.path}")
    print(f"Central: {plan.central.path}")
    print(
        "Archive skipped lines: "
        f"blank={plan.archive.blank_lines}, "
        f"unparseable={plan.archive.unparseable_lines}"
    )
    print(
        "Central skipped lines: "
        f"blank={plan.central.blank_lines}, "
        f"unparseable={plan.central.unparseable_lines}"
    )
    print(f"Selected key count: {len(plan.selected_keys)}")
    print(f"Total line count to append: {len(plan.lines_to_append)}")
    print("Selected dispatch_ids:")
    for key in plan.selected_keys:
        print(f"- {key}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        required=True,
        help="Path to the worktree-archive t0_receipts.ndjson to backfill from.",
    )
    parser.add_argument("--central", type=Path, default=DEFAULT_CENTRAL)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Back up central and append selected receipts (default: dry-run).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_plan(args.archive, args.central)
        print_plan(plan)
        if not args.apply:
            print("Mode: DRY-RUN (no changes made)")
            return 0

        result = apply_plan(plan)
        if result.appended_count == 0:
            print("Mode: APPLY; selected 0 lines, no backup or append performed")
            print(
                f"Line-count verification: old={result.old_line_count}, "
                f"new={result.new_line_count}, appended=0"
            )
            return 0

        print(f"Mode: APPLY; backup={result.backup_path}")
        print(f"Appended lines: {result.appended_count}")
        print(f"Verbatim unparseable lines appended: {result.verbatim_count}")
        print(
            f"Line-count verification: old={result.old_line_count}, "
            f"new={result.new_line_count}, appended={result.appended_count}"
        )
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"backfill_wt_receipts: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
