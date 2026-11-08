"""ADR-056 zelfst PR-7a: ``receipt_query.py operator-actions`` and ``operator-cohort``.

Dispatch-ID: dlv-023bb58d7818

The commands run as real subprocesses against ``tmp_path`` stores (a real
``runtime_coordination.db`` plus NDJSON files); the real central store is never read.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import project_id_migration  # noqa: E402

PROJECT = "p-test"
INSIDE = "2026-11-02T10:00:00.000Z"
AFTER_END = "2026-11-09T00:00:00.000Z"
SCRIPT = SCRIPT_DIR / "receipt_query.py"

_spec_0017 = _ilu.spec_from_file_location(
    "apply_0017_pr7a", SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py")
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


def make_store(state_dir: Path, project: str = PROJECT) -> Path:
    state_dir.mkdir(parents=True)
    coordination_db.init_schema(state_dir)
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=project)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        _mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    (state_dir / "t0_receipts.ndjson").write_text("", encoding="utf-8")
    return state_dir


def add_delivery(sd: Path, did: str, state: str = "completed", *, pr_ref=None,
                 created_at: str = INSIDE, project: str = PROJECT) -> None:
    with coordination_db.get_connection(sd) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, pr_ref, "
            "created_at, metadata_json) VALUES (?, ?, ?, 1, ?, ?, '{}')",
            (did, project, state, pr_ref, created_at))
        conn.commit()


def add_event(sd: Path, kind: str, did: str, klass, *, project: str = PROJECT, n=[0]) -> None:
    n[0] += 1
    meta = {"project_id": project, "delivery_id": did}
    if klass is not None:
        meta["actor_class"] = klass
    with coordination_db.get_connection(sd) as conn:
        conn.execute(
            "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id, metadata_json) "
            "VALUES (?, ?, 'deliverable', ?, ?)", (f"ev-{n[0]}", kind, did, json.dumps(meta)))
        conn.commit()


def add_merge(sd: Path, did: str, klass, *, project: str = PROJECT) -> None:
    line = {"event_type": "pr_merged", "conclusion": "merged", "dispatch_id": did,
            "delivery_id": did, "project_id": project}
    if klass is not None:
        line["actor_class"] = klass
    with (sd / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def run(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("VNX_") or k in ("VNX_HOME",)}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


def cohort(sd: Path, *extra: str) -> tuple:
    proc = run("operator-cohort", "--project", PROJECT, "--state-dir", str(sd), "--json", *extra)
    return proc, (json.loads(proc.stdout) if proc.stdout.strip() else None)


@pytest.fixture()
def sd(tmp_path: Path) -> Path:
    return make_store(tmp_path / "data" / "state")


def three_deliveries(sd: Path) -> None:
    add_delivery(sd, "dlv-000000000001")  # only autonomous rows
    add_event(sd, "deliverable_promoted", "dlv-000000000001", "t0")
    add_event(sd, "deliverable_completed", "dlv-000000000001", "worker")
    add_delivery(sd, "dlv-000000000002")  # operator promote
    add_event(sd, "deliverable_promoted", "dlv-000000000002", "operator")
    add_delivery(sd, "dlv-000000000003", pr_ref="#3")  # unknown merge
    add_event(sd, "deliverable_promoted", "dlv-000000000003", "t0")
    add_merge(sd, "dlv-000000000003", "unknown")


def test_zero_rate_over_n(sd):
    three_deliveries(sd)

    proc, out = cohort(sd)

    assert proc.returncode == 0, proc.stderr
    assert out["N"] == 3
    assert (out["zero"], out["operator"], out["unknown"]) == (1, 1, 1)
    assert out["zero_rate"] == pytest.approx(1 / 3)
    assert out["unknown_rate"] == pytest.approx(1 / 3)
    assert out["period"] == {"since": "2026-10-30T00:00:00Z", "until": "2026-11-09T00:00:00Z"}
    assert out["coverage"]["state"] in ("covered", "uncovered", "no_data")
    assert out["membership_summary"]["delivered"] == 3


def test_withdrawn_is_not_in_n(sd):
    three_deliveries(sd)
    add_delivery(sd, "dlv-000000000004", "withdrawn")
    add_event(sd, "deliverable_promoted", "dlv-000000000004", "t0")

    _, out = cohort(sd)

    assert out["N"] == 3
    assert out["membership_summary"]["not_delivered"] == 1
    assert "dlv-000000000004" not in {d["delivery_id"] for d in out["deliveries"]}


def test_receipt_missing_is_in_n_and_unknown(sd):
    add_delivery(sd, "dlv-000000000005", pr_ref="#5")
    add_event(sd, "deliverable_promoted", "dlv-000000000005", "t0")

    _, out = cohort(sd)

    assert out["N"] == 1 and out["unknown"] == 1 and out["zero"] == 0
    assert out["unknown_rate"] == 1.0 and out["zero_rate"] == 0.0
    d = out["deliveries"][0]
    assert d["verdict"] == "unknown" and "receipt_missing" in d["reasons"]
    assert "source_missing:merge" in d["reasons"]


def test_missing_promote_row_is_unknown(sd):
    add_delivery(sd, "dlv-000000000006")
    add_event(sd, "deliverable_completed", "dlv-000000000006", "t0")

    _, out = cohort(sd)

    d = out["deliveries"][0]
    assert d["verdict"] == "unknown" and d["reason"] == "source_missing:promote"
    proc = run("operator-actions", "--project", PROJECT, "--state-dir", str(sd))
    assert "unknown:source_missing:promote" in proc.stdout


def test_row_of_another_project_is_ignored(sd):
    add_delivery(sd, "dlv-000000000007")
    add_event(sd, "deliverable_promoted", "dlv-000000000007", "operator", project="other-project")

    _, out = cohort(sd)

    d = out["deliveries"][0]
    assert d["operator_actions"] == 0 and d["reason"] == "source_missing:promote"


def test_project_unknown_row_never_joins(sd):
    add_delivery(sd, "dlv-000000000008")
    add_event(sd, "deliverable_promoted", "dlv-000000000008", "t0")
    add_event(sd, "deliverable_completed", "dlv-000000000008", "operator", project="unknown")

    _, out = cohort(sd)

    d = out["deliveries"][0]
    assert d["verdict"] == "zero" and "close" not in d["sources"]


def test_row_without_class_reads_unknown(sd):
    add_delivery(sd, "dlv-000000000009")
    add_event(sd, "deliverable_promoted", "dlv-000000000009", None)

    _, out = cohort(sd)

    assert out["deliveries"][0]["reason"] == "class_unknown:promote"


def test_row_created_at_or_after_until_is_outside(sd):
    add_delivery(sd, "dlv-00000000000a", created_at=AFTER_END)
    add_event(sd, "deliverable_promoted", "dlv-00000000000a", "t0")

    _, out = cohort(sd)

    assert out["N"] == 0


def test_n_zero_gives_null_rates(sd):
    proc, out = cohort(sd)

    assert proc.returncode == 0
    assert out["N"] == 0 and out["zero_rate"] is None and out["unknown_rate"] is None


def test_operator_actions_lists_counts_per_source(sd):
    three_deliveries(sd)

    out = json.loads(run("operator-actions", "--project", PROJECT, "--state-dir", str(sd), "--json").stdout)

    by_id = {d["delivery_id"]: d for d in out["deliveries"]}
    assert by_id["dlv-000000000001"]["sources"] == {"promote": {"t0": 1}, "close": {"worker": 1}}
    assert by_id["dlv-000000000002"]["verdict"] == "operator"
    assert by_id["dlv-000000000003"]["sources"]["merge"] == {"unknown": 1}


def test_all_projects_reports_per_project_without_aggregate(tmp_path):
    home = tmp_path / "home"
    make_store(home / "vnx-dev" / "state", "vnx-dev")
    sd_mc = make_store(home / "mission-control" / "state", "mission-control")
    add_delivery(sd_mc, "dlv-0000000000b1", project="mission-control")
    add_event(sd_mc, "deliverable_promoted", "dlv-0000000000b1", "t0", project="mission-control")

    proc = run("operator-cohort", "--all-projects", "--json", env_extra={"VNX_DATA_HOME": str(home)})

    out = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stderr
    assert set(out) == {"projects"}
    assert set(out["projects"]) == {"vnx-dev", "seocrawler", "mission-control", "sales-copilot", "pacompany"}
    assert out["projects"]["mission-control"]["N"] == 1
    assert out["projects"]["vnx-dev"]["N"] == 0 and out["projects"]["vnx-dev"]["zero_rate"] is None
    assert out["projects"]["pacompany"]["coverage"]["state"] == "no_data"


def test_unreadable_store_exits_nonzero_with_reason(tmp_path):
    home = tmp_path / "home"
    make_store(home / "vnx-dev" / "state", "vnx-dev")
    broken = home / "seocrawler" / "state"
    broken.mkdir(parents=True)
    (broken / "runtime_coordination.db").mkdir()

    proc = run("operator-cohort", "--all-projects", "--json", env_extra={"VNX_DATA_HOME": str(home)})

    out = json.loads(proc.stdout)["projects"]
    assert proc.returncode == 1
    assert out["seocrawler"]["status"] == "unmeasured" and "not a regular file" in out["seocrawler"]["reason"]
    assert out["vnx-dev"]["N"] == 0


def test_single_unreadable_store_is_not_a_zero(tmp_path):
    broken = tmp_path / "state"
    broken.mkdir()
    (broken / "runtime_coordination.db").mkdir()

    proc, out = cohort(broken)

    assert proc.returncode == 1
    assert out["status"] == "unmeasured" and "N" not in out


def test_project_and_all_projects_are_exclusive(sd):
    assert run("operator-cohort", "--json").returncode == 2
    assert run("operator-cohort", "--project", PROJECT, "--all-projects").returncode == 2


def _snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


def test_nothing_is_written(sd):
    three_deliveries(sd)
    before = _snapshot(sd.parent)

    assert cohort(sd)[0].returncode == 0
    assert run("operator-actions", "--project", PROJECT, "--state-dir", str(sd)).returncode == 0

    assert _snapshot(sd.parent) == before
