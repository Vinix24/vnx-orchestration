"""Tests for scripts/fabric_audit.py — phase-0 fabric hardening audit (ADR-028)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import fabric_audit as fa  # noqa: E402
import chain_origin_anchor as coa  # noqa: E402
from ndjson_hash_chain import append_chained_entry  # noqa: E402


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


# ── Check D: chain-origin anchor provenance (ADR-034) ───────────────────────


def _run_git(*args: str, cwd) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed in {cwd}: {result.stderr}")
    return result.stdout


def _anchor_git_repo(tmp_path):
    """A working-tree repo with a local bare 'origin' remote, so fetch/show/push
    against origin/main work for real with no network access — mirrors
    tests/test_chain_origin_anchor.py's git_repo fixture (kept local here so
    this file doesn't need a cross-file fixture import)."""
    bare = tmp_path / "origin.git"
    _run_git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "repo"
    _run_git("init", "-b", "main", str(work), cwd=tmp_path)
    _run_git("config", "user.email", "test@example.com", cwd=work)
    _run_git("config", "user.name", "Test", cwd=work)
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    _run_git("add", "README.md", cwd=work)
    _run_git("commit", "-m", "seed", cwd=work)
    _run_git("remote", "add", "origin", str(bare), cwd=work)
    _run_git("push", "-u", "origin", "main", cwd=work)
    return work


def _seal_and_merge(project_root, ledger, data_home, project_id="vnx-dev"):
    result = coa.seal_and_commit_origin(
        ledger,
        project_root,
        project_id=project_id,
        project_data_dir=data_home / project_id,
        branch="main",
        branch_protection_confirmed=True,
    )
    assert result.action == "sealed"
    _run_git("checkout", "main", cwd=project_root)
    _run_git("merge", "--no-ff", "-m", f"merge {result.branch_name}", result.branch_name, cwd=project_root)
    _run_git("push", "origin", "main", cwd=project_root)
    return result


def test_check_d_unchained_ledger_with_git_repo_is_green(tmp_path):
    data_home = tmp_path / "data"
    project_root = _anchor_git_repo(tmp_path)
    ledger = data_home / "vnx-dev" / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")  # no prev_hash = unchained

    r = fa.check_anchor_provenance(data_home, [("vnx-dev", str(project_root))])
    assert r.status == "GREEN"
    assert r.findings and r.findings[0]["status"] == "unchained"


def test_check_d_sealed_ledger_reports_provenance(tmp_path, monkeypatch):
    data_home = tmp_path / "data"
    project_root = _anchor_git_repo(tmp_path)
    ledger = data_home / "vnx-dev" / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True)
    append_chained_entry(ledger, {"seq": 0})

    monkeypatch.setattr(coa, "ensure_pr", lambda *a, **kw: {"pr_number": None, "created": False, "reason": "test"})
    _seal_and_merge(project_root, ledger, data_home)

    r = fa.check_anchor_provenance(data_home, [("vnx-dev", str(project_root))])
    assert r.status == "GREEN"
    finding = r.findings[0]
    assert finding["status"] == "verified-segmented"
    assert finding["anchor_commit_sha"] is not None
    assert finding["remote_url"] is not None


def test_check_d_deleted_ledger_with_anchor_is_red(tmp_path, monkeypatch):
    """Reverse-direction case (ADR §2): a git anchor exists, but the ledger was
    reset — check D must go RED, not read as a clean 'unchained' (this is
    exactly the gap check C alone leaves open, since it calls the BASE
    verify_chain with no anchor awareness)."""
    data_home = tmp_path / "data"
    project_root = _anchor_git_repo(tmp_path)
    ledger = data_home / "vnx-dev" / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True)
    append_chained_entry(ledger, {"seq": 0})

    monkeypatch.setattr(coa, "ensure_pr", lambda *a, **kw: {"pr_number": None, "created": False, "reason": "test"})
    _seal_and_merge(project_root, ledger, data_home)

    ledger.write_text("", encoding="utf-8")  # reset / deleted content

    r = fa.check_anchor_provenance(data_home, [("vnx-dev", str(project_root))])
    assert r.status == "RED"
    assert "vnx-dev" in r.detail


def test_check_d_missing_ledger_file_with_anchor_is_red(tmp_path, monkeypatch):
    """Finding 3 (ADR-034 fix-r1): the ledger file is entirely ABSENT (not
    merely emptied, as in test_check_d_deleted_ledger_with_anchor_is_red
    above) while a git anchor exists on origin for this identity. The old
    `if not ledger.exists(): continue` skipped this project before the
    anchor-aware verifier ever ran, so `checked` stayed 0 and the whole
    finding read as SKIP — silently missing the exact reverse-direction
    tamper case check D exists to catch."""
    data_home = tmp_path / "data"
    project_root = _anchor_git_repo(tmp_path)
    ledger = data_home / "vnx-dev" / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True)
    append_chained_entry(ledger, {"seq": 0})

    monkeypatch.setattr(coa, "ensure_pr", lambda *a, **kw: {"pr_number": None, "created": False, "reason": "test"})
    _seal_and_merge(project_root, ledger, data_home)

    ledger.unlink()  # file gone entirely, not just emptied
    assert not ledger.exists()

    r = fa.check_anchor_provenance(data_home, [("vnx-dev", str(project_root))])
    assert r.status == "RED"
    assert "vnx-dev" in r.detail


def test_check_d_no_verifier_import_is_red(monkeypatch, tmp_path):
    """Finding 4 (ADR-034 fix-r1): an import failure means the anchor check
    never ran — that must block the audit (RED), not read as a merely
    advisory WARN that lets `main()` exit 0."""
    monkeypatch.setattr(fa, "anchor_verify_chain", None)
    r = fa.check_anchor_provenance(tmp_path, [("vnx-dev", "")])
    assert r.status == "RED"


def test_check_d_no_projects_is_skip(tmp_path):
    r = fa.check_anchor_provenance(tmp_path, [])
    assert r.status == "SKIP"


def test_check_d_unresolvable_project_root_is_skip(tmp_path):
    data_home = tmp_path / "data"
    ledger = data_home / "vnx-dev" / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")
    r = fa.check_anchor_provenance(data_home, [("vnx-dev", "")])  # empty path -> no resolvable repo
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


def test_main_exits_red_when_anchor_verifier_missing(monkeypatch, tmp_path, capsys):
    """Finding 4 (ADR-034 fix-r1) end-to-end: an anchor-verifier import
    failure must fail the whole `main()` invocation (exit 1), not let an
    otherwise-clean fabric read as GREEN/GREEN-WITH-WARN while the
    anchor-provenance check silently never ran."""
    data_home = tmp_path / "data"
    data_home.mkdir()
    reg = _registry(tmp_path, [("vnx-dev", "vnx-orchestration")])
    _mk_project(data_home, "vnx-dev")
    monkeypatch.setattr(fa, "anchor_verify_chain", None)
    rc = fa.main(["--data-home", str(data_home), "--registry", str(reg), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["overall"] == "RED"


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
