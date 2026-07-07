"""Tests for scripts/fabric_audit.py — phase-0 fabric hardening audit (ADR-028)."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fabric_audit as fa  # noqa: E402


def _mk_project(data_home: Path, pid: str) -> None:
    (data_home / pid / "state").mkdir(parents=True, exist_ok=True)


def _registry(tmp_path: Path, projects: list[tuple[str, str]]) -> Path:
    """Write a registry JSON + .vnx-project-id per project; return registry path."""
    entries = []
    for pid, name in projects:
        proj_root = tmp_path / "repos" / name
        proj_root.mkdir(parents=True, exist_ok=True)
        (proj_root / ".vnx-project-id").write_text(pid + "\n", encoding="utf-8")
        entries.append({"name": name, "path": str(proj_root)})
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": entries}), encoding="utf-8")
    return reg


# ── Check A: legacy shared state ────────────────────────────────────────────

def test_no_bare_state_is_green(tmp_path):
    r = fa.check_shared_root_state(tmp_path)
    assert r.status == "GREEN"


def test_active_bare_state_fork_is_red(tmp_path):
    bare = tmp_path / "state"
    bare.mkdir()
    (bare / "runtime_coordination.db").write_bytes(b"x")  # fresh mtime = now
    r = fa.check_shared_root_state(tmp_path)
    assert r.status == "RED"
    assert "ACTIVE fork" in r.detail


def test_stale_bare_state_relic_is_warn(tmp_path):
    bare = tmp_path / "state"
    bare.mkdir()
    db = bare / "quality_intelligence.db"
    db.write_bytes(b"x")
    old = time.time() - (fa.ACTIVE_FORK_STALE_DAYS + 10) * 86400
    os.utime(db, (old, old))
    r = fa.check_shared_root_state(tmp_path)
    assert r.status == "WARN"
    assert "stale relic" in r.detail


def test_bare_state_without_db_is_green(tmp_path):
    (tmp_path / "state").mkdir()  # empty dir, no *.db
    r = fa.check_shared_root_state(tmp_path)
    assert r.status == "GREEN"


def test_recent_wal_on_stale_db_is_red(tmp_path):
    """A stale .db (old mtime) with a FRESH -wal sidecar is a possible live writer,
    not a safe stale relic — the split-brain false-clean this guards against
    (Jun-20 .db, same-day .db-wal read as 17d stale before this fix)."""
    bare = tmp_path / "state"
    bare.mkdir()
    db = bare / "runtime_coordination.db"
    db.write_bytes(b"x")
    old = time.time() - (fa.ACTIVE_FORK_STALE_DAYS + 10) * 86400
    os.utime(db, (old, old))
    (bare / "runtime_coordination.db-wal").write_bytes(b"")  # fresh mtime = now
    r = fa.check_shared_root_state(tmp_path)
    assert r.status == "RED"
    assert "lsof" in r.detail


def test_stale_db_with_stale_wal_stays_warn(tmp_path):
    """Old .db + old sidecar is still just cleanup debt — no false RED."""
    bare = tmp_path / "state"
    bare.mkdir()
    old = time.time() - (fa.ACTIVE_FORK_STALE_DAYS + 10) * 86400
    for name in ("runtime_coordination.db", "runtime_coordination.db-wal"):
        f = bare / name
        f.write_bytes(b"")
        os.utime(f, (old, old))
    r = fa.check_shared_root_state(tmp_path)
    assert r.status == "WARN"
    assert "stale relic" in r.detail


# ── Check B: per-project stores ─────────────────────────────────────────────

def test_all_project_stores_present_is_green(tmp_path):
    _mk_project(tmp_path, "vnx-dev")
    _mk_project(tmp_path, "seo")
    r = fa.check_per_project_stores(tmp_path, [("vnx-dev", ""), ("seo", "")])
    assert r.status == "GREEN"


def test_missing_project_store_is_red(tmp_path):
    _mk_project(tmp_path, "vnx-dev")
    r = fa.check_per_project_stores(tmp_path, [("vnx-dev", ""), ("ghost", "")])
    assert r.status == "RED"
    assert "ghost" in r.detail


def test_no_projects_is_skip(tmp_path):
    r = fa.check_per_project_stores(tmp_path, [])
    assert r.status == "SKIP"


def test_unreadable_registry_makes_check_b_red(tmp_path):
    r = fa.check_per_project_stores(tmp_path, [], registry_error="cannot read registry: boom")
    assert r.status == "RED"
    assert "unreadable" in r.detail


def test_malformed_registry_returns_error_not_silent_empty(tmp_path):
    reg = tmp_path / "projects.json"
    reg.write_text("{not valid json", encoding="utf-8")
    projects, err = fa._load_project_ids(reg)
    assert projects == []
    assert err is not None  # surfaced, not silently swallowed


def test_missing_registry_is_not_an_error(tmp_path):
    projects, err = fa._load_project_ids(tmp_path / "does-not-exist.json")
    assert projects == []
    assert err is None


def test_unsafe_project_id_is_skipped(tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / ".vnx-project-id").write_text("../escape\n", encoding="utf-8")
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "repo", "path": str(proj)}]}), encoding="utf-8")
    projects, err = fa._load_project_ids(reg)
    assert projects == []  # traversal id skipped
    assert err is None


# ── Check C: hash-chain integrity ───────────────────────────────────────────

def test_unchained_ledger_is_green(tmp_path):
    _mk_project(tmp_path, "vnx-dev")
    ledger = tmp_path / "vnx-dev" / "state" / "t0_receipts.ndjson"
    ledger.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")  # no prev_hash = unchained
    r = fa.check_hash_chains(tmp_path, [("vnx-dev", "")])
    assert r.status == "GREEN"


def test_partial_chain_ledger_is_red(tmp_path):
    _mk_project(tmp_path, "vnx-dev")
    ledger = tmp_path / "vnx-dev" / "state" / "t0_receipts.ndjson"
    # one entry chained, one not = partially chained = broken per verify_chain.
    genesis = "0" * 64
    ledger.write_text(
        json.dumps({"a": 1, "prev_hash": genesis}) + "\n" + json.dumps({"a": 2}) + "\n",
        encoding="utf-8",
    )
    r = fa.check_hash_chains(tmp_path, [("vnx-dev", "")])
    assert r.status == "RED"


def test_no_ledger_is_skip(tmp_path):
    _mk_project(tmp_path, "vnx-dev")
    r = fa.check_hash_chains(tmp_path, [("vnx-dev", "")])
    assert r.status == "SKIP"


# ── Integration: run_audit + project-id resolution ──────────────────────────

def test_run_audit_clean_fabric_all_green(tmp_path):
    data_home = tmp_path / "data"
    data_home.mkdir()
    reg = _registry(tmp_path, [("vnx-dev", "vnx-orchestration")])
    _mk_project(data_home, "vnx-dev")
    results = fa.run_audit(data_home, reg)
    assert all(r.status in ("GREEN", "SKIP") for r in results)


def test_run_audit_resolves_project_id_from_file(tmp_path):
    """Registry name != project-id; the audit must resolve the id from .vnx-project-id."""
    data_home = tmp_path / "data"
    data_home.mkdir()
    reg = _registry(tmp_path, [("vnx-dev", "vnx-orchestration")])
    _mk_project(data_home, "vnx-dev")  # store keyed by the id, not the registry name
    projects, err = fa._load_project_ids(reg)
    b = fa.check_per_project_stores(data_home, projects, err)
    assert b.status == "GREEN"


def test_main_json_exit_code_red(tmp_path, capsys):
    data_home = tmp_path / "data"
    data_home.mkdir()
    bare = data_home / "state"
    bare.mkdir()
    (bare / "x.db").write_bytes(b"x")  # active fork
    reg = _registry(tmp_path, [])
    rc = fa.main(["--data-home", str(data_home), "--registry", str(reg), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["overall"] == "RED"
