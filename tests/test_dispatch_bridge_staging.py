"""tests/test_dispatch_bridge_staging.py — PR-12 bridge staging non-forgeability.

`stage_spec_bundle` is the FIRST writer of a door spec-bundle — the trust boundary
codex RED'd twice on PR-4. These tests pin the non-forgeable invariants the bridge
shipped WITHOUT tests (PR-1 of the PR-12 plan): traversal rejection via `_ID_RE`,
symlink-escape refusal on both the pending root and the bundle dir, the
instruction_sha256 bind over the written bytes, and ADR-007 project_id carriage.

All tests pass an explicit `data_dir=tmp_path` so the door's live `_resolve_data_dir`
is never touched — fully isolated.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_bridge  # noqa: E402

_GOOD_ID = "20260101-120000-feat"


def _stage(tmp_path, **over):
    base = dict(
        instruction_text="do the thing", dispatch_id=_GOOD_ID, role="dev",
        target_slot="T1", project_id="p1", provider="claude", data_dir=tmp_path,
    )
    base.update(over)
    return dispatch_bridge.stage_spec_bundle(**base)


# --- traversal: staging_id is _ID_RE-validated BEFORE any path join ---

@pytest.mark.parametrize("bad", ["../evil", "a/b", "foo/../bar", "..", "/abs", "a\x00b", ""])
def test_stage_rejects_unsafe_dispatch_id(tmp_path, bad):
    with pytest.raises(ValueError):
        _stage(tmp_path, dispatch_id=bad)


# --- happy path: a real promoted bundle under pending/<id>/ ---

def test_stage_writes_bundle_under_pending(tmp_path):
    spec = _stage(tmp_path)
    assert spec.name == "dispatch-spec.json"
    bundle = spec.parent
    assert bundle.name == _GOOD_ID
    assert bundle.parent == (tmp_path / "dispatches" / "pending")
    assert (bundle / "instruction.md").read_text(encoding="utf-8") == "do the thing"


def test_stage_spec_carries_project_id_and_staging_id(tmp_path):
    # ADR-007: the bundle carries project_id; staging_id is derived from dispatch_id.
    payload = json.loads(_stage(tmp_path).read_text(encoding="utf-8"))
    assert payload["project_id"] == "p1"
    assert payload["dispatch_id"] == _GOOD_ID
    assert payload["staging_id"] == _GOOD_ID
    assert payload["provider"] == "claude"


def test_stage_binds_instruction_sha_over_written_bytes(tmp_path):
    text = "exact bytes — TOCTOU-bound"
    payload = json.loads(_stage(tmp_path, instruction_text=text).read_text(encoding="utf-8"))
    assert payload["instruction_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_stage_normalizes_legacy_provider_alias(tmp_path):
    # legacy tmux-mode string normalizes to the closed Provider enum value
    payload = json.loads(_stage(tmp_path, provider="codex_cli").read_text(encoding="utf-8"))
    assert payload["provider"] == "codex"


# --- symlink escape: refused at WRITE time, not just read (defense-in-depth) ---

def test_stage_refuses_symlinked_pending_root_escape(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (data / "dispatches").mkdir()
    # pre-plant pending/ as a symlink escaping the data root
    (data / "dispatches" / "pending").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink escape|outside data root"):
        _stage(data, data_dir=data, dispatch_id="20260101-120000-sym")


def test_stage_refuses_symlinked_bundle_dir_escape(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    pending = data / "dispatches" / "pending"; pending.mkdir(parents=True)
    did = "20260101-120000-bdir"
    # pre-plant the id dir as a symlink escaping pending
    (pending / did).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes pending root"):
        _stage(data, data_dir=data, dispatch_id=did)


# --- bridge_dispatch surfaces a staging error as a clean exit 1, never a side door ---

def test_bridge_dispatch_rejects_unsafe_id_as_exit_1(tmp_path):
    rc = dispatch_bridge.bridge_dispatch(
        instruction_text="x", dispatch_id="../evil", role="dev",
        target_slot="T1", project_id="p1", data_dir=tmp_path, dry_run=True,
    )
    assert rc == 1  # clean reject — never falls back to a side-door delivery
