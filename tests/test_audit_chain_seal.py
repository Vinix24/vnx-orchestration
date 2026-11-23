"""audit_chain.py seal + anchor-aware verify (receipt-hashketen-aan H4).

A temporary git repo with a bare repo as ``origin``; tmp_path ledgers only.
``check_branch_protection`` (calls ``gh``) and ``ensure_pr`` are patched.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS / "lib"))
sys.path.insert(0, str(_SCRIPTS))

import audit_chain  # noqa: E402
import chain_branch_protection_check as cbpc  # noqa: E402
import chain_origin_anchor as coa  # noqa: E402
from ndjson_hash_chain import append_chained_entry  # noqa: E402


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    bare = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "work"
    _git("init", "-b", "main", str(work), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "T", cwd=work)
    _git("commit", "--allow-empty", "-m", "seed", cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    monkeypatch.setattr(coa, "ensure_pr", lambda *a, **kw: {"pr_number": None})
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    ledger = data_dir / "state" / "t0_receipts.ndjson"
    ledger.write_text("".join(json.dumps({"dispatch_id": f"d-{i}"}) + "\n" for i in range(3)))
    ident = ["--project-id", "vnx-dev", "--project-root", str(work), "--data-dir", str(data_dir)]
    return work, bare, ledger, ident


def _protection(monkeypatch, confirmed):
    status = coa.BranchProtectionStatus(
        confirmed=confirmed, owner_repo="acme/repo", branch="main", required_check_present=confirmed,
        enforce_admins=confirmed, force_pushes_blocked=confirmed, reason=None if confirmed else "test refuses",
    )
    monkeypatch.setattr(cbpc, "check_branch_protection", lambda *a, **kw: status)


def _run(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        audit_chain.main(argv)
    return exc.value.code, json.loads(capsys.readouterr().out)


def _seal_and_chain(env, monkeypatch, capsys):
    work, _bare, ledger, ident = env
    _protection(monkeypatch, True)
    code, out = _run(["seal", str(ledger), *ident], capsys)
    assert code == 0, out
    for i in range(2):
        append_chained_entry(ledger, {"dispatch_id": f"c-{i}"})
    return out["seal_result"]["branch_name"], out


def test_seal_refuses_without_confirmed_protection(env, monkeypatch, capsys):
    work, bare, ledger, ident = env
    before = ledger.read_bytes()
    heads = _git("for-each-ref", cwd=work), _git("for-each-ref", cwd=bare)
    _protection(monkeypatch, False)
    code, out = _run(["seal", str(ledger), *ident], capsys)
    assert code != 0 and out["sealed"] is False and "test refuses" in out["error"]
    assert ledger.read_bytes() == before  # no chain_epoch_start marker written
    assert (_git("for-each-ref", cwd=work), _git("for-each-ref", cwd=bare)) == heads


def test_seal_anchors_and_verify_reads_the_anchor(env, monkeypatch, capsys):
    work, bare, ledger, ident = env
    branch, out = _seal_and_chain(env, monkeypatch, capsys)
    assert any(json.loads(line).get("type") == "chain_epoch_start" for line in ledger.read_text().splitlines())
    assert out["anchor_record"]["key"] == "vnx-dev:state/t0_receipts.ndjson#1"
    assert out["anchor"]["resolved"] is True and out["anchor"]["anchor_commit_sha"]
    assert branch in _git("branch", "--list", branch, cwd=bare)
    code, out = _run(["verify", str(ledger), *ident, "--anchor-ref", f"origin/{branch}"], capsys)
    assert (code, out["status"], out["verified"], out["anchor_checked"]) == (0, "verified-segmented", True, True)
    assert out["anchor"]["resolved"] is True and out["anchor"]["ref"] == f"origin/{branch}"
    assert "last valid JSON line" in out["tail_coverage"]


def test_verify_with_identity_but_no_anchor_on_ref_is_broken(env, monkeypatch, capsys):
    _work, _bare, ledger, ident = env
    _seal_and_chain(env, monkeypatch, capsys)
    code, out = _run(["verify", str(ledger), *ident], capsys)  # origin/main has no anchor yet
    assert code == 1 and out["status"] == "broken"


def test_torn_tail_after_seal_is_broken(env, monkeypatch, capsys):
    _work, _bare, ledger, ident = env
    branch, _out = _seal_and_chain(env, monkeypatch, capsys)
    with ledger.open("a") as f:
        f.write('{"dispatch_id": "torn')
    code, out = _run(["verify", str(ledger), *ident, "--anchor-ref", f"origin/{branch}"], capsys)
    assert code != 0 and out["status"] == "broken"


def test_changed_last_valid_line_of_open_epoch_is_not_found(env, monkeypatch, capsys):
    """Binding 3: the documented open tail piece of ADR-034, pinned."""
    _work, _bare, ledger, ident = env
    branch, _out = _seal_and_chain(env, monkeypatch, capsys)
    lines = ledger.read_text().splitlines()
    last = json.loads(lines[-1])
    last["dispatch_id"] = "rewritten"
    ledger.write_text("\n".join(lines[:-1] + [json.dumps(last)]) + "\n")
    code, out = _run(["verify", str(ledger), *ident, "--anchor-ref", f"origin/{branch}"], capsys)
    assert (code, out["status"]) == (0, "verified-segmented")


def test_unresolvable_identity_is_an_error_not_a_guess(env, capsys, tmp_path):
    _work, _bare, ledger, ident = env
    code, out = _run(["verify", str(ledger), *ident[:4], "--data-dir", str(tmp_path / "absent")], capsys)
    assert code == audit_chain.EXIT_IDENTITY_ERROR and "identity" in out["error"]
    code, out = _run(["verify", str(ledger), *ident, "--anchor-ref", "main"], capsys)
    assert code == audit_chain.EXIT_IDENTITY_ERROR


def test_verify_without_identity_checks_no_anchor(env, capsys):
    _work, _bare, ledger, _ident = env
    code, out = _run(["verify", str(ledger)], capsys)
    assert (code, out["status"], out["anchor_checked"]) == (2, "unchained", False)
