#!/usr/bin/env python3
"""scout_effectiveness — measurement harness for the cheap-model scout pre-pass.

Correlates governed dispatch receipts with scout sidecars / scout audit receipts
to measure whether scout-enriched dispatches are cheaper/faster/more successful
than non-enriched dispatches on the same corpus.

Design notes:
  - Read-only over receipts and sidecars; never mutates state.
  - Sidecar existence is the scout-enriched flag (matches ``scout_prepass``).
  - Reports are observational, not causal: dispatch selection is NOT randomized,
    so confounders (task difficulty, role, model, etc.) may explain deltas.
  - True rework attribution lives in ``quality_intelligence.db``
    (``dispatch_metadata.parent_dispatch``). This harness uses receipt
    ``status == "success"`` as an observational first-pass-yield proxy and
    clearly labels it as such.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

RECEIPT_FILE = "t0_receipts.ndjson"
SCOUT_RECEIPT_FILE = "scout_receipts.ndjson"
SCOUT_SUBDIR = "scout"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value == "":
            return None
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _load_ndjson(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Load NDJSON lines. Returns (parsed_objects, invalid_line_count)."""
    rows: List[Dict[str, Any]] = []
    invalid = 0
    if not path.is_file():
        return rows, invalid
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                rows.append(parsed)
            else:
                invalid += 1
        except json.JSONDecodeError:
            invalid += 1
    return rows, invalid


def load_receipts(state_dir: Path) -> Tuple[List[Dict[str, Any]], int]:
    return _load_ndjson(Path(state_dir) / RECEIPT_FILE)


def load_scout_receipts(state_dir: Path) -> Tuple[List[Dict[str, Any]], int]:
    return _load_ndjson(Path(state_dir) / SCOUT_RECEIPT_FILE)


def list_scout_sidecar_dispatch_ids(state_dir: Path) -> set[str]:
    """Return dispatch_ids that have a scout sidecar on disk."""
    scout_dir = Path(state_dir) / SCOUT_SUBDIR
    if not scout_dir.is_dir():
        return set()
    ids: set[str] = set()
    for path in scout_dir.iterdir():
        if path.is_file() and path.suffix == ".json":
            # The safe-id contract means path.stem is a valid dispatch_id.
            ids.add(path.stem)
    return ids


# ---------------------------------------------------------------------------
# Receipt extraction
# ---------------------------------------------------------------------------

def _extract_worker_tokens(receipt: Dict[str, Any]) -> Optional[int]:
    """Sum input + output tokens from ``token_usage`` (governed shape)."""
    token_usage = receipt.get("token_usage")
    if not isinstance(token_usage, dict):
        # Tolerate the flatter session keys used by some append paths.
        token_usage = receipt
    input_t = _safe_int(token_usage.get("input")) or _safe_int(token_usage.get("input_tokens"))
    output_t = _safe_int(token_usage.get("output")) or _safe_int(token_usage.get("output_tokens"))
    if input_t is None or output_t is None:
        return None
    return input_t + output_t


def _extract_worker_cost(receipt: Dict[str, Any]) -> Optional[float]:
    return _safe_float(receipt.get("cost_usd"))


def _extract_duration(receipt: Dict[str, Any]) -> Optional[float]:
    return _safe_float(receipt.get("duration_seconds"))


def _is_success(receipt: Dict[str, Any]) -> Optional[bool]:
    status = receipt.get("status")
    if not status:
        return None
    return str(status).strip().lower() == "success"


@dataclass(frozen=True)
class DispatchRecord:
    dispatch_id: str
    provider: str
    model: str
    terminal_id: str
    status: str
    worker_tokens: Optional[int]
    worker_cost_usd: Optional[float]
    duration_seconds: Optional[float]
    is_success: Optional[bool]
    scout_enriched: bool
    scout_cost_usd: float = 0.0


def correlate_records(
    receipts: Iterable[Dict[str, Any]],
    scout_receipts: Iterable[Dict[str, Any]],
    sidecar_ids: Iterable[str],
) -> List[DispatchRecord]:
    """Join worker receipts with scout audit receipts and sidecar flags."""
    scout_cost_by_dispatch: Dict[str, float] = {}
    for sr in scout_receipts:
        if not isinstance(sr, dict):
            continue
        did = str(sr.get("dispatch_id") or "").strip()
        if not did:
            continue
        scout_cost_by_dispatch[did] = scout_cost_by_dispatch.get(did, 0.0) + (
            _safe_float(sr.get("cost_usd")) or 0.0
        )

    sidecar_set = set(sidecar_ids)

    records: List[DispatchRecord] = []
    for r in receipts:
        if not isinstance(r, dict):
            continue
        did = str(r.get("dispatch_id") or "").strip()
        if not did:
            continue
        enriched = did in sidecar_set or did in scout_cost_by_dispatch
        records.append(
            DispatchRecord(
                dispatch_id=did,
                provider=str(r.get("provider") or "unknown").strip(),
                model=str(r.get("model") or "unknown").strip(),
                terminal_id=str(r.get("terminal_id") or "unknown").strip(),
                status=str(r.get("status") or "unknown").strip(),
                worker_tokens=_extract_worker_tokens(r),
                worker_cost_usd=_extract_worker_cost(r),
                duration_seconds=_extract_duration(r),
                is_success=_is_success(r),
                scout_enriched=enriched,
                scout_cost_usd=scout_cost_by_dispatch.get(did, 0.0) if enriched else 0.0,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Cohort statistics
# ---------------------------------------------------------------------------

def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _rate(values: Sequence[bool]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


@dataclass
class CohortStats:
    count: int = 0
    tokens_values: List[int] = field(default_factory=list)
    cost_values: List[float] = field(default_factory=list)
    duration_values: List[float] = field(default_factory=list)
    success_values: List[bool] = field(default_factory=list)

    @property
    def mean_tokens(self) -> Optional[float]:
        return _mean(self.tokens_values)

    @property
    def mean_cost_usd(self) -> Optional[float]:
        return _mean(self.cost_values)

    @property
    def mean_duration_seconds(self) -> Optional[float]:
        return _mean(self.duration_values)

    @property
    def success_rate(self) -> Optional[float]:
        return _rate(self.success_values)

    @property
    def sum_cost_usd(self) -> float:
        return sum(self.cost_values)

    @property
    def sum_tokens(self) -> int:
        return sum(self.tokens_values)


def build_cohort(records: Iterable[DispatchRecord]) -> CohortStats:
    stats = CohortStats()
    for rec in records:
        stats.count += 1
        if rec.worker_tokens is not None:
            stats.tokens_values.append(rec.worker_tokens)
        if rec.worker_cost_usd is not None:
            stats.cost_values.append(rec.worker_cost_usd)
        if rec.duration_seconds is not None:
            stats.duration_values.append(rec.duration_seconds)
        if rec.is_success is not None:
            stats.success_values.append(rec.is_success)
    return stats


# ---------------------------------------------------------------------------
# Effectiveness report
# ---------------------------------------------------------------------------

def _delta(enriched_val: Optional[float], non_val: Optional[float]) -> Optional[float]:
    if enriched_val is None or non_val is None:
        return None
    return enriched_val - non_val


def _pct_delta(delta: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if delta is None or baseline is None or baseline == 0:
        return None
    return (delta / baseline) * 100.0


@dataclass
class EffectivenessReport:
    total_dispatches: int
    enriched_count: int
    non_enriched_count: int
    enriched: CohortStats
    non_enriched: CohortStats
    total_scout_cost_usd: float
    token_delta_per_dispatch: Optional[float]
    token_delta_pct: Optional[float]
    cost_delta_per_dispatch_usd: Optional[float]
    cost_delta_pct: Optional[float]
    duration_delta_seconds: Optional[float]
    duration_delta_pct: Optional[float]
    success_rate_delta: Optional[float]
    success_rate_delta_pct_points: Optional[float]
    estimated_worker_token_savings: Optional[float]
    estimated_worker_cost_savings_usd: Optional[float]
    net_usd: Optional[float]
    verdict: str
    warnings: List[str] = field(default_factory=list)
    observational_note: str = (
        "This is an OBSERVATIONAL comparison, not a causal A/B test. "
        "Confounders (task difficulty, role, model, lane, instruction length) "
        "are not controlled. A randomized shadow-A/B would be required for a "
        "causal verdict on scout ROI."
    )


def compute_effectiveness(records: Sequence[DispatchRecord]) -> EffectivenessReport:
    enriched = [r for r in records if r.scout_enriched]
    non_enriched = [r for r in records if not r.scout_enriched]

    enriched_stats = build_cohort(enriched)
    non_stats = build_cohort(non_enriched)

    total_scout_cost = sum(r.scout_cost_usd for r in enriched)

    token_delta = _delta(enriched_stats.mean_tokens, non_stats.mean_tokens)
    cost_delta = _delta(enriched_stats.mean_cost_usd, non_stats.mean_cost_usd)
    duration_delta = _delta(enriched_stats.mean_duration_seconds, non_stats.mean_duration_seconds)
    success_delta = _delta(enriched_stats.success_rate, non_stats.success_rate)

    estimated_token_savings: Optional[float] = None
    estimated_cost_savings: Optional[float] = None
    net_usd: Optional[float] = None
    verdict = "insufficient data"

    warnings: List[str] = []

    if enriched_stats.count == 0:
        warnings.append("No scout-enriched dispatches found in corpus.")
    if non_enriched.count == 0:
        warnings.append("No non-enriched dispatches found in corpus.")

    if enriched_stats.count and non_enriched.count:
        if enriched_stats.mean_tokens is not None and non_stats.mean_tokens is not None:
            # Negative delta means enriched uses fewer tokens -> savings.
            estimated_token_savings = (non_stats.mean_tokens - enriched_stats.mean_tokens) * enriched_stats.count
        if enriched_stats.mean_cost_usd is not None and non_stats.mean_cost_usd is not None:
            estimated_cost_savings = (non_stats.mean_cost_usd - enriched_stats.mean_cost_usd) * enriched_stats.count
        if estimated_cost_savings is not None:
            net_usd = estimated_cost_savings - total_scout_cost
            if net_usd > 0:
                verdict = "scout pays for itself (observational)"
            elif net_usd < 0:
                verdict = "scout does not pay for itself (observational)"
            else:
                verdict = "scout breaks even (observational)"
        else:
            verdict = "cannot judge ROI: worker cost data missing"
    else:
        verdict = "cannot judge ROI: missing cohort"

    return EffectivenessReport(
        total_dispatches=len(records),
        enriched_count=enriched_stats.count,
        non_enriched_count=non_stats.count,
        enriched=enriched_stats,
        non_enriched=non_stats,
        total_scout_cost_usd=total_scout_cost,
        token_delta_per_dispatch=token_delta,
        token_delta_pct=_pct_delta(token_delta, non_stats.mean_tokens),
        cost_delta_per_dispatch_usd=cost_delta,
        cost_delta_pct=_pct_delta(cost_delta, non_stats.mean_cost_usd),
        duration_delta_seconds=duration_delta,
        duration_delta_pct=_pct_delta(duration_delta, non_stats.mean_duration_seconds),
        success_rate_delta=success_delta,
        success_rate_delta_pct_points=success_delta,
        estimated_worker_token_savings=estimated_token_savings,
        estimated_worker_cost_savings_usd=estimated_cost_savings,
        net_usd=net_usd,
        verdict=verdict,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _fmt_optional(value: Optional[float], precision: int = 3, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.{precision}f}{suffix}"


def format_report(report: EffectivenessReport) -> str:
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append("Scout Effectiveness Measurement (observational)")
    lines.append("=" * 72)
    lines.append(f"Total dispatches analyzed: {report.total_dispatches}")
    lines.append(f"  Scout-enriched:    {report.enriched_count}")
    lines.append(f"  Non-enriched:      {report.non_enriched_count}")
    lines.append("")
    lines.append("Cohort means")
    lines.append("-" * 72)
    lines.append(
        f"{'Metric':<36} {'Enriched':>14} {'Non-enriched':>14} {'Delta':>14}"
    )
    lines.append(
        f"{'Worker tokens (input+output)':<36} "
        f"{_fmt_optional(report.enriched.mean_tokens):>14} "
        f"{_fmt_optional(report.non_enriched.mean_tokens):>14} "
        f"{_fmt_optional(report.token_delta_per_dispatch):>14}"
    )
    lines.append(
        f"{'Worker cost USD':<36} "
        f"{_fmt_optional(report.enriched.mean_cost_usd, precision=6):>14} "
        f"{_fmt_optional(report.non_enriched.mean_cost_usd, precision=6):>14} "
        f"{_fmt_optional(report.cost_delta_per_dispatch_usd, precision=6):>14}"
    )
    lines.append(
        f"{'Duration seconds':<36} "
        f"{_fmt_optional(report.enriched.mean_duration_seconds):>14} "
        f"{_fmt_optional(report.non_enriched.mean_duration_seconds):>14} "
        f"{_fmt_optional(report.duration_delta_seconds):>14}"
    )
    lines.append(
        f"{'Success rate (FPY proxy)':<36} "
        f"{_fmt_optional(report.enriched.success_rate, precision=3):>14} "
        f"{_fmt_optional(report.non_enriched.success_rate, precision=3):>14} "
        f"{_fmt_optional(report.success_rate_delta, precision=3):>14}"
    )
    lines.append("")
    lines.append("Economics")
    lines.append("-" * 72)
    lines.append(f"Total DeepSeek/scout cost:        ${_fmt_optional(report.total_scout_cost_usd, precision=6)}")
    lines.append(
        f"Estimated worker token savings:   {_fmt_optional(report.estimated_worker_token_savings)} tokens"
    )
    lines.append(
        f"Estimated worker cost savings:    ${_fmt_optional(report.estimated_worker_cost_savings_usd, precision=6)}"
    )
    lines.append(f"Net USD (savings - scout cost):   ${_fmt_optional(report.net_usd, precision=6)}")
    lines.append("")
    lines.append(f"Verdict: {report.verdict}")
    lines.append("")
    if report.warnings:
        lines.append("Warnings")
        lines.append("-" * 72)
        for w in report.warnings:
            lines.append(f"  - {w}")
        lines.append("")
    lines.append("Notes")
    lines.append("-" * 72)
    lines.append(report.observational_note)
    lines.append(
        "True rework attribution requires quality_intelligence.db "
        "(dispatch_metadata.parent_dispatch); receipt status is used here as a proxy."
    )
    return "\n".join(lines) + "\n"


def report_to_dict(report: EffectivenessReport) -> Dict[str, Any]:
    """Serialize the report to a plain JSON-serializable dict."""
    return {
        "total_dispatches": report.total_dispatches,
        "enriched_count": report.enriched_count,
        "non_enriched_count": report.non_enriched_count,
        "enriched": {
            "count": report.enriched.count,
            "mean_tokens": report.enriched.mean_tokens,
            "mean_cost_usd": report.enriched.mean_cost_usd,
            "mean_duration_seconds": report.enriched.mean_duration_seconds,
            "success_rate": report.enriched.success_rate,
            "sum_tokens": report.enriched.sum_tokens,
            "sum_cost_usd": report.enriched.sum_cost_usd,
        },
        "non_enriched": {
            "count": report.non_enriched.count,
            "mean_tokens": report.non_enriched.mean_tokens,
            "mean_cost_usd": report.non_enriched.mean_cost_usd,
            "mean_duration_seconds": report.non_enriched.mean_duration_seconds,
            "success_rate": report.non_enriched.success_rate,
            "sum_tokens": report.non_enriched.sum_tokens,
            "sum_cost_usd": report.non_enriched.sum_cost_usd,
        },
        "total_scout_cost_usd": report.total_scout_cost_usd,
        "token_delta_per_dispatch": report.token_delta_per_dispatch,
        "token_delta_pct": report.token_delta_pct,
        "cost_delta_per_dispatch_usd": report.cost_delta_per_dispatch_usd,
        "cost_delta_pct": report.cost_delta_pct,
        "duration_delta_seconds": report.duration_delta_seconds,
        "duration_delta_pct": report.duration_delta_pct,
        "success_rate_delta": report.success_rate_delta,
        "estimated_worker_token_savings": report.estimated_worker_token_savings,
        "estimated_worker_cost_savings_usd": report.estimated_worker_cost_savings_usd,
        "net_usd": report.net_usd,
        "verdict": report.verdict,
        "warnings": report.warnings,
        "observational_note": report.observational_note,
    }


def write_artifact(path: Path, report: EffectivenessReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (tmp + os.replace via atomic_io) so an interruption cannot
    # truncate/corrupt a previously-written artifact (codex gate finding, PR #1072).
    atomic_write_json(path, report_to_dict(report))
    return path
