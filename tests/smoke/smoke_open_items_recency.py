"""Smoke test - assert open_items_digest is fresh and consistent.

Three invariants:

  1. ``open_items_digest.json`` was refreshed within the last 24 hours
     (uses ``digest_generated`` / ``last_updated`` / file mtime as
     fallback).
  2. No two entries share the same ``dedup_key`` — duplicate keys mean
     the deduper has regressed and items will accumulate silently.
  3. No entry carries a ``last_updated`` before ``2026-01-01`` — sanity
     guard for time-warped or never-evicted items.

When the digest file is missing, the test is skipped (bare checkout).

Run:
    pytest tests/smoke/smoke_open_items_recency.py -xvs
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

REFRESH_WINDOW_SECONDS = 24 * 3600
SANITY_FLOOR = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)


def _resolve_state_dir() -> Path:
    try:
        from project_root import resolve_state_dir
        return resolve_state_dir(__file__)
    except Exception:
        env = os.environ.get("VNX_STATE_DIR")
        if env:
            return Path(env)
        data = os.environ.get("VNX_DATA_DIR")
        if data:
            return Path(data) / "state"
        return _REPO_ROOT / ".vnx-data" / "state"


def _parse_iso(value: object) -> Optional[_dt.datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def _iter_entries(digest: dict) -> Iterable[dict]:
    for key, value in digest.items():
        if not isinstance(value, list):
            continue
        for entry in value:
            if isinstance(entry, dict):
                yield entry


def test_open_items_digest_recency_and_consistency() -> None:
    state_dir = _resolve_state_dir()
    digest_path = state_dir / "open_items_digest.json"

    if not digest_path.exists():
        pytest.skip(f"open_items_digest.json missing under {state_dir}")

    try:
        raw = digest_path.read_text(encoding="utf-8")
    except OSError as exc:
        pytest.fail(f"could not read {digest_path}: {exc}")

    try:
        digest = json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.fail(f"open_items_digest.json is not valid JSON: {exc}")

    if not isinstance(digest, dict):
        pytest.fail(
            "open_items_digest.json root must be a JSON object, "
            f"got {type(digest).__name__}"
        )

    now = _dt.datetime.now(tz=_dt.timezone.utc)

    refreshed_at: Optional[_dt.datetime] = (
        _parse_iso(digest.get("digest_generated"))
        or _parse_iso(digest.get("last_updated"))
        or _parse_iso(digest.get("generated_at"))
    )
    if refreshed_at is None:
        try:
            mtime = digest_path.stat().st_mtime
            refreshed_at = _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc)
        except OSError:
            refreshed_at = None

    assert refreshed_at is not None, (
        "open_items_digest.json has no digest_generated / last_updated and "
        "mtime is unreadable"
    )

    age_seconds = (now - refreshed_at).total_seconds()
    assert age_seconds <= REFRESH_WINDOW_SECONDS, (
        f"open_items_digest.json refreshed {age_seconds/3600.0:.2f}h ago "
        f"(expected within {REFRESH_WINDOW_SECONDS/3600:.0f}h); "
        f"refreshed_at={refreshed_at.isoformat()}"
    )

    entries = list(_iter_entries(digest))
    seen: dict[str, int] = {}
    duplicates: List[str] = []
    for entry in entries:
        key = entry.get("dedup_key")
        if not key:
            continue
        seen[key] = seen.get(key, 0) + 1
    for key, count in seen.items():
        if count > 1:
            duplicates.append(f"{key} (x{count})")
    assert not duplicates, (
        "open_items_digest.json contains duplicate dedup_key values:\n  - "
        + "\n  - ".join(duplicates)
    )

    too_old: List[str] = []
    for entry in entries:
        ts = _parse_iso(entry.get("last_updated"))
        if ts is None:
            continue
        if ts < SANITY_FLOOR:
            ident = (
                entry.get("dedup_key")
                or entry.get("id")
                or entry.get("title")
                or "<unknown>"
            )
            too_old.append(f"{ident}: last_updated={ts.isoformat()}")
    assert not too_old, (
        f"open_items_digest.json has entries with last_updated before "
        f"{SANITY_FLOOR.date().isoformat()}:\n  - " + "\n  - ".join(too_old)
    )


def main() -> int:
    state_dir = _resolve_state_dir()
    digest_path = state_dir / "open_items_digest.json"
    if not digest_path.exists():
        print(f"SKIP: {digest_path} missing")
        return 0
    try:
        digest = json.loads(digest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot parse digest: {exc}")
        return 1
    if not isinstance(digest, dict):
        print("FAIL: digest root not a JSON object")
        return 1
    print("OK: digest readable (run pytest for full assertions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
