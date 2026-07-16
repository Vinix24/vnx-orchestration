"""Tests for ADR-034 external chain-origin anchor (scripts/lib/chain_origin_anchor.py).

Maps to docs/governance/decisions/ADR-034-external-chain-origin-anchor.md §7's
required test list T1-T18 (T19 is not implemented here: the ADR itself frames
it as "demonstrates the partial backstop, not a fix" — already covered by
T13's mechanism — and needs no new CI wiring, which is PR-2 scope anyway).
T6/T7 exercise check_anchor_immutability's pure diff LOGIC directly; the
.github/workflows/ wiring that will invoke it in CI is explicitly PR-2 scope
and is not created by this PR.

Fixtures use a real local git repo with a local bare "origin" remote so
fetch/show/push against origin/main work for real, without any network or
GitHub access. `gh` (PR creation) is monkeypatched out — seal_and_commit_origin
must never depend on `gh` being installed/authenticated for its core (file +
commit + push) contract to be testable.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import chain_origin_anchor as coa  # noqa: E402
from chain_origin_anchor import (  # noqa: E402
    BranchProtectionUnconfirmedError,
    anchor_key,
    check_anchor_immutability,
    closure_key,
    compute_epoch_fingerprint,
    ledger_identity,
    read_git_anchor,
    read_git_anchors_for_identity,
    seal_and_commit_origin,
    verify_chain,
)
from ndjson_hash_chain import (  # noqa: E402
    GENESIS_HASH,
    _ledger_locked,
    append_chained_entry,
    append_epoch_marker,
    compute_entry_hash,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _run(*args: str, cwd: Path) -> str:
    result = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed in {cwd}: {result.stderr}")
    return result.stdout


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A working-tree repo with a local `origin` remote (a bare repo) so
    fetch/show/push against origin/main work for real, with no network
    access. `ensure_pr` is stubbed so seal_and_commit_origin never shells out
    to `gh` (which has nothing real to talk to here)."""
    bare = tmp_path / "origin.git"
    _run("git", "init", "--bare", "-b", "main", str(bare), cwd=tmp_path)

    work = tmp_path / "work"
    _run("git", "init", "-b", "main", str(work), cwd=tmp_path)
    _run("git", "config", "user.email", "test@example.com", cwd=work)
    _run("git", "config", "user.name", "Test", cwd=work)
    (work / "README.md").write_text("seed\n")
    _run("git", "add", "README.md", cwd=work)
    _run("git", "commit", "-m", "seed", cwd=work)
    _run("git", "remote", "add", "origin", str(bare), cwd=work)
    _run("git", "push", "-u", "origin", "main", cwd=work)

    monkeypatch.setattr(
        coa, "ensure_pr", lambda *a, **kw: {"pr_number": None, "created": False, "reason": "test-stub"}
    )
    return work


def _merge_branch_to_main(work: Path, branch: str) -> None:
    """Simulate a PR merge: merge `branch` into local main and push to origin."""
    _run("git", "checkout", "main", cwd=work)
    _run("git", "merge", "--no-ff", "-m", f"merge {branch}", branch, cwd=work)
    _run("git", "push", "origin", "main", cwd=work)


def _seal(work, ledger, *, project_id="vnx-dev", branch="main", force_new_epoch=False, confirmed=True):
    data_dir = ledger.parent.parent
    result = seal_and_commit_origin(
        ledger,
        work,
        project_id=project_id,
        project_data_dir=data_dir,
        branch=branch,
        force_new_epoch=force_new_epoch,
        branch_protection_confirmed=confirmed,
    )
    if result.action == "sealed":
        _merge_branch_to_main(work, result.branch_name)
    return result


def _write_raw_anchor_to_main(work: Path, records: list[dict]) -> None:
    """Write anchor records DIRECTLY (bypassing seal_and_commit_origin) — used
    to construct adversarial/forged states verify_chain must reject."""
    anchor_path = work / coa.ANCHOR_REL_PATH
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    with anchor_path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
    _run("git", "add", str(coa.ANCHOR_REL_PATH), cwd=work)
    _run("git", "commit", "-m", "test: seed anchor", cwd=work)
    _run("git", "push", "origin", "main", cwd=work)


def _ledger_and_data_dir(tmp_path: Path, project_id: str = "vnx-dev") -> tuple[Path, Path]:
    data_dir = tmp_path / ".vnx-data" / project_id
    ledger = data_dir / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True)
    return ledger, data_dir


# ---------------------------------------------------------------------------
# Key/identity primitives
# ---------------------------------------------------------------------------


def test_anchor_key_and_closure_key_format():
    identity = "vnx-dev:state/t0_receipts.ndjson"
    assert anchor_key(identity, 1) == "vnx-dev:state/t0_receipts.ndjson#1"
    assert closure_key(identity, 1) == "vnx-dev:state/t0_receipts.ndjson#1:close"
    assert closure_key(identity, 1) == f"{anchor_key(identity, 1)}:close"


def test_ledger_identity_is_relative_not_absolute(tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    identity = ledger_identity("vnx-dev", ledger, data_dir)
    assert identity == "vnx-dev:state/t0_receipts.ndjson"
    assert str(data_dir) not in identity
    assert str(tmp_path) not in identity


def test_compute_epoch_closure_zero_entries_degenerates_to_marker(tmp_path):
    """force_new_epoch closing an epoch with nothing appended since it opened
    (ADR §1's "shrink the residual window" pattern) must produce a
    well-defined closure, not raise — the closure degenerates to the epoch's
    own opening marker."""
    ledger, _data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)  # line 1
    append_epoch_marker(ledger, 2)  # line 2, immediately closes epoch 1 with nothing inside

    fp = compute_epoch_fingerprint(ledger, 1)
    closure = coa.compute_epoch_closure(ledger, 1, next_epoch_marker_line=2)
    assert closure.entries_in_epoch == 0
    assert closure.closure_hash == fp.origin_hash
    assert closure.closure_line_number == fp.origin_line_number == 1


def test_compute_epoch_fingerprint_and_closure(tmp_path):
    ledger, _data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})
    append_chained_entry(ledger, {"seq": 2})
    append_epoch_marker(ledger, 2)

    fp = compute_epoch_fingerprint(ledger, 1)
    assert fp.epoch == 1
    assert fp.origin_line_number == 1
    assert fp.entries_before_origin == 0

    closure = coa.compute_epoch_closure(ledger, 1, next_epoch_marker_line=4)
    assert closure.closure_line_number == 3
    assert closure.entries_in_epoch == 2


# ---------------------------------------------------------------------------
# T1-T5, T8, T13, T15, T17, T18: verify_chain's anchor-aware contract
# ---------------------------------------------------------------------------


def test_t1_chaining_no_anchor_anywhere_is_broken(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})

    ok, violations, status, prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"
    assert prov is not None and prov.resolved is True


def test_t2_working_tree_anchor_edit_is_ignored(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})
    result = _seal(git_repo, ledger)
    assert result.action == "sealed"

    baseline = verify_chain(ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir)
    assert baseline[2] == "verified-segmented"

    anchor_path = git_repo / coa.ANCHOR_REL_PATH
    anchor_path.write_text("garbage not json\n")
    edited = verify_chain(ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir)
    assert edited[:3] == baseline[:3]

    anchor_path.unlink()
    deleted = verify_chain(ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir)
    assert deleted[:3] == baseline[:3]


def test_t3_no_project_root_on_chaining_ledger_is_broken(tmp_path):
    ledger, _data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})

    ok, violations, status, prov = verify_chain(ledger)
    assert ok is False
    assert status == "broken"
    assert prov is None


def test_t4_unchained_no_anchor_stays_unchained(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    with ledger.open("a") as f:
        f.write(json.dumps({"seq": 0}) + "\n")

    ok, violations, status, prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is True
    assert status == "unchained"
    assert prov is not None and prov.resolved is True


def test_t5_duplicate_anchor_records_is_corrupt_then_broken(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})
    identity = ledger_identity("vnx-dev", ledger, data_dir)
    fp = compute_epoch_fingerprint(ledger, 1)
    record = coa._origin_record(identity, fp, sealed_at="2026-01-01T00:00:00Z")
    _write_raw_anchor_to_main(git_repo, [record, record])  # forged duplicate

    rec, _prov = read_git_anchor(git_repo, identity, 1)
    assert rec == "corrupt"

    ok, violations, status, prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"


def test_t8_core_bypass_prefix_strip_and_rechain_is_broken(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})
    append_chained_entry(ledger, {"seq": 2})
    result = _seal(git_repo, ledger)
    assert result.action == "sealed"

    baseline_ok, _v, baseline_status, _p = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert baseline_ok and baseline_status == "verified-segmented"

    # Attack: strip the ledger entirely, insert a fresh epoch-1 marker (same
    # epoch number as the anchored one, but a DIFFERENT line/hash) and
    # re-chain the remainder — the #1085/#1086/#1171 bypass.
    ledger.write_text("")
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": "re-chained-1"})
    append_chained_entry(ledger, {"seq": "re-chained-2"})

    ok, violations, status, _prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"
    assert any("fingerprint mismatch" in str(v.get("note", "")) for v in violations)


def test_t13_reverse_direction_ledger_deleted_anchor_remains_is_broken(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})
    result = _seal(git_repo, ledger)
    assert result.action == "sealed"

    ledger.unlink()
    assert not ledger.exists()

    ok, violations, status, prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"
    assert status != "unchained"
    assert prov is not None and prov.resolved is True


def test_t15_unanchored_new_epoch_is_broken(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})
    result = _seal(git_repo, ledger)
    assert result.action == "sealed" and result.epoch == 1

    # Bypass: legitimately open + chain epoch 2, but never anchor it.
    append_epoch_marker(ledger, 2)
    append_chained_entry(ledger, {"seq": 2})

    ok, violations, status, _prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"
    assert any(v.get("epoch") == 2 for v in violations)


def test_t17_intra_epoch_rewrite_between_markers_is_broken(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": "a"})
    append_chained_entry(ledger, {"seq": "b"})
    result1 = _seal(git_repo, ledger)
    assert result1.action == "sealed" and result1.epoch == 1

    result2 = _seal(git_repo, ledger, force_new_epoch=True)
    assert result2.action == "sealed" and result2.epoch == 2 and result2.closed_epoch == 1

    baseline_ok, _v, baseline_status, _p = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert baseline_ok and baseline_status == "verified-segmented"

    # Attack: rewrite the two entries strictly BETWEEN epoch 1's marker and
    # epoch 2's marker, preserving line count, recomputing prev_hash for the
    # rewritten suffix, leaving BOTH markers themselves untouched. The base
    # walk must see this as internally consistent — only the anchored
    # closure fingerprint (computed on the ORIGINAL content) can catch it.
    lines = ledger.read_text().splitlines()
    marker_entry = json.loads(lines[0])
    marker_hash = compute_entry_hash(marker_entry)
    rewritten_a = {"seq": "REWRITTEN-a", "prev_hash": marker_hash}
    rewritten_b = {"seq": "REWRITTEN-b", "prev_hash": compute_entry_hash(rewritten_a)}
    lines[1] = json.dumps(rewritten_a)
    lines[2] = json.dumps(rewritten_b)
    ledger.write_text("\n".join(lines) + "\n")

    ok, violations, status, _prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"
    assert any(v.get("epoch") == 1 and "closure" in str(v.get("note", "")) for v in violations)


def test_t18_reverse_scan_finds_epoch2_only_anchor_no_epoch1(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    identity = ledger_identity("vnx-dev", ledger, data_dir)
    fake_fp = coa.OriginFingerprint(
        epoch=2, origin_type="chain_epoch_start", origin_hash="f" * 64, origin_line_number=5, entries_before_origin=4
    )
    record = coa._origin_record(identity, fake_fp, sealed_at="2026-01-01T00:00:00Z")
    _write_raw_anchor_to_main(git_repo, [record])

    assert not ledger.exists()
    anchors, prov = read_git_anchors_for_identity(git_repo, identity)
    assert prov.resolved is True
    assert len(anchors) == 1 and anchors[0].epoch == 2

    ok, violations, status, _prov2 = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"
    assert status != "unchained"


def test_t19_chained_markerless_rechain_with_existing_anchor_is_broken(git_repo, tmp_path):
    """ADR-034 fix-r1 Finding 1+2 (a): stripping every chain_epoch_start
    marker and re-chaining from GENESIS makes _observed_epochs(path) == [],
    so the per-epoch loop never runs. Only the reverse identity-scan — which
    must run unconditionally in the chained path, not only when base_status
    == "unchained" — catches that this identity has a committed anchor the
    ledger no longer shows any epoch for."""
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})
    result = _seal(git_repo, ledger)
    assert result.action == "sealed"

    baseline_ok, _v, baseline_status, _p = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert baseline_ok and baseline_status == "verified-segmented"

    # Attack: strip the ledger entirely and re-chain from GENESIS with no
    # chain_epoch_start marker at all — the base walk sees a self-consistent
    # marker-less "verified" chain (saw_chain True via base_status != "unchained").
    ledger.write_text("")
    append_chained_entry(ledger, {"seq": "re-chained-1"})

    ok, violations, status, _prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"
    assert status != "unchained"
    assert any("markerless" in str(v.get("note", "")) or "no longer observes" in str(v.get("note", "")) for v in violations)


def test_t20_rollback_to_earlier_epoch_with_later_anchor_is_broken(git_repo, tmp_path):
    """ADR-034 fix-r1 Finding 1+2 (b): rolling the ledger back to only epoch
    1's original content (stripping epoch 2) while origin still holds the
    epoch-2 open anchor + epoch-1 closure record. Epoch 1's own fingerprint
    still matches and it's the LAST epoch the (rolled-back) ledger observes —
    the bug this test guards against is treating that as "the open latest
    epoch, no closure required" instead of checking it against the true
    highest ANCHORED epoch (2)."""
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": "a"})
    append_chained_entry(ledger, {"seq": "b"})
    result1 = _seal(git_repo, ledger)
    assert result1.action == "sealed" and result1.epoch == 1

    original_epoch1_content = ledger.read_text()

    result2 = _seal(git_repo, ledger, force_new_epoch=True)
    assert result2.action == "sealed" and result2.epoch == 2 and result2.closed_epoch == 1

    baseline_ok, _v, baseline_status, _p = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert baseline_ok and baseline_status == "verified-segmented"

    # Attack: roll the local ledger back to JUST epoch 1's original content.
    ledger.write_text(original_epoch1_content)

    ok, violations, status, _prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"
    assert any("rollback" in str(v.get("note", "")) or "no longer observes" in str(v.get("note", "")) for v in violations)


def test_t23_markerless_chaining_no_anchor_anywhere_is_broken(git_repo, tmp_path):
    """ADR-034 fix-r2 Finding 1: a marker-less GENESIS chain (chained from
    line 1 via prev_hash == GENESIS, no chain_epoch_start marker at all)
    that has NEVER been sealed — zero anchors anywhere for its identity —
    must fail closed as `broken`, not pass through as the legitimate
    "verified" marker-less-chain case. _observed_epochs(path) is [] here
    (no markers), so the per-epoch loop never runs; only a reverse "no
    anchor anywhere for this chaining-active identity" check catches it."""
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_chained_entry(ledger, {"seq": 1})  # marker-less GENESIS chain, never sealed

    ok, violations, status, prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False
    assert status == "broken"
    assert prov is not None and prov.resolved is True
    assert any("no git anchor anywhere" in str(v.get("note", "")) for v in violations)

    # No over-fire: sealing this SAME identity properly (real marker + real
    # anchor) must still verify — the new check must not catch a
    # legitimately-anchored chain.
    ledger.write_text("")
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})
    result = _seal(git_repo, ledger)
    assert result.action == "sealed"

    ok2, _v2, status2, _p2 = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok2 is True and status2 == "verified-segmented"


def test_t24_force_new_epoch_crash_before_commit_resume_backfills_prior_closure(
    git_repo, tmp_path, monkeypatch
):
    """ADR-034 fix-r2 Finding 2: force_new_epoch appends the new epoch's
    marker under the lock (durable — max_epoch advances) and computes epoch
    1's closure, but crashes before `_commit_and_push_anchor` ever runs — no
    commit, no push, nothing lands anywhere except the marker on the ledger.
    On retry (a plain re-seal, not re-passing force_new_epoch), epoch_state
    now reports the new epoch as already-current: opened_new_epoch=False,
    prior_epoch=None. A naive "only close what I just opened" check would
    never emit epoch 1's closure again — it would stay permanently
    un-anchored and verify_chain permanently `broken` on "no closure record
    for a CLOSED epoch"."""
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": "a"})
    result1 = _seal(git_repo, ledger)
    assert result1.action == "sealed" and result1.epoch == 1

    real_commit = coa._commit_and_push_anchor

    def crash_before_commit(*_args, **_kwargs):
        raise RuntimeError("simulated crash before commit/push")

    monkeypatch.setattr(coa, "_commit_and_push_anchor", crash_before_commit)
    with pytest.raises(RuntimeError, match="simulated crash before commit/push"):
        seal_and_commit_origin(
            ledger,
            git_repo,
            project_id="vnx-dev",
            project_data_dir=data_dir,
            branch="main",
            force_new_epoch=True,
            branch_protection_confirmed=True,
        )

    # Precondition: the epoch-2 marker landed (inside the lock, durable on
    # disk before the crash), but nothing was ever committed for it, and
    # epoch 1's closure never reached origin either.
    identity = ledger_identity("vnx-dev", ledger, data_dir)
    assert coa.epoch_state(ledger) == (2, True)
    assert coa._read_local_head_anchor(git_repo, coa.anchor_key(identity, 2)) is None
    assert coa._read_remote_base_anchor(git_repo, "main", coa.closure_key(identity, 1)) is None

    ok_broken, _v, status_broken, _p = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok_broken is False and status_broken == "broken"

    monkeypatch.setattr(coa, "_commit_and_push_anchor", real_commit)
    result2 = _seal(git_repo, ledger)  # plain retry — does NOT re-pass force_new_epoch
    assert result2.action == "sealed"
    assert result2.epoch == 2

    closure1, prov = read_git_anchor(git_repo, identity, 1, kind="close")
    assert prov.resolved is True
    assert closure1 is not None and closure1 != "corrupt"

    ok, _v2, status, _p2 = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is True and status == "verified-segmented"


# ---------------------------------------------------------------------------
# T6/T7: check_anchor_immutability pure diff logic (write-side rule; the
# GitHub Actions wiring that invokes this in CI is PR-2 scope)
# ---------------------------------------------------------------------------


def test_t6_immutability_check_rejects_modified_existing_key():
    base_record = {"key": "id#1", "ledger_identity": "id", "epoch": 1, "record_type": "open", "origin_hash": "aaa"}
    head_record = {**base_record, "origin_hash": "FORGED"}
    base = json.dumps(base_record, sort_keys=True) + "\n"
    head = json.dumps(head_record, sort_keys=True) + "\n"

    violations = check_anchor_immutability(base, head)
    assert violations == [{"key": "id#1", "violation": "modified"}]


def test_t6b_immutability_check_rejects_removed_key():
    base_record = {"key": "id#1", "ledger_identity": "id", "epoch": 1, "record_type": "open"}
    base = json.dumps(base_record, sort_keys=True) + "\n"

    violations = check_anchor_immutability(base, "")
    assert violations == [{"key": "id#1", "violation": "removed"}]


def test_t7_immutability_check_accepts_new_key():
    base_record = {"key": "id#1", "ledger_identity": "id", "epoch": 1, "record_type": "open"}
    new_record = {"key": "id#2", "ledger_identity": "id", "epoch": 2, "record_type": "open"}
    base = json.dumps(base_record, sort_keys=True) + "\n"
    head = base + json.dumps(new_record, sort_keys=True) + "\n"

    assert check_anchor_immutability(base, head) == []


# ---------------------------------------------------------------------------
# T9-T11, T16: seal_and_commit_origin
# ---------------------------------------------------------------------------


def test_t9_concurrent_append_blocks_while_lock_held(tmp_path):
    """T9: a concurrent append while seal_and_commit_origin holds
    ledger_lock_path's flock serializes rather than interleaving — same lock
    file _ledger_locked and append_chained_entry both take."""
    ledger, _data_dir = _ledger_and_data_dir(tmp_path)
    append_chained_entry(ledger, {"seq": 0})

    lock_acquired = threading.Event()
    release_lock = threading.Event()
    order: list[str] = []

    def hold_lock():
        with _ledger_locked(ledger):
            order.append("lock-acquired")
            lock_acquired.set()
            release_lock.wait(timeout=5)
        order.append("lock-released")

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert lock_acquired.wait(timeout=5)

    def do_append():
        append_chained_entry(ledger, {"seq": 1})
        order.append("append-done")

    appender = threading.Thread(target=do_append)
    appender.start()
    time.sleep(0.3)
    assert "append-done" not in order  # still blocked behind the held lock

    release_lock.set()
    holder.join(timeout=5)
    appender.join(timeout=5)
    assert "append-done" in order
    assert len(ledger.read_text().splitlines()) == 2


def test_t10_partial_seal_is_broken_then_idempotent_retry(git_repo, tmp_path, monkeypatch):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_chained_entry(ledger, {"seq": 0})  # marker-less genesis chain -> forces epoch-1 open

    real_commit = coa._commit_and_push_anchor

    def failing_commit(*_args, **_kwargs):
        raise RuntimeError("simulated push failure")

    monkeypatch.setattr(coa, "_commit_and_push_anchor", failing_commit)
    with pytest.raises(RuntimeError, match="simulated push failure"):
        seal_and_commit_origin(
            ledger,
            git_repo,
            project_id="vnx-dev",
            project_data_dir=data_dir,
            branch="main",
            branch_protection_confirmed=True,
        )

    # Marker WAS appended (inside the lock, before the raise); no anchor was
    # ever pushed -> verify_chain must report broken, not healthy.
    ok, _v, status, _p = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is False and status == "broken"

    monkeypatch.setattr(coa, "_commit_and_push_anchor", real_commit)
    result = _seal(git_repo, ledger)
    assert result.action == "sealed"
    assert result.epoch == 1

    ok2, _v2, status2, _p2 = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok2 is True and status2 == "verified-segmented"


def test_t21_partial_seal_push_failed_after_commit_resumes_not_noop(git_repo, tmp_path, monkeypatch):
    """ADR-034 fix-r1 Finding 3: commit succeeds locally (checkout + add +
    commit all real), then `git push` itself raises — HEAD stays parked on
    the seal branch WITH the anchor commit. A naive retry that only checks
    local HEAD would find the anchor already there and wrongly report
    "noop", even though origin never received it and verify_chain would stay
    broken forever. The retry must check origin, find nothing, and resume by
    re-pushing the same local branch instead."""
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_chained_entry(ledger, {"seq": 0})  # marker-less genesis chain -> forces epoch-1 open

    real_git_run_checked = coa._git_run_checked

    def push_fails(project_root, *args, **kwargs):
        if args and args[0] == "push":
            raise RuntimeError("simulated push failure (network unreachable)")
        return real_git_run_checked(project_root, *args, **kwargs)

    monkeypatch.setattr(coa, "_git_run_checked", push_fails)
    with pytest.raises(RuntimeError, match="simulated push failure"):
        seal_and_commit_origin(
            ledger,
            git_repo,
            project_id="vnx-dev",
            project_data_dir=data_dir,
            branch="main",
            branch_protection_confirmed=True,
        )

    # Confirm the bug precondition: HEAD is on the seal branch, anchor
    # committed locally, nothing on origin at all.
    identity = ledger_identity("vnx-dev", ledger, data_dir)
    key = coa.anchor_key(identity, 1)
    assert coa._read_local_head_anchor(git_repo, key) is not None
    assert coa._read_remote_base_anchor(git_repo, "main", key) is None

    monkeypatch.setattr(coa, "_git_run_checked", real_git_run_checked)
    result = seal_and_commit_origin(
        ledger,
        git_repo,
        project_id="vnx-dev",
        project_data_dir=data_dir,
        branch="main",
        branch_protection_confirmed=True,
    )
    assert result.action != "noop"
    assert result.branch_name is not None

    # The resumed push actually landed the commit on origin's seal branch —
    # no duplicate record was appended (still exactly one "open" record for
    # this key).
    rec, prov = read_git_anchor(git_repo, identity, 1, ref=f"origin/{result.branch_name}")
    assert prov.resolved is True
    assert rec is not None and rec != "corrupt"


def test_t11_double_run_is_noop_new_epoch_produces_new_record(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_chained_entry(ledger, {"seq": 0})

    first = _seal(git_repo, ledger)
    assert first.action == "sealed" and first.epoch == 1

    second = _seal(git_repo, ledger)
    assert second.action == "noop"

    third = _seal(git_repo, ledger, force_new_epoch=True)
    assert third.action == "sealed"
    assert third.epoch == 2
    assert third.closed_epoch == 1

    identity = ledger_identity("vnx-dev", ledger, data_dir)
    rec1, _p1 = read_git_anchor(git_repo, identity, 1)
    rec2, _p2 = read_git_anchor(git_repo, identity, 2)
    closure1, _p3 = read_git_anchor(git_repo, identity, 1, kind="close")
    assert rec1 is not None and rec1 != "corrupt"
    assert rec2 is not None and rec2 != "corrupt"
    assert closure1 is not None and closure1 != "corrupt"


def test_t16_seal_refuses_without_branch_protection_confirmed(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_chained_entry(ledger, {"seq": 0})

    log_before = _run("git", "log", "--oneline", "--all", cwd=git_repo)
    anchor_path = git_repo / coa.ANCHOR_REL_PATH
    assert not anchor_path.exists()

    with pytest.raises(BranchProtectionUnconfirmedError):
        seal_and_commit_origin(
            ledger,
            git_repo,
            project_id="vnx-dev",
            project_data_dir=data_dir,
            branch="main",
            branch_protection_confirmed=False,
        )

    assert not anchor_path.exists()
    log_after = _run("git", "log", "--oneline", "--all", cwd=git_repo)
    assert log_before == log_after
    # Ledger itself is also untouched — no marker appended before the refusal.
    assert len(ledger.read_text().splitlines()) == 1


# ---------------------------------------------------------------------------
# T12: existing behavior / read-path unchanged for callers that don't opt in
# ---------------------------------------------------------------------------


def test_t12_old_verify_chain_signature_and_behavior_unchanged(tmp_path):
    """The EXISTING ndjson_hash_chain.verify_chain (used by all five
    pre-ADR-034 call sites) still returns a plain 3-tuple and is byte-for-byte
    unaffected by this module existing — ADR-034 §6 step 1: "additive,
    verify_chain's read path unchanged"."""
    from ndjson_hash_chain import verify_chain as old_verify_chain

    p = tmp_path / "plain.ndjson"
    for i in range(3):
        with p.open("a") as f:
            f.write(json.dumps({"seq": i}) + "\n")

    result = old_verify_chain(p)
    assert len(result) == 3
    ok, violations, status = result
    assert ok is True and violations == [] and status == "unchained"


def test_t12_new_verify_chain_without_identity_on_unchained_ledger_is_unaffected(tmp_path):
    """Calling the NEW anchor-aware verify_chain with no project_root/id/
    data_dir on a genuinely unchained ledger is the additive no-op path —
    same result as the old function, plus a None provenance."""
    p = tmp_path / "plain.ndjson"
    for i in range(3):
        with p.open("a") as f:
            f.write(json.dumps({"seq": i}) + "\n")

    ok, violations, status, prov = verify_chain(p)
    assert ok is True and violations == [] and status == "unchained"
    assert prov is None


# ---------------------------------------------------------------------------
# read_git_anchor / read_git_anchors_for_identity — direct unit coverage
# ---------------------------------------------------------------------------


def test_read_git_anchor_none_when_never_sealed(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    identity = ledger_identity("vnx-dev", ledger, data_dir)

    rec, prov = read_git_anchor(git_repo, identity, 1)
    assert rec is None
    assert prov.resolved is True  # ref resolves fine; the anchor file just doesn't exist yet


def test_read_git_anchor_fails_closed_when_ref_unresolvable(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    rec, prov = read_git_anchor(not_a_repo, "vnx-dev:state/t0_receipts.ndjson", 1)
    assert rec is None
    assert prov.resolved is False
    assert prov.error is not None


def test_anchor_provenance_carries_commit_sha_and_remote_url(git_repo, tmp_path):
    """ADR §2 off-host cross-check: the resolved anchor commit SHA + remote
    URL are always populated when the ref resolves, even with no record."""
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    identity = ledger_identity("vnx-dev", ledger, data_dir)

    _rec, prov = read_git_anchor(git_repo, identity, 1)
    assert prov.resolved is True
    assert prov.anchor_commit_sha is not None and len(prov.anchor_commit_sha) == 40
    assert prov.remote_url is not None


def test_read_git_anchors_for_identity_scopes_by_key_prefix(git_repo, tmp_path):
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    identity = ledger_identity("vnx-dev", ledger, data_dir)
    other_identity = "vnx-dev:state/other_ledger.ndjson"

    fp1 = coa.OriginFingerprint(
        epoch=1, origin_type="chain_epoch_start", origin_hash="a" * 64, origin_line_number=1, entries_before_origin=0
    )
    fp_other = coa.OriginFingerprint(
        epoch=1, origin_type="chain_epoch_start", origin_hash="b" * 64, origin_line_number=1, entries_before_origin=0
    )
    records = [
        coa._origin_record(identity, fp1, sealed_at="2026-01-01T00:00:00Z"),
        coa._origin_record(other_identity, fp_other, sealed_at="2026-01-01T00:00:00Z"),
    ]
    _write_raw_anchor_to_main(git_repo, records)

    anchors, prov = read_git_anchors_for_identity(git_repo, identity)
    assert prov.resolved is True
    assert len(anchors) == 1
    assert anchors[0].ledger_identity == identity


def test_t22_local_branch_named_like_remote_ref_does_not_shadow_it(git_repo, tmp_path):
    """ADR-034 fix-r1 Finding 5: git allows slashes in branch names, so a
    local attacker can create a branch literally named `origin/main`. Per
    gitrevisions(7)'s disambiguation order, an unqualified `git rev-parse
    origin/main` resolves refs/heads/origin/main (the local shadow) BEFORE
    refs/remotes/origin/main (the real remote-tracking ref) — a
    committed-but-never-pushed forged anchor would otherwise get trusted
    over the genuine remote content. The read path must qualify the ref
    explicitly (refs/remotes/origin/main) and never fall back to the
    ambiguous short form."""
    ledger, data_dir = _ledger_and_data_dir(tmp_path)
    append_epoch_marker(ledger, 1)
    append_chained_entry(ledger, {"seq": 1})
    result = _seal(git_repo, ledger)
    assert result.action == "sealed"

    identity = ledger_identity("vnx-dev", ledger, data_dir)
    real_rec, real_prov = read_git_anchor(git_repo, identity, 1)
    assert real_prov.resolved is True
    assert real_rec is not None and real_rec != "corrupt"

    # Attacker: create a LOCAL branch literally named "origin/main", branched
    # from the current (real) main, then commit FORGED anchor content on it
    # -- never pushed anywhere. This branch is never merged/pushed; it just
    # sits locally as a same-named shadow of the remote-tracking ref.
    fake_fp = coa.OriginFingerprint(
        epoch=1, origin_type="chain_epoch_start", origin_hash="f" * 64, origin_line_number=1, entries_before_origin=0
    )
    forged_record = coa._origin_record(identity, fake_fp, sealed_at="2026-01-01T00:00:00Z")
    _run("git", "checkout", "-b", "origin/main", cwd=git_repo)
    anchor_path = git_repo / coa.ANCHOR_REL_PATH
    anchor_path.write_text(json.dumps(forged_record, sort_keys=True, separators=(",", ":")) + "\n")
    _run("git", "add", str(coa.ANCHOR_REL_PATH), cwd=git_repo)
    _run("git", "commit", "-m", "forged shadow branch content", cwd=git_repo)
    _run("git", "checkout", "main", cwd=git_repo)

    shadowed_rec, shadowed_prov = read_git_anchor(git_repo, identity, 1)
    assert shadowed_prov.resolved is True  # the real remote-tracking ref still resolves
    assert shadowed_rec is not None and shadowed_rec != "corrupt"
    assert shadowed_rec.origin_hash == real_rec.origin_hash
    assert shadowed_rec.origin_hash != "f" * 64  # NOT the forged shadow-branch content

    # verify_chain must also read the genuine content, not the local shadow.
    ok, _v, status, _prov = verify_chain(
        ledger, project_root=git_repo, project_id="vnx-dev", project_data_dir=data_dir
    )
    assert ok is True and status == "verified-segmented"
