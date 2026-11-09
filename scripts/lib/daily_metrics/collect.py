"""The daily-metrics collector: discovery, the day document, lock and beacon.

``discover_sources`` imports every ``*.py`` in the sources directory except
``__init__`` and keeps the modules that expose a callable
``collect(ctx) -> Mapping[str, ScalarMetric | KeyedMetric]`` (decision 4). A
module that fails to import, or whose ``collect`` raises, is never skipped:
its ids stay missing with a ``source_import_failed`` reason, which
``metric.is_transient_reason`` marks transient, so the day stays
``incomplete`` and is retried until every source has landed.

``collect_day`` builds one day document: exactly the ids of
``metric.REQUIRED``, each produced record as ``to_dict()``, each id no source
produced as a scalar unknown record with ``source "daily_metrics.collect"``.
An id outside ``REQUIRED`` is dropped and listed in ``details.unexpected_ids``.

The run lock is an exclusive non-blocking ``fcntl.flock`` on
``<store root>/.lock`` held for the whole run (decision 20). The beacon is
event-driven: ``HealthBeacon(state_dir, "daily_metrics", None)`` (decision 29).
"""

from __future__ import annotations

import fcntl
import importlib.util
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from daily_metrics import days, metric
from daily_metrics.store import DayStore
from health_beacon import HealthBeacon

SOURCES_DIR_ENV = "VNX_DAILY_METRICS_SOURCES_DIR"
COLLECTOR_SOURCE = "daily_metrics.collect"
BEACON_DETAILS_FINAL = "final_count"
BEACON_DETAILS_DAYS = "counted_days"
BEACON_SETTLED_WINDOW = 3


@dataclass(frozen=True)
class SourceFailure:
    """A source module that raised on import, with the exception class name."""

    module: str
    exception: str


class CollectContext:
    """The inputs one source collection needs; ``project_root`` resolves lazily."""

    def __init__(self, project_id: str, day: date, now: datetime) -> None:
        self.project_id = project_id
        self.day = day
        self.now = now
        self._project_root: Path | None = None

    def project_root(self) -> Path:
        """The checkout for ``project_id``, resolved on first use (decision 12)."""
        if self._project_root is None:
            from launchd_project_root import resolve_project_root

            self._project_root = resolve_project_root(self.project_id)
        return self._project_root


def sources_dir() -> Path:
    """The sources directory: default under this package, or the env override (decision 18)."""
    override = os.environ.get(SOURCES_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "sources"


def _load_source(path: Path) -> tuple[str, ModuleType | None, str | None]:
    """Load one source module; ``(name, module|None, exception_class|None)``.

    A module without a callable ``collect`` is not a source and is not a
    failure (``module`` and ``exception`` are both ``None``).
    """
    name = path.stem
    spec = importlib.util.spec_from_file_location(f"daily_metrics.sources.{name}", path)
    if spec is None or spec.loader is None:
        return name, None, "ImportError"
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - an unimportable source is data, not a crash
        return name, None, type(exc).__name__
    if not callable(getattr(module, "collect", None)):
        return name, None, None
    return name, module, None


def discover_sources(path: Path) -> tuple[list[ModuleType], list[SourceFailure]]:
    """Import every ``*.py`` in ``path`` except ``__init__`` (decision 4).

    Returns ``(modules, failures)``: the modules with a callable ``collect``,
    and the failures (a module that raised on import, with its exception
    class). A failure is never skipped -- the caller turns its missing ids
    into ``unknown`` records.
    """
    path = Path(path)
    modules: list[ModuleType] = []
    failures: list[SourceFailure] = []
    if not path.is_dir():
        return modules, failures
    for file in sorted(path.glob("*.py")):
        if file.name == "__init__.py":
            continue
        name, module, exception = _load_source(file)
        if exception is not None:
            failures.append(SourceFailure(name, exception))
        elif module is not None:
            modules.append(module)
    return modules, failures


def _missing_reason(failed: Mapping[str, str]) -> str:
    """The reason for an id no source produced (decision 4)."""
    if failed:
        return f"source_import_failed:{','.join(sorted(failed))}"
    return "source_import_failed:no_source"


def _unknown_record(metric_id: str, failed: Mapping[str, str]) -> metric.ScalarMetric:
    details: dict[str, Any] = {}
    if failed:
        details["failed_sources"] = dict(failed)
    return metric.ScalarMetric(
        value=None,
        source=COLLECTOR_SOURCE,
        unknown_reason=_missing_reason(failed),
        details=details,
    )


def _record_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, (metric.ScalarMetric, metric.KeyedMetric)):
        return record.to_dict()
    return dict(record)


def collect_day(ctx: CollectContext) -> dict[str, Any]:
    """Build the day document for ``ctx.day`` (decision 2)."""
    modules, failures = discover_sources(sources_dir())
    failed = {failure.module: failure.exception for failure in failures}
    produced: dict[str, Any] = {}
    unexpected: list[str] = []

    for module in modules:
        name = module.__name__.rsplit(".", 1)[-1]
        try:
            result = module.collect(ctx)
        except Exception as exc:  # noqa: BLE001 - a raising collect keeps its ids missing
            failed[name] = type(exc).__name__
            continue
        if not isinstance(result, Mapping):
            continue
        for metric_id, record in result.items():
            if metric_id in metric.REQUIRED:
                produced[metric_id] = record
            else:
                unexpected.append(metric_id)

    metrics: dict[str, Any] = {}
    for metric_id in sorted(metric.REQUIRED):
        if metric_id in produced:
            metrics[metric_id] = _record_dict(produced[metric_id])
        else:
            metrics[metric_id] = _unknown_record(metric_id, failed).to_dict()

    status = metric.day_status(metrics)
    document: dict[str, Any] = {
        "schema": 1,
        "project_id": ctx.project_id,
        "day": ctx.day.isoformat(),
        "collected_at": ctx.now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "backfilled": days.is_backfilled(ctx.day, ctx.now),
        "metrics": metrics,
        "status": status,
        "details": {},
    }
    if unexpected:
        document["details"]["unexpected_ids"] = sorted(set(unexpected))
    return document


@contextmanager
def run_lock(store_root: Path) -> Iterator[bool]:
    """An exclusive non-blocking ``flock`` on ``<store root>/.lock`` (decision 20).

    Yields ``True`` when the lock is acquired, ``False`` when another run
    holds it. The caller reports ``skipped: locked`` and exits 0 on ``False``.
    """
    store_root = Path(store_root)
    store_root.mkdir(parents=True, exist_ok=True)
    lock_path = store_root / ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def write_beacon(state_dir: Path, store: DayStore, now: datetime) -> None:
    """Write the event-driven ``daily_metrics`` beacon (decision 29).

    ``fail`` when none of the last three settled days has a ``final`` file,
    else ``ok``; the counted days are carried in ``details``.
    """
    beacon = HealthBeacon(state_dir, "daily_metrics", None)
    counted = days.completed_days(now)[-BEACON_SETTLED_WINDOW:]
    final_days = [day for day in counted if store.is_final(day.isoformat())]
    beacon.heartbeat(
        "fail" if not final_days else "ok",
        {
            BEACON_DETAILS_DAYS: [day.isoformat() for day in counted],
            BEACON_DETAILS_FINAL: len(final_days),
        },
    )
