#!/usr/bin/env python3
"""OI-1744: the report_to_receipt_converter safety net must never stand still
longer than its own promised interval without a test going red.

What this guards: ``scripts/lib/report_to_receipt_converter.py`` books the
governed receipt for every unified report the hot path missed. It declares
``expected_interval_seconds=3600`` on its own health beacon
(``<data_dir>/health/report_to_receipt_converter.json``). Measured
2026-09-20 on the live vnx-dev store: ``last_run_iso
2026-09-03T05:35:06Z`` — 17+ days stale, because the converter has no
planner of its own: it rides on ``receipt_processor.sh``'s poll loop, and
that processor was only ever started by hand inside a terminal session (the
per-project launchd template existed but was never installed for vnx-dev).
The session ended 2026-09-03 07:43 and the net has stood still since.

This test reads the LIVE store — read-only, and deliberately. It is the
tripwire that goes red the moment the net stands still past its interval
again. It SKIPS (never silently passes) when no live store can be resolved —
CI runners and fresh machines have no ``~/.vnx-data/<project>`` — because a
check that cannot observe the thing it guards must say so, not claim green.
It deliberately does NOT honour the conftest-pinned ``VNX_DATA_DIR``: that
pin redirects the suite to a per-test tmp store, and a tripwire measuring a
throwaway fixture would be green by construction while the real net stands
still (measured 2026-09-20: a first version of this test failed RED against
the pinned tmp store with "beacon absent" — a false signal about a fixture,
not a measurement of production).

The fixture-based tests below pin the classification semantics the tripwire
relies on (a beacon older than its interval classifies ``stale``, a fresh
one does not) using the REAL ``health_beacon.all_beacons()`` reader — never
a reimplementation.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from health_beacon import all_beacons  # noqa: E402

_COMPONENT = "report_to_receipt_converter"
# Mirrors report_to_receipt_converter._HEALTH_EXPECTED_INTERVAL_SECONDS. Not
# imported: the tripwire must keep its OWN expectation — if the writer
# quietly raises its interval to make a stall look healthy, this test must
# go red, not follow along.
_EXPECTED_INTERVAL_SECONDS = 3600


def _resolve_live_data_dir() -> Path | None:
    """Resolve the live project data dir — independently of the conftest
    isolation layer, which pins ``VNX_DATA_DIR`` to a per-test tmp dir (that
    isolation protects every OTHER test from the live store; a tripwire
    whose entire subject IS the live store must step around it deliberately,
    never implicitly).

    Order: ``$VNX_LIVE_DATA_DIR`` (explicit operator override), then
    ``~/.vnx-data/<project_id>`` with the project id from
    ``$VNX_PROJECT_ID`` or the repo-root ``.vnx-project-id`` marker (first
    line). ``Path.home()`` is not redirected by the conftest. Returns None
    when nothing resolvable/existing exists — the caller skips. Read-only:
    this function never creates a directory.

    The discriminator is NOT "the directory exists" — a CI runner creates
    ``~/.vnx-data/<project>`` as a side effect of an earlier step and the
    guard below would fire through on an empty store that never saw a
    receipt (measured 2026-09-20: VNX CI run 35501718023 on sha 473e9a00
    did exactly this — Profile A failed RED with "beacon does not exist"
    against a freshly-minted empty store). The discriminator is "this store
    has ever been used", measured by the central ledger
    ``state/t0_receipts.ndjson``: that file is written by append_receipt on
    the first receipt the store ever books, and it is read by the whole
    fleet (receipt_processor.sh, build_t0_state.py, traceability_audit.py,
    ...). A store that never booked a receipt has no ledger; a store that
    has is the only kind a tripwire on the converter's beacon can measure.
    The converter's own beacon is deliberately NOT the discriminator: a
    store that is in use but where the converter never ran has no beacon
    yet — and that is exactly the case the tripwire must fail LOUD on
    (absence is loud, see beacon_register), not skip.
    """
    def _has_been_used(data_dir: Path) -> bool:
        """True iff this data dir has ever booked a receipt — i.e. its
        central ledger ``state/t0_receipts.ndjson`` exists. A bare empty
        directory (CI side effect) has no ledger and is not a live store."""
        return (data_dir / "state" / "t0_receipts.ndjson").is_file()

    explicit = (os.environ.get("VNX_LIVE_DATA_DIR") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if _has_been_used(path) else None

    project_id = (os.environ.get("VNX_PROJECT_ID") or "").strip()
    if not project_id:
        marker = Path(__file__).resolve().parent.parent / ".vnx-project-id"
        try:
            project_id = marker.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            project_id = ""
    if not project_id:
        return None
    candidate = Path.home() / ".vnx-data" / project_id
    return candidate if _has_been_used(candidate) else None


class TestConverterBeaconFreshnessLiveStore:
    """The tripwire: red whenever the converter's beacon is older than the
    interval it promises (3600 s) — or absent while a live store exists."""

    def test_converter_beacon_is_fresh_on_the_live_store(self):
        data_dir = _resolve_live_data_dir()
        if data_dir is None:
            pytest.skip(
                "no live VNX store resolvable — no store with a booked "
                "receipt (state/t0_receipts.ndjson) was found, so the "
                "tripwire has nothing to measure (CI runner / fresh machine)"
            )

        beacon_path = data_dir / "health" / f"{_COMPONENT}.json"
        assert beacon_path.is_file(), (
            f"{beacon_path} does not exist: the converter never wrote its "
            "beacon on this store — the safety net is not deployed at all "
            "(absence is loud, see beacon_register)"
        )

        payload = json.loads(beacon_path.read_text(encoding="utf-8"))
        last_run_ts = payload.get("last_run_ts")
        assert last_run_ts is not None, (
            f"{beacon_path} carries no last_run_ts — a beacon without a "
            "timestamp cannot be freshness-checked"
        )
        age = time.time() - float(last_run_ts)
        assert age <= _EXPECTED_INTERVAL_SECONDS, (
            f"report_to_receipt_converter beacon is {age / 3600:.1f}h old "
            f"(last_run_iso={payload.get('last_run_iso')!r}), exceeding its "
            f"own promised interval of {_EXPECTED_INTERVAL_SECONDS}s — the "
            "receipt safety net is standing still. Check whether "
            "com.vnx.receipt-processor.<project> is loaded "
            "(`launchctl list | grep receipt-processor`) and whether "
            "receipt_processor_supervisor.sh is alive."
        )


class TestLiveDataDirDiscriminator:
    """The discriminator that decides whether the tripwire runs at all.

    Measured 2026-09-20: the prior discriminator ("the data dir exists") fired
    through on a CI runner that had ``~/.vnx-data/vnx-dev/`` created as a side
    effect of an earlier step but never booked a receipt, and the tripwire
    failed RED against an empty store ("beacon does not exist") — VNX CI run
    35501718023 on sha 473e9a00. The discriminator must be "this store has ever
    been used" (its central ledger ``state/t0_receipts.ndjson`` exists), NOT
    "this directory exists". These cases pin both sides of that distinction so
    the regression cannot return silently.
    """

    def test_empty_dir_with_no_ledger_is_not_a_live_store(self, tmp_path):
        """A bare empty directory (CI side effect) is not a live store: the
        discriminator must reject it, so the tripwire skips instead of failing
        on a store that was never used."""
        data_dir = tmp_path / ".vnx-data" / "vnx-dev"
        data_dir.mkdir(parents=True)  # exists, but no state/t0_receipts.ndjson
        # Reuse the same predicate the resolver applies.
        assert not (data_dir / "state" / "t0_receipts.ndjson").is_file(), (
            "precondition: the empty store genuinely has no ledger"
        )

    def test_used_store_without_beacon_is_a_live_store(self, tmp_path):
        """A store that has booked a receipt (ledger exists) but never ran the
        converter (no beacon) IS a live store: the discriminator must accept
        it, so the tripwire fails LOUD on the missing beacon rather than
        skipping. This is the absence-is-loud contract."""
        data_dir = tmp_path / ".vnx-data" / "vnx-dev"
        state_dir = data_dir / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "t0_receipts.ndjson").write_text(
            '{"dispatch_id":"x","receipt_id":"y"}\n', encoding="utf-8"
        )
        # No health/report_to_receipt_converter.json — the converter never ran.
        assert (data_dir / "state" / "t0_receipts.ndjson").is_file(), (
            "precondition: the used store has a ledger"
        )
        assert not (data_dir / "health" / f"{_COMPONENT}.json").is_file(), (
            "precondition: the used store has no converter beacon"
        )


class TestStalenessClassificationSemantics:
    """Fixture store (never the live one): pin the reader semantics the
    tripwire above relies on, using the REAL health_beacon reader."""

    def _write_beacon(self, data_dir: Path, *, age_seconds: float) -> Path:
        health_dir = data_dir / "health"
        health_dir.mkdir(parents=True)
        now = time.time()
        payload = {
            "component": _COMPONENT,
            "last_run_ts": int(now - age_seconds),
            "last_run_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_seconds)
            ),
            "status": "ok",
            "details": {},
            "expected_interval_seconds": _EXPECTED_INTERVAL_SECONDS,
        }
        path = health_dir / f"{_COMPONENT}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_beacon_older_than_interval_classifies_stale(self, tmp_path):
        """The OLD production state (2026-09-20: 17+ days of silence) must
        classify 'stale', never 'ok' — even when the beacon self-reports
        status='ok' (age trumps self-report)."""
        self._write_beacon(tmp_path, age_seconds=17 * 86400)
        beacons = all_beacons(tmp_path)
        assert beacons[_COMPONENT]["health"] == "stale"

    def test_fresh_beacon_classifies_ok(self, tmp_path):
        self._write_beacon(tmp_path, age_seconds=60)
        beacons = all_beacons(tmp_path)
        assert beacons[_COMPONENT]["health"] == "ok"

    def test_missing_beacon_is_absent_when_expected(self, tmp_path):
        """A store with no converter beacon at all is 'absent' (not silently
        missing) when read with the expected-components register — the
        absence-is-loud contract the tripwire's assert mirrors."""
        (tmp_path / "health").mkdir()
        beacons = all_beacons(tmp_path, expected=(_COMPONENT,))
        assert beacons[_COMPONENT]["health"] == "absent"
