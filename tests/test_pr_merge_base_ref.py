"""OI-1572: the merge door checks the dispatch's intended PR base."""

import json
import os
from pathlib import Path

import pytest

from test_pr_merge_door_permit import GO, PR, PR_DATA, door
import pr_merge

DID = "fix-base-ref"


@pytest.fixture
def base_door(door, monkeypatch):
    data = dict(PR_DATA)
    calls = []
    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **kw: (dict(GO), data))
    door.merge = lambda: calls.append("merge") or (True, "")
    return door, data, calls


def stage(door, content, bucket="active", dispatch_id=DID):
    path = door.ledger.parent.parent / "dispatches" / bucket / dispatch_id / "dispatch-spec.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def run(*extra, dispatch_id=DID):
    return pr_merge.main(["--pr", str(PR), "--dispatch-id", dispatch_id, *extra])


@pytest.mark.parametrize("dry_run", [False, True])
def test_mismatched_base_ref_refuses_before_merge(base_door, capsys, dry_run):
    door, data, calls = base_door
    data["baseRefName"] = "feature-x"
    stage(door, {"base_ref": "origin/main"})

    assert run(*(["--dry-run"] if dry_run else [])) == pr_merge.EXIT_ERROR
    assert calls == []
    assert door.receipts("pr_merged") == []
    error = capsys.readouterr().err
    assert "NO-GO" in error and "base_ref_mismatch" in error
    assert DID in error and "'feature-x'" in error and "'main'" in error
    if dry_run:
        assert not door.ledger.exists()
    else:
        receipt, = door.receipts("pr_merge_refused")
        assert receipt["preflight_gates"][0]["reason_code"] == "base_ref_mismatch"


@pytest.mark.parametrize("actual,expected", [
    ("main", "origin/main"), ("main", "refs/heads/main"),
    ("refs/heads/main", "origin/main"), ("origin/main", "refs/heads/main"),
    ("feature-x", "origin/feature-x"),
])
def test_matching_and_stacked_bases_merge(base_door, capsys, actual, expected):
    door, data, calls = base_door
    data["baseRefName"] = actual
    stage(door, {"base_ref": expected})
    assert run() == pr_merge.EXIT_OK
    assert calls == ["merge"] and len(door.receipts("pr_merged")) == 1
    assert "WARNING:" not in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("spec", [None, {}])
def test_missing_spec_or_field_warns_without_refusal(base_door, capsys, dry_run, spec):
    door, _, calls = base_door
    if spec is not None:
        stage(door, spec)
    assert run(*(["--dry-run"] if dry_run else [])) == pr_merge.EXIT_OK
    warnings = [line for line in capsys.readouterr().err.splitlines() if line.startswith("WARNING:")]
    assert len(warnings) == 1 and DID in warnings[0] and "base check could not apply" in warnings[0]
    assert calls == ([] if dry_run else ["merge"])
    assert door.receipts("pr_merge_refused") == []
    assert len(door.receipts("pr_merged")) == (0 if dry_run else 1)


def test_conflicting_specs_refuse_and_list_both(base_door, capsys):
    door, _, calls = base_door
    stage(door, {"base_ref": "origin/main"})
    stage(door, {"base_ref": "origin/feature-x"}, "completed")
    assert run() == pr_merge.EXIT_ERROR
    error = capsys.readouterr().err
    assert all(text in error for text in ("base_ref_mismatch", "main", "feature-x", "active", "completed"))
    assert calls == [] and door.receipts("pr_merged") == []


def test_equivalent_specs_and_other_dispatch_ids(base_door):
    door, _, calls = base_door
    stage(door, {"base_ref": "origin/main"})
    stage(door, {"base_ref": "refs/heads/main"}, "completed")
    stage(door, {"base_ref": "wrong"}, dispatch_id=DID + "-other")
    assert run() == pr_merge.EXIT_OK and calls == ["merge"]


@pytest.mark.parametrize("field", ["base_ref", "baseRefName"])
@pytest.mark.parametrize("value", [None, 42, "", " ", "origin/", [], {}])
def test_invalid_base_evidence_blocks(base_door, capsys, field, value):
    door, data, calls = base_door
    stage(door, {"base_ref": value if field == "base_ref" else "origin/main"})
    if field == "baseRefName":
        data[field] = value
    assert run() == pr_merge.EXIT_ERROR
    assert "base_ref_unverifiable" in capsys.readouterr().err
    assert calls == [] and door.receipts("pr_merged") == []


@pytest.mark.parametrize("payload", [b"{", b"[]", b"null", b"", b"\xff"])
def test_corrupt_spec_blocks(base_door, capsys, payload):
    door, _, calls = base_door
    stage(door, {}).write_bytes(payload)
    assert run() == pr_merge.EXIT_ERROR
    assert "base_ref_unverifiable" in capsys.readouterr().err and calls == []


@pytest.mark.parametrize("error", [PermissionError, FileNotFoundError])
def test_spec_read_failure_blocks(base_door, monkeypatch, capsys, error):
    door, _, calls = base_door
    path = stage(door, {"base_ref": "main"})
    read = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise error("spec denied")
        return read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert run() == pr_merge.EXIT_ERROR
    assert "spec denied" in capsys.readouterr().err and calls == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read chmod(0) directories")
@pytest.mark.parametrize("level", ["dispatches", "bucket", "dispatch"])
@pytest.mark.parametrize("other_spec", [False, True])
def test_unreadable_dispatch_scan_blocks(base_door, capsys, level, other_spec):
    door, _, calls = base_door
    path = stage(door, {"base_ref": "other"})
    if other_spec:
        stage(door, {"base_ref": "main"}, "completed")
    denied = {"dispatches": path.parents[2], "bucket": path.parents[1], "dispatch": path.parent}[level]
    mode = denied.stat().st_mode
    denied.chmod(0)
    try:
        assert run() == pr_merge.EXIT_ERROR
    finally:
        denied.chmod(mode)
    error = capsys.readouterr().err
    assert "NO-GO" in error and "base_ref_unverifiable" in error
    assert str(denied) in error and "PermissionError" in error
    assert calls == [] and door.receipts("pr_merged") == []
    receipt, = door.receipts("pr_merge_refused")
    assert receipt["preflight_gates"][0]["reason_code"] == "base_ref_unverifiable"


def test_absent_dispatches_warns(tmp_path):
    assert not (tmp_path / "dispatches").exists()
    result = pr_merge._check_dispatch_base_ref(PR, "main", DID, data_dir=tmp_path)
    assert result["verdict"] == "GO"
    assert result["reason_code"] == "base_ref_not_applied"
    assert "WARNING:" in result["message"] and "no dispatch-spec.json" in result["message"]


@pytest.mark.parametrize("dispatch_id", ["..", "/tmp/other", "other/dispatch"])
def test_invalid_dispatch_id_blocks(base_door, capsys, dispatch_id):
    door, _, calls = base_door
    assert run("--dry-run", dispatch_id=dispatch_id) == pr_merge.EXIT_ERROR
    assert "base_ref_unverifiable" in capsys.readouterr().err and calls == []


def test_symlinked_spec_blocks(base_door, capsys):
    door, _, calls = base_door
    target = stage(door, {"base_ref": "main"}, "completed")
    path = target.parent.parent.parent / "active" / DID / target.name
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    assert run() == pr_merge.EXIT_ERROR
    assert "symlinked dispatch spec refused" in capsys.readouterr().err and calls == []


@pytest.mark.parametrize("version", [None, 2, "1", True])
def test_unsupported_spec_version_blocks(base_door, capsys, version):
    door, _, calls = base_door
    stage(door, {"base_ref": "main", "schema_version": version})
    assert run() == pr_merge.EXIT_ERROR
    assert "unsupported dispatch schema_version" in capsys.readouterr().err and calls == []


@pytest.mark.parametrize("base", ["main", "feature-x"])
def test_missing_field_does_not_hide_other_spec(base_door, capsys, base):
    door, _, calls = base_door
    stage(door, {})
    stage(door, {"base_ref": base, "schema_version": 1}, "completed")
    assert run() == (pr_merge.EXIT_OK if base == "main" else pr_merge.EXIT_ERROR)
    assert ("WARNING:" if base == "main" else "base_ref_mismatch") in capsys.readouterr().err
    assert calls == (["merge"] if base == "main" else [])


def test_json_dry_run_cannot_override_mismatch(base_door, capsys):
    door, data, calls = base_door
    data["baseRefName"] = "feature-x"
    stage(door, {"base_ref": "main"})
    assert run("--json", "--dry-run", "--override-reason", "hotfix") == pr_merge.EXIT_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["ci_gate"]["reason_code"] == "base_ref_mismatch"
    assert calls == [] and not door.ledger.exists()


def test_missing_pr_base_blocks(base_door, capsys):
    door, data, calls = base_door
    del data["baseRefName"]
    stage(door, {"base_ref": "main"})
    assert run() == pr_merge.EXIT_ERROR
    assert "base_ref_unverifiable" in capsys.readouterr().err and calls == []


def test_no_dispatch_id_warns(base_door, capsys):
    door, _, calls = base_door
    stage(door, {"base_ref": "other"})
    assert run(dispatch_id="") == pr_merge.EXIT_OK
    assert "no dispatch id supplied" in capsys.readouterr().err and calls == ["merge"]


def test_explicit_store_ignores_other_tenant(base_door, tmp_path, monkeypatch):
    door, _, _ = base_door
    stage(door, {"base_ref": "wrong"})
    other = tmp_path / "other-project"
    path = other / "dispatches" / "active" / DID / "dispatch-spec.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"base_ref": "main"}', encoding="utf-8")
    result = pr_merge._check_dispatch_base_ref(PR, "main", DID, data_dir=other)
    assert result["verdict"] == "GO"
