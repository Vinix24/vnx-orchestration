"""Wave 1 shadow-mode divergence comparator (independent of migration code).

Per P4 lessons §4.5 verifier-independence principle: this comparator MUST NOT
import from scripts/migrate_to_central_vnx.py. Its schema introspection,
counting, and content-hashing logic is built fresh here using only stdlib +
sqlite3, so a bug in the migration cannot mask itself in the verifier.

Six hard metrics per claudedocs/2026-05-09-wave1-design.md §3:
  1. Wrong-project rows on project_id-scoped queries (tolerance: 0)
  2. PR-scoped blocking findings parity (tolerance: 0 divergent dispatches)
  3. IntelligenceSelector top-N parity (tolerance: 0 in top 3)
  4. Per-(project_id, table) row count + content checksum parity (tolerance: 0
     count drift; <0.01% checksum drift)
  5. Lease-key collisions across simultaneous project runs (tolerance: 0)
  6. p95 read latency / error budget (central <= 1.5x per-project p95)

Aggregate-counts tolerance: <0.1% drift (non-decision-support reads only).
"""

from __future__ import annotations

import datetime
import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

SEVERITY_HARD = "hard"
SEVERITY_SOFT = "soft"
SEVERITY_AGGREGATE = "aggregate"
SEVERITY_ADVISORY = "advisory"

# Central latency must be <= this factor * legacy p95
LATENCY_THRESHOLD_FACTOR = 1.5

# Checksum drift tolerance for metric 4 content comparison (<0.01%)
CHECKSUM_DRIFT_TOLERANCE = 0.0001

# Aggregate count drift tolerance (<0.1%)
AGGREGATE_DRIFT_TOLERANCE = 0.001

# Default top-N for metric 3 (per design §3 row 3)
DEFAULT_TOP_N = 3


@dataclass(frozen=True)
class DivergenceEvent:
    """One divergence finding from a shadow comparison."""

    metric_id: int
    severity: str
    project_id: str
    read_site: str
    detail: dict[str, Any]
    legacy_count: int
    central_count: int
    timestamp_iso: str


@dataclass
class ComparisonResult:
    """Result of one shadow comparison."""

    divergences: list[DivergenceEvent] = field(default_factory=list)
    legacy_latency_ms: float = 0.0
    central_latency_ms: float = 0.0
    sql_template_hash: str = ""

    def has_hard_divergence(self) -> bool:
        return any(d.severity == SEVERITY_HARD for d in self.divergences)


# ---------------------------------------------------------------------------
# Helpers — all built fresh, no imports from migrate_to_central_vnx
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sql_template_hash(sql: str) -> str:
    """SHA-256 hash of the parameterized SQL template (parameters stripped)."""
    normalized = re.sub(r":\w+", "?", sql)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _introspect_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names for *table* via PRAGMA table_info (live schema read).

    Do NOT use any column projection from migrate_to_central_vnx.py — this
    function exists specifically to read the live schema independently.
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def _row_to_canonical_bytes(row: Any) -> bytes:
    """Stable byte representation of a row for checksum / sorting."""
    if hasattr(row, "keys"):
        parts = [f"{k}={v!r}" for k, v in sorted(dict(row).items())]
    else:
        parts = [repr(v) for v in row]
    return "|".join(parts).encode()


def _rows_checksum(rows: Sequence[Any]) -> str:
    """SHA-256 over rows, order-independent (sorted by canonical bytes)."""
    h = hashlib.sha256()
    for rb in sorted(_row_to_canonical_bytes(r) for r in rows):
        h.update(rb)
        h.update(b"\n")
    return h.hexdigest()


def _get_field(row: Any, *keys: str) -> Any:
    """Try each key on a dict-like row; return None if none found."""
    if not hasattr(row, "__getitem__"):
        return None
    for k in keys:
        try:
            v = row[k]
            if v is not None:
                return v
        except (KeyError, IndexError):
            pass
    return None


def _project_id_of(row: Any) -> str | None:
    v = _get_field(row, "project_id")
    return str(v) if v is not None else None


# ---------------------------------------------------------------------------
# Per-metric comparators
# ---------------------------------------------------------------------------


def _compare_metric_1_wrong_project_rows(
    legacy: Sequence[Any],
    central: Sequence[Any],
    project_id: str,
    read_site: str,
) -> list[DivergenceEvent]:
    """Metric 1: any row whose project_id != requested project_id is a violation."""
    wrong_legacy = [r for r in legacy if _project_id_of(r) != project_id]
    wrong_central = [r for r in central if _project_id_of(r) != project_id]
    if not (wrong_legacy or wrong_central):
        return []
    return [
        DivergenceEvent(
            metric_id=1,
            severity=SEVERITY_HARD,
            project_id=project_id,
            read_site=read_site,
            detail={
                "wrong_legacy_count": len(wrong_legacy),
                "wrong_central_count": len(wrong_central),
                "sample_legacy_pids": [_project_id_of(r) for r in wrong_legacy[:3]],
                "sample_central_pids": [_project_id_of(r) for r in wrong_central[:3]],
            },
            legacy_count=len(wrong_legacy),
            central_count=len(wrong_central),
            timestamp_iso=_now_iso(),
        )
    ]


def _finding_hash(row: Any) -> str:
    """Extract or compute a content hash for a blocking-finding row."""
    v = _get_field(row, "hash", "finding_hash", "content_hash")
    if v is not None:
        return str(v)
    return hashlib.sha256(_row_to_canonical_bytes(row)).hexdigest()


def _compare_metric_2_blocking_findings(
    legacy: Sequence[Any],
    central: Sequence[Any],
    pr_id: str,
    read_site: str,
) -> list[DivergenceEvent]:
    """Metric 2: SET of blocking finding hashes must match exactly."""
    legacy_hashes = {_finding_hash(r) for r in legacy}
    central_hashes = {_finding_hash(r) for r in central}
    missing = legacy_hashes - central_hashes
    extra = central_hashes - legacy_hashes
    if not (missing or extra):
        return []
    return [
        DivergenceEvent(
            metric_id=2,
            severity=SEVERITY_HARD,
            project_id=pr_id,
            read_site=read_site,
            detail={
                "missing_in_central": sorted(missing),
                "extra_in_central": sorted(extra),
            },
            legacy_count=len(legacy_hashes),
            central_count=len(central_hashes),
            timestamp_iso=_now_iso(),
        )
    ]


def _item_id(row: Any) -> str:
    """Extract item identifier for top-N comparison."""
    v = _get_field(row, "item_id", "pattern_id", "id", "dispatch_id")
    return str(v) if v is not None else repr(row)


def _compare_metric_3_top_n_parity(
    legacy_top_n: Sequence[Any],
    central_top_n: Sequence[Any],
    n: int,
    project_id: str,
    read_site: str,
) -> list[DivergenceEvent]:
    """Metric 3: ordered top-N items must be identical (item_id-stable)."""
    legacy_ids = [_item_id(r) for r in legacy_top_n[:n]]
    central_ids = [_item_id(r) for r in central_top_n[:n]]
    if legacy_ids == central_ids:
        return []

    divergent = [
        i for i, (l, c) in enumerate(zip(legacy_ids, central_ids)) if l != c
    ]
    if len(legacy_ids) != len(central_ids):
        divergent += list(range(min(len(legacy_ids), len(central_ids)), max(len(legacy_ids), len(central_ids))))

    return [
        DivergenceEvent(
            metric_id=3,
            severity=SEVERITY_HARD,
            project_id=project_id,
            read_site=read_site,
            detail={
                "n": n,
                "legacy_top_n": legacy_ids,
                "central_top_n": central_ids,
                "divergent_positions": divergent,
            },
            legacy_count=len(legacy_ids),
            central_count=len(central_ids),
            timestamp_iso=_now_iso(),
        )
    ]


def _compare_metric_4_count_and_checksum(
    legacy: Sequence[Any],
    central: Sequence[Any],
    project_id: str,
    table: str | None,
    read_site: str,
) -> list[DivergenceEvent]:
    """Metric 4: row count must match exactly; checksum drift must be <0.01%.

    Severity split per Wave 1 design §3 metric 4:
    - table is None: SEVERITY_ADVISORY — caller must pass the actual table name.
    - count drift > 0: SEVERITY_HARD — zero-tolerance, structural correctness.
    - checksum drift in (0, 0.01%]: SEVERITY_SOFT — race-window allowance.
    - checksum drift > 0.01%: SEVERITY_HARD — above tolerance threshold.
    """
    if table is None:
        return [
            DivergenceEvent(
                metric_id=4,
                severity=SEVERITY_ADVISORY,
                project_id=project_id,
                read_site=read_site,
                detail={"reason": "table_identity_missing"},
                legacy_count=len(legacy),
                central_count=len(central),
                timestamp_iso=_now_iso(),
            )
        ]

    legacy_count = len(legacy)
    central_count = len(central)

    if legacy_count != central_count:
        return [
            DivergenceEvent(
                metric_id=4,
                severity=SEVERITY_HARD,
                project_id=project_id,
                read_site=read_site,
                detail={
                    "table": table,
                    "count_drift": central_count - legacy_count,
                    "kind": "count_mismatch",
                },
                legacy_count=legacy_count,
                central_count=central_count,
                timestamp_iso=_now_iso(),
            )
        ]

    if legacy_count == 0:
        return []

    legacy_cksum = _rows_checksum(legacy)
    central_cksum = _rows_checksum(central)
    if legacy_cksum == central_cksum:
        return []

    # Checksums differ — compute per-row drift (order-independent via sorted bytes)
    legacy_sorted = sorted(_row_to_canonical_bytes(r) for r in legacy)
    central_sorted = sorted(_row_to_canonical_bytes(r) for r in central)
    diff_count = sum(1 for lb, cb in zip(legacy_sorted, central_sorted) if lb != cb)
    drift_pct = diff_count / legacy_count

    if drift_pct < CHECKSUM_DRIFT_TOLERANCE:
        return [
            DivergenceEvent(
                metric_id=4,
                severity=SEVERITY_SOFT,
                project_id=project_id,
                read_site=read_site,
                detail={
                    "table": table,
                    "legacy_checksum": legacy_cksum,
                    "central_checksum": central_cksum,
                    "drift_pct": drift_pct,
                    "kind": "checksum_drift",
                    "within_tolerance": True,
                },
                legacy_count=legacy_count,
                central_count=central_count,
                timestamp_iso=_now_iso(),
            )
        ]

    return [
        DivergenceEvent(
            metric_id=4,
            severity=SEVERITY_HARD,
            project_id=project_id,
            read_site=read_site,
            detail={
                "table": table,
                "legacy_checksum": legacy_cksum,
                "central_checksum": central_cksum,
                "drift_pct": drift_pct,
                "kind": "checksum_drift",
            },
            legacy_count=legacy_count,
            central_count=central_count,
            timestamp_iso=_now_iso(),
        )
    ]


def _lease_key(row: Any) -> str:
    v = _get_field(row, "lease_key", "key", "terminal_id", "name")
    return str(v) if v is not None else repr(row)


def _compare_metric_5_lease_collisions(
    legacy_leases: Sequence[Any],
    central_leases: Sequence[Any],
    project_id: str,
    read_site: str,
) -> list[DivergenceEvent]:
    """Metric 5: same lease key held by multiple projects simultaneously is fatal.

    A lease row with missing project_id is treated as a structural violation
    (SEVERITY_HARD) — we never substitute the requesting project_id to fill missing
    data, as that would collapse two different projects with the same lease_key into
    the same synthetic owner and mask the exact cross-project collision this metric
    is designed to catch.
    """
    events: list[DivergenceEvent] = []
    # Build map: lease_key -> list of project_ids (None if absent in the row)
    central_map: dict[str, list[str | None]] = {}

    for row in central_leases:
        key = _lease_key(row)
        pid = _project_id_of(row)

        if pid is None:
            events.append(
                DivergenceEvent(
                    metric_id=5,
                    severity=SEVERITY_HARD,
                    project_id=project_id,
                    read_site=read_site,
                    detail={
                        "reason": "missing_project_id_in_lease_row",
                        "lease_key": key,
                        "table": "terminal_leases",
                    },
                    legacy_count=len(legacy_leases),
                    central_count=len(central_leases),
                    timestamp_iso=_now_iso(),
                )
            )

        central_map.setdefault(key, []).append(pid)

    # Detect collisions: same lease_key held by multiple distinct owners.
    # A None alongside any other entry (real pid or another None) is a collision-suspect
    # because we cannot confirm both rows belong to the same project.
    collisions = []
    for k, pids in central_map.items():
        if len(pids) <= 1:
            continue
        distinct_real = set(p for p in pids if p is not None)
        has_none = any(p is None for p in pids)
        if len(distinct_real) > 1 or (has_none and len(pids) > 1):
            entry: dict[str, Any] = {
                "lease_key": k,
                "projects": sorted(distinct_real),
            }
            if has_none:
                entry["has_missing_project_id"] = True
            collisions.append(entry)

    if collisions:
        events.append(
            DivergenceEvent(
                metric_id=5,
                severity=SEVERITY_HARD,
                project_id=project_id,
                read_site=read_site,
                detail={"collisions": collisions},
                legacy_count=len(legacy_leases),
                central_count=len(central_leases),
                timestamp_iso=_now_iso(),
            )
        )

    return events


def _compare_metric_6_latency(
    legacy_latency_ms: float,
    central_latency_ms: float,
    project_id: str,
    read_site: str,
) -> list[DivergenceEvent]:
    """Metric 6: central must be <= LATENCY_THRESHOLD_FACTOR * legacy p95."""
    if legacy_latency_ms <= 0:
        return []
    if central_latency_ms <= LATENCY_THRESHOLD_FACTOR * legacy_latency_ms:
        return []
    return [
        DivergenceEvent(
            metric_id=6,
            severity=SEVERITY_SOFT,
            project_id=project_id,
            read_site=read_site,
            detail={
                "legacy_latency_ms": legacy_latency_ms,
                "central_latency_ms": central_latency_ms,
                "threshold_factor": LATENCY_THRESHOLD_FACTOR,
                "actual_factor": central_latency_ms / legacy_latency_ms,
            },
            legacy_count=int(legacy_latency_ms),
            central_count=int(central_latency_ms),
            timestamp_iso=_now_iso(),
        )
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compare(
    legacy_rows: Sequence[Any],
    central_rows: Sequence[Any],
    project_id: str,
    read_site: str,
    sql_template: str,
    metric_id: int,
    *,
    legacy_latency_ms: float = 0.0,
    central_latency_ms: float = 0.0,
    table: str | None = None,
) -> ComparisonResult:
    """Compare two result sets and return divergence findings.

    Caller is responsible for picking the right metric_id based on the read
    site's semantics. The verifier does NOT decide which metric applies; it
    applies the metric the caller specifies. This keeps the comparator
    schema-agnostic and prevents accidental coupling to migration logic.

    For metric_id=4, callers MUST pass `table` (the actual table name). If
    omitted, a SEVERITY_ADVISORY divergence is emitted instead of the comparison
    — callers that forget `table` are flagged, not silently mis-labeled.
    """
    result = ComparisonResult(
        legacy_latency_ms=legacy_latency_ms,
        central_latency_ms=central_latency_ms,
        sql_template_hash=_sql_template_hash(sql_template),
    )

    if metric_id == 1:
        result.divergences = _compare_metric_1_wrong_project_rows(
            legacy_rows, central_rows, project_id, read_site
        )
    elif metric_id == 2:
        result.divergences = _compare_metric_2_blocking_findings(
            legacy_rows, central_rows, project_id, read_site
        )
    elif metric_id == 3:
        result.divergences = _compare_metric_3_top_n_parity(
            legacy_rows, central_rows, DEFAULT_TOP_N, project_id, read_site
        )
    elif metric_id == 4:
        result.divergences = _compare_metric_4_count_and_checksum(
            legacy_rows, central_rows, project_id, table, read_site
        )
    elif metric_id == 5:
        result.divergences = _compare_metric_5_lease_collisions(
            legacy_rows, central_rows, project_id, read_site
        )
    elif metric_id == 6:
        result.divergences = _compare_metric_6_latency(
            legacy_latency_ms, central_latency_ms, project_id, read_site
        )
    else:
        raise ValueError(f"Unknown metric_id: {metric_id}. Must be 1-6.")

    return result


def compare_aggregate_count(
    legacy_count: int,
    central_count: int,
    project_id: str,
    read_site: str,
    sql_template: str,
    *,
    legacy_latency_ms: float = 0.0,
    central_latency_ms: float = 0.0,
) -> ComparisonResult:
    """Compare aggregate counts for non-decision-support reads.

    Tolerates up to AGGREGATE_DRIFT_TOLERANCE (0.1%) drift. Used for
    dashboard rollups and non-governance-critical counts only — NOT for
    gate evidence or intelligence injection reads.
    """
    result = ComparisonResult(
        legacy_latency_ms=legacy_latency_ms,
        central_latency_ms=central_latency_ms,
        sql_template_hash=_sql_template_hash(sql_template),
    )

    drift = abs(legacy_count - central_count) / max(legacy_count, 1)
    if drift >= AGGREGATE_DRIFT_TOLERANCE:
        result.divergences.append(
            DivergenceEvent(
                metric_id=4,
                severity=SEVERITY_AGGREGATE,
                project_id=project_id,
                read_site=read_site,
                detail={
                    "drift_pct": drift,
                    "tolerance": AGGREGATE_DRIFT_TOLERANCE,
                    "kind": "aggregate_count",
                },
                legacy_count=legacy_count,
                central_count=central_count,
                timestamp_iso=_now_iso(),
            )
        )

    return result
