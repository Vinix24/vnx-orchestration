"""accept stages a dispatch with the exact diff; apply writes nothing (ochtendronde D3c part A)."""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import apply_suggested_edits as ase
import suggested_edit_dispatch as sed

PID = "vnx-dev"
RULE = "Reviewers keep finding a missing fail-closed check on evidence reads"
TARGET = "agents/backend-developer/CLAUDE.md"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "agents" / "backend-developer").mkdir(parents=True)
    (root / TARGET).write_text("# Backend\n\n## Wat review het vaakst vindt\n\n- old rule\n\n## Other\n\nx\n")
    store = tmp_path / "store"
    state = store / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(ase, "PROJECT_ROOT", root)
    monkeypatch.setattr(ase, "STATE_DIR", state)
    monkeypatch.setattr(ase, "PENDING_PATH", state / "pending_edits.json")
    monkeypatch.setattr(ase, "HISTORY_PATH", state / "edit_history.json")
    monkeypatch.setattr(ase, "_project_id", lambda: PID)
    edit = {"id": 1, "category": "role", "target": TARGET, "section": ase.ROLE_SECTION, "action": "append",
            "content": f"- {RULE}", "status": "pending", "evidence": "2 dispatches", "confidence": 0.8}
    ase._save_pending({"generated_at": "", "edits": [edit]})
    return root, state, store


def _pending(state):
    return json.loads((state / "pending_edits.json").read_text())["edits"]


def _approve(env):
    assert ase.cmd_accept("1") == 0
    return _pending(env[1])[0]


def test_accept_stages_one_bundle_with_exact_diff_and_leaves_target(env):
    root, state, store = env
    before = (root / TARGET).read_bytes()
    edit = _approve(env)
    assert (root / TARGET).read_bytes() == before
    did = edit["dispatch_id"]
    assert edit["status"] == "approved" and edit["approved_at"] and len(edit["diff_sha256"]) == 64
    assert did == f"edit-1-{edit['diff_sha256'][:8]}"
    bundle = store / "dispatches" / "pending" / did
    text = (bundle / "instruction.md").read_text()
    assert f"+- {RULE}" in text and f"--- a/{TARGET}" in text and "change nothing else" in text
    spec = json.loads((bundle / "dispatch-spec.json").read_text())
    assert [p["path"] for p in spec["dispatch_paths"]] == [TARGET]
    assert spec["project_id"] == PID and spec["role"] == "backend-developer" and spec["provider"] == "claude"
    assert [p.name for p in (store / "dispatches" / "pending").iterdir()] == [did]


def test_second_accept_stages_nothing_new(env):
    root, state, store = env
    first = _approve(env)
    assert ase.cmd_accept("1") == 0  # already approved: not pending, nothing to do
    data = json.loads((state / "pending_edits.json").read_text())
    data["edits"][0]["status"] = "pending"  # a lost save: the staged bundle is found, not duplicated
    ase._save_pending(data)
    assert ase.cmd_accept("1") == 0
    assert _pending(state)[0]["dispatch_id"] == first["dispatch_id"]
    assert len(list((store / "dispatches" / "pending").iterdir())) == 1


def test_accept_refuses_target_outside_reading_list(env, capsys):
    root, state, store = env
    (root / "secret.md").write_text("x")
    data = json.loads((state / "pending_edits.json").read_text())
    data["edits"][0]["target"] = "secret.md"
    ase._save_pending(data)
    assert ase.cmd_accept("1") == 1
    assert "target_not_on_reading_list" in capsys.readouterr().err
    assert _pending(state)[0]["status"] == "pending"
    assert not (store / "dispatches").exists()


def test_accept_stage_failure_keeps_edit_pending(env, monkeypatch, capsys):
    root, state, store = env
    import dispatch_bridge

    def boom(**kw):
        raise ValueError("no spec staged")
    monkeypatch.setattr(dispatch_bridge, "stage_spec_bundle", boom)
    assert ase.cmd_accept("1") == 1
    assert "staging failed" in capsys.readouterr().err
    edit = _pending(state)[0]
    assert edit["status"] == "pending" and "dispatch_id" not in edit


def test_accept_when_handler_changes_nothing_stays_pending(env, capsys):
    root, state, store = env
    data = json.loads((state / "pending_edits.json").read_text())
    data["edits"][0]["content"] = "- old rule"  # already in the file
    ase._save_pending(data)
    assert ase.cmd_accept("1") == 1
    assert _pending(state)[0]["status"] == "pending"


def test_accept_when_handler_raises_stays_pending(env, monkeypatch, capsys):
    root, state, store = env

    def boom(edit):
        raise RuntimeError("handler broke")
    monkeypatch.setitem(ase.APPLY_HANDLERS, "role", boom)
    assert ase.cmd_accept("1") == 1
    assert "handler raised RuntimeError" in capsys.readouterr().err
    assert _pending(state)[0]["status"] == "pending"
    assert not (store / "dispatches").exists()


def test_apply_writes_nothing_and_says_where_accepted_edits_go(env, capsys):
    root, state, store = env
    edit = _approve(env)
    target_before = (root / TARGET).read_bytes()
    pending_before = (state / "pending_edits.json").read_bytes()
    assert ase.cmd_apply() == 0
    out = capsys.readouterr().out
    assert "staged dispatch" in out and "reconcile" in out
    assert (root / TARGET).read_bytes() == target_before
    assert (state / "pending_edits.json").read_bytes() == pending_before
    assert not (state / "edit_history.json").exists()
    assert edit["status"] == "approved"


def _mutations(state):
    path = state / "t0_receipts.ndjson"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
    return [r for r in rows if r.get("event_type") == "state_mutation"]


def test_accept_records_one_state_mutation_before_pending_changes(env, monkeypatch):
    state = env[1]
    import append_receipt
    seen = []
    real = append_receipt.append_receipt_payload
    monkeypatch.setattr(append_receipt, "append_receipt_payload",
                        lambda r, **kw: (seen.append(_pending(state)[0]["status"]), real(r, **kw))[1])
    edit = _approve(env)
    assert seen == ["pending"]
    (row,) = _mutations(state)
    assert (row["edit_id"], row["from_status"], row["to_status"]) == (1, "pending", "approved")
    assert (row["dispatch_id"], row["diff_sha256"]) == (edit["dispatch_id"], edit["diff_sha256"])


def test_second_accept_of_reused_bundle_appends_no_second_receipt(env):
    state = env[1]
    _approve(env)
    data = json.loads((state / "pending_edits.json").read_text())
    data["edits"][0]["status"] = "pending"  # a lost save: the bundle is reused
    ase._save_pending(data)
    assert ase.cmd_accept("1") == 0
    assert len(_mutations(state)) == 1 and _pending(state)[0]["status"] == "approved"


def test_accept_receipt_failure_keeps_edit_pending(env, monkeypatch, capsys):
    import append_receipt
    state = env[1]
    before = (state / "pending_edits.json").read_bytes()
    def boom(receipt, **kw):
        raise OSError("ledger disk full")
    monkeypatch.setattr(append_receipt, "append_receipt_payload", boom)
    assert ase.cmd_accept("1") == 1
    assert "ledger disk full" in capsys.readouterr().err
    assert (state / "pending_edits.json").read_bytes() == before


def test_accept_unreadable_ledger_keeps_edit_pending(env, capsys):
    state = env[1]
    (state / "t0_receipts.ndjson").mkdir()  # a directory where the ledger should be
    assert ase.cmd_accept("1") == 1 and _pending(state)[0]["status"] == "pending" and capsys.readouterr().err
