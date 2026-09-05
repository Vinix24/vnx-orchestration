#!/usr/bin/env python3
"""guard_reachability_store.py — fill-rate measurement against a real store.

The MEASURE half of the golf-4 unreachable-guard detector
(``guard_reachability_scanner.py`` is the static half). A guard that reads a
field is only a defect if that field is, in practice, never filled — this
module answers that question against three store shapes actually used in
this repo: an NDJSON ledger (the receipt grootboek), a SQLite table+column
(``runtime_coordination.db``), or a directory of per-dispatch JSON documents
(staged ``dispatch-spec.json`` bundles).

A missing SQLite column is reported as its own state (``exists=False``), not
folded into "zero filled" — the PRD is explicit that these are the two
distinct, separately-reportable strengths of evidence: a column that never
existed is the stronger case.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class FillRate:
    """One field's measured presence in a real store.

    ``exists=False`` means the backing column/table was not found at all
    (the strongest defect signal). ``exists=True, total=0`` means the store
    was reachable but had no rows to measure (inconclusive, not a violation —
    an empty ledger is not evidence of an unreachable guard).
    """

    exists: bool
    total: int
    filled: int

    @property
    def fill_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return 100.0 * self.filled / self.total

    @property
    def is_zero_fill(self) -> bool:
        """True only when the store was measurable and EVERY row was empty.

        Deliberately excludes ``total == 0`` (nothing to measure is not the
        same claim as "measured N rows, all empty") and deliberately excludes
        any nonzero fill, however small — a 2% fill rate is a design/adoption
        question, not the "structurally cannot fire" defect this detector
        targets (see percentage-over-een-gemengde-populatie-meet-de-mengverhouding:
        a nonzero rate can still be the wrong number for other reasons, but
        it is not this bug class).
        """
        return self.exists and self.total > 0 and self.filled == 0


def _is_filled(value: object) -> bool:
    return value not in (None, "", [], {})


def measure_ndjson_fill_rate(paths: Sequence[Path], field: str) -> FillRate:
    total = 0
    filled = 0
    for p in paths:
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                total += 1
                if field in rec and _is_filled(rec[field]):
                    filled += 1
    return FillRate(exists=True, total=total, filled=filled)


def measure_json_dir_fill_rate(dirpath: Path, glob: str, field: str) -> FillRate:
    total = 0
    filled = 0
    if dirpath.is_dir():
        for p in sorted(dirpath.rglob(glob)):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(rec, dict):
                continue
            total += 1
            if field in rec and _is_filled(rec[field]):
                filled += 1
    return FillRate(exists=True, total=total, filled=filled)


def measure_sqlite_column_fill_rate(db_path: Path, table: str, column: str) -> FillRate:
    if not (_IDENTIFIER_RE.match(table) and _IDENTIFIER_RE.match(column)):
        raise ValueError(f"unsafe identifier: table={table!r} column={column!r}")
    if not db_path.exists():
        return FillRate(exists=False, total=0, filled=0)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error:
        return FillRate(exists=False, total=0, filled=0)
    try:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            return FillRate(exists=False, total=0, filled=0)
        total, filled = conn.execute(
            f"SELECT COUNT(*), COUNT({column}) FROM {table}"
        ).fetchone()
        return FillRate(exists=True, total=int(total), filled=int(filled))
    finally:
        conn.close()
