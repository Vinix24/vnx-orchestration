"""Merged-PR reads whose coverage is proven, not assumed (doorloop PR-4, decision 21).

A read proves coverage only when it returns fewer rows than its limit; a full
page is halved by time and both halves are read down to ``MAX_DEPTH`` levels,
and a leaf that stays full makes the population ``truncated``. Every row's
``mergedAt`` must lie inside the requested range. One population per run: the
result is memoised per ``(project_root, from_day, to_day, extra_fields)``.
"""
from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

LIMIT = 1000
MAX_DEPTH = 6
GH_TIMEOUT_SECONDS = 60
BASE_FIELDS = ("number", "createdAt", "mergedAt", "headRefOid")
GH_FAILED, ROW_OUTSIDE_RANGE = "gh_failed", "row_outside_range"

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class MergedPopulation:
    """Rows for a range; ``truncated``/``error`` name an unknown read, never a short count.

    ``error_detail`` is the stderr or exception behind ``error`` (display evidence only).
    """
    rows: tuple[dict[str, Any], ...]
    range_from: datetime
    range_to: datetime
    fetched_at: datetime
    truncated: bool
    error: str | None
    calls: int
    error_detail: str | None = None

_CACHE: dict[tuple, MergedPopulation] = {}

def clear_cache() -> None:
    """Drop every memoised population (tests, or a caller that wants a reread)."""
    _CACHE.clear()

def parse_timestamp(value: Any) -> datetime | None:
    """A UTC-aware stamp, or ``None`` when it does not parse or carries no offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None
    return stamp.astimezone(timezone.utc)

def _default_runner(args: Sequence[str], cwd: Path) -> tuple:
    try:
        done = subprocess.run(list(args), cwd=str(cwd), capture_output=True,
                              text=True, timeout=GH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a failed launch is data, not a crash
        return None, "", f"{type(exc).__name__}: {str(exc)[:120]}"
    return done.returncode, done.stdout, done.stderr

def _halves(start: datetime, end: datetime) -> tuple | None:
    first, last = int(start.timestamp()), int(end.timestamp())
    if last <= first:
        return None
    mid, utc = first + (last - first) // 2, timezone.utc
    return ((datetime.fromtimestamp(first, utc), datetime.fromtimestamp(mid, utc)),
            (datetime.fromtimestamp(mid + 1, utc), datetime.fromtimestamp(last, utc)))

def _read_once(runner, root, limit, fields, start, end) -> tuple:
    span = f"{start:%Y-%m-%dT%H:%M:%S}Z..{end:%Y-%m-%dT%H:%M:%S}Z"
    args = ["gh", "pr", "list", "--state", "merged", "--search", f"merged:{span}",
            "--limit", str(limit), "--json", ",".join(fields)]
    try:
        returncode, stdout, stderr = runner(args, cwd=root)
    except Exception as exc:  # noqa: BLE001 - a raising runner is a failed read
        return [], GH_FAILED, f"{type(exc).__name__}: {str(exc)[:120]}"
    if returncode != 0:
        return [], GH_FAILED, f"rc={returncode}: {(stderr or '').strip()[:120]}"
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        return [], GH_FAILED, f"unparseable gh output: {str(exc)[:120]}"
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        return [], GH_FAILED, "unparseable gh output: expected a list of objects"
    return data, None, None

def _scan(runner, root, limit, fields, start, end, depth, max_depth) -> tuple:
    """``(rows, calls, truncated, error, detail)`` for ``[start, end]``, coverage proven."""
    rows, error, detail = _read_once(runner, root, limit, fields, start, end)
    if error is not None:
        return [], 1, False, error, detail
    halves = _halves(start, end)
    if len(rows) < limit:
        return rows, 1, False, None, None
    if depth >= max_depth or halves is None:
        return rows, 1, True, None, None
    left = _scan(runner, root, limit, fields, *halves[0], depth + 1, max_depth)
    right = _scan(runner, root, limit, fields, *halves[1], depth + 1, max_depth)
    return (rows + left[0] + right[0], 1 + left[1] + right[1],
            left[2] or right[2], left[3] or right[3], left[4] or right[4])


def _dedupe(rows: list) -> list:
    """One row per PR ``number``; a row without a number is never dropped."""
    seen: set = set()
    unique: list = []
    for row in rows:
        number = row.get("number")
        if number is not None:
            if number in seen:
                continue
            seen.add(number)
        unique.append(row)
    return unique


def fetch_merged(
    from_day: date, to_day: date, *, project_root: Path, now: datetime,
    extra_fields: tuple[str, ...] = (), runner: Callable[..., tuple] | None = None,
    use_cache: bool = True,
) -> MergedPopulation:
    """Read merged PRs for ``[from_day, to_day]``, memoised per key.

    ``use_cache=False`` neither reads nor writes ``_CACHE`` (the receipt watcher
    needs fresh rows); a population carrying an ``error`` is never stored, so a
    transient ``gh`` failure is retried by the next caller.
    """
    root = Path(project_root)
    # ``use_cache=False`` works on a private dict: it neither reads nor writes _CACHE.
    cache = _CACHE if use_cache else {}
    key = (str(root.resolve()), from_day, to_day, tuple(extra_fields))
    if key not in cache:
        start = datetime.combine(from_day, time(0, 0, 0), tzinfo=timezone.utc)
        end = datetime.combine(to_day, time(23, 59, 59), tzinfo=timezone.utc)
        rows, calls, truncated, error, detail = _scan(
            runner or _default_runner, root, LIMIT,
            BASE_FIELDS + tuple(extra_fields), start, end, 1,
            MAX_DEPTH)
        rows = _dedupe(rows)
        if error is None:
            for row in rows:
                merged = parse_timestamp(row.get("mergedAt"))
                if merged is None or not start <= merged <= end:
                    error = ROW_OUTSIDE_RANGE
                    break
        fetched = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        population = MergedPopulation(
            rows=tuple(rows), range_from=start, range_to=end,
            fetched_at=fetched.astimezone(timezone.utc), truncated=truncated,
            error=error, calls=calls, error_detail=detail if error else None)
        if population.error is None:  # a failed read is retried by the next caller
            cache[key] = population
        return population
    return cache[key]


class RunReadFailure(ValueError):
    """A named, blocking Actions read failure; never an empty population."""


class RunReader:
    """One project/night budget, including discovery, pagination and any retries."""

    def __init__(self, root, maximum, runner=None):
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("VNX_CI_RECORD_MAX_CALLS must be a positive integer")
        self.root, self.maximum = root, maximum
        self.runner, self.calls, self.stopped = runner or _default_runner, 0, None

    def read(self, args):
        if self.stopped is None and self.calls >= self.maximum:
            self.stopped = "call_budget_exhausted"
        if self.stopped:
            raise RunReadFailure(self.stopped)
        self.calls += 1
        try:
            code, stdout, stderr = self.runner(["gh", *args], cwd=self.root)
        except Exception as exc:
            raise RunReadFailure("source_missing") from exc
        if type(code) is not int or code != 0:
            if isinstance(stderr, str) and any(s in stderr.lower() for s in ("rate limit", "http 429")):
                self.stopped = "rate_limited"
            raise RunReadFailure(self.stopped or "source_missing")
        try:
            data = json.loads(stdout)
        except (ValueError, TypeError) as exc:
            raise RunReadFailure("source_invalid") from exc
        if not isinstance(data, dict):
            raise RunReadFailure("source_invalid")
        return data

    def pages(self, endpoint, key, first=None):
        """Return observed rows plus a failure reason unless every page is proven."""
        rows, seen, total, page = [], set(), None, 1
        while True:
            try:
                sep = "&" if "?" in endpoint else "?"
                data = first if page == 1 and first is not None else self.read(
                    ["api", f"{endpoint}{sep}per_page=100&page={page}"])
                batch, count = data.get(key), data.get("total_count")
                if (not isinstance(batch, list) or type(count) is not int or count < 0
                        or (total is not None and total != count)):
                    raise RunReadFailure("source_invalid")
                total = count
                for row in batch:
                    ident = row.get("id") if isinstance(row, dict) else None
                    if type(ident) is not int or ident <= 0 or ident in seen:
                        raise RunReadFailure("source_invalid")
                    seen.add(ident)
                    rows.append(row)
                if len(rows) == total:
                    return rows, None
                if not batch or len(rows) > total:
                    raise RunReadFailure("source_invalid")
                page += 1
            except RunReadFailure as exc:
                return rows, str(exc)

    def runs(self, repo, start, end):
        """Freeze creation bounds; split GitHub's 1000-result filtered searches."""
        if start > end:
            return [], None
        endpoint = (f"repos/{repo}/actions/runs?event=pull_request&created="
                    f"{start:%Y-%m-%dT%H:%M:%S}Z..{end:%Y-%m-%dT%H:%M:%S}Z")
        try:
            data = self.read(["api", endpoint + "&per_page=100&page=1"])
        except RunReadFailure as exc:
            return [], str(exc)
        rows, count = data.get("workflow_runs"), data.get("total_count")
        if not isinstance(rows, list) or type(count) is not int or count < 0:
            return [], "source_invalid"
        if count > 1000:
            halves = _halves(start, end)
            if halves is None:
                return rows, "source_invalid"
            left, le = self.runs(repo, *halves[0])
            right, re = self.runs(repo, *halves[1])
            rows, error = rows + left + right, le or re
        else:
            rows, error = self.pages(endpoint, "workflow_runs", first=data)
        unique = {}
        for row in rows:
            stamp = parse_timestamp(row.get("created_at")) if isinstance(row, dict) else None
            if (stamp is None or not start <= stamp <= end
                    or type(row.get("id")) is not int or row["id"] <= 0):
                return list(unique.values()), error or "source_invalid"
            unique[row["id"]] = row
        return list(unique.values()), error or (None if len(unique) == count else "source_invalid")
