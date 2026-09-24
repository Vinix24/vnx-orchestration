"""The effectiveness probes read the CENTRAL store when no state_dir is given
(absence-is-loud, punt 1).

Without an env pin (launchd, a plain shell) ``project_root.resolve_state_dir(__file__)``
resolved the checkout-local ``.vnx-data/state``. That store is a stale copy: on
24-09 the checkout DB dated from 15-08 and had no ``track_open_items`` table, so
every ``oi_plan_*`` counter read 0 and the ``plan-gate-panel`` beacon said ``ok``
while the live store held 90 unresolved OI-PLAN blockers.

Every test here builds a tmp HOME with a central store and a checkout-local
``.vnx-data/state`` that the old resolver would have picked. Nothing touches the
real ``~/.vnx-data``.

Dispatch-ID: 20260924-ail-probes-centrale-store
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import project_root  # noqa: E402
import subsystem_health  # noqa: E402
from injection_effectiveness_probe import (  # noqa: E402
    InjectionEffectivenessProbe,
    InjectionReasonEvaluator,
)
from migration_effectiveness_probe import MigrationEffectivenessProbe  # noqa: E402
from plan_gate_effectiveness_probe import (  # noqa: E402
    COORDINATION_DB_FILENAME,
    PlanGateEffectivenessProbe,
)

PROJECT_ID = "probe-fixture"


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """A tmp HOME holding the central store, plus the checkout-local store the
    un-pinned ``project_root`` resolver returns."""
    home = tmp_path / "home"
    central_data = home / ".vnx-data" / PROJECT_ID
    central_state = central_data / "state"
    central_state.mkdir(parents=True)

    checkout_data = tmp_path / "checkout" / ".vnx-data"
    checkout_state = checkout_data / "state"
    checkout_state.mkdir(parents=True)

    # tests/conftest.py pins the whole store tree to a tmp dir; undo that pin so
    # the canonical resolver falls through to ~/.vnx-data/<project_id> (of the
    # tmp HOME), which is what production resolves to without a pin.
    for key in ("VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR", "VNX_DATA_HOME"):
        monkeypatch.delenv(key, raising=False)
    # Path.home(), not $HOME: the real-store write guard resolves "~" from $HOME
    # and must keep seeing the REAL ~/.vnx-data as the one it protects.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT_ID)

    # What the old resolver returned for an un-pinned process: the checkout.
    monkeypatch.setattr(project_root, "resolve_state_dir", lambda caller_file=None: checkout_state)
    monkeypatch.setattr(project_root, "resolve_data_dir", lambda caller_file=None: checkout_data)

    return SimpleNamespace(
        central_data=central_data.resolve(),
        central_state=central_state.resolve(),
        checkout_data=checkout_data,
        checkout_state=checkout_state,
    )


def _plan_gate_db(state_dir: Path, unresolved: int) -> None:
    conn = sqlite3.connect(str(state_dir / COORDINATION_DB_FILENAME))
    conn.execute(
        "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, oi_id TEXT, "
        "link_type TEXT, linked_at TEXT, resolved_at TEXT)"
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO track_open_items VALUES (?,?,?,?,?,?)",
        [(f"t{i}", PROJECT_ID, f"OI-PLAN-t{i}", "blocks", now, None) for i in range(unresolved)],
    )
    conn.commit()
    conn.close()


def _stale_checkout_db(state_dir: Path) -> None:
    """A coordination DB with no ``track_open_items`` table, like the 15-08 copy."""
    conn = sqlite3.connect(str(state_dir / COORDINATION_DB_FILENAME))
    conn.execute("CREATE TABLE dispatches (id TEXT)")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# plan-gate-panel
# ---------------------------------------------------------------------------

def test_plan_gate_probe_without_state_dir_counts_the_central_store(stores, tmp_path):
    _plan_gate_db(stores.central_state, unresolved=2)
    _stale_checkout_db(stores.checkout_state)

    raw = PlanGateEffectivenessProbe(repo_root=tmp_path / "repo").probe()

    assert raw["oi_plan_unresolved"] == 2


def test_plan_gate_probe_explicit_state_dir_still_wins(stores, tmp_path):
    _plan_gate_db(stores.central_state, unresolved=2)
    explicit = tmp_path / "explicit-state"
    explicit.mkdir()
    _plan_gate_db(explicit, unresolved=5)

    raw = PlanGateEffectivenessProbe(repo_root=tmp_path / "repo", state_dir=explicit).probe()

    assert raw["oi_plan_unresolved"] == 5


# ---------------------------------------------------------------------------
# migration-mechanisms
# ---------------------------------------------------------------------------

def test_migration_probe_without_state_dir_reads_the_central_db(stores):
    conn = sqlite3.connect(str(stores.central_state / COORDINATION_DB_FILENAME))
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    # The checkout has no DB at all: the old resolver reported "no DB yet".

    raw = MigrationEffectivenessProbe().probe()

    assert raw["db_exists"] is True
    assert raw["claimed_version"] == 3


# ---------------------------------------------------------------------------
# intelligence-self-learning-loop
# ---------------------------------------------------------------------------

def _pending_rules(state_dir: Path, pending: int) -> None:
    (state_dir / "pending_rules.json").write_text(
        json.dumps({
            "pending_rules": [
                {"id": f"r{i}", "status": "pending", "created_at": "2026-09-20T00:00:00Z"}
                for i in range(pending)
            ]
        }),
        encoding="utf-8",
    )


def test_injection_probe_without_state_dir_reads_the_central_store(stores):
    _pending_rules(stores.central_state, pending=2)

    raw = InjectionEffectivenessProbe().probe()

    assert raw["pending_proposals"] == 2


def test_injection_reason_evaluator_without_state_dir_reads_the_central_store(stores):
    conn = sqlite3.connect(str(stores.central_state / "quality_intelligence.db"))
    conn.execute("CREATE TABLE pattern_injection_outcome (reason TEXT, used INTEGER)")
    conn.executemany(
        "INSERT INTO pattern_injection_outcome VALUES (?, 0)",
        [("stale",), ("stale",), ("bad-timing",)],
    )
    conn.commit()
    conn.close()

    distribution = InjectionReasonEvaluator().evaluate()

    assert distribution["total_ignored"] == 3


# ---------------------------------------------------------------------------
# the aggregator threads the data root it is given into the probes
# ---------------------------------------------------------------------------

def test_aggregate_hands_its_data_root_to_the_probes(stores, tmp_path):
    """subsystems.py passes its own resolved data_dir as ``state_dir``. That dir
    is the data ROOT (beacons go under ``<root>/health``); the probes read
    ``<root>/state``."""
    data_root = tmp_path / "cli-data-root"
    (data_root / "state").mkdir(parents=True)
    _plan_gate_db(data_root / "state", unresolved=2)

    results = subsystem_health.aggregate(state_dir=data_root, subsystems=["plan-gate-panel"])

    assert results["plan-gate-panel"]["detail"]["oi_plan_unresolved"] == 2


def test_aggregate_without_state_dir_reads_and_writes_the_central_store(stores):
    _plan_gate_db(stores.central_state, unresolved=2)
    _stale_checkout_db(stores.checkout_state)

    results = subsystem_health.aggregate(subsystems=["plan-gate-panel"])

    assert results["plan-gate-panel"]["detail"]["oi_plan_unresolved"] == 2
    assert (stores.central_data / "health" / "plan-gate-panel.json").exists()
    assert not (stores.checkout_data / "health").exists()
