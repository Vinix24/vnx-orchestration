#!/usr/bin/env python3
"""provider_costs.py — Central provider-cost NDJSON emitter.

ADR-005: Writes append-only NDJSON events to .vnx-data/events/provider_costs.ndjson
BEFORE any downstream state mutation.

ADR-007: Every event includes project_id for multi-tenant traceability.

ADR-043 §6: every actor on the ledger takes the lock file
``events/provider_costs.lock`` first (:func:`ledger_lock`), and the ledger's own
``flock`` under it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Price resolution (ADR-040)
# ---------------------------------------------------------------------------
# The hardcoded _PROVIDER_RATES table is gone. Every price now comes from the
# model registry (scripts/lib/providers/wave7_models.yaml), read through
# price_run(). A model without a price returns "unavailable:no_price:<model>"
# as the cost_source — never a silent 0.

# Maps VNX provider literals to wave7_models.yaml registry section keys.
# litellm:<sub> providers are resolved separately (see price_run).
_PROVIDER_TO_REGISTRY_KEY: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
    "kimi": "kimi_cli",
    "deepseek-harness": "deepseek_harness",
    "glm-harness": "zai",
    "local-gemma": "local_gemma",
}

# Provider literals billed as a flat subscription/OAuth lane rather than per
# token. This is a LANE property (kimi CLI OAuth, local ollama), not a price —
# it exists so emit_provider_cost can still label the billing_mode after the
# rate table was removed. Everything metered goes through price_run.
_SUBSCRIPTION_FLAT_PROVIDERS = frozenset({"kimi", "litellm:moonshot", "litellm:ollama"})


def registry_key_for_provider(provider: str) -> str | None:
    """The wave7_models.yaml section a VNX provider literal is priced and identified in.

    ``litellm:<sub>[:...]`` resolves to ``<sub>``. ``None`` when the provider has
    no section: the caller reports it, it never guesses one.
    """
    registry_key = _PROVIDER_TO_REGISTRY_KEY.get(provider)
    if registry_key is None and provider.startswith("litellm:") and ":" in provider:
        registry_key = provider.split(":", 1)[1].split(":", 1)[0]
    return registry_key or None


def _is_peak(ts: object) -> bool:
    """Classify a timestamp against the DeepSeek peak window (ADR-040).

    Peak is 01:00-04:00 and 06:00-10:00 UTC on weekdays; off-peak (dal) is the
    rest and bills at half the peak rate. A weekend or an out-of-window weekday
    hour is off-peak.
    """
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    if dt.weekday() >= 5:  # Saturday / Sunday
        return False
    hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    return (1.0 <= hour < 4.0) or (6.0 <= hour < 10.0)


def _segment_is_peak(segment: dict, has_split: bool) -> bool:
    """Decide whether a segment bills at the peak or off-peak rate.

    ``has_split`` is whether the resolved price version carries an off-peak
    split at all. When it does, the segment's explicit ``peak`` flag wins, then
    its ``ts`` timestamp (classified via :func:`_is_peak`), then the peak
    default — the upper bound, never an under-report.
    """
    if not has_split:
        return True
    if "peak" in segment:
        return bool(segment["peak"])
    ts = segment.get("ts")
    if ts is not None:
        return _is_peak(ts)
    return True


def price_run(provider: str, model: str, segments) -> tuple[float | None, str]:
    """Price one run, split into segments at a price boundary (ADR-040).

    The single entry point for pricing. Resolves ``(provider, model)`` against
    the model registry and applies the latest dated price version per segment,
    so a run that crosses a price boundary (DeepSeek's peak window) is priced
    with two rates.

    Args:
        provider: VNX provider literal (``"claude"``, ``"codex"``, ``"gemini"``,
            ``"kimi"``, ``"deepseek-harness"``, ``"glm-harness"``,
            ``"local-gemma"``) or a litellm sub-provider (``"litellm:deepseek"``,
            ``"litellm:zai"``, ``"litellm:moonshot"``).
        model: model name as carried by the caller (bare registry key,
            provider-prefixed slug, or slash-qualified litellm name).
        segments: iterable of segment mappings. Each segment carries token
            counts — ``input``, ``output``, ``cache_read``, ``cache_write``
            (all optional, default 0) — and optionally a rate-class selector
            ``peak`` (bool) or ``ts`` (datetime / ISO-8601) for providers with
            a peak/off-peak split. A segment with neither defaults to peak.

    Returns:
        ``(cost_usd, cost_source)``. ``cost_usd`` is the total USD cost, or
        ``None`` when the model has no price. ``cost_source`` is a provenance
        string: ``"<model>@<version-date>"`` for a priced run, or
        ``"unavailable:no_price:<model>"`` for a model without a price — never
        a silent 0.
    """
    from providers import provider_registry  # noqa: PLC0415 — lazy, cycle-safe

    registry_key = registry_key_for_provider(provider)
    if not registry_key:
        return None, f"unavailable:no_price:{model}"

    versions = provider_registry.resolve_price_versions(registry_key, model)
    if not versions:
        return None, f"unavailable:no_price:{model}"

    version = versions[-1]  # latest by date
    has_split = (
        version.off_peak_input_per_mtok is not None
        and version.off_peak_output_per_mtok is not None
    )

    total = 0.0
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        peak = _segment_is_peak(segment, has_split)
        in_rate = (
            version.off_peak_input_per_mtok if (has_split and not peak) else version.input_per_mtok
        )
        out_rate = (
            version.off_peak_output_per_mtok if (has_split and not peak) else version.output_per_mtok
        )
        in_t = int(segment.get("input") or 0)
        out_t = int(segment.get("output") or 0)
        cache_read_t = int(segment.get("cache_read") or 0)
        cache_write_t = int(segment.get("cache_write") or 0)
        total += (in_t / 1_000_000) * in_rate
        total += (out_t / 1_000_000) * out_rate
        if version.cache_read_per_mtok:
            total += (cache_read_t / 1_000_000) * version.cache_read_per_mtok
        if version.cache_write_per_mtok:
            total += (cache_write_t / 1_000_000) * version.cache_write_per_mtok

    return round(total, 8), f"{model}@{version.date}"


def _make_record_id(
    dispatch_id: str | None,
    timestamp: str,
    project_id: str = "",
    event_type: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> str:
    """Stable idempotency key: sha256[:32] of all discriminating fields.

    Includes token counts so two emits in the same second remain distinct.
    Includes project_id per ADR-007.
    """
    raw = (
        f"{dispatch_id or ''}:{project_id}:{event_type}:{timestamp}"
        f":{input_tokens or 0}:{output_tokens or 0}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _resolve_costs_path() -> Path:
    """Resolve <central store>/events/provider_costs.ndjson.

    Central store = ``$HOME/.vnx-data/<project_id>`` — mirroring
    ``provider_dispatch._resolve_data_dir`` exactly, so the cost event lands in
    the same tenant store as the lane's report and receipt. ``VNX_DATA_DIR`` is
    honored ONLY together with ``VNX_DATA_DIR_EXPLICIT=1`` (OI-126 two-key
    contract).

    NEVER derive this from the module's own location: in a central install
    ``__file__`` sits inside the read-only ``~/.vnx-system/versions/<v>/`` tree,
    so ``resolve_data_dir(__file__)`` resolved the keystone and the mkdir in
    ``emit_provider_cost`` died with EACCES on EVERY provider lane — the
    2026-07-31 fleet-wide plan-gate block.
    """
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = os.environ.get("VNX_DATA_DIR", "")
    if explicit_flag and explicit_val:
        data_dir = Path(explicit_val).resolve()
    else:
        project_id = os.environ.get("VNX_PROJECT_ID", "vnx-dev")
        data_dir = Path.home() / ".vnx-data" / project_id
    return costs_path_in(data_dir)


def costs_path_in(data_dir: Path) -> Path:
    """The cost ledger of the store ``data_dir``: ``<data_dir>/events/provider_costs.ndjson``."""
    return Path(data_dir) / "events" / "provider_costs.ndjson"


def ledger_lock_path(costs_path: Path) -> Path:
    """``provider_costs.lock`` next to the ledger: the lock file of ADR-043 §6."""
    return Path(costs_path).with_suffix(".lock")


@contextmanager
def ledger_lock(costs_path: Path, operation: int):
    """Hold the ledger's lock file, the lock every actor on the ledger takes first (ADR-043 §6).

    ``operation`` is ``fcntl.LOCK_EX`` for a writer and ``fcntl.LOCK_SH`` for a
    reader. The file is created with ``O_CREAT`` and never removed: a removed
    lock file lets two actors lock two different files. The ledger's own
    ``flock`` is taken under this one, never the other way round. The open or
    the lock raises its ``OSError``, and nothing is held then.
    """
    lock_path = ledger_lock_path(costs_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, operation)
        yield
    finally:
        os.close(fd)


def emit_provider_cost(
    provider: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd_estimate: float | None,
    dispatch_id: str | None = None,
    project_id: str = "",
    metadata: dict | None = None,
    cost_source: str | None = None,
) -> None:
    """Append a provider cost NDJSON event to .vnx-data/events/provider_costs.ndjson.

    ADR-005: written before any downstream state mutation; raises on failure.
    ADR-007: project_id stamped on every event.
    ADR-043 §6: appended under the lock file (:func:`ledger_lock`, exclusive),
    then under the ledger's own ``flock``, the order of every ledger actor.

    Raises OSError/IOError on write failure — no silent except.

    Tenant resolution (best-effort, NOT fail-closed): the cost log is an
    append-only NDJSON receipt, not a central-DB table, so the ADR-007
    DEFAULT-ban does not bind it and it must NEVER skip an event (cost-audit =
    no data-loss). Callers that hold a store ``db_path`` (provider_dispatch,
    recovery) pass a store-derived pid explicitly; absent that, this falls back
    to the env (then ``vnx-dev``). The ``if not project_id`` trigger treats
    ``None`` and ``""`` identically.

    ``cost_source`` carries the price provenance from ``price_run`` (``<model>@
    <version-date>`` or ``unavailable:no_price:<model>``), so a missing price
    is never recorded as a free run — ``cost_usd_estimate`` stays ``None`` and
    the source names why.
    """
    effective_project_id = project_id or os.environ.get("VNX_PROJECT_ID", "vnx-dev")
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record_id = _make_record_id(
        dispatch_id,
        timestamp,
        project_id=effective_project_id,
        event_type="provider_cost",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    # Determine billing mode from cost_usd_estimate and the subscription-flat
    # lane set (the rate table that used to carry the flat flag is gone).
    if cost_usd_estimate is not None:
        billing_mode = "metered"
    elif provider in _SUBSCRIPTION_FLAT_PROVIDERS:
        billing_mode = "subscription"
    else:
        billing_mode = "unmetered"

    event: dict = {
        "record_id": record_id,
        "project_id": effective_project_id,
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd_estimate": cost_usd_estimate,
        "cost_source": cost_source,
        "billing_mode": billing_mode,
        "dispatch_id": dispatch_id,
        "timestamp": timestamp,
    }
    if metadata:
        event["metadata"] = metadata

    costs_path = _resolve_costs_path()
    costs_path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(event, separators=(",", ":")) + "\n"
    with ledger_lock(costs_path, fcntl.LOCK_EX), costs_path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

    logger.debug(
        "emit_provider_cost: provider=%s model=%s cost=%s dispatch=%s record=%s",
        provider, model, cost_usd_estimate, dispatch_id, record_id,
    )
